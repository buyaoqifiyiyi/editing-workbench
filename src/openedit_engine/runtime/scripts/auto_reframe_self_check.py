"""Scene-aware Auto-Reframe regression check: subject pan math, zero-black-border clamp, YuNet/saliency & refusals."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw

import auto_reframe
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = auto_reframe.main([str(x) for x in arguments])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def _make_fixtures(root):
    # 1.6s 640x360 @ 10fps (16 frames):
    # First 0.8s (scene 1, blue studio): bright subject moves from LEFT (x=80) to RIGHT (x=540)
    # Last 0.8s (scene 2, warm orange studio, hard cut): bright target stays on LEFT side
    frames = root / 'frames'
    frames.mkdir()
    for idx in range(16):
        if idx < 8:
            im = Image.new('RGB', (640, 360), (15, 22, 35))
            draw = ImageDraw.Draw(im)
            cx = 90 + idx * 64
            draw.ellipse([cx - 45, 90, cx + 45, 270], fill=(250, 240, 120))
        else:
            im = Image.new('RGB', (640, 360), (220, 110, 40))
            draw = ImageDraw.Draw(im)
            draw.rectangle([70, 80, 190, 290], fill=(20, 25, 35))
        im.save(frames / f'f_{idx:03d}.png')

    vid = root / 'wide_16_9.mp4'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', '10', '-i', str(frames / 'f_%03d.png'),
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.6',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(vid),
    ])
    still = root / 'still.png'
    Image.new('RGB', (640, 360), (30, 30, 30)).save(still)
    return vid, still


def check(root, face_model=None):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    vid, still = _make_fixtures(root)

    # 1. Exact cover clamp math: 1920x1080 -> 1080x1920 yields max_nx = (3413.33 - 1080) / 1080 ~= 2.1605, max_ny = 0.
    nx_l, ny_l, mx, my = auto_reframe.subject_to_filmcraft_norm(0.0, 0.5, 1920, 1080, 1080, 1920)
    nx_r, _, _, _ = auto_reframe.subject_to_filmcraft_norm(1.0, 0.5, 1920, 1080, 1080, 1920)
    assert abs(mx - 2.1605) < 0.002 and my == 0.0
    assert abs(nx_l - mx) < 1e-4 and abs(nx_r + mx) < 1e-4
    # Reverse 1080x1920 -> 1920x1080 yields max_ny ~= 2.1605, top subject negative ny, bottom subject positive ny
    _, ny_top, mx_v, my_v = auto_reframe.subject_to_filmcraft_norm(0.5, 0.0, 1080, 1920, 1920, 1080)
    _, ny_bot, _, _ = auto_reframe.subject_to_filmcraft_norm(0.5, 1.0, 1080, 1920, 1920, 1080)
    assert mx_v == 0.0 and abs(ny_top + my_v) < 1e-4 and abs(ny_bot - my_v) < 1e-4
    checks.append(f'clamp math: 16:9->9:16 max_nx={mx}, 9:16->16:9 max_ny={my_v}')

    # 2. Saliency mode end-to-end with scene detection (follows left->right pan in scene 1)
    out_sal = root / 'reframe-sal'
    code, rep = _run_main([
        '--input', vid, '--out', out_sal, '--aspect', '9:16',
        '--detector', 'saliency', '--scene-detector', 'ffmpeg', '--sample-fps', '10', '--smoothing', '0.3',
    ])
    assert code == 0 and rep['status'] == 'passed', rep
    assert rep['canvas']['width'] == 1080 and rep['canvas']['height'] == 1920
    plan = core.read_json(rep['plan_path'])
    core.validate_plan(rep['plan_path'])
    assert all(c['fit'] == 'cover' for c in plan['clips'])
    kfs0 = [k for k in plan['clips'][0].get('keyframes', []) if k['property'] == 'position_x']
    assert len(kfs0) >= 2 and kfs0[0]['value'] > kfs0[-1]['value'], kfs0
    assert all(abs(k['value']) <= mx + 1e-3 for c in plan['clips'] for k in c.get('keyframes', []))
    checks.append(
        f'saliency reframe: {rep["stats"]["scene_count"]} scene(s), '
        f'pan position_x {kfs0[0]["value"]:+.3f} -> {kfs0[-1]["value"]:+.3f}'
    )

    # 3. YuNet face model initialization + fallback run if YuNet weights available
    resolved_face = face_model or runtime_paths.discover_model('face')
    if resolved_face is not None and resolved_face.is_file():
        out_yu = root / 'reframe-yunet'
        code_y, rep_y = _run_main([
            '--input', vid, '--out', out_yu, '--aspect', '4:5',
            '--detector', 'yunet', '--face-model', resolved_face, '--scene-detector', 'none',
        ])
        assert code_y == 0 and rep_y['status'] == 'passed' and rep_y['detector']['yunet_active'] is True, rep_y
        core.validate_plan(rep_y['plan_path'])
        checks.append(f'yunet active: loaded {resolved_face.name}, output 4:5 {rep_y["canvas"]["width"]}x{rep_y["canvas"]["height"]}')

    # 4. Refusals
    refusals = {
        'existing out': ['--input', vid, '--out', out_sal],
        'image rejected': ['--input', still, '--out', root / 'r1'],
        'bad aspect': ['--input', vid, '--out', root / 'r2', '--aspect', 'wide'],
        'missing yunet model': ['--input', vid, '--out', root / 'r3', '--detector', 'yunet'],
    }
    for label, arguments in refusals.items():
        saved = {}
        if label == 'missing yunet model':
            for k in ('OPENEDIT_FACE_MODEL', 'OPENEDIT_MODELS_DIR', 'OPENEDIT_NO_AUTO_MODELS'):
                saved[k] = os.environ.pop(k, None)
            os.environ['OPENEDIT_NO_AUTO_MODELS'] = '1'
        try:
            code_r, rep_r = _run_main(arguments)
        finally:
            if label == 'missing yunet model':
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        assert code_r == 1 and rep_r['status'] == 'failed' and rep_r.get('error'), (label, rep_r)
        checks.append(f'refused: {label}')
    for rdir in ('r1', 'r2', 'r3'):
        assert not (root / rdir).exists(), rdir

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks), 'yunet_tested': resolved_face is not None}
    core.write_json(root / 'auto-reframe-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--face-model', type=Path, default=None)
    args = parser.parse_args()
    core.emit(check(args.out.resolve(), face_model=args.face_model.resolve() if args.face_model else None))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
