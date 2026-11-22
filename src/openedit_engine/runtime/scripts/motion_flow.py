"""Offline OpenCV Farneback optical-flow motion analyzer & procedural 3D .cube cinema LUT generator (MIT)."""
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

import file_hash
import runtime_core as core

LUT_PRESETS = ('none', 'steel_violet', 'cinema_warm', 'teal_orange')
_VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mxf', '.ts'}


def _direction_label(vx, vy, speed, *, thresh=0.18):
    if speed < thresh:
        return 'static'
    angle = math.degrees(math.atan2(-vy, vx))  # screen +x=right, +y=up in intuitive compass coords
    dirs = [
        (-22.5, 22.5, 'right'),
        (22.5, 67.5, 'up_right'),
        (67.5, 112.5, 'up'),
        (112.5, 157.5, 'up_left'),
        (-67.5, -22.5, 'down_right'),
        (-112.5, -67.5, 'down'),
        (-157.5, -112.5, 'down_left'),
    ]
    for lo, hi, name in dirs:
        if lo <= angle < hi:
            return name
    return 'left'


def _zoom_label(div, *, thresh=0.12):
    if div >= thresh:
        return 'push_in'
    if div <= -thresh:
        return 'pull_out'
    return 'neutral'


def _summarize_window(records):
    if not records:
        return {
            'vx': 0.0,
            'vy': 0.0,
            'speed': 0.0,
            'direction': 'static',
            'zoom_divergence': 0.0,
            'zoom': 'neutral',
            'centroid': [0.5, 0.5],
        }
    vx = float(np.mean([r['vx'] for r in records]))
    vy = float(np.mean([r['vy'] for r in records]))
    speed = float(np.mean([r['speed'] for r in records]))
    div = float(np.mean([r['div'] for r in records]))
    weights = np.array([max(0.05, r['speed']) for r in records], dtype=np.float64)
    cx = float(np.average([r['cx'] for r in records], weights=weights))
    cy = float(np.average([r['cy'] for r in records], weights=weights))
    return {
        'vx': round(vx, 4),
        'vy': round(vy, 4),
        'speed': round(speed, 4),
        'direction': _direction_label(vx, vy, math.hypot(vx, vy)),
        'zoom_divergence': round(div, 4),
        'zoom': _zoom_label(div),
        'centroid': [round(cx, 4), round(cy, 4)],
    }


def _find_action_peaks(records, *, min_gap_s=0.25, limit=8):
    if len(records) < 3:
        return [
            {
                'time': round(r['time'], 4),
                'speed': round(r['speed'], 4),
                'zoom_divergence': round(r['div'], 4),
                'centroid': [round(r['cx'], 4), round(r['cy'], 4)],
            }
            for r in sorted(records, key=lambda item: item['speed'], reverse=True)[:limit]
        ]
    speeds = np.array([r['speed'] for r in records], dtype=np.float64)
    smoothed = np.convolve(speeds, np.array([0.25, 0.5, 0.25]), mode='same')
    candidates = []
    for i in range(1, len(records) - 1):
        if smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1] and smoothed[i] > 0.08:
            candidates.append((float(smoothed[i]), i))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = []
    for _, idx in candidates:
        t = records[idx]['time']
        if any(abs(t - records[s]['time']) < min_gap_s for s in selected):
            continue
        selected.append(idx)
        if len(selected) >= limit:
            break
    selected.sort()
    return [
        {
            'time': round(records[i]['time'], 4),
            'speed': round(records[i]['speed'], 4),
            'zoom_divergence': round(records[i]['div'], 4),
            'centroid': [round(records[i]['cx'], 4), round(records[i]['cy'], 4)],
        }
        for i in selected
    ]


