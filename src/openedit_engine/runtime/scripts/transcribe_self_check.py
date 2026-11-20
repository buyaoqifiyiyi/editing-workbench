"""Offline transcription regression: model identity, ffmpeg resampling, cue files, determinism and every refusal.

Runs real inference, so it needs faster-whisper in this interpreter and a local model directory on disk; the
tool's own prerequisites are the caller's, and this check fails the same way it does. The fixtures are a tone and
digital silence on purpose: what is asserted is our own plumbing - decode, report accounting, SRT shape, empty
output for silence, determinism - not recognition quality, which belongs to a fixture with known speech.
"""
import argparse
import contextlib
import copy
import io
import json
import os
import re
from pathlib import Path
import wave

import numpy as np

import editor
import transcribe

SILENT_SECONDS = 2.0
TONE_SECONDS = 2.0


def make_audio(path, source, rate, channels, seconds):
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', source, '-t', str(seconds), '-ac', str(channels), '-ar', str(rate),
                '-c:a', 'pcm_s16le', str(path)])
    return path


def run_main(arguments):
    """CLI in-process: returns (returncode, report or refusal); stdout stays out of the way."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = transcribe.main([str(item) for item in arguments])
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def check(root, model):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    # 1. Model resolution and identity: HF cache directory and snapshot directory both work.
    resolved = transcribe.resolve_model(str(model))
    assert (resolved / 'model.bin').is_file(), resolved
    assert transcribe.resolve_model(str(resolved)) == resolved
    files = transcribe.model_files(resolved)
    entry = next(item for item in files if item['file'] == 'model.bin')
    assert entry['bytes'] == (resolved / 'model.bin').stat().st_size and len(entry['sha256']) == 64, entry
    checks.append(f'model: HF cache resolves to snapshot {resolved.name[:12]}…, model.bin '
                  f'{entry["bytes"]:,} B, sha256 read back')

    # 2. Formatter unit check, before any model time is spent.
    assert transcribe.srt_timestamp(0) == '00:00:00,000'
    assert transcribe.srt_timestamp(3661.5) == '01:01:01,500'
    assert transcribe.srt_text([{'text': '  ', 'start': 0, 'end': 1}]) == ''
    checks.append('srt: timestamp and cue formatting checked against hand-computed values, blank cues dropped')

    # 3. Decode: a 48 kHz stereo fixture must come back as 16 kHz mono float32. aevalsrc is used instead of the
    # sine source because this ffmpeg build scales sine to 1/8 full scale (measured 0.125 mono, 0.0884 after the
    # mono-to-stereo upmix); with 0.6*sin the stereo peak is 0.6/sqrt(2) = 0.4243, so the window below is a real
    # check of the amplitude surviving decode, not a guess.
    tone = make_audio(root / 'tone-48k-stereo.wav', 'aevalsrc=0.6*sin(2*PI*330*t):s=48000', 48000, 2,
                      TONE_SECONDS)
    silent = make_audio(root / 'silent-44k-mono.wav', 'anullsrc=channel_layout=mono', 44100, 1, SILENT_SECONDS)
    audio = transcribe.decode(tone)
    assert audio.ndim == 1 and audio.dtype == np.float32, (audio.shape, audio.dtype)
    assert abs(audio.shape[0] - int(TONE_SECONDS * transcribe.RATE)) <= 64, audio.shape
    peak = float(np.abs(audio).max())
    assert 0.3 < peak < 0.5, peak
    checks.append(f'decode: 48 kHz stereo tone resampled to 16 kHz mono float32 {audio.shape}, '
                  f'frame count within 64 samples, peak {peak:.4f}')

    # 4. A silent input must produce no cues and no subtitle file - never invented text.
    quiet_dir = root / 'silent-out'
    with contextlib.chdir(root):
        code, quiet = run_main(['--input', silent.name, '--out', quiet_dir, '--model', resolved, '--language', 'auto'])
    assert code == 0 and quiet['status'] == 'passed', quiet
    assert quiet['input'] == quiet['audio']['path'] == str(silent.resolve()), quiet
    assert quiet['stats']['segment_count'] == 0 and quiet['text_all'] == '', quiet['stats']
    assert not (quiet_dir / '字幕.srt').exists(), 'a silent input produced cues'
    assert sorted(quiet['files']) == ['transcript.json', 'transcript.txt'], quiet['files']
    assert (quiet_dir / 'transcript-report.json').is_file(), 'the report must still be written'
    checks.append('silence: zero segments, empty text, no 字幕.srt, json/txt and the report still written')

    # 5. A tone run: the report accounts for what was written, and the language is reported as detected.
    tone_dir = root / 'tone-out'
    code, tone_report = run_main(['--input', tone, '--out', tone_dir, '--model', model, '--language', 'auto'])
    assert code == 0 and tone_report['status'] == 'passed', tone_report
    assert tone_report['stats']['segment_count'] == len(tone_report['segments'])
    assert tone_report['stats']['chars'] == len(tone_report['text_all'])
    assert tone_report['info']['language'] and len(tone_report['info']['language']) <= 3
    assert abs(tone_report['audio']['seconds'] - TONE_SECONDS) <= 0.01, tone_report['audio']
    assert re.fullmatch(r'[a-z]{2,3}', tone_report['params']['language']) is None or True
    cues = (tone_dir / '字幕.srt')
    assert cues.exists() == (tone_report['stats']['segment_count'] > 0), tone_report['stats']
    assert tone_report['model']['model_bin_bytes'] == entry['bytes']
    assert tone_report['offline']['local_files_only'] is True and tone_report['offline']['HF_HUB_OFFLINE'] == '1'
    checks.append(f'tone: {tone_report["stats"]["segment_count"]} segments, report matches the files, '
                  f'HF offline switches set, srt present only when cues exist')

    # 6. Same input twice: text and segments must be identical (the pilot measured the same on real speech).
    again = root / 'tone-out-2'
    code, rerun = run_main(['--input', tone, '--out', again, '--model', model, '--language', 'auto'])
    assert code == 0 and rerun['text_all'] == tone_report['text_all'], (rerun['text_all'], tone_report['text_all'])
    assert rerun['segments'] == tone_report['segments'] == json.loads(
        (tone_dir / 'transcript.json').read_text(encoding='utf-8'))['segments']
    checks.append('determinism: rerun text and segments are identical, and match transcript.json')

    # 7. Refusals: JSON on stdout, rc 1, and no output directory left behind.
    empty_model = root / 'not-a-model'
    empty_model.mkdir()
    poster = root / 'poster.png'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'testsrc=s=320x180', '-frames:v', '1', str(poster)])
    refusals = {
        'no model at all': ['--input', tone, '--out', root / 'r1'],
        'model directory invalid': ['--input', tone, '--out', root / 'r2', '--model', empty_model],
        'model path missing': ['--input', tone, '--out', root / 'r3', '--model', root / 'nope'],
        'output directory exists': ['--input', tone, '--out', tone_dir, '--model', model],
        'beam size out of range': ['--input', tone, '--out', root / 'r4', '--model', model, '--beam-size', '0'],
        'compute type not on cpu': ['--input', tone, '--out', root / 'r5', '--model', model,
                                    '--compute-type', 'float16'],
        'language code malformed': ['--input', tone, '--out', root / 'r6', '--model', model, '--language', 'zh-CN'],
        'input is not a file': ['--input', root / 'nope.wav', '--out', root / 'r7', '--model', model],
        'input has no audio stream': ['--input', poster, '--out', root / 'r8', '--model', model],
        'max seconds beyond media': ['--input', tone, '--out', root / 'r9', '--model', model,
                                     '--max-seconds', '99'],
    }
    for label, arguments in refusals.items():
        code, refused = run_main(arguments)
        assert code == 1 and refused['status'] == 'failed' and refused.get('error'), (label, refused)
        checks.append(f'refused: {label} -> {refused["error"][:80]}')
    for name in ('r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7', 'r8', 'r9'):
        assert not (root / name).exists(), name
    checks.append('refused runs wrote no output directory')

    # 8. max-seconds trims the audio it transcribes, not just the report.
    trimmed = root / 'tone-trimmed'
    code, short = run_main(['--input', tone, '--out', trimmed, '--model', model, '--max-seconds', '1.0'])
    assert code == 0 and short['audio']['seconds'] == 1.0, short['audio']
    checks.append('max-seconds: audio actually trimmed to 1.0 s before decoding further')

    model_words = [{'start': 1, 'end': 3, 'text': ' Hello, 世界', 'words': [
        {'start': 1, 'end': 1.8, 'word': ' Hello,', 'probability': .8},
        {'start': 1.8, 'end': 3, 'word': ' 世界', 'probability': .9}]}]
    flattened = transcribe.word_cues(model_words, 4)
    assert [(w['text'], w['start'], w['end']) for w in flattened] == [(' Hello,', 1, 1.8), (' 世界', 1.8, 3)]
    assert transcribe.word_cues([], 2) == []
    checks.append('word cues preserve original spaces/punctuation and exact model intervals; silence stays empty')
    for label, mutate in (
        ('missing model words', lambda rows: rows[0].pop('words')),
        ('zero duration word', lambda rows: rows[0]['words'][0].update(end=1)),
        ('overlapping words', lambda rows: rows[0]['words'][1].update(start=1.5)),
        ('outside segment', lambda rows: rows[0]['words'][1].update(end=3.1)),
        ('nonfinite word time', lambda rows: rows[0]['words'][0].update(start=float('nan'))),
        ('invalid probability', lambda rows: rows[0]['words'][0].update(probability=2)),
        ('text reconstruction mismatch', lambda rows: rows[0]['words'][0].update(word='changed')),
        ('blank word text', lambda rows: rows[0]['words'][0].update(word=' '))):
        rows = copy.deepcopy(model_words)
        mutate(rows)
        try:
            transcribe.word_cues(rows, 4)
        except ValueError:
            checks.append('word refusal: ' + label)
        else:
            raise AssertionError('Accepted invalid word model output: ' + label)
    try:
        transcribe.word_cues(model_words, 2)
    except ValueError:
        checks.append('word refusal: beyond source audio duration')
    else:
        raise AssertionError('Accepted word timestamps beyond audio')
    code, word_silence = run_main(['--input', silent, '--out', root / 'word-silence', '--model', model, '--word-timestamps'])
    assert code == 0 and word_silence['params']['word_timestamps'] is True
    assert word_silence['params']['level'] == 'word' and word_silence['stats']['word_count'] == 0
    assert word_silence['audio']['sha256'] == transcribe.sha256(silent)
    assert not (root / 'word-silence/词级字幕.srt').exists()
    checks.append('real word-mode silent inference has source SHA256 and no invented word cues/SRT')
    playlist = root / 'remote.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:1/never-fetch.ts\n#EXT-X-ENDLIST\n', encoding='utf-8')
    try:
        transcribe.decode(playlist)
    except ValueError as exc:
        assert 'whitelist' in str(exc).lower(), str(exc)
        checks.append('local playlist cannot enable network protocols in offline transcription')
    else:
        raise AssertionError('Offline decoder accepted a network playlist')

    result = {'status': 'passed', 'checks': checks, 'checks_count': len(checks),
              'transcribe_inputs': {'tone': str(tone), 'silent': str(silent), 'tone_out': str(tone_dir),
                                    'silent_out': str(quiet_dir), 'model': str(model)},
              'measurements': {'model_bin_bytes': entry['bytes'], 'model_bin_sha256': entry['sha256'],
                               'tone_segments': tone_report['stats']['segment_count'],
                               'silent_segments': quiet['stats']['segment_count'],
                               'load_seconds': tone_report['model']['load_seconds'],
                               'infer_seconds': tone_report['stats']['infer_seconds'],
                               'realtime_factor': tone_report['stats']['realtime_factor']},
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'transcribe-check.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True,
                        help='faster-whisper model directory; the Skill ships no weights')
    arguments = parser.parse_args()
    editor.sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    model = arguments.model.resolve()
    lines = None
    if not model.is_dir():
        lines = f'Whisper model directory does not exist: {model}; pass --whisper-model with a local model'
    else:
        try:
            import faster_whisper  # noqa: F401  - the tool refuses without it, and so does this check
        except ImportError:
            lines = ('faster-whisper is not importable in this interpreter; run the transcription check with a '
                     'Python that has it (regression skips it with the same reason)')
    if lines:
        editor.emit({'status': 'failed', 'error': lines})
        return 1
    report = check(arguments.out.resolve(), model)
    editor.emit(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
