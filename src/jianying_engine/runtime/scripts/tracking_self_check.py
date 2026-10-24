"""Synthetic CSRT regression: ground-truth motion, PTS, disappearance and refusal boundaries."""
import argparse
import contextlib
from fractions import Fraction
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

import av
import numpy as np

import editor
import tracking


def make_video(path, pts=None, disappear=False):
    times = pts if pts is not None else [i * 100 for i in range(20)]
    random = np.random.default_rng(20261018)
    texture = random.integers(40, 255, (28, 28, 3), dtype=np.uint8)
    ground = []
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('libx264', rate=10)
        stream.width, stream.height = 160, 120
        stream.pix_fmt = 'yuv420p'
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        stream.sample_aspect_ratio = Fraction(1, 1)
        for index, stamp in enumerate(times):
            image = np.full((120, 160, 3), 15, dtype=np.uint8)
            x, y = 24 + index * 2, 28 + index
            if not disappear or index < 8:
                image[y:y+28, x:x+28] = texture
            ground.append((x, y, 28, 28))
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            frame.pts, frame.time_base = stamp, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return times, ground


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    source = root / 'motion.mp4'
    times, ground = make_video(source)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    def options(name, **extra):
        values = dict(input=str(source), out=str(root / name), bbox=[24, 28, 28, 28], start=0, duration=2)
        values.update(extra)
        return SimpleNamespace(**values)
    report = tracking.execute(options('tracked'))
    assert report['status'] == 'tracked' and len(report['samples']) == len(times)
    assert report['coverage_start'] == 0 and report['coverage_end'] == 2 and report['lost_at'] is None
    errors, ious = [], []
    for sample, expected, time in zip(report['samples'], ground, times):
        assert abs(sample['time'] - time / 1000) < 1e-9
        x, y, w, h = sample['bbox']; gx, gy, gw, gh = expected
        errors.append(((x + w / 2 - gx - gw / 2)**2 + (y + h / 2 - gy - gh / 2)**2)**0.5)
        intersection = max(0, min(x+w, gx+gw)-max(x, gx))*max(0, min(y+h, gy+gh)-max(y, gy))
        ious.append(intersection / (w*h + gw*gh - intersection))
    assert max(errors) < 8 and float(np.mean(ious)) > 0.6, (errors, ious)
    assert report['source']['sha256'] == original
    assert 1 <= len(list((root / 'tracked').glob('*.png'))) <= 3
    checks.append('real CSRT follows synthetic textured motion; center error and IoU measured against ground truth')
    variable = root / 'vfr.mp4'
    vfr_times = [0, 100, 250, 350, 550, 650, 800, 900, 1100, 1200]
    make_video(variable, vfr_times)
    vfr = tracking.execute(options('vfr', input=str(variable), duration=1.2))
    assert [s['time'] for s in vfr['samples']] == [x / 1000 for x in vfr_times[:-1]]
    assert vfr['coverage_end'] == 1.2
    checks.append('VFR samples use real decoded PTS, including a real following timestamp for end coverage')
    shifted = root / 'shifted-pts.mp4'
    make_video(shifted, [1000 + x for x in times])
    normalized = tracking.execute(options('normalized-pts', input=str(shifted)))
    assert [s['time'] for s in normalized['samples']] == [x / 1000 for x in times]
    assert normalized['coverage_start'] == 0 and normalized['coverage_end'] == 2
    checks.append('nonzero first PTS is normalized to media zero without FPS assumptions')
    offset = tracking.execute(options('offset', start=0.15, duration=0.5, bbox=list(ground[2])))
    assert offset['coverage_start'] == 0.2 and offset['coverage_end'] == 0.65
    checks.append('non-frame-aligned request exposes actual start and bounded end coverage')
    decimal = tracking.execute(options('decimal-boundary', start=0.1, duration=0.2, bbox=list(ground[1])))
    assert [s['time'] for s in decimal['samples']] == [0.1, 0.2]
    assert decimal['coverage_start'] == 0.1 and decimal['coverage_end'] == 0.3
    assert editor.us(decimal['coverage_end']) > editor.us(decimal['samples'][-1]['time'])
    checks.append('decimal request boundary uses integer microseconds and excludes the frame at its exact end')
    vanished = root / 'vanish.mp4'; make_video(vanished, disappear=True)
    lost = tracking.execute(options('lost', input=str(vanished)))
    assert lost['status'] == 'lost' and lost['lost_at'] is not None and len(lost['samples']) < len(times)
    assert lost['coverage_end'] <= lost['lost_at']
    checks.append('real disappearance returns lost with partial samples and bounded coverage')
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = tracking.main(['--input', str(vanished), '--bbox', '24', '28', '28', '28',
                              '--duration', '2', '--out', str(root / 'lost-cli')])
    assert code == 2 and editor.json.loads(output.getvalue())['status'] == 'lost'
    checks.append('lost CLI writes its partial report and exits nonzero')

    def reject(label, request, expected):
        try:
            tracking.execute(request)
        except (ValueError, OSError) as exc:
            assert expected.lower() in str(exc).lower(), (label, str(exc))
        else:
            raise AssertionError(label + ' accepted')
        checks.append(label)
    reject('missing input refused', options('missing', input=str(root / 'absent.mp4')), 'not a file')
    reject('existing output refused', options('tracked'), 'already exists')
    reject('empty ROI refused', options('empty', bbox=[1, 1, 0, 2]), 'ROI')
    reject('out-of-frame ROI refused', options('outside', bbox=[150, 1, 28, 28]), 'ROI')
    reject('nonfinite start refused', options('nan', start=float('nan')), 'finite')
    reject('negative duration refused', options('negative', duration=-1), 'supported range')
    reject('range beyond video refused', options('too-long', duration=2.1), 'beyond')
    with patch.object(tracking, 'MAX_FRAMES', 2):
        reject('selected frame cap enforced', options('capped'), 'limited')
    anamorphic = root / 'anamorphic.mp4'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-i', source, '-vf', 'setsar=2', '-c:v', 'libx264', anamorphic])
    reject('non-square pixels refused', options('anamorphic-out', input=str(anamorphic)), 'square pixels')
    rotated = root / 'rotated.mp4'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-display_rotation', '90', '-i', source, '-c', 'copy', rotated])
    reject('rotation metadata refused', options('rotated-out', input=str(rotated)), 'Rotation')
    for name, stamps in [('missing PTS', [None]), ('duplicate PTS', [0, 0]), ('decreasing PTS', [2, 1])]:
        frames = [SimpleNamespace(pts=p, time_base=Fraction(1, 10), width=10, height=10,
                                  rotation=0, duration=1) for p in stamps]
        stream = SimpleNamespace(sample_aspect_ratio=Fraction(1, 1), metadata={})
        container = SimpleNamespace(decode=lambda unused: iter(frames))
        try:
            list(tracking.frame_intervals(container, stream))
        except ValueError:
            checks.append(name + ' refused without synthetic timestamps')
        else:
            raise AssertionError(name + ' accepted')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    result = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'max_center_error_pixels': max(errors), 'mean_center_error_pixels': float(np.mean(errors)),
              'mean_iou': float(np.mean(ious)), 'lost_at': lost['lost_at'], 'source_hash_unchanged': True,
              'model_downloads': 0, 'ui_operations': 0,
              'boundary': 'Synthetic target only; does not prove arbitrary target identity or privacy protection'}
    editor.write_json(root / 'tracking-self-check.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args()
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    if args.out:
        editor.emit(check(Path(args.out).resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-tracking-check-') as temporary:
            editor.emit(check(Path(temporary) / 'check'))