def analyze_video(video_path, *, sample_fps=15.0, window_s=0.25):
    """Compute dense Farneback optical flow entry/exit vectors, radial divergence, centroids, and peaks."""
    path = Path(video_path).resolve()
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f'OpenCV failed to open video: {path}')
    try:
        src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if not math.isfinite(src_fps) or src_fps <= 0:
            src_fps = 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(round(src_fps / sample_fps)))
        tw, th = 256, 144

        # Precompute unit radial vectors from frame center for divergence/zoom estimation.
        xs = (np.arange(tw, dtype=np.float32) + 0.5) / tw
        ys = (np.arange(th, dtype=np.float32) + 0.5) / th
        gx, gy = np.meshgrid(xs, ys)
        dx_c = gx - 0.5
        dy_c = gy - 0.5
        rad = np.sqrt(dx_c * dx_c + dy_c * dy_c) + 1e-6
        rx = dx_c / rad
        ry = dy_c / rad
        center_mask = rad > 0.08

        prev_gray = None
        prev_t = 0.0
        records = []
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_idx % step == 0:
                small = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                curr_t = frame_idx / src_fps
                if prev_gray is not None:
                    dt = max(curr_t - prev_t, 1e-3)
                    # Scale dt normalization to standard 30fps baseline (~33.3ms step) so speeds compare across frame rates.
                    scale_dt = (1.0 / 30.0) / dt
                    flow = cv2.calcOpticalFlowFarneback(
                        prev_gray, gray, None,
                        pyr_scale=0.5, levels=3, winsize=13, iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
                    )
                    fx = flow[..., 0] * scale_dt
                    fy = flow[..., 1] * scale_dt
                    mag = np.sqrt(fx * fx + fy * fy)
                    thresh = float(np.percentile(mag, 60))
                    w = np.power(np.maximum(0.0, mag - thresh), 1.5)
                    w_sum = float(np.sum(w))
                    top_mag = mag[mag >= float(np.percentile(mag, 75))]
                    speed = float(np.mean(top_mag)) if top_mag.size else float(np.mean(mag))
                    if w_sum > 1e-5 and speed >= 0.12:
                        vx = float(np.sum(w * fx) / w_sum)
                        vy = float(np.sum(w * fy) / w_sum)
                        cx = float(np.sum(w * gx) / w_sum)
                        cy = float(np.sum(w * gy) / w_sum)
                    else:
                        vx = float(np.mean(fx))
                        vy = float(np.mean(fy))
                        cx, cy = 0.5, 0.5
                    div = float(np.mean((fx * rx + fy * ry)[center_mask]))
                    records.append({
                        'time': 0.5 * (prev_t + curr_t),
                        'vx': vx,
                        'vy': vy,
                        'speed': speed,
                        'div': div,
                        'cx': cx,
                        'cy': cy,
                    })
                prev_gray = gray
                prev_t = curr_t
            frame_idx += 1
    finally:
        cap.release()

    dur = max(frame_idx / src_fps, total_frames / src_fps if total_frames > 0 else 0.0)
    entry_recs = [r for r in records if r['time'] <= window_s] or records[:1]
    exit_recs = [r for r in records if r['time'] >= max(0.0, dur - window_s)] or records[-1:]
    overall = _summarize_window(records)
    return {
        'path': str(path),
        'name': path.name,
        'duration': round(dur, 4),
        'source_fps': round(src_fps, 3),
        'sampled_pairs': len(records),
        'entry_vector': _summarize_window(entry_recs),
        'exit_vector': _summarize_window(exit_recs),
        'overall': overall,
        'peak_speed': round(max((r['speed'] for r in records), default=0.0), 4),
        'action_peaks': _find_action_peaks(records),
    }


