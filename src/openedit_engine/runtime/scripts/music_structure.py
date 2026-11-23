"""Offline music structure, beat, downbeat, and drop-point analysis (Beat This! ONNX / allin1 / spectral)."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

import numpy as np

import file_hash
import runtime_core as core
import runtime_paths

SR = 22050
BEAT_THIS_FPS = 50.0
_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_WORKER = Path(__file__).resolve().with_name('music_structure_worker.py')


def _decode_mono_wav(src_path, wav_out, start, duration):
    cmd = [
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-protocol_whitelist', 'file,pipe', '-i', str(src_path),
        '-ss', f'{start:.6f}',
    ]
    if duration is not None:
        cmd.extend(['-t', f'{duration:.6f}'])
    cmd.extend([
        '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-ac', '1', '-ar', str(SR), '-c:a', 'pcm_s16le', str(wav_out),
    ])
    core.run(cmd, timeout=600)
    with wave.open(str(wav_out), 'rb') as handle:
        n = handle.getnframes()
        if n < SR:
            raise ValueError('Decoded audio selection is shorter than 1.0 second')
        raw = np.frombuffer(handle.readframes(n), dtype='<i2')
    return raw.astype(np.float32) / 32768.0


def _pick_peaks(probs, min_dist, thr):
    arr = np.asarray(probs, dtype=np.float64).reshape(-1)
    if arr.size < 3:
        return []
    candidates = []
    for idx in range(1, arr.size - 1):
        val = float(arr[idx])
        if val >= thr and val >= float(arr[idx - 1]) and val >= float(arr[idx + 1]):
            candidates.append((val, idx))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    chosen = []
    for _, idx in candidates:
        if all(abs(idx - kept) >= min_dist for kept in chosen):
            chosen.append(idx)
    chosen.sort()
    return chosen


def _rms_db(samples):
    if samples.size == 0:
        return -80.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    return round(max(-80.0, 20.0 * math.log10(max(rms, 1e-4))), 2)


def _estimate_bpm(beats, fallback_bpm=0.0):
    if len(beats) < 2:
        return round(float(fallback_bpm), 3)
    ibis = np.diff(np.asarray(beats, dtype=np.float64))
    valid = ibis[(ibis >= 0.24) & (ibis <= 2.0)]
    if valid.size == 0:
        return round(float(fallback_bpm), 3)
    return round(60.0 / float(np.median(valid)), 3)


def _derive_segments(y, dur, beats, downbeats):
    # Pick bar/section grid points inside (0, dur)
    if len(downbeats) >= 2:
        anchors = [t for t in downbeats if 0.2 < t < dur - 0.2]
    elif len(beats) >= 4:
        anchors = [beats[i] for i in range(0, len(beats), 4) if 0.2 < beats[i] < dur - 0.2]
    else:
        step = max(1.0, min(2.0, dur / 3.0))
        anchors = [round(t, 4) for t in np.arange(step, dur - 0.2, step)]

    bounds = [0.0] + anchors + [round(dur, 4)]
    # Clean strictly increasing
    cleaned = [bounds[0]]
    for t in bounds[1:]:
        if t - cleaned[-1] >= 0.25:
            cleaned.append(round(float(t), 4))
    if len(cleaned) < 2:
        cleaned = [0.0, round(dur, 4)]
    elif dur - cleaned[-1] >= 0.05:
        cleaned[-1] = round(dur, 4)

    bars = []
    for a, b in zip(cleaned, cleaned[1:]):
        i0 = max(0, min(len(y), int(round(a * SR))))
        i1 = max(i0 + 1, min(len(y), int(round(b * SR))))
        bars.append({'start': a, 'end': b, 'energy_db': _rms_db(y[i0:i1])})

    energies = np.array([bar['energy_db'] for bar in bars], dtype=np.float64)
    p35 = float(np.percentile(energies, 35))
    p65 = float(np.percentile(energies, 65))
    peak_e = float(np.max(energies))

    raw_labels = []
    for idx, bar in enumerate(bars):
        e = bar['energy_db']
        if e >= max(p65, peak_e - 3.5):
            lbl = 'chorus'
        elif idx == 0 and e <= p35 + 1.5 and len(bars) >= 3:
            lbl = 'intro'
        elif idx == len(bars) - 1 and e <= p35 + 1.5 and len(bars) >= 3:
            lbl = 'outro'
        elif idx + 1 < len(bars) and bars[idx + 1]['energy_db'] - e >= 2.0 and bars[idx + 1]['energy_db'] >= p65:
            lbl = 'buildup'
        else:
            lbl = 'verse'
        raw_labels.append(lbl)

    # Merge contiguous identically labeled bars into coherent sections
    segments = []
    for bar, lbl in zip(bars, raw_labels):
        if segments and segments[-1]['label'] == lbl:
            prev = segments[-1]
            i0 = int(round(prev['start'] * SR))
            i1 = max(i0 + 1, min(len(y), int(round(bar['end'] * SR))))
            prev['end'] = bar['end']
            prev['energy_db'] = _rms_db(y[i0:i1])
        else:
            segments.append({'label': lbl, 'start': bar['start'], 'end': bar['end'], 'energy_db': bar['energy_db']})

    drop_points = []
    for idx, seg in enumerate(segments):
        if seg['label'] == 'chorus' and seg['start'] > 0.05:
            drop_points.append({'time': seg['start'], 'type': 'chorus_drop', 'energy_db': seg['energy_db']})
        elif idx > 0 and seg['energy_db'] - segments[idx - 1]['energy_db'] >= 2.5:
            drop_points.append({'time': seg['start'], 'type': 'energy_surge', 'energy_db': seg['energy_db']})

    return segments, drop_points


def _run_beat_this(y, dur, *, gpu=False):
    import onnxruntime as ort

    bt_path = runtime_paths.discover_model('beat_this')
    if bt_path is None or not bt_path.is_file():
        raise ValueError('Beat This! ONNX model not found (expect beats/beat_this_final0.onnx or --backend spectral)')
    mel_path = bt_path.parent / 'logmel.onnx'
    if not mel_path.is_file():
        raise ValueError(f'Missing sibling logmel.onnx next to {bt_path}')

    if gpu:
        runtime_paths.prepare_cuda_dlls()
    avail = ort.get_available_providers()
    providers = runtime_paths.resolve_ort_providers(avail, device='auto' if gpu else 'cpu', gpu=gpu)
    mel_sess = ort.InferenceSession(str(mel_path), providers=['CPUExecutionProvider'])
    bt_sess = ort.InferenceSession(str(bt_path), providers=providers)
    active_provider = bt_sess.get_providers()[0]

    chunk_s = 45.0
    overlap_s = 5.0
    chunk_n = int(chunk_s * SR)
    step_n = int((chunk_s - overlap_s) * SR)
    total_frames = int(round(dur * BEAT_THIS_FPS)) + 4
    beat_acc = np.zeros(total_frames, dtype=np.float64)
    down_acc = np.zeros(total_frames, dtype=np.float64)
    wt_acc = np.zeros(total_frames, dtype=np.float64)

    starts = [0] if len(y) <= int(50.0 * SR) else list(range(0, max(1, len(y) - int(1.0 * SR)), step_n))
    for s_idx in starts:
        e_idx = min(len(y), s_idx + chunk_n)
        seg = y[s_idx:e_idx]
        if seg.size < SR:
            break
        mel = mel_sess.run(None, {'audio': seg[None, :].astype(np.float32)})[0]
        b_log, d_log = bt_sess.run(None, {'mel': mel[None, :, :].astype(np.float32)})
        b_p = 1.0 / (1.0 + np.exp(-np.clip(b_log[0], -30.0, 30.0)))
        d_p = 1.0 / (1.0 + np.exp(-np.clip(d_log[0], -30.0, 30.0)))
        f0 = int(round((s_idx / SR) * BEAT_THIS_FPS))
        n_f = min(len(b_p), total_frames - f0)
        if n_f <= 0:
            continue
        w = np.ones(n_f, dtype=np.float64)
        if len(starts) > 1 and s_idx > 0:
            fade = min(n_f // 3, int(2.0 * BEAT_THIS_FPS))
            w[:fade] = np.linspace(0.0, 1.0, fade)
        beat_acc[f0:f0 + n_f] += b_p[:n_f] * w
        down_acc[f0:f0 + n_f] += d_p[:n_f] * w
        wt_acc[f0:f0 + n_f] += w

    valid = wt_acc > 0
    beat_probs = np.where(valid, beat_acc / np.maximum(wt_acc, 1e-6), 0.0)
    down_probs = np.where(valid, down_acc / np.maximum(wt_acc, 1e-6), 0.0)

    b_idx = _pick_peaks(beat_probs, min_dist=12, thr=0.35)
    if len(b_idx) < 2:
        b_idx = _pick_peaks(beat_probs, min_dist=12, thr=0.15)
    d_idx = _pick_peaks(down_probs, min_dist=35, thr=0.35)
    if len(d_idx) < 2 and len(b_idx) >= 4:
        d_idx = _pick_peaks(down_probs, min_dist=35, thr=0.15)

    beats = sorted({round(i / BEAT_THIS_FPS, 4) for i in b_idx if 0.0 <= i / BEAT_THIS_FPS <= dur})
    raw_down = [round(i / BEAT_THIS_FPS, 4) for i in d_idx if 0.0 <= i / BEAT_THIS_FPS <= dur]
    downbeats = []
    for dt in raw_down:
        if beats:
            nearest = min(beats, key=lambda bt: abs(bt - dt))
            if abs(nearest - dt) <= 0.06:
                dt = nearest
        if not downbeats or dt > downbeats[-1] + 0.25:
            downbeats.append(dt)
    if not downbeats and len(beats) >= 4:
        downbeats = beats[::4]

    segments, drop_points = _derive_segments(y, dur, beats, downbeats)
    return {
        'backend': 'beat_this_onnx',
        'provider': active_provider,
        'model': str(bt_path),
        'bpm': _estimate_bpm(beats),
        'beats': beats,
        'downbeats': downbeats,
        'segments': segments,
        'drop_points': drop_points,
    }


def _run_allin1(wav_path, y, dur, *, gpu=False):
    py = runtime_paths.discover_torch_worker_python()
    if py is None:
        raise ValueError('Isolated CUDA PyTorch worker python not found for allin1 backend')
    models_dir = runtime_paths.models_root()
    cache_dir = str((models_dir / 'allin1') if models_dir else (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools' / 'models' / 'allin1'))
    out_json = Path(wav_path).with_name('allin1_out.json')
    cmd = [
        str(py), str(_WORKER),
        '--input', str(wav_path),
        '--out-json', str(out_json),
        '--device', 'cuda' if gpu else 'cpu',
        '--cache-dir', cache_dir,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=600, creationflags=_NO_WIN,
    )
    if proc.returncode != 0 or not out_json.is_file():
        raise ValueError(f'allin1 worker failed (rc={proc.returncode}): {proc.stderr[-1500:]}')
    data = core.read_json(out_json)
    beats = sorted({round(float(t), 4) for t in data.get('beats', []) if 0.0 <= float(t) <= dur + 0.05})
    downbeats = sorted({round(float(t), 4) for t in data.get('downbeats', []) if 0.0 <= float(t) <= dur + 0.05})
    segments = []
    for s in data.get('segments', []):
        a = max(0.0, round(float(s['start']), 4))
        b = min(round(dur, 4), round(float(s['end']), 4))
        if b > a:
            i0 = int(round(a * SR))
            i1 = max(i0 + 1, min(len(y), int(round(b * SR))))
            segments.append({'label': str(s['label']), 'start': a, 'end': b, 'energy_db': _rms_db(y[i0:i1])})
    if not segments:
        segments, drop_points = _derive_segments(y, dur, beats, downbeats)
    else:
        drop_points = []
        for idx, seg in enumerate(segments):
            if seg['label'] == 'chorus' and seg['start'] > 0.05:
                drop_points.append({'time': seg['start'], 'type': 'chorus_drop', 'energy_db': seg['energy_db']})
            elif idx > 0 and seg['energy_db'] - segments[idx - 1]['energy_db'] >= 2.5:
                drop_points.append({'time': seg['start'], 'type': 'energy_surge', 'energy_db': seg['energy_db']})
    return {
        'backend': 'allin1_infer',
        'provider': f'torch_{data.get("device", "cpu")}',
        'model': data.get('model', 'harmonix-all'),
        'bpm': round(float(data.get('bpm') or _estimate_bpm(beats)), 3),
        'beats': beats,
        'downbeats': downbeats,
        'segments': segments,
        'drop_points': drop_points,
    }


def _run_spectral(y, dur):
    beats = []
    tempo_bpm = 0.0
    try:
        import librosa
        tempo, frames = librosa.beat.beat_track(y=y, sr=SR, hop_length=256, start_bpm=120.0, units='frames')
        tempo_bpm = float(np.asarray(tempo).reshape(-1)[0])
        beats = sorted({round(int(f) * 256 / SR, 4) for f in np.asarray(frames) if 0.0 <= int(f) * 256 / SR <= dur})
    except ImportError:
        pass
    if len(beats) < 2:
        # Pure numpy energy-flux peak fallback
        hop = 441
        n_frames = max(3, len(y) // hop)
        rms = np.array([
            float(np.sqrt(np.mean(y[i * hop:min(len(y), (i + 2) * hop)] ** 2)))
            for i in range(n_frames)
        ], dtype=np.float64)
        flux = np.maximum(0.0, np.diff(rms, prepend=rms[0]))
        top = float(np.max(flux)) if flux.size else 0.0
        if top > 1e-4:
            pks = _pick_peaks(flux / top, min_dist=12, thr=0.35)
            beats = [round(i * hop / SR, 4) for i in pks if 0.0 <= i * hop / SR <= dur]
    downbeats = beats[::4] if len(beats) >= 4 else beats[:1]
    segments, drop_points = _derive_segments(y, dur, beats, downbeats)
    return {
        'backend': 'spectral_librosa',
        'provider': 'CPU',
        'model': None,
        'bpm': _estimate_bpm(beats, fallback_bpm=tempo_bpm),
        'beats': beats,
        'downbeats': downbeats,
        'segments': segments,
        'drop_points': drop_points,
    }


def run_music_structure(args):
    src = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not src.is_file():
        raise ValueError(f'Input audio/video file does not exist: {src}')
    info = core.probe(src)
    if not info.get('has_audio'):
        raise ValueError(f'Input has no audio stream: {src}')
    src_dur = float(info.get('duration') or 0.0)
    start = core.number(args.start, 'start', 0.0)
    if start >= src_dur - 1.0:
        raise ValueError(f'Start {start}s leaves less than 1.0s in {src.name} (duration={src_dur}s)')
    if args.duration is not None:
        duration = core.number(args.duration, 'duration', 1.0, 1800.0)
        if start + duration > src_dur + 0.05:
            raise ValueError(f'Selection [{start}, {start + duration}] exceeds source duration {src_dur}s')
    else:
        duration = min(1800.0, src_dur - start)

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    backend = args.backend
    if backend == 'auto':
        bt = runtime_paths.discover_model('beat_this')
        if bt is not None and (bt.parent / 'logmel.onnx').is_file():
            backend = 'beat-this'
        else:
            backend = 'spectral'

    with tempfile.TemporaryDirectory(prefix='oe-mstruct-') as td:
        wav_path = Path(td) / 'mono_22k.wav'
        y = _decode_mono_wav(src, wav_path, start, duration)
        actual_dur = round(len(y) / SR, 4)
        if backend == 'beat-this':
            res = _run_beat_this(y, actual_dur, gpu=bool(args.gpu))
        elif backend == 'allin1':
            res = _run_allin1(wav_path, y, actual_dur, gpu=bool(args.gpu))
        else:
            res = _run_spectral(y, actual_dur)

    out_dir = core.new_directory(out_cand)
    boundaries = sorted({round(s['start'], 4) for s in res['segments'] if s['start'] > 0.0})
    report = {
        'status': 'passed',
        'operation': 'music-structure',
        'backend': res['backend'],
        'provider': res['provider'],
        'model': res['model'],
        'source': {
            'path': str(src),
            'sha256': file_hash.sha256(src),
            'source_duration': round(src_dur, 4),
        },
        'start_seconds': round(start, 4),
        'duration': actual_dur,
        'bpm': res['bpm'],
        'beat_count': len(res['beats']),
        'downbeat_count': len(res['downbeats']),
        'beats': res['beats'],
        'downbeats': res['downbeats'],
        'segments': res['segments'],
        'drop_points': res['drop_points'],
        'boundaries': boundaries,
        'auditory_review': 'unverified',
    }
    core.write_json(out_dir / 'structure.json', report)
    return report


def load_rhythm_grid(path_or_dir):
    """Load validated beat/downbeat/drop grid from music-structure (structure.json or dir) or legacy beats dir."""
    target = Path(os.path.expanduser(os.path.expandvars(str(path_or_dir)))).resolve()
    struct_file = target if (target.is_file() and target.name == 'structure.json') else (target / 'structure.json')
    if struct_file.is_file():
        data = core.read_json(struct_file)
        if not isinstance(data, dict) or data.get('status') != 'passed' or data.get('operation') != 'music-structure':
            raise ValueError(f'Invalid music-structure report: {struct_file}')
        src = data.get('source')
        if not isinstance(src, dict) or not src.get('path') or not src.get('sha256'):
            raise ValueError(f'music-structure report missing source provenance: {struct_file}')
        src_path = Path(src['path']).resolve()
        if not src_path.is_file():
            raise ValueError(f'music-structure source media is missing: {src_path}')
        if file_hash.sha256(src_path) != src['sha256']:
            raise ValueError('music-structure source media changed; rerun music-structure')
        start_s = float(data.get('start_seconds', 0.0))
        dur_s = float(data.get('duration', 0.0))
        if not (math.isfinite(start_s) and start_s >= 0.0 and math.isfinite(dur_s) and dur_s > 0.0):
            raise ValueError(f'Invalid music-structure timing window in {struct_file}')
        return {
            'kind': 'music_structure',
            'structure_file': str(struct_file),
            'source_path': str(src_path),
            'source_sha256': src['sha256'],
            'start_seconds': start_s,
            'duration': dur_s,
            'bpm': float(data.get('bpm') or 0.0),
            'beats': [float(t) for t in data.get('beats', [])],
            'downbeats': [float(t) for t in data.get('downbeats', [])],
            'drop_points': list(data.get('drop_points', [])),
            'boundaries': [float(t) for t in data.get('boundaries', [])],
            'segments': list(data.get('segments', [])),
        }

    import beats as _beats_mod
    b = _beats_mod.load_result(target)
    rate = b['selection']['sample_rate']
    return {
        'kind': 'beats',
        'source_path': b['source']['path'],
        'source_sha256': b['source']['sha256'],
        'start_seconds': b['selection']['start_us'] / 1_000_000.0,
        'duration': b['selection']['sample_count'] / float(rate),
        'bpm': float(b.get('tempo_bpm') or 0.0),
        'beats': [round(s / float(rate), 6) for s in b['beat_samples']],
        'downbeats': [],
        'drop_points': [],
        'boundaries': [],
        'segments': [],
        'raw_beats': b,
    }


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local audio or video file with an audio stream')
    parser.add_argument('--out', required=True, help='New output directory for structure.json')
    parser.add_argument('--backend', choices=('auto', 'beat-this', 'allin1', 'spectral'), default='auto')
    parser.add_argument('--start', type=float, default=0.0, help='Start offset in seconds (default 0)')
    parser.add_argument('--duration', type=float, default=None, help='Optional duration in [1, 1800] seconds')
    parser.add_argument('--gpu', action='store_true', help='Enable CUDA GPU inference when available')
    args = parser.parse_args(argv)
    try:
        report = run_music_structure(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
