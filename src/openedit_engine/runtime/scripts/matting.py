"""Offline non-green-screen AI video/image matting (RVM / BiRefNet / RMBG ONNX) -> ProRes 4444 RGBA."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from PIL import Image

import file_hash
import runtime_core as core
import runtime_paths

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def resolve_model(explicit, *, prefer_fp16=False):
    """--model, OPENEDIT_MATTING_MODEL, or local models/matting dir; never downloads weights."""
    candidate = explicit or os.environ.get('OPENEDIT_MATTING_MODEL')
    if candidate:
        path = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
        if not path.is_file():
            raise ValueError(f'Matting model is not a file: {path}')
        return path
    discovered = runtime_paths.discover_model('matting', include_env=False, prefer_fp16=prefer_fp16)
    if discovered is not None and discovered.is_file():
        return discovered
    raise ValueError(
        'Offline AI matting requires an ONNX matting model (RVM / BiRefNet / RMBG): '
        'pass --model PATH, set OPENEDIT_MATTING_MODEL, or place weights under %LOCALAPPDATA%\\OpenEditTools\\models\\matting\\. '
        'No weights are bundled in this package.'
    )


def inspect_session(session, requested_backend, model_path):
    """Detect or validate ONNX matting graph inputs/outputs across RVM, BiRefNet, and RMBG families."""
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    input_names = [i.name for i in inputs]
    output_names = [o.name for o in outputs]

    if requested_backend == 'auto':
        lower_names = {n.lower() for n in input_names}
        if len(inputs) >= 6 and {'r1i', 'r2i', 'r3i', 'r4i'}.issubset(lower_names):
            backend = 'rvm'
        elif model_path is not None and 'rmbg' in Path(model_path).stem.lower():
            backend = 'rmbg'
        else:
            backend = 'birefnet'
    else:
        backend = requested_backend

    if backend == 'rvm':
        by_lower = {i.name.lower(): i.name for i in inputs}
        required = ('src', 'r1i', 'r2i', 'r3i', 'r4i', 'downsample_ratio')
        missing = [k for k in required if k not in by_lower]
        if missing:
            raise ValueError(f'RVM model missing expected inputs {missing}; got {input_names}')
        out_lower = {o.name.lower(): idx for idx, o in enumerate(outputs)}
        out_req = ('fgr', 'pha', 'r1o', 'r2o', 'r3o', 'r4o')
        if not set(out_req).issubset(out_lower):
            raise ValueError(f'RVM model outputs must include {out_req}; got {output_names}')
        return {
            'backend': 'rvm',
            'input_map': {k: by_lower[k] for k in required},
            'output_indices': {k: out_lower[k] for k in out_req},
        }

    if len(inputs) != 1:
        raise ValueError(f'{backend} matting expects a single 4D image input, got {input_names}')
    in_meta = inputs[0]
    shape = list(in_meta.shape)
    if len(shape) != 4:
        raise ValueError(f'{backend} matting expects NCHW 4D tensor, got shape {shape}')
    ch = shape[1]
    if isinstance(ch, int) and ch != 3:
        raise ValueError(f'{backend} matting expects 3-channel RGB input, got shape {shape}')
    in_h = int(shape[2]) if isinstance(shape[2], int) and shape[2] > 0 else 1024
    in_w = int(shape[3]) if isinstance(shape[3], int) and shape[3] > 0 else 1024
    return {
        'backend': backend,
        'input_name': in_meta.name,
        'input_size': (in_w, in_h),
        'input_dtype': 'float16' if 'float16' in str(getattr(in_meta, 'type', '')) else 'float32',
    }


def load_session(model_path, requested_backend, *, device='cpu', gpu=False):
    """Create onnxruntime session on CPU or optional CUDA/DirectML when requested."""
    if gpu or (device and device != 'cpu'):
        runtime_paths.prepare_cuda_dlls()
    try:
        import onnxruntime as ort
    except ImportError:
        raise ValueError('Offline AI matting requires onnxruntime in this interpreter') from None

    providers = runtime_paths.resolve_ort_providers(ort.get_available_providers(), device=device, gpu=gpu)
    try:
        session = ort.InferenceSession(str(model_path), providers=providers)
    except Exception as exc:
        raise ValueError(f'onnxruntime could not initialize {Path(model_path).name}: {exc}') from None
    meta = inspect_session(session, requested_backend, model_path)
    return session, meta, list(session.get_providers())


class MattingRunner:
    """Stateful ONNX inference adapter holding RVM recurrent hidden states across frames."""

    def __init__(self, session, meta, *, downsample_ratio=None, use_model_fgr=False):
        try:
            import cv2  # noqa: F401
        except ImportError:
            raise ValueError('AI matting requires opencv (cv2) for image resize and matte filtering') from None
        self.session = session
        self.meta = meta
        self.backend = meta['backend']
        self.downsample_ratio = downsample_ratio
        self.use_model_fgr = bool(use_model_fgr)
        self.states = [np.zeros((1, 1, 1, 1), dtype=np.float32) for _ in range(4)]

    def predict(self, rgb_uint8):
        import cv2

        h, w = rgb_uint8.shape[:2]
        if self.backend == 'rvm':
            ratio = float(self.downsample_ratio if self.downsample_ratio is not None else min(1.0, 512.0 / max(w, h)))
            src_tensor = np.ascontiguousarray((rgb_uint8.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
            imap = self.meta['input_map']
            feed = {
                imap['src']: src_tensor,
                imap['r1i']: self.states[0],
                imap['r2i']: self.states[1],
                imap['r3i']: self.states[2],
                imap['r4i']: self.states[3],
                imap['downsample_ratio']: np.array([ratio], dtype=np.float32),
            }
            outputs = self.session.run(None, feed)
            oidx = self.meta['output_indices']
            self.states = [outputs[oidx[k]] for k in ('r1o', 'r2o', 'r3o', 'r4o')]
            pha = np.squeeze(outputs[oidx['pha']]).astype(np.float32)
            if pha.shape != (h, w):
                pha = cv2.resize(pha, (w, h), interpolation=cv2.INTER_LINEAR)
            alpha = np.clip(pha, 0.0, 1.0)
            if self.use_model_fgr:
                fgr = np.squeeze(outputs[oidx['fgr']]).astype(np.float32)
                if fgr.ndim == 3 and fgr.shape[0] == 3:
                    fgr = fgr.transpose(1, 2, 0)
                if fgr.shape[:2] != (h, w):
                    fgr = cv2.resize(fgr, (w, h), interpolation=cv2.INTER_LINEAR)
                fg_rgb = np.clip(np.round(fgr * 255.0), 0, 255).astype(np.uint8)
            else:
                fg_rgb = rgb_uint8
            return fg_rgb, alpha

        in_w, in_h = self.meta['input_size']
        interp = cv2.INTER_AREA if (in_w < w or in_h < h) else cv2.INTER_LINEAR
        resized = rgb_uint8 if (w, h) == (in_w, in_h) else cv2.resize(rgb_uint8, (in_w, in_h), interpolation=interp)
        norm = resized.astype(np.float32) / 255.0
        if self.backend == 'birefnet':
            norm = (norm - _IMAGENET_MEAN) / _IMAGENET_STD
        else:
            norm = norm - 0.5
        dtype = np.float16 if self.meta.get('input_dtype') == 'float16' else np.float32
        tensor = np.ascontiguousarray(norm.transpose(2, 0, 1)[None], dtype=dtype)
        raw_out = self.session.run(None, {self.meta['input_name']: tensor})[0]
        matte = np.squeeze(raw_out).astype(np.float32)
        if matte.ndim != 2:
            raise ValueError(f'Unexpected {self.backend} matte shape {raw_out.shape}')
        # Apply sigmoid only when model emits raw logits instead of [0, 1] probabilities.
        if float(matte.min()) < -0.01 or float(matte.max()) > 1.01:
            matte = 1.0 / (1.0 + np.exp(-np.clip(matte, -50.0, 50.0)))
        matte = np.clip(matte, 0.0, 1.0)
        if matte.shape != (h, w):
            matte = cv2.resize(matte, (w, h), interpolation=cv2.INTER_LINEAR)
        return rgb_uint8, np.clip(matte, 0.0, 1.0)


def postprocess_alpha(alpha, *, prev_alpha=None, threshold=0.0, erode=0, feather=0.0, temporal_ema=0.0):
    """Apply optional floor threshold, morphological erode/dilate, feather blur, and EMA smoothing."""
    import cv2

    out = alpha.astype(np.float32, copy=True)
    if threshold > 0.0:
        out = np.where(out < threshold, 0.0, out)
    if erode != 0:
        ksize = 2 * abs(int(erode)) + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
        matte_u8 = np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)
        matte_u8 = cv2.erode(matte_u8, kernel) if erode > 0 else cv2.dilate(matte_u8, kernel)
        out = matte_u8.astype(np.float32) / 255.0
    if feather > 0.0:
        out = cv2.GaussianBlur(out, (0, 0), sigmaX=float(feather), sigmaY=float(feather))
    if temporal_ema > 0.0 and prev_alpha is not None:
        out = (1.0 - float(temporal_ema)) * out + float(temporal_ema) * prev_alpha
    return np.clip(out, 0.0, 1.0)


def _save_alpha_previews(rgba, alpha, out_dir, *, rgba_name='preview_rgba.png', alpha_name='preview_alpha.png'):
    rgba_path = out_dir / rgba_name
    alpha_path = out_dir / alpha_name
    Image.fromarray(rgba, mode='RGBA').save(rgba_path)
    alpha_u8 = np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(alpha_u8, mode='L').save(alpha_path)
    return rgba_path, alpha_path


def run_matting(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')

    start = core.number(args.start, 'start', 0.0, 86400.0)
    max_frames = int(core.number(args.max_frames, 'max_frames', 1, 18000))
    feather = core.number(args.alpha_feather, 'alpha_feather', 0.0, 15.0)
    erode = int(core.number(args.alpha_erode, 'alpha_erode', -10, 10))
    threshold = core.number(args.threshold, 'threshold', 0.0, 0.95)
    temporal_ema = core.number(args.temporal_ema, 'temporal_ema', 0.0, 0.8)
    ds_ratio = core.number(args.downsample_ratio, 'downsample_ratio', 0.1, 1.0) if args.downsample_ratio is not None else None
    if type(args.alpha_erode) is bool or abs(args.alpha_erode - erode) > 1e-9:
        raise ValueError('alpha_erode must be an integer in [-10, 10]')

    info = core.probe(source)
    if info['kind'] not in ('image', 'video') or not info.get('has_video'):
        raise ValueError('AI matting requires a local image or video file with a video stream')

    w, h = int(info['width']), int(info['height'])
    if w <= 0 or h <= 0:
        raise ValueError(f'Invalid video/image dimensions: {w}x{h}')

    span = None
    fps = float(info.get('fps') or 30.0)
    preview_time = None
    if info['kind'] == 'video':
        total_dur = float(info.get('duration') or 0.0)
        if total_dur <= 0.0 or start >= total_dur:
            raise ValueError(f'start={start}s exceeds video duration {total_dur:.3f}s')
        remaining = total_dur - start
        span = min(10.0, remaining) if args.duration is None else core.number(args.duration, 'duration', 0.04, remaining)
        preview_time = span * 0.5 if args.preview_time is None else core.number(args.preview_time, 'preview_time', 0.0, span)
        est_frames = math.ceil(span * fps)
        if est_frames > max_frames:
            raise ValueError(
                f'Requested range (~{est_frames} frames) exceeds --max-frames {max_frames}; '
                'shorten --duration or raise --max-frames explicitly'
            )

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    want_gpu = bool(args.gpu or getattr(args, 'device', 'cpu') in ('cuda', 'auto'))
    model_file = resolve_model(args.model, prefer_fp16=want_gpu)
    session, meta, providers = load_session(
        model_file, args.backend, device=getattr(args, 'device', 'cpu'), gpu=args.gpu,
    )
    runner = MattingRunner(session, meta, downsample_ratio=ds_ratio, use_model_fgr=args.use_model_fgr)
    license_note = runtime_paths.model_license_note('matting', model_file)

    out = core.new_directory(out_cand)
    t0 = time.monotonic()

    if info['kind'] == 'image':
        with Image.open(source) as im:
            rgb = np.asarray(im.convert('RGB'), dtype=np.uint8)
        fg_rgb, raw_alpha = runner.predict(rgb)
        alpha = postprocess_alpha(raw_alpha, threshold=threshold, erode=erode, feather=feather)
        rgba = np.dstack([fg_rgb, np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)])
        rgba_path, alpha_path = _save_alpha_previews(
            rgba, alpha, out, rgba_name='matte_rgba.png', alpha_name='matte_alpha.png',
        )
        elapsed = round(time.monotonic() - t0, 3)
        report = {
            'status': 'passed',
            'kind': 'image',
            'backend': meta['backend'],
            'providers': providers,
            'model': {
                'path': str(model_file),
                'bytes': model_file.stat().st_size,
                'sha256': file_hash.sha256(model_file),
                'license_note': license_note,
            },
            'input': {'path': str(source), 'width': w, 'height': h, 'sha256': file_hash.sha256(source)},
            'out': str(out),
            'metrics': {
                'frame_count': 1,
                'elapsed_seconds': elapsed,
                'sec_per_frame': elapsed,
                'mean_alpha': round(float(alpha.mean()), 4),
                'fg_ratio': round(float((alpha >= 0.5).mean()), 4),
                'adjacent_alpha_mad': None,
            },
            'outputs': {
                'matte_rgba': str(rgba_path),
                'matte_alpha': str(alpha_path),
                'matte_video': None,
            },
            'boundaries': [
                license_note,
                'Single-image RGBA/alpha PNG masks cannot represent temporal edge stability across video motion.',
            ],
        }
        core.write_json(out / 'matting-report.json', report)
        return report

    ffmpeg_bin = core.tool('ffmpeg')
    dec_cmd = [
        ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-protocol_whitelist', 'file,pipe',
        '-ss', str(start), '-t', str(span), '-i', str(source),
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
    ]
    mov_path = out / 'matte.mov'
    enc_proc = None
    if not args.no_video:
        enc_cmd = [
            ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', f'{fps:.6g}', '-i', 'pipe:0',
        ]
        if args.keep_audio and info.get('has_audio'):
            enc_cmd.extend(['-protocol_whitelist', 'file,pipe', '-ss', str(start), '-t', str(span), '-i', str(source)])
            enc_cmd.extend(['-map', '0:v:0', '-map', '1:a:0?', '-c:a', 'pcm_s16le', '-shortest'])
        else:
            enc_cmd.extend(['-an'])
        enc_cmd.extend(['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p12le', str(mov_path)])
        enc_proc = subprocess.Popen(
            enc_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_NO_WIN,
        )

    dec_proc = subprocess.Popen(
        dec_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WIN,
    )

    frame_bytes = w * h * 3
    preview_idx = max(0, int(round(preview_time * fps)))
    frame_count = 0
    prev_alpha = None
    mad_sum = 0.0
    fg_ratio_sum = 0.0
    preview_rgba = None
    preview_alpha = None

    try:
        while True:
            raw = dec_proc.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError(f'Truncated RGB video pipe frame: expected {frame_bytes} B, got {len(raw)} B')
            if frame_count >= max_frames:
                raise ValueError(f'Decoded frame count exceeded --max-frames {max_frames}')

            rgb = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            fg_rgb, raw_alpha = runner.predict(rgb)
            alpha = postprocess_alpha(
                raw_alpha,
                prev_alpha=prev_alpha,
                threshold=threshold,
                erode=erode,
                feather=feather,
                temporal_ema=temporal_ema,
            )
            rgba = np.dstack([fg_rgb, np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)])

            if enc_proc is not None:
                enc_proc.stdin.write(rgba.tobytes())

            if prev_alpha is not None:
                mad_sum += float(np.abs(alpha - prev_alpha).mean())
            fg_ratio_sum += float((alpha >= 0.5).mean())
            if frame_count == 0 or frame_count == preview_idx:
                preview_rgba = rgba.copy()
                preview_alpha = alpha.copy()
            prev_alpha = alpha
            frame_count += 1
    finally:
        if dec_proc.stdout:
            dec_proc.stdout.close()
        dec_stderr = dec_proc.stderr.read().decode('utf-8', errors='replace') if dec_proc.stderr else ''
        dec_rc = dec_proc.wait(timeout=60)
        enc_stderr = ''
        enc_rc = 0
        if enc_proc is not None:
            if enc_proc.stdin and not enc_proc.stdin.closed:
                enc_proc.stdin.close()
            enc_stderr = enc_proc.stderr.read().decode('utf-8', errors='replace') if enc_proc.stderr else ''
            enc_rc = enc_proc.wait(timeout=300)

    if dec_rc != 0:
        raise RuntimeError(f'ffmpeg decode pipe failed: {dec_stderr[-1500:]}')
    if enc_proc is not None and enc_rc != 0:
        raise RuntimeError(f'ffmpeg ProRes 4444 encode pipe failed: {enc_stderr[-1500:]}')
    if frame_count == 0:
        raise RuntimeError('ffmpeg decoded 0 video frames in requested range')

    rgba_path, alpha_path = _save_alpha_previews(preview_rgba, preview_alpha, out)
    video_info = None
    if not args.no_video:
        if not mov_path.is_file():
            raise RuntimeError(f'Matting encode produced no output: {mov_path}')
        m_probe = core.probe(mov_path)
        if not m_probe.get('has_alpha'):
            raise RuntimeError(f'Matting output lacks alpha channel (pix_fmt={m_probe.get("pix_fmt")})')
        core.run([ffmpeg_bin, '-v', 'error', '-xerror', '-i', str(mov_path), '-f', 'null', '-'], timeout=300)
        video_info = {
            'path': str(mov_path),
            'sha256': file_hash.sha256(mov_path),
            'bytes': mov_path.stat().st_size,
            'codec': m_probe.get('video_codec'),
            'pix_fmt': m_probe.get('pix_fmt'),
            'has_alpha': m_probe.get('has_alpha'),
            'has_audio': m_probe.get('has_audio'),
            'duration': m_probe.get('duration'),
            'strict_decode': 'passed',
        }

    elapsed = round(time.monotonic() - t0, 3)
    mad = round(mad_sum / max(1, frame_count - 1), 6) if frame_count > 1 else 0.0
    report = {
        'status': 'passed',
        'kind': 'video',
        'backend': meta['backend'],
        'providers': providers,
        'model': {
            'path': str(model_file),
            'bytes': model_file.stat().st_size,
            'sha256': file_hash.sha256(model_file),
            'license_note': license_note,
        },
        'input': {'path': str(source), 'width': w, 'height': h, 'fps': round(fps, 3), 'sha256': file_hash.sha256(source)},
        'out': str(out),
        'range': {
            'start': round(start, 3),
            'duration': round(span, 3),
            'frame_count': frame_count,
            'keep_audio': bool(args.keep_audio and info.get('has_audio')),
        },
        'metrics': {
            'elapsed_seconds': elapsed,
            'sec_per_frame': round(elapsed / frame_count, 3),
            'mean_fg_ratio': round(fg_ratio_sum / frame_count, 4),
            'adjacent_alpha_mad': mad,
            'adjacent_alpha_mad_note': 'Candidate flicker indicator only (cannot distinguish motion from flicker without ground truth).',
        },
        'outputs': {
            'preview_rgba': str(rgba_path),
            'preview_alpha': str(alpha_path),
            'matte_video': video_info,
        },
        'boundaries': [
            license_note,
            'ProRes 4444 (.mov, yuva444p12le) preserves straight alpha for direct FilmCraft V2+ compositing.',
            'Inspect preview frames and final playback around fine hair, fast motion, and edge halos before final delivery.',
        ],
    }
    core.write_json(out / 'matting-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video or image file')
    parser.add_argument('--out', required=True, help='New output directory for ProRes 4444 matte.mov and previews')
    parser.add_argument('--backend', choices=('auto', 'rvm', 'birefnet', 'rmbg'), default='auto')
    parser.add_argument('--model', help='ONNX matting model path (else OPENEDIT_MATTING_MODEL / auto-discovery)')
    parser.add_argument('--start', type=float, default=0.0, help='Video start offset in seconds (default 0)')
    parser.add_argument('--duration', type=float, default=None, help='Video duration slice in seconds (default min(10, remaining))')
    parser.add_argument('--max-frames', type=int, default=600, help='Maximum decoded video frames (default 600)')
    parser.add_argument('--downsample-ratio', type=float, default=None, help='RVM internal downsample ratio in [0.1, 1.0]')
    parser.add_argument('--alpha-feather', type=float, default=0.0, help='Gaussian alpha feather sigma in px (0..15)')
    parser.add_argument('--alpha-erode', type=int, default=0, help='Morphological erode (>0) or dilate (<0) radius in [-10, 10]')
    parser.add_argument('--threshold', type=float, default=0.0, help='Zero alpha values below threshold in [0.0, 0.95]')
    parser.add_argument('--temporal-ema', type=float, default=0.0, help='Optional temporal EMA smoothing in [0.0, 0.8]')
    parser.add_argument('--use-model-fgr', action='store_true', help='RVM only: use predicted foreground RGB instead of original pixels')
    parser.add_argument('--keep-audio', action='store_true', help='Mux bounded source audio (PCM16) into matte.mov')
    parser.add_argument('--no-video', action='store_true', help='Write RGBA/alpha preview PNGs without rendering matte.mov')
    parser.add_argument('--preview-time', type=float, default=None, help='Preview capture time within slice in seconds')
    parser.add_argument('--device', choices=('cpu', 'cuda', 'auto'), default='cpu', help='Execution device (default cpu)')
    parser.add_argument('--gpu', action='store_true', help='Prefer CUDA/DirectML ORT provider when available')
    args = parser.parse_args(argv)
    try:
        report = run_matting(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
