"""Offline temporal 2.5D depth estimation, DoF bokeh & EffectCraft 3D parallax layer builder (Apache-2.0)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image

import file_hash
import runtime_core as core
import runtime_paths

_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_WORKER = Path(__file__).resolve().with_name('depth_worker.py')
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _read_media(path, *, is_video, max_frames, max_res=1080):
    if not is_video:
        with Image.open(path) as img:
            rgb = np.asarray(img.convert('RGB'), dtype=np.uint8)
        return rgb[None, ...], 30.0, rgb.shape[1], rgb.shape[0]
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f'Failed to open video: {path}')
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if fps <= 0:
            fps = 30.0
        frames = []
        while len(frames) < max_frames:
            ok, bgr = cap.read()
            if not ok:
                break
            h, w = bgr.shape[:2]
            if max(h, w) > max_res:
                s = max_res / float(max(h, w))
                nw = max(2, int(round(w * s)) // 2 * 2)
                nh = max(2, int(round(h * s)) // 2 * 2)
                bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
            elif w % 2 or h % 2:
                nw = max(2, w - (w % 2))
                nh = max(2, h - (h % 2))
                bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
            frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    if not frames:
        raise ValueError(f'Decoded 0 video frames from {path}')
    arr = np.stack(frames, axis=0)
    return arr, round(fps, 4), int(arr.shape[2]), int(arr.shape[1])


def _normalize_depths(depths_f):
    lo = float(np.percentile(depths_f, 1.0))
    hi = float(np.percentile(depths_f, 99.0))
    span = max(hi - lo, 1e-6)
    return np.clip((depths_f - lo) / span * 255.0, 0.0, 255.0).round().astype(np.uint8)


def _run_da_v2(frames_rgb, *, input_size=518, gpu=False):
    import onnxruntime as ort

    model_path = runtime_paths.discover_model('depth', prefer_fp16=bool(gpu)) or runtime_paths.discover_model('depth')
    if model_path is None or not model_path.is_file():
        raise ValueError('Depth Anything V2 ONNX weights not found (expect models/depth/depth-anything-v2-small)')
    if gpu:
        runtime_paths.prepare_cuda_dlls()
    providers = runtime_paths.resolve_ort_providers(
        ort.get_available_providers(), device='auto' if gpu else 'cpu', gpu=bool(gpu),
    )
    sess = ort.InferenceSession(str(model_path), providers=providers)
    active_provider = sess.get_providers()[0]

    _, h, w, _ = frames_rgb.shape
    scale = float(input_size) / float(max(h, w))
    tw = max(14, int(round(w * scale / 14.0)) * 14)
    th = max(14, int(round(h * scale / 14.0)) * 14)

    raw_depths = []
    prev_map = None
    for frame in frames_rgb:
        resized = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0
        normed = (resized - _IMAGENET_MEAN) / _IMAGENET_STD
        inp = np.transpose(normed, (2, 0, 1))[None, ...].astype(np.float32)
        pred = sess.run(None, {'pixel_values': inp})[0][0]
        full = cv2.resize(pred.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        if prev_map is not None:
            full = 0.70 * full + 0.30 * prev_map
        prev_map = full
        raw_depths.append(full)

    return _normalize_depths(np.stack(raw_depths, axis=0)), 'depth_anything_v2_onnx', active_provider, str(model_path)


def _run_vda(src_video, *, input_size=518, max_frames=300, gpu=False):
    py = runtime_paths.discover_torch_worker_python()
    ckpt = runtime_paths.discover_model('depth_video')
    vendor = runtime_paths.discover_vendor_dir('Video-Depth-Anything')
    if py is None or ckpt is None or vendor is None:
        raise ValueError('Video Depth Anything requires isolated PyTorch CUDA worker, video_depth_anything_vits.pth, and vendor repo')

    with tempfile.TemporaryDirectory(prefix='oe-vda-') as td:
        td_p = Path(td)
        out_npy = td_p / 'depths.npy'
        out_json = td_p / 'vda.json'
        cmd = [
            str(py), str(_WORKER),
            '--input', str(src_video),
            '--vendor', str(vendor),
            '--ckpt', str(ckpt),
            '--out-npy', str(out_npy),
            '--out-json', str(out_json),
            '--input-size', str(int(input_size)),
            '--max-frames', str(int(max_frames)),
            '--device', 'cuda' if gpu else 'cpu',
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=1200, creationflags=_NO_WIN,
        )
        if proc.returncode != 0 or not out_npy.is_file():
            raise ValueError(f'Video Depth Anything worker failed (rc={proc.returncode}): {proc.stderr[-1500:]}')
        depths_u8 = np.load(out_npy)
        meta = core.read_json(out_json)
    return depths_u8, 'video_depth_anything_vits', f'torch_{meta.get("device", "cpu")}', str(ckpt)


def _run_gradient(frames_rgb):
    t_len, h, w, _ = frames_rgb.shape
    ys = np.linspace(0.0, 255.0, h, dtype=np.float32)[:, None]
    xs = np.linspace(0.85, 1.0, w, dtype=np.float32)[None, :]
    base = ys * xs
    out = []
    for idx in range(t_len):
        luma = cv2.cvtColor(frames_rgb[idx], cv2.COLOR_RGB2GRAY).astype(np.float32) * 0.25
        out.append(np.clip(base * 0.75 + luma, 0.0, 255.0).round().astype(np.uint8))
    return np.stack(out, axis=0), 'gradient_fallback', 'CPU', None


def _encode_rgb_mp4(frames_rgb, fps, out_mp4):
    _, h, w, _ = frames_rgb.shape
    cmd = [
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', f'{fps:.6f}', '-i', '-',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '14', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', '-map_metadata', '-1', str(out_mp4),
    ]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(frames_rgb).tobytes(), capture_output=True, timeout=600, creationflags=_NO_WIN)
    if proc.returncode != 0:
        raise RuntimeError(f'FFmpeg MP4 encode failed: {proc.stderr.decode("utf-8", "replace")[-1500:]}')
    core.run([core.tool('ffmpeg'), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-i', str(out_mp4), '-f', 'null', '-'], timeout=300)


def _encode_rgba_prores(frames_rgba, fps, out_mov):
    _, h, w, _ = frames_rgba.shape
    cmd = [
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', f'{fps:.6f}', '-i', '-',
        '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p12le',
        '-map_metadata', '-1', str(out_mov),
    ]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(frames_rgba).tobytes(), capture_output=True, timeout=600, creationflags=_NO_WIN)
    if proc.returncode != 0:
        raise RuntimeError(f'FFmpeg ProRes 4444 encode failed: {proc.stderr.decode("utf-8", "replace")[-1500:]}')
    core.run([core.tool('ffmpeg'), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-i', str(out_mov), '-f', 'null', '-'], timeout=300)


def _render_dof_frames(frames_rgb, depths_u8, focus, blur_px):
    k = max(3, int(round(blur_px)) * 2 + 1)
    out = []
    for rgb, d_u8 in zip(frames_rgb, depths_u8):
        d = d_u8.astype(np.float32) / 255.0
        w_blur = np.clip(np.abs(d - focus) * 1.8, 0.0, 1.0)[..., None]
        blurred = cv2.GaussianBlur(rgb, (k, k), sigmaX=blur_px * 0.65).astype(np.float32)
        comp = rgb.astype(np.float32) * (1.0 - w_blur) + blurred * w_blur
        out.append(np.clip(comp, 0.0, 255.0).round().astype(np.uint8))
    return np.stack(out, axis=0)


def _build_parallax_layers(frames_rgb, depths_u8, out_dir, *, is_video, fps):
    d_norm = depths_u8.astype(np.float32) / 255.0
    split_t = float(np.clip(np.percentile(d_norm, 55.0), 0.35, 0.72))
    fg_alpha = np.clip(0.5 + (d_norm - split_t) / 0.12, 0.0, 1.0)
    fg_u8 = np.clip(fg_alpha * 255.0, 0.0, 255.0).round().astype(np.uint8)
    fg_rgba = np.concatenate([frames_rgb, fg_u8[..., None]], axis=-1)
    # Keep solid background matte so 3D camera parallax pans never reveal black gaps behind foreground edges.
    bg_u8 = np.full(fg_u8.shape, 255, dtype=np.uint8)
    bg_rgba = np.concatenate([frames_rgb, bg_u8[..., None]], axis=-1)

    t_len, h, w, _ = frames_rgb.shape
    dur = round(t_len / fps, 4) if is_video else 2.0

    if is_video:
        fg_path = out_dir / 'fg_parallax.mov'
        bg_path = out_dir / 'bg_parallax.mov'
        _encode_rgba_prores(fg_rgba, fps, fg_path)
        _encode_rgba_prores(bg_rgba, fps, bg_path)
    else:
        fg_path = out_dir / 'fg_parallax.png'
        bg_path = out_dir / 'bg_parallax.png'
        Image.fromarray(fg_rgba[0], mode='RGBA').save(fg_path)
        Image.fromarray(bg_rgba[0], mode='RGBA').save(bg_path)

    half_w, half_h = w * 0.5, h * 0.5
    spec = {
        'name': 'parallax_2_5d',
        'width': w,
        'height': h,
        'fps': fps if is_video else 30.0,
        'duration': dur,
        'motion_blur': True,
        'layers': [
            {
                'type': 'camera',
                'name': 'ParallaxCam',
                'start': 0.0,
                'duration': dur,
                'position': [half_w - w * 0.02, half_h, -1500.0],
                'poi': [half_w, half_h, 0.0],
                'zoom': 1500.0,
                'keyframes': [
                    {'property': 'transform/position', 'time': 0.0, 'value': [half_w - w * 0.02, half_h, -1500.0], 'easing': 'easeInOut'},
                    {'property': 'transform/position', 'time': dur, 'value': [half_w + w * 0.02, half_h, -1420.0], 'easing': 'easeInOut'},
                ],
            },
            {
                'type': 'footage',
                'name': 'BG_Plane',
                'path': str(bg_path),
                'start': 0.0,
                'duration': dur,
                'three_d': True,
                'motion_blur': True,
                'position': [half_w, half_h, 180.0],
                'scale': [114.0, 114.0],
            },
            {
                'type': 'footage',
                'name': 'FG_Plane',
                'path': str(fg_path),
                'start': 0.0,
                'duration': dur,
                'three_d': True,
                'motion_blur': True,
                'position': [half_w, half_h, -120.0],
                'scale': [102.0, 102.0],
            },
        ],
    }
    spec_path = out_dir / 'parallax-overlay-spec.json'
    core.write_json(spec_path, spec)
    return {
        'foreground': str(fg_path),
        'background': str(bg_path),
        'split_threshold': round(split_t, 4),
        'overlay_spec': str(spec_path),
    }


def run_depth_map(args):
    src = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not src.is_file():
        raise ValueError(f'Input image/video does not exist: {src}')
    info = core.probe(src)
    is_video = info.get('kind') == 'video'
    if not info.get('has_video'):
        raise ValueError(f'depth requires an image or video stream: {src}')

    input_size = int(args.input_size)
    if not 140 <= input_size <= 1036:
        raise ValueError(f'--input-size must be in [140, 1036], got {input_size}')
    max_frames = int(args.max_frames)
    if not 1 <= max_frames <= 1800:
        raise ValueError(f'--max-frames must be in [1, 1800], got {max_frames}')
    dof_focus = core.number(args.dof_focus, 'dof_focus', 0.0, 1.0)
    dof_blur = core.number(args.dof_blur, 'dof_blur', 0.0, 30.0)

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    req = args.backend
    if req == 'video-depth-anything' and not is_video:
        raise ValueError('--backend video-depth-anything requires a video file (use auto or depth-anything-v2 for still images)')

    frames_rgb, fps, width, height = _read_media(src, is_video=is_video, max_frames=max_frames)
    if req == 'auto':
        has_vda = bool(
            is_video
            and runtime_paths.discover_torch_worker_python() is not None
            and runtime_paths.discover_model('depth_video') is not None
            and runtime_paths.discover_vendor_dir('Video-Depth-Anything') is not None
        )
        if has_vda:
            backend = 'video-depth-anything'
        elif runtime_paths.discover_model('depth') is not None:
            backend = 'depth-anything-v2'
        else:
            backend = 'gradient'
    else:
        backend = req

    if backend == 'video-depth-anything':
        depths_u8, used_backend, provider, model_path = _run_vda(
            src, input_size=input_size, max_frames=max_frames, gpu=bool(args.gpu),
        )
        # Ensure frame alignment matches capped decoded RGB frames if worker resized >1080
        if depths_u8.shape[1:] != frames_rgb.shape[1:]:
            depths_u8 = np.stack([
                cv2.resize(d, (width, height), interpolation=cv2.INTER_LINEAR)
                for d in depths_u8
            ], axis=0)
    elif backend == 'depth-anything-v2':
        depths_u8, used_backend, provider, model_path = _run_da_v2(
            frames_rgb, input_size=input_size, gpu=bool(args.gpu),
        )
    else:
        depths_u8, used_backend, provider, model_path = _run_gradient(frames_rgb)

    mean_diff = (
        round(float(np.mean(np.abs(np.diff(depths_u8.astype(np.float32), axis=0))) / 255.0), 5)
        if len(depths_u8) > 1 else 0.0
    )

    out_dir = core.new_directory(out_cand)
    depth_rgb = np.repeat(depths_u8[..., None], 3, axis=-1)
    mid_idx = len(depths_u8) // 2
    preview_png = out_dir / 'preview_depth.png'
    Image.fromarray(depths_u8[mid_idx], mode='L').save(preview_png)

    if is_video:
        depth_out = out_dir / 'depth.mp4'
        _encode_rgb_mp4(depth_rgb, fps, depth_out)
        strict_decode = 'passed'
    else:
        depth_out = out_dir / 'depth.png'
        Image.fromarray(depths_u8[0], mode='L').save(depth_out)
        strict_decode = 'not_applicable_still'

    dof_out = None
    if dof_blur > 0.0:
        dof_frames = _render_dof_frames(frames_rgb, depths_u8, dof_focus, dof_blur)
        if is_video:
            dof_out = out_dir / 'dof_preview.mp4'
            _encode_rgb_mp4(dof_frames, fps, dof_out)
        else:
            dof_out = out_dir / 'dof_preview.png'
            Image.fromarray(dof_frames[0], mode='RGB').save(dof_out)

    parallax_info = None
    if args.parallax_layers:
        parallax_info = _build_parallax_layers(frames_rgb, depths_u8, out_dir, is_video=is_video, fps=fps)

    report = {
        'status': 'passed',
        'operation': 'depth',
        'backend': used_backend,
        'provider': provider,
        'model': model_path,
        'input': {
            'path': str(src),
            'sha256': file_hash.sha256(src),
            'kind': 'video' if is_video else 'image',
            'width': width,
            'height': height,
            'frames': int(len(depths_u8)),
            'fps': fps if is_video else None,
        },
        'depth_media': str(depth_out),
        'preview_png': str(preview_png),
        'dof_preview': str(dof_out) if dof_out else None,
        'parallax': parallax_info,
        'temporal_mean_frame_diff': mean_diff,
        'strict_decode': strict_decode,
    }
    core.write_json(out_dir / 'depth-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video or image file')
    parser.add_argument('--out', required=True, help='New output directory for depth map & parallax layers')
    parser.add_argument('--backend', choices=('auto', 'video-depth-anything', 'depth-anything-v2', 'gradient'), default='auto')
    parser.add_argument('--input-size', type=int, default=518, help='Model inference long edge in [140, 1036] (default 518)')
    parser.add_argument('--max-frames', type=int, default=300, help='Maximum video frames in [1, 1800] (default 300)')
    parser.add_argument('--dof-focus', type=float, default=0.75, help='DoF focal depth plane in [0, 1] (default 0.75 = foreground)')
    parser.add_argument('--dof-blur', type=float, default=0.0, help='Optional Gaussian bokeh radius in [0, 30] px (default 0 = off)')
    parser.add_argument('--parallax-layers', action='store_true', help='Export ProRes 4444 fg/bg split and EffectCraft 3D parallax spec')
    parser.add_argument('--gpu', action='store_true', help='Enable CUDA GPU inference when available')
    args = parser.parse_args(argv)
    try:
        report = run_depth_map(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
