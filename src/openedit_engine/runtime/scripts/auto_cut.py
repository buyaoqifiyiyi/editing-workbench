"""Offline silence removal & jumpcut rough-cut plan generator (Silero VAD ONNX + FFmpeg fallback)."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import wave

import numpy as np

import file_hash
import runtime_core as core
import runtime_paths

VAD_RATE = 16000
VAD_WINDOW = 512
VAD_CONTEXT = 64
_SILENCE_START_RE = re.compile(r'silence_start:\s*([0-9eE.+-]+)')
_SILENCE_END_RE = re.compile(r'silence_end:\s*([0-9eE.+-]+)')


def decode_mono_16k(path):
    """Decode input audio stream to 16 kHz mono float32 in [-1, 1] using local file/pipe FFmpeg."""
    with tempfile.TemporaryDirectory(prefix='autocut-wav-') as tmp:
        wav_path = Path(tmp) / 'mono16k.wav'
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file,pipe',
            '-i', str(path), '-vn', '-ac', '1', '-ar', str(VAD_RATE), '-c:a', 'pcm_s16le', str(wav_path),
        ], timeout=600)
        with wave.open(str(wav_path), 'rb') as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1 or handle.getframerate() != VAD_RATE:
                raise ValueError('ffmpeg did not deliver 16 kHz mono PCM16 for auto-cut')
            frames = handle.getnframes()
            raw = np.frombuffer(handle.readframes(frames), dtype='<i2')
    if frames == 0:
        raise ValueError(f'{Path(path).name} decoded to 0 audio samples; auto-cut needs audible audio')
    return raw.astype(np.float32) / 32768.0


def _resolve_vad_model(explicit, detector):
    if explicit:
        cand = Path(os.path.expanduser(os.path.expandvars(str(explicit)))).resolve()
        if not cand.is_file():
            raise ValueError(f'VAD model is not a file: {cand}')
        return cand
    if detector == 'ffmpeg':
        return None
    env_vad = os.environ.get('OPENEDIT_VAD_MODEL')
    if env_vad and env_vad.strip():
        cand = Path(os.path.expanduser(os.path.expandvars(env_vad.strip()))).resolve()
        if not cand.is_file():
            raise ValueError(f'OPENEDIT_VAD_MODEL is not a file: {cand}')
        return cand
    discovered = runtime_paths.discover_model('vad', include_env=False)
    if discovered is not None and discovered.is_file():
        return discovered
    return None


def _load_silero_session(model_path, *, gpu=False):
    if gpu:
        runtime_paths.prepare_cuda_dlls()
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise ValueError(f'Silero VAD requires onnxruntime in this interpreter: {exc}') from None
    providers = runtime_paths.resolve_ort_providers(ort.get_available_providers(), gpu=gpu)
    try:
        session = ort.InferenceSession(str(model_path), providers=providers)
    except Exception as exc:
        raise ValueError(f'onnxruntime could not initialize {model_path.name}: {exc}') from None
    input_names = {i.name for i in session.get_inputs()}
    if not {'input', 'state', 'sr'}.issubset(input_names):
        raise ValueError(f'Unsupported VAD ONNX inputs {sorted(input_names)}; expected Silero VAD v5 (input, state, sr)')
    return session


def detect_speech_silero(audio, duration, model_path, *, threshold, min_silence, gpu=False):
    """Run Silero VAD v5 chunked inference (512 samples + 64-sample context @ 16 kHz)."""
    session = _load_silero_session(model_path, gpu=gpu)
    total = int(audio.shape[0])
    pad_len = (-total) % VAD_WINDOW
    padded = np.pad(audio, (0, pad_len)) if pad_len else audio
    num_chunks = padded.shape[0] // VAD_WINDOW

    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, VAD_CONTEXT), dtype=np.float32)
    sr = np.array(VAD_RATE, dtype=np.int64)
    neg_thresh = max(0.05, threshold - 0.15)

    triggered = False
    cur_start = 0.0
    temp_end = None
    spans = []

    for idx in range(num_chunks):
        chunk = padded[idx * VAD_WINDOW:(idx + 1) * VAD_WINDOW][None, :]
        window_in = np.concatenate([context, chunk], axis=1).astype(np.float32)
        out, state = session.run(None, {'input': window_in, 'state': state, 'sr': sr})
        context = chunk[:, -VAD_CONTEXT:]
        prob = float(out[0, 0])
        cs = (idx * VAD_WINDOW) / VAD_RATE
        ce = min(duration, ((idx + 1) * VAD_WINDOW) / VAD_RATE)

        if prob >= threshold:
            if temp_end is not None:
                temp_end = None
            if not triggered:
                triggered = True
                cur_start = cs
        elif prob < neg_thresh and triggered:
            if temp_end is None:
                temp_end = cs
            if ce - temp_end >= min_silence:
                spans.append((cur_start, temp_end))
                triggered = False
                temp_end = None

    if triggered:
        end = temp_end if (temp_end is not None and duration - temp_end >= min_silence) else duration
        if end > cur_start:
            spans.append((cur_start, min(duration, end)))
    return spans


def detect_speech_ffmpeg(source, duration, *, threshold_db, min_silence):
    """Detect non-silent spans via FFmpeg silencedetect filter."""
    proc = core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin',
        '-protocol_whitelist', 'file,pipe',
        '-i', str(source), '-vn',
        '-af', f'silencedetect=noise={threshold_db:.2f}dB:d={min_silence:.3f}',
        '-f', 'null', '-',
    ], timeout=600)
    silences = []
    open_start = None
    for line in proc.stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            val = max(0.0, min(duration, float(m_start.group(1))))
            if open_start is None:
                open_start = val
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end and open_start is not None:
            end_val = max(open_start, min(duration, float(m_end.group(1))))
            silences.append((open_start, end_val))
            open_start = None
    if open_start is not None:
        silences.append((open_start, duration))

    spans = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor + 1e-4:
            spans.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if duration > cursor + 1e-4:
        spans.append((cursor, duration))
    return spans


def merge_speech_spans(raw_spans, duration, *, min_speech, min_silence, pad):
    """Drop short noise blips, apply safety padding, and merge adjacent speech spans."""
    kept = []
    for start, end in raw_spans:
        if end - start < min_speech:
            continue
        ps = max(0.0, start - pad)
        pe = min(duration, end + pad)
        if pe - ps > 0.02:
            kept.append((ps, pe))
    if not kept:
        return []

    kept.sort(key=lambda item: item[0])
    merged = [kept[0]]
    for start, end in kept[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e or (start - prev_e) < min_silence:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def _build_segments_and_silences(merged_spans, duration, *, fade):
    kept_segments = []
    removed_silences = []
    tl_cursor_us = 0
    sil_cursor = 0.0

    for idx, (s, e) in enumerate(merged_spans):
        src_in_u = core.us(max(0.0, s))
        src_end_u = core.us(min(duration, e))
        dur_u = src_end_u - src_in_u
        if dur_u < 20_000:
            continue
        src_in = src_in_u / 1_000_000
        seg_dur = dur_u / 1_000_000
        tl_start = tl_cursor_us / 1_000_000
        tl_cursor_us += dur_u

        if src_in > sil_cursor + 1e-4:
            removed_silences.append({
                'start': round(sil_cursor, 4),
                'end': round(src_in, 4),
                'duration': round(src_in - sil_cursor, 4),
            })
        sil_cursor = src_in + seg_dur

        seg_fade = round(min(fade, seg_dur * 0.4), 4) if fade > 0 else 0.0
        kept_segments.append({
            'index': len(kept_segments),
            'source_in': src_in,
            'source_out': round(src_in + seg_dur, 6),
            'duration': seg_dur,
            'timeline_start': tl_start,
            'fade': seg_fade,
        })

    if duration > sil_cursor + 1e-4:
        removed_silences.append({
            'start': round(sil_cursor, 4),
            'end': round(duration, 4),
            'duration': round(duration - sil_cursor, 4),
        })
    return kept_segments, removed_silences


def run_auto_cut(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')

    min_silence = core.number(args.min_silence, 'min_silence', 0.05, 30.0)
    min_speech = core.number(args.min_speech, 'min_speech', 0.05, 30.0)
    pad = core.number(args.pad, 'pad', 0.0, 2.0)
    fade = core.number(args.fade, 'fade', 0.0, 0.5)
    plan_name = core.check_text(args.name, 'name', 160) if args.name else f'{source.stem[:120]}_autocut'

    info = core.probe(source)
    if info['kind'] not in ('video', 'audio') or not info.get('has_audio'):
        raise ValueError('auto-cut requires a local video or audio file with an audible audio stream')
    duration = float(info.get('duration') or 0.0)
    if duration <= 0.0:
        raise ValueError('Input media duration must be positive')

    width = int(core.number(args.width, 'width', 64, 7680)) if args.width is not None else int(info.get('width') or 1920)
    height = int(core.number(args.height, 'height', 64, 4320)) if args.height is not None else int(info.get('height') or 1080)
    fps = core.number(args.fps, 'fps', 1, 120) if args.fps is not None else float(info.get('fps') or 30.0)

    vad_model = _resolve_vad_model(args.vad_model, args.detector)
    if args.detector == 'silero':
        if vad_model is None:
            raise ValueError(
                'Silero VAD requested (--detector silero), but no silero_vad.onnx model was found; '
                'pass --vad-model PATH, set OPENEDIT_VAD_MODEL, or use --detector ffmpeg'
            )
        detector_used = 'silero'
    elif args.detector == 'ffmpeg':
        if args.vad_model:
            raise ValueError('--vad-model is not valid with --detector ffmpeg')
        detector_used = 'ffmpeg'
    else:
        detector_used = 'silero' if vad_model is not None else 'ffmpeg'

    if detector_used == 'silero':
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            if args.detector == 'silero':
                raise ValueError('Silero VAD requires onnxruntime in this interpreter') from None
            detector_used = 'ffmpeg'
            vad_model = None

    if detector_used == 'silero':
        thresh = 0.5 if args.threshold is None else core.number(args.threshold, 'threshold', 0.05, 0.95)
    else:
        thresh = -35.0 if args.threshold is None else core.number(args.threshold, 'threshold', -80.0, -5.0)

    if Path(args.out).exists() or Path(args.out).is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    if detector_used == 'silero':
        audio_16k = decode_mono_16k(source)
        raw_spans = detect_speech_silero(
            audio_16k, duration, vad_model, threshold=thresh, min_silence=min_silence,
            gpu=getattr(args, 'gpu', False),
        )
    else:
        raw_spans = detect_speech_ffmpeg(source, duration, threshold_db=thresh, min_silence=min_silence)

    merged_spans = merge_speech_spans(raw_spans, duration, min_speech=min_speech, min_silence=min_silence, pad=pad)
    kept_segments, removed_silences = _build_segments_and_silences(merged_spans, duration, fade=fade)

    out = core.new_directory(args.out)
    kept_dur = round(sum(seg['duration'] for seg in kept_segments), 4)
    removed_dur = round(max(0.0, duration - kept_dur), 4)
    speech_ratio = round(kept_dur / duration, 4) if duration > 0 else 0.0

    audio_clips = []
    if fade > 0 or info['kind'] == 'audio':
        for seg in kept_segments:
            entry = {
                'path': str(source),
                'start': seg['timeline_start'],
                'source_in': seg['source_in'],
                'duration': seg['duration'],
                'track': 'voice',
                'volume': 1.0,
            }
            if seg['fade'] > 0:
                entry['fade_in'] = seg['fade']
                entry['fade_out'] = seg['fade']
            audio_clips.append(entry)

    plan_path = None
    note = None
    if info['kind'] == 'video' and kept_segments:
        plan_obj = {
            'schema_version': 2,
            'name': plan_name,
            'canvas': {'width': width, 'height': height, 'fps': fps},
            'clips': [
                {
                    'path': str(source),
                    'start': seg['timeline_start'],
                    'source_in': seg['source_in'],
                    'duration': seg['duration'],
                    'track': 'video',
                    'volume': 0.0 if audio_clips else 1.0,
                    'reason': f"auto-cut speech #{seg['index']}",
                }
                for seg in kept_segments
            ],
            'audio': audio_clips,
            'layer_order': ['voice', 'video'] if audio_clips else ['video'],
        }
        plan_path = out / 'edit-plan.json'
        core.write_json(plan_path, plan_obj)
        core.validate_plan(plan_path)
    elif not kept_segments:
        note = 'No speech segments exceeded threshold/min_speech; edit-plan.json not written because clips would be empty.'
    else:
        note = 'Input is audio-only; audio_clips written in cuts.json without a video edit-plan.json.'

    report = {
        'status': 'passed',
        'input': str(source),
        'sha256': file_hash.sha256(source),
        'out': str(out),
        'detector_requested': args.detector,
        'detector_used': detector_used,
        'vad_model': (
            {'path': str(vad_model), 'bytes': vad_model.stat().st_size, 'sha256': file_hash.sha256(vad_model)}
            if vad_model is not None else None
        ),
        'params': {
            'threshold': thresh,
            'min_silence': min_silence,
            'min_speech': min_speech,
            'pad': pad,
            'fade': fade,
        },
        'source_duration': round(duration, 4),
        'kept_duration': kept_dur,
        'removed_duration': removed_dur,
        'speech_ratio': speech_ratio,
        'kept_count': len(kept_segments),
        'removed_silence_count': len(removed_silences),
        'kept_segments': kept_segments,
        'removed_silences': removed_silences,
        'audio_clips': audio_clips,
        'plan_path': str(plan_path) if plan_path else None,
        'note': note,
        'boundaries': [
            'Candidate speech boundaries depend on VAD/dB thresholds and should be reviewed at natural breathing pauses.',
            'fade > 0 routes audio onto track "voice" (audio[]) with video clip volume=0.0 so click-free fade_in/fade_out validate natively.',
        ],
    }
    core.write_json(out / 'cuts.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video or audio file with an audio stream')
    parser.add_argument('--out', required=True, help='New output directory for cuts.json and edit-plan.json')
    parser.add_argument('--detector', choices=('auto', 'silero', 'ffmpeg'), default='auto')
    parser.add_argument('--vad-model', help='Optional Silero VAD ONNX file (else OPENEDIT_VAD_MODEL / auto-discovery)')
    parser.add_argument('--threshold', type=float, default=None, help='Prob in [0.05, 0.95] for silero, dB in [-80, -5] for ffmpeg')
    parser.add_argument('--min-silence', type=float, default=0.45, help='Minimum silence duration in seconds (default 0.45)')
    parser.add_argument('--min-speech', type=float, default=0.25, help='Minimum kept speech segment in seconds (default 0.25)')
    parser.add_argument('--pad', type=float, default=0.12, help='Pre/post padding around speech in seconds (default 0.12)')
    parser.add_argument('--fade', type=float, default=0.02, help='Click-free audio fade_in/fade_out in seconds (default 0.02)')
    parser.add_argument('--name', help='Optional edit plan name')
    parser.add_argument('--width', type=int, default=None, help='Optional canvas width override')
    parser.add_argument('--height', type=int, default=None, help='Optional canvas height override')
    parser.add_argument('--fps', type=float, default=None, help='Optional canvas fps override')
    parser.add_argument('--gpu', action='store_true', help='Prefer CUDA/DirectML ORT provider when available')
    args = parser.parse_args(argv)
    try:
        report = run_auto_cut(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
