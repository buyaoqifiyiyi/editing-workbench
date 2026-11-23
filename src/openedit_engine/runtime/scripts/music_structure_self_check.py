"""Regression check for music_structure (Beat This! ONNX and spectral offline fallback)."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import wave

import numpy as np

import music_structure
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = music_structure.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_musical_fixture(path, sr=22050, dur=8.0):
    """Build 120 BPM 8s WAV: quiet 0..4s intro/verse kick + loud 4..8s chorus surge with chords."""
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    y = np.zeros(n, dtype=np.float64)
    for idx in range(int(dur * 2)):
        bt = idx * 0.5
        i0 = int(bt * sr)
        burst_n = min(int(0.12 * sr), n - i0)
        tt = np.arange(burst_n, dtype=np.float64) / sr
        accent = 1.45 if idx % 4 == 0 else 1.0
        amp = (0.35 if bt < 4.0 else 0.72) * accent
        kick = amp * np.sin(2.0 * np.pi * (120.0 * np.exp(-tt * 28.0) + 48.0) * tt) * np.exp(-tt * 16.0)
        click = 0.25 * amp * np.sin(2.0 * np.pi * 1800.0 * tt) * np.exp(-tt * 70.0)
        y[i0:i0 + burst_n] += kick + click
    # Chorus pad bed on 4..8s
    mask = t >= 4.0
    y[mask] += 0.22 * (np.sin(2.0 * np.pi * 220.0 * t[mask]) + 0.6 * np.sin(2.0 * np.pi * 330.0 * t[mask]))
    y = np.clip(y * 0.85, -1.0, 1.0)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes((y * 32767.0).astype('<i2').tobytes())


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    song = root / 'song_120bpm.wav'
    _make_musical_fixture(song)

    # 1. Deterministic offline spectral fallback
    sp_out = root / 'spectral'
    code, sp_rep = _run_main(['--input', song, '--out', sp_out, '--backend', 'spectral'])
    assert code == 0 and sp_rep['status'] == 'passed' and sp_rep['backend'] == 'spectral_librosa', sp_rep
    assert (sp_out / 'structure.json').is_file()
    assert sp_rep['beat_count'] >= 4 and abs(sp_rep['bpm'] - 120.0) <= 12.0, sp_rep
    assert len(sp_rep['segments']) >= 2 and any(s['label'] == 'chorus' for s in sp_rep['segments']), sp_rep
    assert all(b > a for a, b in zip(sp_rep['beats'], sp_rep['beats'][1:]))
    checks.append(f'spectral fallback: bpm={sp_rep["bpm"]}, beats={sp_rep["beat_count"]}, segments={len(sp_rep["segments"])}')

    # 2. Live Beat This! ONNX when weights present
    bt_path = runtime_paths.discover_model('beat_this')
    if bt_path is not None and (bt_path.parent / 'logmel.onnx').is_file():
        bt_out = root / 'beat-this'
        code_bt, bt_rep = _run_main(['--input', song, '--out', bt_out, '--backend', 'beat-this', '--gpu'])
        assert code_bt == 0 and bt_rep['status'] == 'passed' and bt_rep['backend'] == 'beat_this_onnx', bt_rep
        assert bt_rep['beat_count'] >= 6 and bt_rep['downbeat_count'] >= 2, bt_rep
        assert any(s['label'] == 'chorus' for s in bt_rep['segments']), bt_rep
        assert all(b > a for a, b in zip(bt_rep['beats'], bt_rep['beats'][1:]))
        checks.append(
            f'beat-this ONNX ({bt_rep["provider"]}): bpm={bt_rep["bpm"]}, '
            f'beats={bt_rep["beat_count"]}, downbeats={bt_rep["downbeat_count"]}'
        )
    else:
        checks.append('beat-this ONNX skipped: weights not installed')

    # 3. Refusals
    code_bad, rep_bad = _run_main(['--input', song, '--out', sp_out])
    assert code_bad == 1 and rep_bad['status'] == 'failed'
    checks.append('existing output refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'music-structure-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
