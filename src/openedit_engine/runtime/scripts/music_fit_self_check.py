"""Regression check for music_fit: on-beat cuts beat chance, off-beat cuts do not, output is never overwritten."""
import argparse
from pathlib import Path

import file_hash
import music_fit
import music_structure_self_check
import runtime_core as core

COLORS = ('red', 'blue', 'green', 'white', 'yellow', 'purple', 'orange', 'cyan')


def _cut_video(song, out, first, step, total=8.0, colors=COLORS):
    """Colour blocks: first block lasts `first` s, then one block every `step` s; music = the 120 BPM fixture."""
    lengths, t = [first], first
    while t + step < total - 1e-6:
        lengths.append(step)
        t += step
    lengths.append(round(total - t, 4))
    cmd = [core.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n']
    for i, d in enumerate(lengths):
        cmd += ['-f', 'lavfi', '-i', f'color=c={colors[i % len(colors)]}:s=320x180:r=30:d={d}']
    cmd += ['-i', str(song)]
    n = len(lengths)
    cmd += ['-filter_complex', ''.join(f'[{i}:v]' for i in range(n)) + f'concat=n={n}:v=1:a=0[v]',
            '-map', '[v]', '-map', f'{n}:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(out)]
    core.run(cmd, timeout=120)
    return out


def _args(video, out, **extra):
    base = dict(input=str(video), out=str(out), structure=None, music_offset=0.0, backend='spectral',
                threshold=0.35, tolerance=0.05, skip_motion=True, plan=None, plan_track='video')
    base.update(extra)
    return argparse.Namespace(**base)


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    song = root / 'song_120bpm.wav'
    music_structure_self_check._make_musical_fixture(song)
    on = _cut_video(song, root / 'on_beat.mp4', 1.0, 1.0)
    off = _cut_video(song, root / 'off_beat.mp4', 1.25, 1.0)
    before = file_hash.sha256(on)
    checks = []

    rep_on = music_fit.run(_args(on, root / 'fit-on'))
    b = rep_on['beat_fit']
    assert rep_on['status'] == 'measured_for_review' and len(rep_on['cuts']) >= 5, rep_on['cuts']
    assert b['cuts_on_beat_ratio'] >= 0.8 and b['cuts_on_beat_ratio'] > 1.5 * b['random_cut_expected_ratio'], b
    assert '明显高于随机' in b['observation'], b
    assert (root / 'fit-on' / 'music-fit.json').is_file() and Path(rep_on['chart']).is_file()
    assert (root / 'fit-on' / 'music-fit.md').is_file() and file_hash.sha256(on) == before
    assert rep_on['ending_fit']['quarter_cuts_per_minute'] and rep_on['human_watching'] == 'not_performed'
    checks.append(f"on-beat cuts: ratio={b['cuts_on_beat_ratio']} chance={b['random_cut_expected_ratio']}")

    rep_off = music_fit.run(_args(off, root / 'fit-off'))
    bo = rep_off['beat_fit']
    assert bo['cuts_on_beat_ratio'] <= 0.2 and '看不出在卡拍' in bo['observation'], bo
    assert len(bo['off_beat_cuts']) >= 5, bo
    checks.append(f"off-beat cuts flagged: ratio={bo['cuts_on_beat_ratio']} off={len(bo['off_beat_cuts'])}")

    rep_struct = music_fit.run(_args(on, root / 'fit-struct', structure=str(root / 'fit-on' / 'music')))
    assert rep_struct['music']['source']['kind'] == 'existing_structure'
    assert rep_struct['beat_fit']['cuts_on_beat_ratio'] == b['cuts_on_beat_ratio'], rep_struct['beat_fit']
    checks.append('existing --structure reused with identical beat fit')

    # near-identical greys: the scene detector cannot see these on-beat cuts, the plan can
    greys = ('0x404040', '0x414141', '0x424242', '0x434343')
    hidden = _cut_video(song, root / 'hidden_cuts.mp4', 1.0, 1.0, colors=greys)
    plan = {'schema_version': 2, 'name': 'hidden-cuts', 'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': 'x.mp4', 'track': 'video', 'start': float(i), 'duration': 1.0, 'source_in': 0.0,
                       **({'transition': {'name': 'Cross Dissolve', 'duration': 0.2}} if i == 3 else {})} for i in range(8)]}
    plan_path = root / 'hidden-plan.json'
    core.write_json(plan_path, plan)
    rep_det = music_fit.run(_args(hidden, root / 'fit-hidden-detector'))
    assert rep_det['cut_source']['kind'] == 'detector_only' and len(rep_det['cuts']) <= 1, rep_det['cuts']
    rep_plan = music_fit.run(_args(hidden, root / 'fit-hidden-plan', plan=str(plan_path)))
    cs, bp = rep_plan['cut_source'], rep_plan['beat_fit']
    assert cs['kind'] == 'plan_plus_detector' and cs['plan_boundaries'] == 7 and cs['plan_boundaries_with_transition'] == 1, cs
    assert cs['plan_boundaries_seen_by_detector'] <= 1 and len(rep_plan['cuts']) == 7, (cs, rep_plan['cuts'])
    assert bp['cuts_on_beat_ratio'] >= 0.8, bp
    assert '工程主轨' in (root / 'fit-hidden-plan' / 'music-fit.md').read_text(encoding='utf-8')
    checks.append(f"plan cuts recovered: detector={len(rep_det['cuts'])} plan={len(rep_plan['cuts'])} on_beat={bp['cuts_on_beat_ratio']}")

    short = dict(plan, clips=plan['clips'][:4])
    core.write_json(root / 'wrong-plan.json', short)
    try:
        music_fit.run(_args(hidden, root / 'fit-wrong-plan', plan=str(root / 'wrong-plan.json')))
        raise AssertionError('Mismatched plan was not rejected')
    except ValueError as exc:
        assert 'is this the plan' in str(exc), exc
    checks.append('plan that does not match the cut length refused')

    try:
        music_fit.run(_args(on, root / 'fit-on'))
        raise AssertionError('Existing output was not rejected')
    except ValueError as exc:
        assert 'already exists' in str(exc)
    checks.append('existing output refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks), 'human_watching': 'not_performed'}
    core.write_json(root / 'music-fit-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
