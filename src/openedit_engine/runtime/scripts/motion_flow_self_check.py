"""OpenCV Farneback optical-flow analyzer & procedural 3D .cube LUT self-check."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import cv2
import numpy as np

import motion_flow
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = motion_flow.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_pan_right_clip(path, *, w=256, h=144, fps=30, frames=24):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    assert vw.isOpened(), path
    rng = np.random.default_rng(7)
    # Textured background + bright square moving strictly right (+x).
    bg = rng.integers(35, 90, size=(h, w, 3), dtype=np.uint8)
    bg = cv2.GaussianBlur(bg, (9, 9), 2.0)
    for i in range(frames):
        fr = np.roll(bg, shift=i * 3, axis=1).copy()
        x0 = 40 + i * 5
        cv2.rectangle(fr, (x0, 44), (x0 + 44, 100), (220, 235, 255), -1)
        vw.write(fr)
    vw.release()


def _make_zoom_in_clip(path, *, w=256, h=144, fps=30, frames=24):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    assert vw.isOpened(), path
    rng = np.random.default_rng(11)
    canvas = rng.integers(25, 195, size=(h * 2, w * 2, 3), dtype=np.uint8)
    canvas = cv2.GaussianBlur(canvas, (7, 7), 1.5)
    cv2.circle(canvas, (w, h), 60, (240, 210, 120), 6)
    cv2.rectangle(canvas, (w - 110, h - 70), (w + 110, h + 70), (140, 220, 250), 4)
    for i in range(frames):
        scale = 1.0 + 0.03 * i
        cw, ch = w * 2 / scale, h * 2 / scale
        x0, y0 = w - cw / 2.0, h - ch / 2.0
        crop = canvas[max(0, int(y0)):min(h * 2, int(y0 + ch)), max(0, int(x0)):min(w * 2, int(x0 + cw))]
        vw.write(cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR))
    vw.release()


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    pan_clip = root / 'pan_right.mp4'
    zoom_clip = root / 'zoom_in.mp4'
    _make_pan_right_clip(pan_clip)
    _make_zoom_in_clip(zoom_clip)

    out_pan = root / 'pan_out'
    code_p, rep_p = _run_main([
        '--input', pan_clip, '--out', out_pan,
        '--backend', 'farneback',
        '--sample-fps', 30, '--window', 0.25,
        '--make-lut', 'steel_violet', '--lut-size', 33,
    ])
    assert code_p == 0 and rep_p['status'] == 'passed' and rep_p['flow_backend'] == 'opencv_farneback', rep_p
    pan_info = rep_p['clips'][0]
    assert pan_info['overall']['vx'] > 1.0, pan_info
    assert pan_info['overall']['direction'] == 'right', pan_info
    assert pan_info['entry_vector']['direction'] == 'right' and pan_info['exit_vector']['direction'] == 'right'
    assert rep_p['lut'] is not None and rep_p['lut']['entries'] == 33 ** 3

    lut_lines = Path(rep_p['lut']['path']).read_text(encoding='utf-8').splitlines()
    assert lut_lines[1] == 'LUT_3D_SIZE 33'
    data_lines = [ln for ln in lut_lines if ln and not ln.startswith(('TITLE', 'LUT_3D_SIZE', 'DOMAIN_'))]
    assert len(data_lines) == 33 ** 3
    checks.append(f'farneback pan_right vx={pan_info["overall"]["vx"]} dir=right + valid 33^3 steel_violet.cube')

    out_zoom = root / 'zoom_out'
    code_z, rep_z = _run_main([
        '--input', zoom_clip, '--out', out_zoom, '--backend', 'farneback', '--sample-fps', 30, '--window', 0.25,
    ])
    assert code_z == 0 and rep_z['status'] == 'passed', rep_z
    zoom_info = rep_z['clips'][0]
    assert zoom_info['overall']['zoom_divergence'] > 0.3, zoom_info
    assert zoom_info['overall']['zoom'] == 'push_in', zoom_info
    checks.append(f'farneback zoom_in divergence={zoom_info["overall"]["zoom_divergence"]} zoom=push_in')

    if runtime_paths.discover_model('flow') is not None:
        out_nf = root / 'neuflow_out'
        c_nf, r_nf = _run_main([
            '--input', pan_clip, '--out', out_nf, '--backend', 'neuflow', '--gpu', '--sample-fps', 30, '--window', 0.25,
        ])
        assert c_nf == 0 and r_nf['status'] == 'passed' and r_nf['flow_backend'] == 'neuflow_v2_onnx', r_nf
        nf_info = r_nf['clips'][0]
        assert nf_info['overall']['vx'] > 1.0 and nf_info['overall']['direction'] == 'right', nf_info
        checks.append(f'NeuFlow v2 ONNX ({r_nf["flow_provider"]}) pan_right vx={nf_info["overall"]["vx"]} dir=right')
    else:
        checks.append('NeuFlow v2 ONNX skipped: weights not installed')

    # Standalone OCIO 2.6 AgX and ACES 2.0 3D .cube LUT verification
    n_lut = 33
    for preset in ('agx_punchy', 'aces_cinema'):
        out_lut = root / f'lut_{preset}'
        cl, rl = _run_main(['--out', out_lut, '--make-lut', preset, '--lut-size', str(n_lut)])
        assert cl == 0 and rl['status'] == 'passed' and rl['mode'] == 'lut_only', rl
        assert rl['lut']['engine'] == 'opencolorio_2.6' and rl['lut']['entries'] == n_lut ** 3, rl
        lines = [
            ln for ln in Path(rl['lut']['path']).read_text(encoding='utf-8').splitlines()
            if ln and not ln.startswith(('TITLE', 'LUT_3D_SIZE', 'DOMAIN_'))
        ]
        assert len(lines) == n_lut ** 3
        table = np.array([[float(v) for v in ln.split()] for ln in lines]).reshape(n_lut, n_lut, n_lut, 3)
        gray = [float(np.mean(table[i, i, i])) for i in range(n_lut)]
        assert gray[0] <= 0.05 and gray[-1] >= 0.92, (preset, gray[0], gray[-1])
        assert all(b >= a - 1e-4 for a, b in zip(gray, gray[1:])), (preset, 'non-monotonic gray ramp')
        red_corner = table[0, 0, -1]  # Adobe order (B, G, R): R=1, G=0, B=0
        assert red_corner[0] > 0.7 and red_corner[1] > 0.05, (preset, red_corner)
        checks.append(f'OCIO 2.6 {preset}: 33^3 monotonic gray={gray[0]:.3f}->{gray[-1]:.3f}, highlight_red={np.round(red_corner, 3).tolist()}')

    code_b, rep_b = _run_main(['--input', root / 'missing.mp4', '--out', root / 'bad_out'])
    assert code_b == 1 and rep_b['status'] == 'failed' and not (root / 'bad_out').exists()
    checks.append('refused missing video without creating output dir')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'motion-flow-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
