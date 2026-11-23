"""Regression check for depth_map (Depth Anything V2 ONNX, Video Depth Anything CUDA, DoF & 3D parallax)."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import cv2
import numpy as np

import depth_map
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = depth_map.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_2_5d_clip(mp4_path, frames=12, fps=24, w=256, h=144):
    raw_dir = mp4_path.parent / '_depth_src'
    raw_dir.mkdir(exist_ok=True)
    for i in range(frames):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        # Far sky gradient + middle hills + close bright foreground figure
        img[:70, :] = (160, 120, 80)
        img[70:, :] = (50, 75, 45)
        cx = 96 + i * 4
        cv2.rectangle(img, (cx, 32), (cx + 42, 130), (220, 230, 245), -1)
        cv2.circle(img, (cx + 21, 24), 14, (240, 210, 190), -1)
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
    clip = _make_2_5d_clip(root / 'scene_25d.mp4')

    # 1. Deterministic gradient fallback + DoF + ProRes 4444 parallax layers
    g_out = root / 'grad'
    cg, rg = _run_main([
        '--input', clip, '--out', g_out, '--backend', 'gradient',
        '--dof-focus', '0.75', '--dof-blur', '8', '--parallax-layers',
    ])
    assert cg == 0 and rg['status'] == 'passed' and rg['strict_decode'] == 'passed', rg
    assert Path(rg['depth_media']).is_file() and Path(rg['dof_preview']).is_file()
    fg_info = core.probe(rg['parallax']['foreground'])
    assert fg_info['pix_fmt'] == 'yuva444p12le', fg_info
    spec = core.read_json(rg['parallax']['overlay_spec'])
    assert len(spec['layers']) == 3 and spec['layers'][0]['type'] == 'camera', spec
    checks.append('gradient fallback + DoF + ProRes 4444 parallax (yuva444p12le) + 3D overlay spec')

    # 2. Live Depth Anything V2 Small ONNX when available
    if runtime_paths.discover_model('depth') is not None:
        d2_out = root / 'da-v2'
        cd2, rd2 = _run_main([
            '--input', clip, '--out', d2_out, '--backend', 'depth-anything-v2', '--input-size', '280', '--gpu',
        ])
        assert cd2 == 0 and rd2['status'] == 'passed' and rd2['strict_decode'] == 'passed', rd2
        assert rd2['backend'] == 'depth_anything_v2_onnx', rd2
        checks.append(f'Depth Anything V2 ONNX ({rd2["provider"]}): mean_diff={rd2["temporal_mean_frame_diff"]}')
    else:
        checks.append('Depth Anything V2 ONNX skipped: weights not installed')

    # 3. Live Video Depth Anything Small temporal CUDA when available
    vda_ok = (
        runtime_paths.discover_torch_worker_python() is not None
        and runtime_paths.discover_model('depth_video') is not None
        and runtime_paths.discover_vendor_dir('Video-Depth-Anything') is not None
    )
    if vda_ok:
        vda_out = root / 'vda'
        cvda, rvda = _run_main([
            '--input', clip, '--out', vda_out, '--backend', 'video-depth-anything', '--input-size', '280', '--gpu',
        ])
        assert cvda == 0 and rvda['status'] == 'passed' and rvda['strict_decode'] == 'passed', rvda
        assert rvda['backend'] == 'video_depth_anything_vits', rvda
        checks.append(f'Video Depth Anything ({rvda["provider"]}): frames={rvda["input"]["frames"]}, mean_diff={rvda["temporal_mean_frame_diff"]}')
    else:
        checks.append('Video Depth Anything skipped: worker/weights/vendor not installed')

    # 4. Refusal on existing out
    cb, rb = _run_main(['--input', clip, '--out', g_out])
    assert cb == 1 and rb['status'] == 'failed'
    checks.append('existing out refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'depth-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