def generate_cube_lut(preset, out_path, *, size=33):
    """Write standard Adobe 3D .cube LUT (R fastest, G middle, B slowest) with smooth filmic curve."""
    if preset not in LUT_PRESETS or preset == 'none':
        raise ValueError(f'LUT preset must be one of {LUT_PRESETS[1:]}, got {preset!r}')
    n = int(size)
    if not 9 <= n <= 65:
        raise ValueError(f'lut_size must be in [9, 65], got {n}')

    grid = np.linspace(0.0, 1.0, n, dtype=np.float64)
    # Adobe .cube loop order: R fastest, G middle, B slowest -> meshgrid(B, G, R, indexing='ij') flattened C-order.
    b_g, g_g, r_g = np.meshgrid(grid, grid, grid, indexing='ij')
    r = r_g.reshape(-1)
    g = g_g.reshape(-1)
    b = b_g.reshape(-1)

    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    sh_w = np.exp(-((luma - 0.16) / 0.20) ** 2)
    mid_w = np.exp(-((luma - 0.50) / 0.26) ** 2)
    hi_w = np.exp(-((luma - 0.84) / 0.22) ** 2)

    def _s_curve(x, strength=0.14, toe_lift=0.010, shoulder_cap=0.988):
        s = x - strength * np.sin(2.0 * np.pi * x) / (2.0 * np.pi)
        return np.clip(toe_lift + (shoulder_cap - toe_lift) * s, 0.0, 1.0)

    if preset == 'steel_violet':
        ro, go, bo = _s_curve(r, 0.16, 0.012, 0.985), _s_curve(g, 0.16, 0.012, 0.985), _s_curve(b, 0.16, 0.014, 0.988)
        # Cool steel slate shadow, clean violet-plum midtone richness, crisp cool-silver highlight.
        ro = ro - 0.018 * sh_w + 0.010 * mid_w - 0.008 * hi_w
        go = go + 0.004 * sh_w - 0.008 * mid_w + 0.008 * hi_w
        bo = bo + 0.030 * sh_w + 0.022 * mid_w + 0.016 * hi_w
        # Gentle filmic saturation restraint on over-bright channels.
        y_n = 0.2126 * ro + 0.7152 * go + 0.0722 * bo
        sat_k = 0.96 - 0.06 * hi_w
        ro = y_n + (ro - y_n) * sat_k
        go = y_n + (go - y_n) * sat_k
        bo = y_n + (bo - y_n) * sat_k

    elif preset == 'cinema_warm':
        ro, go, bo = _s_curve(r, 0.15, 0.010, 0.986), _s_curve(g, 0.15, 0.010, 0.984), _s_curve(b, 0.15, 0.012, 0.980)
        ro = ro - 0.012 * sh_w + 0.016 * mid_w + 0.020 * hi_w
        go = go + 0.004 * sh_w + 0.004 * mid_w + 0.005 * hi_w
        bo = bo + 0.020 * sh_w - 0.012 * mid_w - 0.020 * hi_w

    elif preset == 'teal_orange':
        ro, go, bo = _s_curve(r, 0.18, 0.008, 0.988), _s_curve(g, 0.18, 0.008, 0.986), _s_curve(b, 0.18, 0.010, 0.986)
        ro = ro - 0.024 * sh_w + 0.020 * mid_w + 0.022 * hi_w
        go = go + 0.010 * sh_w + 0.002 * mid_w - 0.004 * hi_w
        bo = bo + 0.028 * sh_w - 0.016 * mid_w - 0.024 * hi_w

    rgb = np.clip(np.column_stack((ro, go, bo)), 0.0, 1.0)
    lut_file = Path(out_path).resolve()
    lines = [
        f'TITLE "OpenEdit {preset}"',
        f'LUT_3D_SIZE {n}',
        'DOMAIN_MIN 0.0 0.0 0.0',
        'DOMAIN_MAX 1.0 1.0 1.0',
    ]
    lines.extend(f'{rv:.6f} {gv:.6f} {bv:.6f}' for rv, gv, bv in rgb)
    lut_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {
        'preset': preset,
        'path': str(lut_file),
        'lut_size': n,
        'entries': int(rgb.shape[0]),
        'bytes': lut_file.stat().st_size,
        'sha256': file_hash.sha256(lut_file),
    }


def run_motion_analyze(args):
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        raise ValueError(f'Input video or directory does not exist: {src}')
    sample_fps = core.number(args.sample_fps, 'sample_fps', 1.0, 120.0)
    window_s = core.number(args.window, 'window', 0.05, 5.0)
    lut_size = int(args.lut_size)
    if not 9 <= lut_size <= 65:
        raise ValueError(f'--lut-size must be in [9, 65], got {lut_size}')

    if src.is_file():
        videos = [src]
        mode = 'single'
    elif src.is_dir():
        videos = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in _VIDEO_EXT)
        if not videos:
            raise ValueError(f'No video files found in directory: {src}')
        mode = 'batch'
    else:
        raise ValueError(f'Unsupported input path: {src}')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    clips_analysis = [analyze_video(v, sample_fps=sample_fps, window_s=window_s) for v in videos]
    out_dir = core.new_directory(out_cand)
    lut_info = None
    if args.make_lut != 'none':
        lut_info = generate_cube_lut(args.make_lut, out_dir / f'{args.make_lut}.cube', size=lut_size)

    report = {
        'status': 'passed',
        'operation': 'motion-analyze',
        'mode': mode,
        'sample_fps': sample_fps,
        'window_seconds': window_s,
        'clip_count': len(clips_analysis),
        'clips': clips_analysis,
        'lut': lut_info,
    }
    core.write_json(out_dir / 'motion-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video file or folder of videos')
    parser.add_argument('--out', required=True, help='New output directory for motion-report.json and optional .cube LUT')
    parser.add_argument('--sample-fps', type=float, default=15.0, help='Optical flow sampling FPS in [1, 120] (default 15)')
    parser.add_argument('--window', type=float, default=0.25, help='Entry/exit motion window seconds in [0.05, 5.0] (default 0.25)')
    parser.add_argument('--make-lut', choices=LUT_PRESETS, default='none', help='Optional procedural 3D cinema LUT preset to export')
    parser.add_argument('--lut-size', type=int, default=33, help='3D LUT grid size per axis in [9, 65] (default 33)')
    args = parser.parse_args(argv)
    try:
        report = run_motion_analyze(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
