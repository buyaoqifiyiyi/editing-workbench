"""Offline AI matting regression: RMBG/BiRefNet/RVM graph inspection, real inference -> ProRes 4444 RGBA, and refusals."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

import matting
import runtime_core as core


class _FakeTensorInfo:
    def __init__(self, name, shape, typ='tensor(float)'):
        self.name = name
        self.shape = shape
        self.type = typ


class _FakeRvmSession:
    def __init__(self):
        self.seen_states = []

    def get_inputs(self):
        return [
            _FakeTensorInfo('src', [1, 3, None, None]),
            _FakeTensorInfo('r1i', [1, None, None, None]),
            _FakeTensorInfo('r2i', [1, None, None, None]),
            _FakeTensorInfo('r3i', [1, None, None, None]),
            _FakeTensorInfo('r4i', [1, None, None, None]),
            _FakeTensorInfo('downsample_ratio', [1]),
        ]

    def get_outputs(self):
        return [
            _FakeTensorInfo('fgr', [1, 3, None, None]),
            _FakeTensorInfo('pha', [1, 1, None, None]),
            _FakeTensorInfo('r1o', [1, 16, None, None]),
            _FakeTensorInfo('r2o', [1, 20, None, None]),
            _FakeTensorInfo('r3o', [1, 40, None, None]),
            _FakeTensorInfo('r4o', [1, 64, None, None]),
        ]

    def run(self, _, feed):
        step = len(self.seen_states) + 1
        self.seen_states.append(float(np.asarray(feed['r1i']).mean()))
        _, _, h, w = feed['src'].shape
        fgr = np.full((1, 3, h, w), 0.75, dtype=np.float32)
        pha = np.full((1, 1, h, w), min(1.0, 0.25 * step), dtype=np.float32)
        return [
            fgr,
            pha,
            np.full((1, 4, 4, 4), float(step), dtype=np.float32),
            np.full((1, 4, 2, 2), float(step), dtype=np.float32),
            np.full((1, 4, 2, 2), float(step), dtype=np.float32),
            np.full((1, 4, 1, 1), float(step), dtype=np.float32),
        ]


class _FakeBiRefNetSession:
    def __init__(self):
        self.last_tensor = None

    def get_inputs(self):
        return [_FakeTensorInfo('input_image', [1, 3, 64, 64])]

    def get_outputs(self):
        return [_FakeTensorInfo('output_image', [1, 1, 64, 64])]

    def run(self, _, feed):
        self.last_tensor = feed['input_image']
        logits = np.full((1, 1, 64, 64), -8.0, dtype=np.float32)
        logits[:, :, 16:48, 16:48] = 8.0
        return [logits]


def _make_fixtures(root):
    # High-contrast centered white/cyan square object on dark slate studio backdrop.
    still_path = root / 'subject.png'
    im = Image.new('RGB', (320, 180), (14, 18, 28))
    draw = ImageDraw.Draw(im)
    draw.rectangle([90, 25, 230, 160], fill=(245, 240, 230))
    im.save(still_path)

    # 2-frame (0.2s @ 10fps) moving high-contrast square video for fast real CPU video matting regression.
    frames_dir = root / 'frames'
    frames_dir.mkdir()
    for idx in range(2):
        fim = Image.new('RGB', (320, 180), (14, 18, 28))
        fdraw = ImageDraw.Draw(fim)
        dx = idx * 14
        fdraw.rectangle([90 + dx, 25, 230 + dx, 160], fill=(245, 240, 230))
        fim.save(frames_dir / f'f_{idx:03d}.png')
    vid_path = root / 'clip.mp4'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', '10', '-i', str(frames_dir / 'f_%03d.png'),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(vid_path),
    ])

    audio_path = root / 'sound.wav'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
        '-c:a', 'pcm_s16le', str(audio_path),
    ])
    return still_path, vid_path, audio_path


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = matting.main([str(x) for x in arguments])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def check(root, model):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    # 1. RVM 6-input recurrent state & BiRefNet ImageNet normalization + logit sigmoid contracts.
    rvm_sess = _FakeRvmSession()
    rvm_meta = matting.inspect_session(rvm_sess, 'auto', Path('rvm_mobilenetv3_fp32.onnx'))
    assert rvm_meta['backend'] == 'rvm', rvm_meta
    rvm_runner = matting.MattingRunner(rvm_sess, rvm_meta, downsample_ratio=0.5, use_model_fgr=True)
    dummy = np.full((32, 48, 3), 20, dtype=np.uint8)
    for step in range(3):
        fg, alpha = rvm_runner.predict(dummy)
        assert fg.shape == (32, 48, 3) and abs(float(fg.mean()) - 191.0) <= 1.0
        assert alpha.shape == (32, 48) and abs(float(alpha.mean()) - 0.25 * (step + 1)) < 1e-4
    assert rvm_sess.seen_states == [0.0, 1.0, 2.0], rvm_sess.seen_states
    checks.append('rvm contract: auto-detected 6-input graph, carried recurrent r1..r4 states across 3 frames')

    biref_sess = _FakeBiRefNetSession()
    biref_meta = matting.inspect_session(biref_sess, 'auto', Path('birefnet_general.onnx'))
    assert biref_meta['backend'] == 'birefnet', biref_meta
    biref_runner = matting.MattingRunner(biref_sess, biref_meta)
    _, biref_alpha = biref_runner.predict(np.zeros((64, 64, 3), dtype=np.uint8))
    # Zero RGB normalized by -mean/std -> R channel ~ -0.485 / 0.229 = -2.1179
    assert abs(float(biref_sess.last_tensor[0, 0, 0, 0]) + (0.485 / 0.229)) < 1e-3
    assert float(biref_alpha[32, 32]) > 0.99 and float(biref_alpha[2, 2]) < 0.01
    checks.append('birefnet contract: ImageNet normalization and logit->sigmoid matte verified')

    # 1b. Decontamination & dropout telemetry contracts (pure NumPy/OpenCV, zero network/GPU needed)
    decon_rgb = np.zeros((48, 48, 3), dtype=np.uint8)
    decon_rgb[12:36, 12:36] = (240, 230, 220)
    decon_alpha = np.zeros((48, 48), dtype=np.float32)
    decon_alpha[12:36, 12:36] = 1.0
    decon_alpha[11:37, 11:37] = np.where(decon_alpha[11:37, 11:37] > 0.5, 1.0, 0.4)
    cleaned = matting.decontaminate_foreground(decon_rgb, decon_alpha)
    assert int(cleaned[11, 24, 0]) > 120 and int(cleaned[24, 24, 0]) == 240
    drop_info = matting._dropout_metrics([0.45, 0.44, 0.43], [0.5, 0.0, 0.5])
    assert drop_info['dropout_warning'] is True and drop_info['top_band_min_fg_ratio'] == 0.0
    hole_rgb = np.full((32, 32, 3), (255, 220, 195), dtype=np.uint8)
    hole_rgb[8:16, 8:16] = (90, 175, 250)
    hole_alpha = np.ones((32, 32), dtype=np.float32)
    punched = matting.clean_color_holes(hole_rgb, hole_alpha, matting.parse_color_hole_spec('blue_sky'))
    assert float(punched[12, 12]) < 0.05 and float(punched[2, 2]) > 0.99
    feathered = matting.apply_bottom_feather(np.ones((40, 20), dtype=np.float32), 0.2)
    assert float(feathered[-1, 10]) == 0.0 and float(feathered[0, 10]) == 1.0
    checks.append('decontaminate, color-hole cleanup, bottom-feather & dropout contracts verified')

    still_path, vid_path, audio_path = _make_fixtures(root)

    # 2. Real ONNX inference on still PNG -> matte_rgba.png + matte_alpha.png
    img_out = root / 'still-out'
    code, img_report = _run_main(['--input', still_path, '--out', img_out, '--model', model])
    assert code == 0 and img_report['status'] == 'passed' and img_report['kind'] == 'image', img_report
    with Image.open(img_report['outputs']['matte_rgba']) as rgba_im:
        assert rgba_im.mode == 'RGBA' and rgba_im.size == (320, 180)
        assert rgba_im.getpixel((160, 90))[3] > 200
        assert rgba_im.getpixel((5, 5))[3] < 30
    assert len(img_report['model']['sha256']) == 64 and img_report['model']['license_note']
    checks.append(f'still matting ({img_report["backend"]}): centered foreground preserved, dark corners transparent')

    # 3. Real ONNX video matting -> ProRes 4444 RGBA matte.mov + strict decode verification
    vid_out = root / 'video-out'
    code, vid_report = _run_main([
        '--input', vid_path, '--out', vid_out, '--model', model,
        '--alpha-feather', '0.5', '--temporal-ema', '0.1',
    ])
    assert code == 0 and vid_report['status'] == 'passed' and vid_report['kind'] == 'video', vid_report
    assert vid_report['range']['frame_count'] == 2
    mv = vid_report['outputs']['matte_video']
    assert mv['codec'] == 'prores' and mv['pix_fmt'] == 'yuva444p12le' and mv['has_alpha'] is True
    assert mv['strict_decode'] == 'passed' and Path(mv['path']).is_file()
    assert Path(vid_report['outputs']['preview_rgba']).is_file()
    checks.append(
        f'video matting ({vid_report["backend"]}): 2 frames -> ProRes 4444 ({mv["pix_fmt"]}), '
        f'strict_decode=passed, MAD={vid_report["metrics"]["adjacent_alpha_mad"]}'
    )

    # 4. --no-video mode omits matte.mov cleanly while still writing previews
    novid_out = root / 'novid-out'
    code, novid_report = _run_main(['--input', vid_path, '--out', novid_out, '--model', model, '--no-video'])
    assert code == 0 and novid_report['outputs']['matte_video'] is None
    assert Path(novid_report['outputs']['preview_rgba']).is_file()
    assert not (novid_out / 'matte.mov').exists()
    checks.append('--no-video mode wrote RGBA preview without encoding matte.mov')

    # 5. Loud refusals leave no partial directories behind
    refusals = {
        'existing out': ['--input', still_path, '--out', img_out, '--model', model],
        'missing input': ['--input', root / 'nope.png', '--out', root / 'r1', '--model', model],
        'audio-only input': ['--input', audio_path, '--out', root / 'r2', '--model', model],
        'missing model with auto disabled': ['--input', still_path, '--out', root / 'r3'],
        'matanyone2 missing with auto disabled': ['--input', still_path, '--out', root / 'r3b', '--backend', 'matanyone2'],
        'max-frames exceeded': ['--input', vid_path, '--out', root / 'r4', '--model', model, '--max-frames', '1'],
        'threshold out of range': ['--input', still_path, '--out', root / 'r5', '--model', model, '--threshold', '0.99'],
    }
    for label, arguments in refusals.items():
        saved = {}
        if 'auto disabled' in label:
            for k in (
                'OPENEDIT_MATTING_MODEL', 'OPENEDIT_MATANYONE2_MODEL', 'OPENEDIT_MATANYONE2_PYTHON',
                'OPENEDIT_RVM_MODEL', 'OPENEDIT_MODELS_DIR', 'OPENEDIT_NO_AUTO_MODELS',
            ):
                saved[k] = os.environ.pop(k, None)
            os.environ['OPENEDIT_NO_AUTO_MODELS'] = '1'
        try:
            code, refused = _run_main(arguments)
        finally:
            if 'auto disabled' in label:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        assert code == 1 and refused['status'] == 'failed' and refused.get('error'), (label, refused)
        checks.append(f'refused: {label} -> {refused["error"][:80]}')
    for name in ('r1', 'r2', 'r3', 'r3b', 'r4', 'r5'):
        assert not (root / name).exists(), name

    result = {
        'status': 'passed',
        'checks': checks,
        'checks_count': len(checks),
        'measurements': {
            'backend': vid_report['backend'],
            'still_seconds': img_report['metrics']['elapsed_seconds'],
            'video_2frames_seconds': vid_report['metrics']['elapsed_seconds'],
            'prores_pix_fmt': mv['pix_fmt'],
            'adjacent_alpha_mad': vid_report['metrics']['adjacent_alpha_mad'],
        },
        'visual_audio_review': 'unverified',
    }
    core.write_json(root / 'matting-check.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True, help='ONNX matting weights')
    args = parser.parse_args()
    if hasattr(core.sys.stdout, 'reconfigure'):
        core.sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    model = args.model.resolve()
    if not model.is_file():
        core.emit({'status': 'failed', 'error': f'Matting model is not a file: {model}'})
        return 1
    try:
        import cv2  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError as exc:
        core.emit({'status': 'failed', 'error': f'Matting check requires cv2 and onnxruntime: {exc}'})
        return 1
    report = check(args.out.resolve(), model)
    core.emit(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
