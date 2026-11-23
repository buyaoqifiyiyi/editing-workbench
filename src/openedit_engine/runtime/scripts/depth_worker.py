"""Isolated CUDA PyTorch worker for Video Depth Anything (Small, Apache-2.0) temporal depth estimation."""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np


def _read_rgb_frames(video_path, *, max_frames=300, max_res=1080):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f'Failed to open video: {video_path}')
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
                scale = max_res / float(max(h, w))
                nw = max(2, int(round(w * scale)) // 2 * 2)
                nh = max(2, int(round(h * scale)) // 2 * 2)
                bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
            frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    if not frames:
        raise ValueError(f'No frames decoded from video: {video_path}')
    return np.stack(frames, axis=0), fps


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--vendor', required=True)
    parser.add_argument('--ckpt', required=True)
    parser.add_argument('--out-npy', required=True)
    parser.add_argument('--out-json', required=True)
    parser.add_argument('--input-size', type=int, default=518)
    parser.add_argument('--max-frames', type=int, default=300)
    parser.add_argument('--max-res', type=int, default=1080)
    parser.add_argument('--device', default='cuda')
    args = parser.parse_args(argv)

    vendor_dir = str(Path(args.vendor).resolve())
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)

    import torch
    from video_depth_anything.video_depth import VideoDepthAnything

    dev = 'cuda' if (args.device == 'cuda' and torch.cuda.is_available()) else 'cpu'
    model = VideoDepthAnything(encoder='vits', features=64, out_channels=[48, 96, 192, 384], metric=False)
    state = torch.load(str(Path(args.ckpt).resolve()), map_location='cpu', weights_only=True)
    model.load_state_dict(state, strict=True)
    model = model.to(dev).eval()

    frames, fps = _read_rgb_frames(args.input, max_frames=int(args.max_frames), max_res=int(args.max_res))
    depths, _ = model.infer_video_depth(
        frames, fps, input_size=int(args.input_size), device=dev, fp32=(dev == 'cpu'),
    )
    lo = float(np.percentile(depths, 1.0))
    hi = float(np.percentile(depths, 99.0))
    span = max(hi - lo, 1e-6)
    norm_u8 = np.clip((depths - lo) / span * 255.0, 0.0, 255.0).round().astype(np.uint8)
    np.save(str(Path(args.out_npy).resolve()), norm_u8)

    mean_diff = float(np.mean(np.abs(np.diff(norm_u8.astype(np.float32), axis=0))) / 255.0) if len(norm_u8) > 1 else 0.0
    summary = {
        'status': 'passed',
        'device': dev,
        'frames': int(norm_u8.shape[0]),
        'height': int(norm_u8.shape[1]),
        'width': int(norm_u8.shape[2]),
        'fps': round(fps, 4),
        'mean_frame_diff': round(mean_diff, 5),
    }
    Path(args.out_json).resolve().write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
