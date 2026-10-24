"""Offline transcription for a plan's audio: faster-whisper on CPU, local weights only, no network, no host token.

Reads any media ffmpeg can decode, resamples it to 16 kHz mono, transcribes segments with a local CTranslate2
Whisper model, and writes `transcript.json` (report), `transcript.txt` (timeline) and `字幕.srt`. The model and
`faster-whisper` are the caller's prerequisites: this Skill ships neither and never installs packages. The hub
offline switches are set before the import so a missing model can never turn into a download.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import editor

RATE = 16000
COMPUTE_TYPES = ('int8', 'int8_float32', 'float32')
MODEL_CACHES = (('models--Systran--faster-whisper-base', 145217532),
                ('models--Systran--faster-whisper-tiny', 75538270))


def decode(path):
    """Decode any media to (samples,) float32 at 16 kHz mono through ffmpeg, then read it with the stdlib."""
    with tempfile.TemporaryDirectory(prefix='transcribe-') as tmp:
        target = Path(tmp) / 'decoded.wav'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-protocol_whitelist', 'file,pipe', '-i', str(path), '-vn', '-ac', '1', '-ar', str(RATE), '-c:a', 'pcm_s16le', str(target)],
                   timeout=600)
        with wave.open(str(target), 'rb') as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1 or handle.getframerate() != RATE:
                raise ValueError('ffmpeg did not deliver 16 kHz mono 16-bit PCM for transcription')
            frames = handle.getnframes()
            raw = np.frombuffer(handle.readframes(frames), dtype='<i2')
    if frames == 0:
        raise ValueError(f'{Path(path).name} decoded to 0 audio samples; transcription needs audible speech')
    return raw.astype(np.float32) / 32768.0


def resolve_model(explicit):
    """--model, then JIANYING_WHISPER_MODEL; a HF cache directory or a directory holding model.bin."""
    candidate = explicit or os.environ.get('JIANYING_WHISPER_MODEL')
    if not candidate:
        listing = ', '.join(f'{name} (model.bin {size:,} B)' for name, size in MODEL_CACHES)
        raise ValueError(
            'Offline transcription needs a local faster-whisper model: pass --model DIR or set '
            f'JIANYING_WHISPER_MODEL. This Skill ships no weights; the caches measured here are {listing}, '
            'either the HF cache directory or its snapshot directory.')
    root = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
    if not root.is_dir():
        raise ValueError(f'Whisper model directory does not exist: {root}')
    snapshots = root / 'snapshots'
    if snapshots.is_dir():
        found = sorted(path for path in snapshots.iterdir() if (path / 'model.bin').is_file())
        if not found:
            raise ValueError(f'No snapshot with model.bin under {snapshots}')
        return found[-1]
    if (root / 'model.bin').is_file():
        return root
    raise ValueError(f'{root} is neither a snapshot directory nor a HuggingFace cache directory for '
                     'faster-whisper (needs model.bin)')


def model_files(model_dir):
    files = []
    for name in sorted(path.name for path in model_dir.iterdir() if path.is_file()):
        path = model_dir / name
        files.append({'file': name, 'bytes': path.stat().st_size,
                      'sha256': sha256(path) if name == 'model.bin' else None})
    return files


def sha256(path):
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(8 << 20), b''):
            checksum.update(block)
    return checksum.hexdigest().upper()


def load_model(model_dir, compute_type, threads):
    """Import and open the model offline; both the runtime and the weights are caller-supplied."""
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ValueError(
            'Offline transcription needs faster-whisper in the interpreter running this script; install it '
            'there (pip install faster-whisper). It runs on CTranslate2 and does not need torch. This Skill '
            'never installs packages itself.')
    kwargs = {'device': 'cpu', 'compute_type': compute_type, 'local_files_only': True}
    if threads:
        kwargs['cpu_threads'] = threads
    started = time.perf_counter()
    try:
        model = WhisperModel(str(model_dir), **kwargs)
    except Exception as error:  # CTranslate2 raises its own RuntimeError family, not OSError
        raise ValueError(f'faster-whisper could not load {model_dir.name}: {error}')
    return model, time.perf_counter() - started


def srt_timestamp(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    whole, milliseconds = divmod(rest, 1000)
    return f'{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}'


def srt_text(segments):
    lines = []
    for index, segment in enumerate(segments, start=1):
        text = segment['text'].strip()
        if not text:
            continue
        lines += [str(index), f"{srt_timestamp(segment['start'])} --> {srt_timestamp(segment['end'])}", text, '']
    return '\n'.join(lines)


def word_cues(segments, duration):
    """Validate model words without inventing durations, text or character-level timestamps."""
    limit = editor.us(editor.number(duration, 'word source duration', .000001))
    cues, previous_end = [], None
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError('Word-level transcript segments must be objects')
        words = segment.get('words')
        if not isinstance(words, list) or not words:
            raise ValueError('Word-level transcript requires nonempty model words for each segment')
        a = editor.us(editor.number(segment.get('start'), 'word segment start'))
        b = editor.us(editor.number(segment.get('end'), 'word segment end'))
        if b <= a or b > limit:
            raise ValueError('Word segment range is invalid or beyond source duration')
        text_parts = []
        for word_index, word in enumerate(words):
            if not isinstance(word, dict):
                raise ValueError('Each model word must be an object')
            text = word.get('word')
            if not isinstance(text, str) or not text.strip() or '\x00' in text:
                raise ValueError('Each model word requires nonempty original text without NUL')
            start = editor.us(editor.number(word.get('start'), 'word start'))
            end = editor.us(editor.number(word.get('end'), 'word end'))
            editor.number(word.get('probability'), 'word probability', 0, 1)
            if end <= start:
                raise ValueError('Model word has nonpositive duration; do not invent an interval')
            if start < a or end > b or (previous_end is not None and start < previous_end):
                raise ValueError('Model words overlap, are unordered or extend outside their segment')
            previous_end = end
            text_parts.append(text)
            cues.append({'text': text, 'start': start / 1e6, 'end': end / 1e6,
                         'segment_index': segment_index, 'word_index': word_index})
        if ''.join(text_parts) != segment.get('text'):
            raise ValueError('Model word text does not exactly reconstruct the original segment')
    return cues


def run_transcription(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')
    out = Path(args.out).resolve()
    if out.exists():
        raise ValueError(f'Output directory already exists; build a new named version: {out}')
    if not 1 <= args.beam_size <= 10:
        raise ValueError('--beam-size must be in [1, 10]')
    if args.compute_type not in COMPUTE_TYPES:
        raise ValueError(f'--compute-type must be one of {COMPUTE_TYPES} on CPU')
    if args.language != 'auto' and not re.fullmatch(r'[a-z]{2,3}', args.language):
        raise ValueError("--language must be 'auto' or a two-to-three letter code such as zh or en")
    want_words = getattr(args, 'word_timestamps', False)
    if type(want_words) is not bool:
        raise ValueError('--word-timestamps must be a boolean')
    source_hash = sha256(source) if want_words else None

    model_dir = resolve_model(args.model)
    files = model_files(model_dir)
    model_bin = model_dir / 'model.bin'
    model, load_seconds = load_model(model_dir, args.compute_type, args.threads)

    audio = decode(source)
    seconds = audio.shape[0] / RATE
    if args.max_seconds:
        if not 0 < args.max_seconds <= seconds:
            raise ValueError(f'--max-seconds must be inside the media ({seconds:.3f} s), or omitted')
        audio = audio[:int(args.max_seconds * RATE)]
    if args.verbose:
        print(f'transcribing {audio.shape[0] / RATE:.3f} s from {source.name}', file=sys.stderr, flush=True)

    language = None if args.language == 'auto' else args.language
    started = time.perf_counter()
    iterator, info = model.transcribe(audio, language=language, beam_size=args.beam_size,
                                      vad_filter=not args.no_vad, condition_on_previous_text=True,
                                      word_timestamps=want_words)
    segments = []
    for segment in iterator:
        item = {'id': len(segments), 'start': round(segment.start, 3), 'end': round(segment.end, 3),
                'text': segment.text, 'avg_logprob': round(segment.avg_logprob, 4),
                'no_speech_prob': round(segment.no_speech_prob, 4),
                'compression_ratio': round(segment.compression_ratio, 4)}
        if want_words:
            item['words'] = [{'word': word.word, 'start': round(word.start, 3), 'end': round(word.end, 3),
                              'probability': round(word.probability, 4)} for word in segment.words or []]
        segments.append(item)
    infer_seconds = time.perf_counter() - started
    text_all = ''.join(segment['text'] for segment in segments).strip()
    words = word_cues(segments, round(audio.shape[0] / RATE, 3)) if want_words else []
    if want_words and sha256(source) != source_hash:
        raise ValueError('Source changed during word transcription; no transcript will be delivered')

    out.mkdir(parents=True)
    (out / 'transcript.json').write_text(json.dumps({'segments': segments, 'textAll': text_all},
                                                    ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    (out / 'transcript.txt').write_text(''.join(
        f"[{segment['start']:8.3f} → {segment['end']:8.3f}] {segment['text']}\n" for segment in segments),
        encoding='utf-8')
    cues = srt_text(segments)
    if cues:
        (out / '字幕.srt').write_text(cues + '\n', encoding='utf-8')
    if words:
        (out / '词级字幕.srt').write_text(srt_text(words) + '\n', encoding='utf-8')

    report = {
        'status': 'passed',
        'input': str(source),
        'out': str(out),
        'offline': {'HF_HUB_OFFLINE': os.environ.get('HF_HUB_OFFLINE'),
                    'TRANSFORMERS_OFFLINE': os.environ.get('TRANSFORMERS_OFFLINE'), 'local_files_only': True},
        'model': {'requested': str(args.model or os.environ.get('JIANYING_WHISPER_MODEL')),
                  'resolved': str(model_dir), 'files': files, 'model_bin_bytes': model_bin.stat().st_size,
                  'model_bin_sha256': next(item['sha256'] for item in files if item['file'] == 'model.bin'),
                  'device': 'cpu', 'compute_type': args.compute_type, 'load_seconds': round(load_seconds, 3)},
        'audio': {'path': str(source), 'bytes': source.stat().st_size, 'sample_rate': RATE,
                  'seconds': round(audio.shape[0] / RATE, 3),
                  'decode_path': 'ffmpeg 16 kHz mono PCM16, then stdlib wave + numpy (PyAV is bypassed: '
                                 'av 19.0.1 rejects the metadata_errors keyword faster-whisper passes)'},
        'params': {'language': args.language, 'beam_size': args.beam_size, 'vad_filter': not args.no_vad,
                   'compute_type': args.compute_type, 'threads': args.threads,
                   'condition_on_previous_text': True, 'word_timestamps': want_words,
                   'level': 'word' if want_words else 'segment'},
        'info': {'language': info.language, 'language_probability': round(info.language_probability, 6),
                 'duration': round(info.duration, 3),
                 'duration_after_vad': round(float(getattr(info, 'duration_after_vad', 0.0) or 0.0), 3)},
        'stats': {'segment_count': len(segments), 'chars': len(text_all),
                  'first_segment_start': segments[0]['start'] if segments else None,
                  'last_segment_end': segments[-1]['end'] if segments else None,
                  'infer_seconds': round(infer_seconds, 3),
                  'realtime_factor': round(infer_seconds / info.duration, 4) if info.duration else None},
        'segments': segments,
        'text_all': text_all,
        'files': {path.name: path.stat().st_size for path in sorted(out.iterdir())},
        'boundaries': [
            'CPU int8 by default; the weights and faster-whisper are caller-supplied and never downloaded here.',
            'Segment text is machine output: no human correction, no punctuation or wording cleanup.',
            ('Word intervals are model alignment estimates, not forced alignment or guaranteed character timing; zero/overlapping words are refused.'
             if want_words else 'Timings are the model\'s segment boundaries, not word-level alignment (word_timestamps=False).'),
            'Set --no-vad to keep non-speech regions; with VAD on, silent stretches produce no cues at all.',
            'files lists the artifacts written beside the report; transcript-report.json itself is not listed.',
        ],
    }
    if want_words:
        report['audio']['sha256'] = source_hash
        report['stats']['word_count'] = len(words)
    (out / 'transcript-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='media ffmpeg can decode')
    parser.add_argument('--out', required=True, help='new directory for the transcript, cues and report')
    parser.add_argument('--model', help='faster-whisper model directory, else JIANYING_WHISPER_MODEL')
    parser.add_argument('--language', default='zh', help="'auto' or a code such as zh / en; default zh")
    parser.add_argument('--beam-size', type=int, default=5)
    parser.add_argument('--compute-type', default='int8',
                        help='one of int8 / int8_float32 / float32; validated below so every refusal is JSON')
    parser.add_argument('--threads', type=int, default=0, help='0 leaves the thread count to the runtime')
    parser.add_argument('--no-vad', action='store_true', help='disable the voice-activity filter')
    parser.add_argument('--word-timestamps', action='store_true',
                        help='Include validated model word intervals and 词级字幕.srt; not forced alignment or per-character timing')
    parser.add_argument('--max-seconds', type=float, default=None, help='transcribe only the first N seconds')
    parser.add_argument('--verbose', action='store_true', help='progress on stderr')
    args = parser.parse_args(argv)
    try:
        report = run_transcription(args)
    except Exception as error:  # keep the JSON-only stdout contract, like editor.py does
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
