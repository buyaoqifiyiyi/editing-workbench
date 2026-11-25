"""Offline non-green-screen AI video/image matting (MatAnyone 2 / RVM / BiRefNet / RMBG) -> ProRes 4444 RGBA."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image

import file_hash
import runtime_core as core
import runtime_paths

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_WORKER_SCRIPT = Path(__file__).resolve().with_name('matanyone2_worker.py')
COLOR_HOLE_PRESETS = {
    'blue_sky': (90, 130, 55, 115),
    'cyan_sky': (80, 115, 45, 130),
    'green_screen': (35, 85, 60, 60),
}


def _read_exact(stream, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def resolve_model(explicit, *, backend='auto', prefer_fp16=False):
    """Resolve explicit model override or local offline weights; never downloads weights."""
    if backend == 'matanyone2':
        candidate = explicit or os.environ.get('OPENEDIT_MATANYONE2_MODEL')
        if candidate:
            path = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
            if not path.is_file():
                raise ValueError(f'Matting model is not a file: {path}')
            return path
        discovered = runtime_paths.discover_model('matanyone2', include_env=False)
        if discovered is not None and discovered.is_file():
            return discovered
        raise ValueError(
            'MatAnyone 2 requires matanyone2.pth checkpoint: '
            'pass --model PATH, set OPENEDIT_MATANYONE2_MODEL, or place weights under %LOCALAPPDATA%\\OpenEditTools\\models\\matting\\.'
        )
    if backend == 'rvm':
        candidate = explicit or os.environ.get('OPENEDIT_RVM_MODEL') or os.environ.get('OPENEDIT_MATTING_MODEL')
        if candidate:
            path = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
            if not path.is_file():
                raise ValueError(f'Matting model is not a file: {path}')
            return path
        discovered = runtime_paths.discover_model('rvm', include_env=False, prefer_fp16=prefer_fp16)
        if discovered is not None and discovered.is_file():
            return discovered
        raise ValueError(
            'RVM matting requires an RVM ONNX model: '
            'pass --model PATH, set OPENEDIT_RVM_MODEL, or place weights under %LOCALAPPDATA%\\OpenEditTools\\models\\matting\\rvm\\.'
        )
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
        src_info = next(i for i in inputs if i.name.lower() == 'src')
        return {
            'backend': 'rvm',
            'input_map': {k: by_lower[k] for k in required},
            'output_indices': {k: out_lower[k] for k in out_req},
            'input_dtype': 'float16' if 'float16' in str(getattr(src_info, 'type', '')) else 'float32',
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
            rdtype = np.float16 if self.meta.get('input_dtype') == 'float16' else np.float32
            src_tensor = np.ascontiguousarray((rgb_uint8.astype(np.float32) / 255.0).transpose(2, 0, 1)[None], dtype=rdtype)
            if self.states[0].dtype != rdtype:
                self.states = [s.astype(rdtype) for s in self.states]
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


def parse_color_hole_spec(spec):
    """Parse preset name or 'h_min,h_max,s_min,v_min' HSV thresholds for enclosed color hole cleanup."""
    if spec is None:
        return None
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError('clean_color_holes must be a preset name or "h_min,h_max,s_min,v_min"')
    text = spec.strip()
    if text in COLOR_HOLE_PRESETS:
        return COLOR_HOLE_PRESETS[text]
    parts = [p.strip() for p in text.split(',')]
    if len(parts) != 4:
        raise ValueError(
            f'Invalid clean_color_holes "{spec}": expected one of {tuple(COLOR_HOLE_PRESETS)} '
            'or 4 ints "h_min,h_max,s_min,v_min"'
        )
    try:
        h_min, h_max, s_min, v_min = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f'clean_color_holes HSV values must be integers: {spec}') from exc
    if not (0 <= h_min <= h_max <= 179 and 0 <= s_min <= 255 and 0 <= v_min <= 255):
        raise ValueError(
            f'clean_color_holes out of range (0<=h_min<=h_max<=179, 0<=s,v<=255): {spec}'
        )
    return (h_min, h_max, s_min, v_min)


def clean_color_holes(rgb_uint8, alpha, hole_spec):
    """Safely punch out enclosed chroma holes (e.g. blue sky inside hair/ribbons) from foreground alpha."""
    if hole_spec is None:
        return alpha
    import cv2

    h_min, h_max, s_min, v_min = hole_spec
    hsv = cv2.cvtColor(rgb_uint8, cv2.COLOR_RGB2HSV)
    mask = (
        (hsv[:, :, 0] >= h_min) & (hsv[:, :, 0] <= h_max)
        & (hsv[:, :, 1] >= s_min)
        & (hsv[:, :, 2] >= v_min)
        & (alpha > 0.05)
    ).astype(np.float32)
    if float(mask.max()) <= 0.0:
        return alpha
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.dilate(mask, kernel)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=0.8, sigmaY=0.8)
    return np.clip(alpha.astype(np.float32) * (1.0 - mask), 0.0, 1.0)


def apply_bottom_feather(alpha, ratio):
    """Apply smooth vertical cosine alpha fade across the bottom fraction of a half-body cutout."""
    if ratio <= 0.0 or alpha.shape[0] < 2:
        return alpha
    band = max(1, min(alpha.shape[0], int(round(alpha.shape[0] * float(ratio)))))
    t = np.linspace(0.0, 1.0, band, dtype=np.float32)
    ramp = 0.5 * (1.0 + np.cos(np.pi * t))
    out = alpha.astype(np.float32, copy=True)
    out[-band:, :] *= ramp[:, None]
    return np.clip(out, 0.0, 1.0)


def _finalize_fg_alpha(
    fg_rgb,
    raw_alpha,
    *,
    prev_alpha=None,
    threshold=0.0,
    erode=0,
    feather=0.0,
    temporal_ema=0.0,
    hole_spec=None,
    bottom_feather=0.0,
    decontaminate=False,
):
    alpha = postprocess_alpha(
        raw_alpha,
        prev_alpha=prev_alpha,
        threshold=threshold,
        erode=erode,
        feather=feather,
        temporal_ema=temporal_ema,
    )
    if hole_spec is not None:
        alpha = clean_color_holes(fg_rgb, alpha, hole_spec)
    if bottom_feather > 0.0:
        alpha = apply_bottom_feather(alpha, bottom_feather)
    if decontaminate:
        fg_rgb = decontaminate_foreground(fg_rgb, alpha)
    rgba = np.dstack([fg_rgb, np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)])
    return fg_rgb, alpha, rgba


def decontaminate_foreground(rgb_uint8, alpha):
    """Reduce background color bleed on semi-transparent edges by propagating solid foreground RGB."""
    import cv2

    a = np.clip(alpha.astype(np.float32), 0.0, 1.0)
    solid = (a >= 0.92).astype(np.float32)
    if float(solid.max()) <= 0.0:
        return rgb_uint8
    fringe = (a > 0.02) & (a < 0.92)
    if not bool(fringe.any()):
        return rgb_uint8
    rgb_f = rgb_uint8.astype(np.float32)
    h, w = a.shape
    sigma = max(3.0, round(min(h, w) * 0.015, 2))
    num = cv2.GaussianBlur(rgb_f * solid[:, :, None], (0, 0), sigmaX=sigma, sigmaY=sigma)
    den = cv2.GaussianBlur(solid, (0, 0), sigmaX=sigma, sigmaY=sigma)
    valid = fringe & (den > 1e-4)
    if not bool(valid.any()):
        return rgb_uint8
    clean = num / np.maximum(den[:, :, None], 1e-4)
    blend = np.clip((0.92 - a) / 0.90, 0.0, 1.0)[:, :, None]
    out = rgb_f.copy()
    out[valid] = (rgb_f * (1.0 - blend) + clean * blend)[valid]
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


class MatAnyone2Runner:
    """Subprocess adapter running MatAnyone 2 CUDA memory propagation in its isolated venv."""

    def __init__(self, python_bin, model_path, mask_path, width, height, *, max_size=1080, warmup=10, device='auto'):
        if not _WORKER_SCRIPT.is_file():
            raise RuntimeError(f'Missing MatAnyone 2 worker script: {_WORKER_SCRIPT}')
        self.width = int(width)
        self.height = int(height)
        self.alpha_bytes = self.width * self.height
        self.stats = None
        self._closed = False
        cmd = [
            str(python_bin), str(_WORKER_SCRIPT),
            '--model', str(model_path),
            '--mask', str(mask_path),
            '--width', str(self.width),
            '--height', str(self.height),
            '--max-size', str(int(max_size)),
            '--warmup', str(int(warmup)),
            '--device', str(device),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WIN,
        )
        self._stderr_chunks = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_chunks.append(chunk)
        except Exception:
            pass

    def predict(self, rgb_uint8):
        if self._closed or self.proc.poll() is not None:
            raise RuntimeError(f'MatAnyone 2 worker exited unexpectedly: {self._stderr_tail()}')
        try:
            self.proc.stdin.write(np.ascontiguousarray(rgb_uint8, dtype=np.uint8).tobytes())
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError(f'MatAnyone 2 worker pipe broke: {self._stderr_tail()}') from exc
        raw_alpha = _read_exact(self.proc.stdout, self.alpha_bytes)
        if len(raw_alpha) != self.alpha_bytes:
            raise RuntimeError(
                f'MatAnyone 2 worker returned truncated alpha ({len(raw_alpha)} != {self.alpha_bytes}): {self._stderr_tail()}'
            )
        alpha = np.frombuffer(raw_alpha, dtype=np.uint8).reshape((self.height, self.width)).astype(np.float32) / 255.0
        return rgb_uint8, alpha

    def _stderr_tail(self):
        if self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)
        return b''.join(self._stderr_chunks).decode('utf-8', errors='replace')[-1500:]

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        if self.proc.stdout and not self.proc.stdout.closed:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
        rc = self.proc.wait(timeout=120)
        self._stderr_thread.join(timeout=5.0)
        stderr_txt = b''.join(self._stderr_chunks).decode('utf-8', errors='replace')
        if rc != 0:
            raise RuntimeError(f'MatAnyone 2 worker failed (exit {rc}): {stderr_txt[-1500:]}')
        for line in reversed(stderr_txt.strip().splitlines()):
            line = line.strip()
            if line.startswith('{') and line.endswith('}'):
                try:
                    self.stats = json.loads(line)
                    break
                except ValueError:
                    pass


def _save_alpha_previews(rgba, alpha, out_dir, *, rgba_name='preview_rgba.png', alpha_name='preview_alpha.png'):
    rgba_path = out_dir / rgba_name
    alpha_path = out_dir / alpha_name
    Image.fromarray(rgba, mode='RGBA').save(rgba_path)
    alpha_u8 = np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(alpha_u8, mode='L').save(alpha_path)
    return rgba_path, alpha_path


def _dropout_metrics(fg_ratios, top_fg_ratios):
    if not fg_ratios:
        return {}
    min_fg = min(fg_ratios)
    max_fg = max(fg_ratios)
    min_top = min(top_fg_ratios)
    max_top = max(top_fg_ratios)
    max_drop = max((abs(fg_ratios[i] - fg_ratios[i - 1]) for i in range(1, len(fg_ratios))), default=0.0)
    max_top_drop = max((abs(top_fg_ratios[i] - top_fg_ratios[i - 1]) for i in range(1, len(top_fg_ratios))), default=0.0)
    warning = bool(max_drop > 0.20 or max_top_drop > 0.25 or (max_top >= 0.15 and min_top < 0.02))
    return {
        'min_fg_ratio': round(min_fg, 4),
        'max_fg_ratio': round(max_fg, 4),
        'top_band_min_fg_ratio': round(min_top, 4),
        'top_band_max_fg_ratio': round(max_top, 4),
        'max_frame_fg_drop': round(max_drop, 4),
        'max_top_band_drop': round(max_top_drop, 4),
        'dropout_warning': warning,
    }


def run_matting(args):
    import gc

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
    max_size = int(core.number(getattr(args, 'max_size', 720), 'max_size', -1, 4320))
    warmup = int(core.number(getattr(args, 'warmup', 10), 'warmup', 0, 60))
    decontaminate = bool(getattr(args, 'decontaminate', False))
    raw_defringe = getattr(args, 'defringe', 0)
    defringe = int(core.number(raw_defringe, 'defringe', 0, 4))
    if type(raw_defringe) is bool or abs(raw_defringe - defringe) > 1e-9:
        raise ValueError('defringe must be an integer in [0, 4]')
    if defringe > 0:
        if erode >= 0:
            erode = min(10, erode + defringe)
        decontaminate = True
        if feather == 0.0:
            feather = 0.5
    raw_hole_spec = getattr(args, 'clean_color_holes', None)
    hole_spec = parse_color_hole_spec(raw_hole_spec)
    bottom_feather = core.number(getattr(args, 'bottom_feather', 0.0), 'bottom_feather', 0.0, 0.45)
    raw_target_fps = getattr(args, 'target_fps', None)
    target_fps = core.number(raw_target_fps, 'target_fps', 1.0, 120.0) if raw_target_fps is not None else None
    if type(args.alpha_erode) is bool or abs(args.alpha_erode - int(core.number(args.alpha_erode, 'alpha_erode', -10, 10))) > 1e-9:
        raise ValueError('alpha_erode must be an integer in [-10, 10]')

    polish_meta = {
        'target_fps': target_fps,
        'defringe': defringe,
        'clean_color_holes': raw_hole_spec.strip() if isinstance(raw_hole_spec, str) and raw_hole_spec.strip() else None,
        'bottom_feather': round(bottom_feather, 4),
    }

    info = core.probe(source)
    if info['kind'] not in ('image', 'video') or not info.get('has_video'):
        raise ValueError('AI matting requires a local image or video file with a video stream')

    w, h = int(info['width']), int(info['height'])
    if w <= 0 or h <= 0:
        raise ValueError(f'Invalid video/image dimensions: {w}x{h}')

    span = None
    fps = float(info.get('fps') or 30.0)
    out_fps = float(target_fps) if target_fps is not None else fps
    preview_time = None
    if info['kind'] == 'video':
        total_dur = float(info.get('duration') or 0.0)
        if total_dur <= 0.0 or start >= total_dur:
            raise ValueError(f'start={start}s exceeds video duration {total_dur:.3f}s')
        remaining = total_dur - start
        span = min(10.0, remaining) if args.duration is None else core.number(args.duration, 'duration', 0.04, remaining)
        preview_time = span * 0.5 if args.preview_time is None else core.number(args.preview_time, 'preview_time', 0.0, span)
        est_frames = math.ceil(span * out_fps)
        if est_frames > max_frames:
            raise ValueError(
                f'Requested range (~{est_frames} frames) exceeds --max-frames {max_frames}; '
                'shorten --duration or raise --max-frames explicitly'
            )

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    req_device = getattr(args, 'device', 'cpu')
    want_gpu = bool(args.gpu or req_device in ('cuda', 'auto'))
    worker_device = 'cuda' if (args.gpu or req_device == 'cuda') else ('auto' if req_device == 'auto' else 'cpu')

    explicit_mask = getattr(args, 'mask', None)
    mask_path = None
    seed_model_file = None
    worker_py = None
    if args.backend == 'matanyone2':
        model_file = resolve_model(args.model, backend='matanyone2', prefer_fp16=want_gpu)
        worker_py = runtime_paths.discover_matanyone2_python()
        if worker_py is None or not worker_py.is_file():
            raise ValueError(
                'MatAnyone 2 requires its isolated Python environment at %LOCALAPPDATA%\\OpenEditTools\\venvs\\matanyone2 '
                '(or set OPENEDIT_MATANYONE2_PYTHON)'
            )
        if explicit_mask:
            mask_path = Path(os.path.expanduser(os.path.expandvars(str(explicit_mask)))).resolve()
            if not mask_path.is_file():
                raise ValueError(f'MatAnyone 2 seed mask is not a file: {mask_path}')
        else:
            seed_model_file = resolve_model(getattr(args, 'seed_model', None), backend='auto', prefer_fp16=want_gpu)
        license_note = runtime_paths.model_license_note('matanyone2', model_file)
        backend_name = 'matanyone2'
        providers = [f'PyTorch:{worker_device}']
        runner = None
    else:
        model_file = resolve_model(args.model, backend=args.backend, prefer_fp16=want_gpu)
        session, meta, providers = load_session(
            model_file, args.backend, device=req_device, gpu=args.gpu,
        )
        runner = MattingRunner(session, meta, downsample_ratio=ds_ratio, use_model_fgr=args.use_model_fgr)
        license_note = runtime_paths.model_license_note(
            'rvm' if meta['backend'] == 'rvm' else 'matting', model_file,
        )
        backend_name = meta['backend']

    out = core.new_directory(out_cand)
    t0 = time.monotonic()

    def _make_seed_mask(rgb_first):
        s_sess, s_meta, _ = load_session(seed_model_file, 'auto', device=req_device, gpu=args.gpu)
        try:
            s_runner = MattingRunner(s_sess, s_meta)
            _, s_alpha = s_runner.predict(rgb_first)
        finally:
            s_sess = None
            gc.collect()
        seed_out = out / 'seed_mask.png'
        Image.fromarray(np.clip(np.round(s_alpha * 255.0), 0, 255).astype(np.uint8), mode='L').save(seed_out)
        return seed_out

    if info['kind'] == 'image':
        with Image.open(source) as im:
            rgb = np.asarray(im.convert('RGB'), dtype=np.uint8)
        worker_stats = None
        if args.backend == 'matanyone2':
            if mask_path is None:
                mask_path = _make_seed_mask(rgb)
            m2 = MatAnyone2Runner(
                worker_py, model_file, mask_path, w, h,
                max_size=max_size, warmup=warmup, device=worker_device,
            )
            try:
                fg_rgb, raw_alpha = m2.predict(rgb)
            finally:
                m2.close()
            worker_stats = m2.stats
            if worker_stats:
                providers = [f'PyTorch:{worker_stats.get("device", worker_device)}']
        else:
            fg_rgb, raw_alpha = runner.predict(rgb)
        fg_rgb, alpha, rgba = _finalize_fg_alpha(
            fg_rgb,
            raw_alpha,
            threshold=threshold,
            erode=erode,
            feather=feather,
            hole_spec=hole_spec,
            bottom_feather=bottom_feather,
            decontaminate=decontaminate,
        )
        rgba_path, alpha_path = _save_alpha_previews(
            rgba, alpha, out, rgba_name='matte_rgba.png', alpha_name='matte_alpha.png',
        )
        elapsed = round(time.monotonic() - t0, 3)
        report = {
            'status': 'passed',
            'kind': 'image',
            'backend': backend_name,
            'providers': providers,
            'model': {
                'path': str(model_file),
                'bytes': model_file.stat().st_size,
                'sha256': file_hash.sha256(model_file),
                'license_note': license_note,
            },
            'input': {'path': str(source), 'width': w, 'height': h, 'sha256': file_hash.sha256(source)},
            'out': str(out),
            'decontaminated': decontaminate,
            'polish': polish_meta,
            'seed_mask': str(mask_path) if mask_path else None,
            'worker': worker_stats,
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
    ]
    if target_fps is not None:
        dec_cmd.extend(['-vf', f'fps={out_fps:.6g}'])
    dec_cmd.extend(['-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
    mov_path = out / 'matte.mov'
    enc_proc = None
    if not args.no_video:
        enc_cmd = [
            ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', f'{out_fps:.6g}', '-i', 'pipe:0',
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
    preview_idx = max(0, int(round(preview_time * out_fps)))
    top_h = max(1, h // 5)
    frame_count = 0
    prev_alpha = None
    mad_sum = 0.0
    fg_ratios = []
    top_fg_ratios = []
    preview_rgba = None
    preview_alpha = None
    m2_runner = None
    worker_stats = None

    try:
        while True:
            raw = _read_exact(dec_proc.stdout, frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError(f'Truncated RGB video pipe frame: expected {frame_bytes} B, got {len(raw)} B')
            if frame_count >= max_frames:
                raise ValueError(f'Decoded frame count exceeded --max-frames {max_frames}')

            rgb = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            if args.backend == 'matanyone2' and m2_runner is None:
                if mask_path is None:
                    mask_path = _make_seed_mask(rgb)
                m2_runner = MatAnyone2Runner(
                    worker_py, model_file, mask_path, w, h,
                    max_size=max_size, warmup=warmup, device=worker_device,
                )
            active_runner = m2_runner if m2_runner is not None else runner
            fg_rgb, raw_alpha = active_runner.predict(rgb)
            fg_rgb, alpha, rgba = _finalize_fg_alpha(
                fg_rgb,
                raw_alpha,
                prev_alpha=prev_alpha,
                threshold=threshold,
                erode=erode,
                feather=feather,
                temporal_ema=temporal_ema,
                hole_spec=hole_spec,
                bottom_feather=bottom_feather,
                decontaminate=decontaminate,
            )

            if enc_proc is not None:
                enc_proc.stdin.write(rgba.tobytes())

            if prev_alpha is not None:
                mad_sum += float(np.abs(alpha - prev_alpha).mean())
            fg_ratios.append(float((alpha >= 0.5).mean()))
            top_fg_ratios.append(float((alpha[:top_h, :] >= 0.5).mean()))
            if frame_count == 0 or frame_count == preview_idx:
                preview_rgba = rgba.copy()
                preview_alpha = alpha.copy()
            prev_alpha = alpha
            frame_count += 1
            if frame_count == 1 or frame_count % 25 == 0:
                sys.stderr.write(f'[matting:{backend_name}] frame {frame_count}\n')
                sys.stderr.flush()
        if m2_runner is not None:
            m2_runner.close()
            worker_stats = m2_runner.stats
            if worker_stats:
                providers = [f'PyTorch:{worker_stats.get("device", worker_device)}']
    finally:
        if m2_runner is not None and not m2_runner._closed:
            try:
                m2_runner.close()
            except Exception:
                pass
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
    metrics = {
        'elapsed_seconds': elapsed,
        'sec_per_frame': round(elapsed / frame_count, 3),
        'mean_fg_ratio': round(sum(fg_ratios) / frame_count, 4),
        'adjacent_alpha_mad': mad,
        'adjacent_alpha_mad_note': 'Candidate flicker indicator only (cannot distinguish motion from flicker without ground truth).',
    }
    metrics.update(_dropout_metrics(fg_ratios, top_fg_ratios))
    report = {
        'status': 'passed',
        'kind': 'video',
        'backend': backend_name,
        'providers': providers,
        'model': {
            'path': str(model_file),
            'bytes': model_file.stat().st_size,
            'sha256': file_hash.sha256(model_file),
            'license_note': license_note,
        },
        'input': {'path': str(source), 'width': w, 'height': h, 'fps': round(fps, 3), 'sha256': file_hash.sha256(source)},
        'out': str(out),
        'decontaminated': decontaminate,
        'polish': polish_meta,
        'seed_mask': str(mask_path) if mask_path else None,
        'worker': worker_stats,
        'range': {
            'start': round(start, 3),
            'duration': round(span, 3),
            'fps': round(out_fps, 4),
            'target_fps': target_fps,
            'frame_count': frame_count,
            'keep_audio': bool(args.keep_audio and info.get('has_audio')),
        },
        'metrics': metrics,
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
    parser.add_argument('--backend', choices=('auto', 'rvm', 'birefnet', 'rmbg', 'matanyone2'), default='auto')
    parser.add_argument('--model', help='ONNX matting model or MatAnyone 2 .pth checkpoint (else env / auto-discovery)')
    parser.add_argument('--mask', help='Optional first-frame seed mask PNG for matanyone2 (auto-seeded if omitted)')
    parser.add_argument('--seed-model', help='Optional ONNX seed model path for matanyone2 frame-0 auto-masking')
    parser.add_argument('--max-size', type=int, default=720, help='MatAnyone 2 shortest-side cap in px (default 720; <=0 full)')
    parser.add_argument('--warmup', type=int, default=10, help='MatAnyone 2 frame-0 warmup steps (default 10)')
    parser.add_argument('--decontaminate', action='store_true', help='Reduce background color bleed on semi-transparent edges')
    parser.add_argument('--target-fps', type=float, default=None, help='Optional output/inference FPS in [1, 120] for video matting (e.g. 30 for 2x anime speedup)')
    parser.add_argument('--defringe', type=int, default=0, help='Anime halo cleanup strength in [0, 4] (combines outer erode + edge decontamination)')
    parser.add_argument('--clean-color-holes', default=None, help='Punch out enclosed chroma holes: blue_sky|cyan_sky|green_screen or h_min,h_max,s_min,v_min')
    parser.add_argument('--bottom-feather', type=float, default=0.0, help='Bottom edge vertical alpha fade ratio in [0.0, 0.45] for half-body cutouts')
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
    parser.add_argument('--gpu', action='store_true', help='Prefer CUDA/DirectML ORT provider or PyTorch CUDA when available')
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
