"""Regression check for interpolate (RIFE ncnn Vulkan GPU slow-motion/deband + minterpolate fallback)."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import cv2
import numpy as np

import interpolate
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = interpolate.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_moving_bar_video(mp4_path, frames=12, fps=24, w=256, h=144):
    raw_dir = mp4_path.parent / '_src_frames'
    raw_dir.mkdir(exist_ok=True)
    for i in range(frames):
        img = np.full((h, w, 3), 24, dtype=np.uint8)
        x0 = 20 + i * 14
        img[36:108, x0:x0 + 28] = (230, 180, 90)
        cv2.imwrite(str(raw_dir / f'{i:04d}.png'), img)
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', str(fps), '-i', str(raw_dir / '%04d.png'),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(mp4_path),
    ], timeout=60)
    return mp4_path


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    clip = _make_moving_bar_video(root / 'bar_24fps.mp4')

    # 1. FFmpeg minterpolate fallback
    fb_out = root / 'minterp'
    c_fb, r_fb = _run_main([
        '--input', clip, '--out', fb_out, '--backend', 'minterpolate', '--multiplier', '2', '--deband',
    ])
    assert c_fb == 0 and r_fb['status'] == 'passed' and r_fb['strict_decode'] == 'passed', r_fb
    assert r_fb['output_media']['output_frames'] >= 20, r_fb
    checks.append(
        f'minterpolate + deband: {r_fb["input"]["source_frames"]} -> '
        f'{r_fb["output_media"]["output_frames"]} frames @ {r_fb["output_media"]["output_fps"]}fps'
    )

    # 2. Live RIFE Vulkan GPU (2x + 0.5x slow-mo + deband) when binary present
    if runtime_paths.discover_rife_cli() is not None:
        rf_out = root / 'rife-gpu'
        c_rf, r_rf = _run_main([
            '--input', clip, '--out', rf_out, '--backend', 'rife',
            '--multiplier', '2', '--slow-factor', '0.5', '--deband', '--gpu-id', '0',
        ])
        assert c_rf == 0 and r_rf['status'] == 'passed' and r_rf['strict_decode'] == 'passed', r_rf
        assert r_rf['output_media']['output_frames'] == 48, r_rf
        assert abs(r_rf['output_media']['output_fps'] - 48.0) <= 0.2, r_rf
        checks.append(
            f'RIFE Vulkan ({r_rf["model"]}): 12 frames -> '
            f'{r_rf["output_media"]["output_frames"]} frames @ {r_rf["output_media"]["output_fps"]}fps (0.5x slow-mo)'
        )
    else:
        checks.append('RIFE Vulkan skipped: executable not installed')

    # 3. Refusals
    c_bad, r_bad = _run_main(['--input', clip, '--out', fb_out])
    assert c_bad == 1 and r_bad['status'] == 'failed'
    c_noop, r_noop = _run_main(['--input', clip, '--out', root / 'noop', '--multiplier', '1', '--slow-factor', '1'])
    assert c_noop == 1 and r_noop['status'] == 'failed'
    checks.append('existing out and no-op args refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'interpolate-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
