"""Isolated PyTorch CUDA worker for MatAnyone 2 streaming raw rgb24 -> u8 alpha pipe."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys

import cv2
from hydra import compose, initialize_config_dir
import numpy as np
from omegaconf import open_dict
from PIL import Image
import torch
import torch.nn.functional as F

import matanyone2
from matanyone2.inference.inference_core import InferenceCore
from matanyone2.model.matanyone2 import MatAnyone2


def _read_exact(stream, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _load_model(ckpt_path, device):
    cfg_dir = str((Path(matanyone2.__file__).resolve().parent / 'config').resolve())
    with initialize_config_dir(version_base='1.3.2', config_dir=cfg_dir, job_name='openedit_matanyone2'):
        cfg = compose(config_name='eval_matanyone_config', overrides=['hydra/job_logging=disabled'])
    with open_dict(cfg):
        cfg['weights'] = str(ckpt_path)
        if 'model' in cfg and cfg['model'] is not None:
            cfg['model']['pretrained_resnet'] = False
    model = MatAnyone2(cfg, single_object=True).to(device).eval()
    try:
        weights = torch.load(str(ckpt_path), map_location=device, weights_only=True)
    except Exception:
        weights = torch.load(str(ckpt_path), map_location=device, weights_only=False)
    model.load_weights(weights)
    return model


def _load_seed_mask(mask_path, w, h, erode, dilate):
    with Image.open(mask_path) as im:
        gray = im.convert('L')
        if gray.size != (w, h):
            gray = gray.resize((w, h), Image.BILINEAR)
        mask = np.asarray(gray, dtype=np.uint8)
    if dilate > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate + 1, 2 * dilate + 1))
        mask = cv2.dilate(mask, k, iterations=1)
    if erode > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * erode + 1, 2 * erode + 1))
        mask = cv2.erode(mask, k, iterations=1)
    return mask


def main(argv=None):
    # Reserve raw fd 1 strictly for binary alpha frames; route all Python/Hydra/Torch stdout to stderr.
    bin_out = os.fdopen(os.dup(1), 'wb', buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--mask', required=True)
    parser.add_argument('--width', type=int, required=True)
    parser.add_argument('--height', type=int, required=True)
    parser.add_argument('--max-size', type=int, default=720)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--erode', type=int, default=0)
    parser.add_argument('--dilate', type=int, default=0)
    parser.add_argument('--device', choices=('auto', 'cuda', 'cpu'), default='auto')
    args = parser.parse_args(argv)

    if args.device == 'cuda' and not torch.cuda.is_available():
        sys.stderr.write('CUDA requested for MatAnyone 2 but torch.cuda.is_available() is False\n')
        return 2
    use_cuda = args.device in ('auto', 'cuda') and torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')

    w, h = int(args.width), int(args.height)
    min_side = min(w, h)
    if args.max_size > 0 and min_side > args.max_size:
        proc_h = max(16, int(h / min_side * args.max_size))
        proc_w = max(16, int(w / min_side * args.max_size))
    else:
        proc_w, proc_h = w, h

    if use_cuda:
        torch.cuda.reset_peak_memory_stats(device)
    model = _load_model(Path(args.model).resolve(), device)
    processor = InferenceCore(model, cfg=model.cfg, device=device)

    seed_u8 = _load_seed_mask(Path(args.mask).resolve(), w, h, args.erode, args.dilate)
    if (proc_w, proc_h) != (w, h):
        seed_u8 = cv2.resize(seed_u8, (proc_w, proc_h), interpolation=cv2.INTER_NEAREST)
    mask_t = torch.from_numpy(seed_u8).float().to(device)

    frame_bytes = w * h * 3
    frames = 0
    ctx = torch.amp.autocast('cuda', dtype=torch.float16) if use_cuda else contextlib.nullcontext()
    with torch.inference_mode(), ctx:
        while True:
            raw = _read_exact(sys.stdin.buffer, frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                sys.stderr.write(f'Truncated stdin rgb24 frame: {len(raw)} != {frame_bytes}\n')
                return 3
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            if (proc_w, proc_h) != (w, h):
                rgb = cv2.resize(rgb, (proc_w, proc_h), interpolation=cv2.INTER_AREA)
            img_t = torch.from_numpy(np.ascontiguousarray(rgb)).to(device).permute(2, 0, 1).float() / 255.0

            if frames == 0:
                processor.step(img_t, mask_t, objects=[1])
                output_prob = processor.step(img_t, first_frame_pred=True)
                for _ in range(max(0, int(args.warmup))):
                    output_prob = processor.step(img_t, first_frame_pred=True)
            else:
                output_prob = processor.step(img_t)

            pha = processor.output_prob_to_mask(output_prob).detach().float().clamp(0.0, 1.0)
            if (proc_w, proc_h) != (w, h):
                pha = F.interpolate(
                    pha.unsqueeze(0).unsqueeze(0), size=(h, w), mode='bilinear', align_corners=False,
                )[0, 0].clamp(0.0, 1.0)
            out_u8 = np.clip(np.round(pha.cpu().numpy() * 255.0), 0, 255).astype(np.uint8)
            bin_out.write(out_u8.tobytes())
            bin_out.flush()
            frames += 1

    bin_out.close()
    peak_vram_mb = round(torch.cuda.max_memory_allocated(device) / (1024.0 * 1024.0), 1) if use_cuda else 0.0
    sys.stderr.write(json.dumps({
        'device': str(device),
        'proc_size': [proc_w, proc_h],
        'frames': frames,
        'peak_vram_mb': peak_vram_mb,
    }) + '\n')
    sys.stderr.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
