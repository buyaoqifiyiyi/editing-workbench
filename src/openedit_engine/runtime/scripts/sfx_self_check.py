"""Procedural stereo SFX synthesis & BGM micro-ducking self-check."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import wave

import numpy as np

import runtime_core as core
import sfx


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sfx.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    cues_path = root / 'cues.json'
    cues = [
        {'type': 'whoosh', 'time': 0.50, 'duration': 0.40, 'pan_start': -0.9, 'pan_end': 0.9, 'gain_db': -2.0},
        {'type': 'riser', 'time': 2.00, 'duration': 1.10, 'gain_db': -3.0, 'duck_db': -2.0},
        {'type': 'sub_drop', 'time': 2.00, 'duration': 1.20, 'gain_db': 2.0, 'duck_db': -3.0},
        {'type': 'impact', 'time': 3.30, 'duration': 0.60, 'gain_db': 3.5, 'duck_db': -2.5},
        {'type': 'katana_draw', 'time': 4.10, 'duration': 0.55, 'pan': 0.35, 'gain_db': 0.0},
        {'type': 'laser_sweep', 'time': 4.90, 'duration': 0.50, 'pan_start': 0.8, 'pan_end': -0.8},
        {'type': 'glitch_tick', 'time': 5.50, 'duration': 0.14, 'pan': -0.4},
        {'type': 'bass_swell', 'time': 6.40, 'duration': 0.80, 'gain_db': -1.0},
    ]
    core.write_json(cues_path, {'cues': cues, 'duck_events': [{'start': 5.7, 'end': 5.95, 'duck_db': -2.0}]})

    out_dir = root / 'sfx_out'
    duck_extra = root / 'extra_duck.json'
    dur = 6.60
    code, rep = _run_main([
        '--cues', cues_path,
        '--duration', dur,
        '--out', out_dir,
        '--sample-rate', 48000,
        '--master-peak-db', -1.5,
        '--duck-out', duck_extra,
    ])
    assert code == 0 and rep['status'] == 'passed', rep
    assert rep['cue_count'] == 8
    wav_path = Path(rep['output_wav']['path'])
    assert wav_path.is_file() and (out_dir / 'sfx-report.json').is_file() and duck_extra.is_file()

    with wave.open(str(wav_path), 'rb') as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 48000
        expected_frames = round(dur * 48000)
        assert wf.getnframes() == expected_frames, (wf.getnframes(), expected_frames)
        pcm = np.frombuffer(wf.readframes(expected_frames), dtype='<i2').reshape(-1, 2)

    assert np.count_nonzero((pcm == 32767) | (pcm == -32768)) == 0
    assert rep['audio_stats']['full_scale_clipped_samples'] == 0
    assert rep['audio_stats']['sample_peak_dbfs'] <= -1.45

    # Verify early stereo whoosh pan sweep: first third heavier on L, final third heavier on R.
    w_early = np.sqrt(np.mean(pcm[int(0.30 * 48000):int(0.45 * 48000), 0].astype(float) ** 2))
    w_late = np.sqrt(np.mean(pcm[int(0.54 * 48000):int(0.68 * 48000), 1].astype(float) ** 2))
    w_early_r = np.sqrt(np.mean(pcm[int(0.30 * 48000):int(0.45 * 48000), 1].astype(float) ** 2))
    w_late_l = np.sqrt(np.mean(pcm[int(0.54 * 48000):int(0.68 * 48000), 0].astype(float) ** 2))
    assert w_early > w_early_r * 1.4 and w_late > w_late_l * 1.4, (w_early, w_early_r, w_late, w_late_l)

    duck = json.loads(duck_extra.read_text(encoding='utf-8'))
    kfs = duck['keyframes']
    assert len(kfs) >= 4 and kfs[0]['time'] == 0.0
    core.validate_parameter_keys(kfs, dur, 'duck_check', 0, 10)
    checks.append(f'synthesized 8 cue types ({expected_frames} frames @ 48kHz stereo, 0 clipped samples, {len(kfs)} duck keys)')

    bad_cues = root / 'bad_cues.json'
    core.write_json(bad_cues, [{'type': 'nonexistent', 'time': 0.5}])
    code_b, rep_b = _run_main(['--cues', bad_cues, '--duration', 2.0, '--out', root / 'bad_out'])
    assert code_b == 1 and rep_b['status'] == 'failed'
    assert not (root / 'bad_out').exists()
    checks.append('refused invalid cue type without creating output dir')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'sfx-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
