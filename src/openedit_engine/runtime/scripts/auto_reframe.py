"""Offline scene-aware smart Auto-Reframe (16:9 <-> 9:16): OpenCV YuNet face / saliency tracking -> editable keyframes."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import numpy as np

import file_hash
import media_prep
import runtime_core as core
import runtime_paths

ASPECT_PRESETS = {
    '9:16': (1080, 1920),
    '4:5': (1080, 1350),
    '1:1': (1080, 1080),
    '16:9': (1920, 1080),
    '3:4': (1080, 1440),
}


def resolve_canvas_size(aspect, width=None, height=None):
    """Resolve output canvas integer dimensions from --aspect or explicit --width/--height."""
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError('Provide both --width and --height when overriding canvas size')
        cw = int(core.number(width, 'width', 64, 7680))
        ch = int(core.number(height, 'height', 64, 4320))
        return cw - (cw % 2), ch - (ch % 2)
    key = str(aspect or '9:16').strip()
    if key in ASPECT_PRESETS:
        return ASPECT_PRESETS[key]
    m = re.fullmatch(r'(\d+):(\d+)', key)
    if not m:
        raise ValueError(f'Invalid --aspect {aspect!r}; expected W:H such as 9:16, 4:5, 1:1, or 16:9')
    aw, ah = int(m.group(1)), int(m.group(2))
    if aw <= 0 or ah <= 0:
        raise ValueError('--aspect ratio numerator and denominator must be positive')
    if aw <= ah:
        cw = 1080
        ch = max(64, int(round(cw * ah / aw / 2.0)) * 2)
    else:
        ch = 1080
        cw = max(64, int(round(ch * aw / ah / 2.0)) * 2)
    return int(core.number(cw, 'width', 64, 7680)), int(core.number(ch, 'height', 64, 4320))


def resolve_face_model(explicit, detector):
    """Resolve YuNet ONNX face model (--face-model, OPENEDIT_FACE_MODEL, or auto-discovery)."""
    candidate = explicit or os.environ.get('OPENEDIT_FACE_MODEL')
    if candidate:
        path = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
        if not path.is_file():
            raise ValueError(f'Face detector model is not a file: {path}')
        return path
    if detector == 'saliency':
        return None
    discovered = runtime_paths.discover_model('face', include_env=False)
    return discovered if discovered is not None and discovered.is_file() else None


def _detect_scenes(source, duration, *, detector, content_threshold, min_scene_seconds):
    """Segment video into hard-cut scenes so pan keyframes never glide across edits."""
    if duration <= min_scene_seconds * 1.5 or detector == 'none':
        return [{'index': 0, 'start': 0.0, 'end': duration, 'duration': duration}], 'single_scene'

    chosen = detector
    if chosen == 'auto':
        try:
            media_prep._load_pyscenedetect()
            chosen = 'pyscenedetect'
        except ValueError:
            chosen = 'ffmpeg'

    if chosen == 'pyscenedetect':
        _, open_video, SceneManager, ContentDetector = media_prep._load_pyscenedetect()
        video = open_video(str(source))
        fps = float(getattr(video, 'frame_rate', None) or 30.0)
        try:
            min_len = max(1, round(min_scene_seconds * fps))
            sm = SceneManager()
            sm.add_detector(ContentDetector(threshold=content_threshold, min_scene_len=min_len))
            sm.detect_scenes(video)
            raw = sm.get_scene_list(start_in_scene=True)
        finally:
            cap = getattr(video, 'capture', None)
            if cap is not None:
                cap.release()
        scenes = []
        for s_tc, e_tc in raw:
            s = max(0.0, min(duration, float(s_tc.seconds)))
            e = max(s, min(duration, float(e_tc.seconds)))
            if e - s >= 0.04:
                scenes.append({'index': len(scenes), 'start': round(s, 6), 'end': round(e, 6), 'duration': round(e - s, 6)})
        if scenes:
            # Ensure continuous exact [0, duration] coverage.
            scenes[0]['start'] = 0.0
            scenes[-1]['end'] = duration
            for sc in scenes:
                sc['duration'] = round(sc['end'] - sc['start'], 6)
            return scenes, 'pyscenedetect'
        return [{'index': 0, 'start': 0.0, 'end': duration, 'duration': duration}], 'pyscenedetect'

    # FFmpeg showinfo cut timestamps fallback
    proc = core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin',
        '-protocol_whitelist', 'file,pipe', '-i', str(source),
        '-vf', 'select=gt(scene\\,0.35),showinfo', '-f', 'null', '-',
    ], timeout=300)
    cuts = [0.0]
    for m in re.finditer(r'pts_time:([0-9eE.+-]+)', proc.stderr or ''):
        t = float(m.group(1))
        if t - cuts[-1] >= min_scene_seconds and duration - t >= min_scene_seconds:
            cuts.append(t)
    cuts.append(duration)
    scenes = [
        {'index': idx, 'start': round(a, 6), 'end': round(b, 6), 'duration': round(b - a, 6)}
        for idx, (a, b) in enumerate(zip(cuts, cuts[1:]))
    ]
    return scenes, 'ffmpeg'


def _saliency_center(gray, prev_gray=None, prev_center=(0.5, 0.5)):
    """Estimate normalized (sx, sy) in [0, 1]^2 from spatial gradients + frame motion energy."""
    import cv2

    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)
    if prev_gray is not None and prev_gray.shape == gray.shape:
        motion = cv2.absdiff(gray, prev_gray).astype(np.float32)
        energy = 0.65 * grad + 1.35 * motion
    else:
        energy = grad
    energy = cv2.GaussianBlur(energy, (9, 9), 0)
    h, w = energy.shape
    thresh = float(np.percentile(energy, 70.0))
    weights = np.maximum(0.0, energy - thresh)
    total = float(weights.sum())
    if total <= 1e-6:
        return prev_center
    xs = (np.arange(w, dtype=np.float64) + 0.5) / w
    ys = (np.arange(h, dtype=np.float64) + 0.5) / h
    sx = float((weights.sum(axis=0) * xs).sum() / total)
    sy = float((weights.sum(axis=1) * ys).sum() / total)
    return float(np.clip(sx, 0.0, 1.0)), float(np.clip(sy, 0.0, 1.0))


def _pick_face_center(faces, dw, dh, prev_center):
    """Choose primary face from YuNet rows, weighting box area + proximity to previous target."""
    if faces is None or len(faces) == 0:
        return None
    best_score = -1.0
    best_pt = None
    px, py = prev_center
    for row in faces:
        fx, fy, fw, fh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
        if fw <= 2 or fh <= 2:
            continue
        cx = (fx + 0.5 * fw) / dw
        cy = (fy + 0.38 * fh) / dh
        area = (fw * fh) / (dw * dh)
        dist = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        score = area / (1.0 + 1.5 * dist)
        if score > best_score:
            best_score = score
            best_pt = (float(np.clip(cx, 0.0, 1.0)), float(np.clip(cy, 0.0, 1.0)))
    return best_pt


def _smooth_series(values, fps, smoothing_sec):
    """Smooth 1D normalized subject coordinates inside a single cut using Savitzky-Golay / box filter."""
    arr = np.asarray(values, dtype=np.float64)
    n = arr.size
    if n <= 2 or smoothing_sec <= 0.0:
        return arr
    win = max(3, int(round(smoothing_sec * fps)))
    if win % 2 == 0:
        win += 1
    win = min(win, n if n % 2 == 1 else max(3, n - 1))
    if win >= 5:
        try:
            from scipy.signal import savgol_filter
            poly = min(2, win - 1)
            return np.clip(savgol_filter(arr, window_length=win, polyorder=poly, mode='nearest'), 0.0, 1.0)
        except Exception:
            pass
    pad = win // 2
    padded = np.pad(arr, pad, mode='edge')
    kernel = np.ones(win, dtype=np.float64) / win
    smoothed = np.convolve(padded, kernel, mode='valid')
    return np.clip(smoothed[:n], 0.0, 1.0)


def _apply_deadband(values, deadband):
    """Hold current coordinate inside a small +/- deadband to eliminate micro-jitter on still shots."""
    if deadband <= 0.0 or len(values) <= 1:
        return list(values)
    out = [float(values[0])]
    anchor = out[0]
    for v in values[1:]:
        vf = float(v)
        if abs(vf - anchor) >= deadband:
            anchor = vf
        out.append(anchor)
    return out


def _rdp_indices(times, vals_x, vals_y, tolerance):
    """Ramer-Douglas-Peucker simplification keeping endpoints [0, N-1] plus key inflection points."""
    n = len(times)
    if n <= 2 or tolerance <= 0.0:
        return list(range(n))
    t_span = max(1e-6, times[-1] - times[0])
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a <= 1:
            continue
        ta, tb = times[a], times[b]
        dt = max(1e-9, tb - ta)
        max_err = -1.0
        max_idx = -1
        for i in range(a + 1, b):
            u = (times[i] - ta) / dt
            ix = vals_x[a] + (vals_x[b] - vals_x[a]) * u
            iy = vals_y[a] + (vals_y[b] - vals_y[a]) * u
            err = max(abs(vals_x[i] - ix), abs(vals_y[i] - iy))
            if err > max_err:
                max_err = err
                max_idx = i
        if max_err > tolerance and max_idx != -1:
            keep.add(max_idx)
            stack.append((a, max_idx))
            stack.append((max_idx, b))
        elif (tb - ta) / t_span > 1.0:
            pass
    return sorted(keep)


def subject_to_filmcraft_norm(sx, sy, sw, sh, cw, ch):
    """Convert normalized source subject center (sx, sy in [0,1]^2) to FilmCraft fit='cover' position_x/y.

    With FilmCraft _norm_to_pixels: px = cw/2 + nx*(cw/2), py = ch/2 - ny*(ch/2).
    Clamps within [-max_n, +max_n] so scaled video never exposes black bars on any edge.
    """
    if sw <= 0 or sh <= 0 or cw <= 0 or ch <= 0:
        return 0.0, 0.0, 0.0, 0.0
    scale = max(cw / float(sw), ch / float(sh))
    scaled_w = float(sw) * scale
    scaled_h = float(sh) * scale
    max_nx = max(0.0, (scaled_w - cw) / float(cw))
    max_ny = max(0.0, (scaled_h - ch) / float(ch))
    raw_nx = (0.5 - float(sx)) * scaled_w / (float(cw) / 2.0) if max_nx > 1e-6 else 0.0
    raw_ny = (float(sy) - 0.5) * scaled_h / (float(ch) / 2.0) if max_ny > 1e-6 else 0.0
    nx = max(-max_nx, min(max_nx, raw_nx))
    ny = max(-max_ny, min(max_ny, raw_ny))
    return round(nx, 4), round(ny, 4), round(max_nx, 4), round(max_ny, 4)


def run_auto_reframe(args):
    try:
        import cv2
    except ImportError:
        raise ValueError('auto-reframe requires opencv (cv2) in this interpreter') from None

    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')

    cw, ch = resolve_canvas_size(args.aspect, args.width, args.height)
    sample_fps = core.number(args.sample_fps, 'sample_fps', 1.0, 30.0)
    smoothing = core.number(args.smoothing, 'smoothing', 0.0, 5.0)
    deadband = core.number(args.deadband, 'deadband', 0.0, 0.3)
    content_threshold = core.number(args.content_threshold, 'content_threshold', 0.1, 255.0)
    min_scene_seconds = core.number(args.min_scene_seconds, 'min_scene_seconds', 0.1, 60.0)

    face_model = resolve_face_model(args.face_model, args.detector)
    if args.detector == 'yunet' and face_model is None:
        raise ValueError(
            'YuNet detector requested (--detector yunet), but no YuNet ONNX model found; '
            'pass --face-model PATH, set OPENEDIT_FACE_MODEL, or place YuNet ONNX under %LOCALAPPDATA%\\OpenEditTools\\models\\face\\'
        )

    info = core.probe(source)
    if info['kind'] != 'video' or not info.get('has_video'):
        raise ValueError('auto-reframe requires a local video file with a video stream')
    if core.is_filmcraft_hi10p(info):
        raise ValueError(
            f'Input uses 10-bit H.264 ({info.get("pix_fmt")}), which FilmCraft renders as MEDIA UNREADABLE; '
            'run normalize-media first'
        )
    sw, sh = int(info['width']), int(info['height'])
    duration = float(info.get('duration') or 0.0)
    fps = float(info.get('fps') or 30.0)
    if sw <= 0 or sh <= 0 or duration <= 0.0:
        raise ValueError('Input video has invalid dimensions or duration')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    scenes, scene_method = _detect_scenes(
        source, duration,
        detector=args.scene_detector,
        content_threshold=content_threshold,
        min_scene_seconds=min_scene_seconds,
    )

    yunet = None
    if face_model is not None:
        if not callable(getattr(cv2, 'FaceDetectorYN_create', None)):
            if args.detector == 'yunet':
                raise ValueError('cv2.FaceDetectorYN_create is unavailable in this OpenCV build')
        else:
            det_scale = min(1.0, 640.0 / max(sw, sh))
            dw = max(32, int(round(sw * det_scale)))
            dh = max(32, int(round(sh * det_scale)))
            yunet = cv2.FaceDetectorYN_create(str(face_model), '', (dw, dh), 0.55, 0.3, 100)

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise ValueError(f'OpenCV failed to open video: {source}')

    plan_name = core.check_text(args.name, 'name', 160) if args.name else f'{source.stem[:110]}_reframe_{cw}x{ch}'
    keep_audio = bool(info.get('has_audio') and not args.mute_audio)

    clips = []
    scene_reports = []
    faces_frames_total = 0
    sampled_frames_total = 0

    try:
        for sc in scenes:
            sc_start = float(sc['start'])
            sc_end = float(sc['end'])
            # Snap segment cleanly so consecutive clips never overlap in microseconds.
            start_u = core.us(sc_start)
            end_u = core.us(sc_end)
            if end_u <= start_u:
                continue
            sc_dur = (end_u - start_u) / 1_000_000
            sc_s = start_u / 1_000_000

            step = 1.0 / sample_fps
            sample_times = list(np.arange(0.0, sc_dur, step))
            if not sample_times or sample_times[-1] < sc_dur - 0.04:
                sample_times.append(max(0.0, sc_dur - 0.01))

            raw_sx, raw_sy, used_times = [], [], []
            prev_gray = None
            prev_center = (0.5, 0.5)
            scene_faces = 0

            for local_t in sample_times:
                abs_ms = (sc_s + local_t) * 1000.0
                cap.set(cv2.CAP_PROP_POS_MSEC, abs_ms)
                ok, bgr = cap.read()
                if not ok or bgr is None:
                    continue
                sampled_frames_total += 1
                face_pt = None
                if yunet is not None:
                    small_bgr = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA) if (dw, dh) != (sw, sh) else bgr
                    _, faces = yunet.detect(small_bgr)
                    face_pt = _pick_face_center(faces, dw, dh, prev_center)
                    if face_pt is not None:
                        scene_faces += 1
                        faces_frames_total += 1
                if face_pt is None:
                    small_g = cv2.resize(bgr, (320, max(180, round(320 * sh / sw))), interpolation=cv2.INTER_AREA)
                    gray = cv2.cvtColor(small_g, cv2.COLOR_BGR2GRAY)
                    face_pt = _saliency_center(gray, prev_gray=prev_gray, prev_center=prev_center)
                    prev_gray = gray
                prev_center = face_pt
                raw_sx.append(face_pt[0])
                raw_sy.append(face_pt[1])
                used_times.append(min(sc_dur, max(0.0, round(float(local_t), 6))))

            if not used_times:
                used_times = [0.0]
                raw_sx, raw_sy = [0.5], [0.5]

            smooth_x = _apply_deadband(_smooth_series(raw_sx, sample_fps, smoothing), deadband)
            smooth_y = _apply_deadband(_smooth_series(raw_sy, sample_fps, smoothing), deadband)

            norm_x_list, norm_y_list = [], []
            max_nx, max_ny = 0.0, 0.0
            for sx, sy in zip(smooth_x, smooth_y):
                nx, ny, max_nx, max_ny = subject_to_filmcraft_norm(sx, sy, sw, sh, cw, ch)
                norm_x_list.append(nx)
                norm_y_list.append(ny)

            # Deduplicate exact microsecond times before simplification.
            dedup_t, dedup_nx, dedup_ny = [], [], []
            last_u = -1
            for t_val, nx, ny in zip(used_times, norm_x_list, norm_y_list):
                u = core.us(t_val)
                if u > last_u:
                    dedup_t.append(u / 1_000_000)
                    dedup_nx.append(nx)
                    dedup_ny.append(ny)
                    last_u = u

            keep_idx = _rdp_indices(dedup_t, dedup_nx, dedup_ny, tolerance=0.025)
            keyframes = []
            for idx in keep_idx:
                t_val = dedup_t[idx]
                if max_nx > 1e-4:
                    keyframes.append({'property': 'position_x', 'time': t_val, 'value': dedup_nx[idx]})
                if max_ny > 1e-4:
                    keyframes.append({'property': 'position_y', 'time': t_val, 'value': dedup_ny[idx]})

            clip_obj = {
                'path': str(source),
                'start': sc_s,
                'source_in': sc_s,
                'duration': sc_dur,
                'track': 'video',
                'fit': 'cover',
                'volume': 1.0 if keep_audio else 0.0,
                'reason': f'auto-reframe scene #{len(clips) + 1} ({cw}x{ch})',
            }
            if max_nx > 1e-4 or max_ny > 1e-4:
                clip_obj['transform'] = {'transform_x': dedup_nx[0], 'transform_y': dedup_ny[0]}
            if len(keyframes) > (2 if max_nx > 1e-4 and max_ny > 1e-4 else 1):
                clip_obj['motion_easing'] = 'smoothstep'
                clip_obj['keyframes'] = keyframes
            clips.append(clip_obj)

            scene_reports.append({
                'scene_index': len(clips) - 1,
                'start': sc_s,
                'duration': sc_dur,
                'samples': len(used_times),
                'face_hits': scene_faces,
                'keyframe_count': len(clip_obj.get('keyframes', [])),
                'x_range': [min(dedup_nx), max(dedup_nx)],
                'y_range': [min(dedup_ny), max(dedup_ny)],
            })
    finally:
        cap.release()

    if not clips:
        raise ValueError('No valid video scenes remained for auto-reframe')

    plan_obj = {
        'schema_version': 2,
        'name': plan_name,
        'canvas': {'width': cw, 'height': ch, 'fps': round(fps, 6)},
        'clips': clips,
        'audio': [],
        'layer_order': ['video'],
        'notes': [f'Generated by auto-reframe ({sw}x{sh} -> {cw}x{ch}) with editable cover crop position keyframes.'],
    }

    out = core.new_directory(out_cand)
    plan_path = out / 'edit-plan.json'
    core.write_json(plan_path, plan_obj)
    core.validate_plan(plan_path)

    report = {
        'status': 'passed',
        'operation': 'auto-reframe',
        'input': {
            'path': str(source),
            'sha256': file_hash.sha256(source),
            'width': sw,
            'height': sh,
            'fps': fps,
            'duration': duration,
        },
        'canvas': {'width': cw, 'height': ch, 'fps': round(fps, 6), 'aspect': f'{cw}:{ch}'},
        'detector': {
            'requested': args.detector,
            'face_model': str(face_model) if face_model else None,
            'yunet_active': yunet is not None,
            'fallback': 'gradient_motion_saliency',
            'scene_method': scene_method,
        },
        'stats': {
            'scene_count': len(clips),
            'sampled_frames': sampled_frames_total,
            'face_detected_frames': faces_frames_total,
            'total_keyframes': sum(len(c.get('keyframes', [])) for c in clips),
        },
        'scenes': scene_reports,
        'plan_path': str(plan_path),
        'out': str(out),
        'boundaries': [
            'Outputs editable fit="cover" position keyframes in edit-plan.json rather than hard-cropping a baked video.',
            'Position coordinates are clamped to scaled-frame bounds so black borders cannot appear.',
            'Fast off-axis motion or non-frontal faces fall back to gradient+motion saliency; preview PNGs or inspect .fcproj in FilmCraft before final export.',
        ],
    }
    core.write_json(out / 'reframe.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video file to reframe')
    parser.add_argument('--out', required=True, help='New output directory for edit-plan.json and reframe.json')
    parser.add_argument('--aspect', default='9:16', help='Target aspect ratio preset or W:H (default 9:16)')
    parser.add_argument('--width', type=int, default=None, help='Optional explicit output canvas width')
    parser.add_argument('--height', type=int, default=None, help='Optional explicit output canvas height')
    parser.add_argument('--detector', choices=('auto', 'yunet', 'saliency'), default='auto', help='Subject tracker (default auto)')
    parser.add_argument('--face-model', help='Optional OpenCV YuNet ONNX model path (else OPENEDIT_FACE_MODEL / auto-discovery)')
    parser.add_argument('--scene-detector', choices=('auto', 'pyscenedetect', 'ffmpeg', 'none'), default='auto', help='Cut boundary detector (default auto)')
    parser.add_argument('--content-threshold', type=float, default=27.0, help='PySceneDetect ContentDetector threshold (default 27.0)')
    parser.add_argument('--min-scene-seconds', type=float, default=0.5, help='Minimum scene length in seconds (default 0.5)')
    parser.add_argument('--sample-fps', type=float, default=5.0, help='Tracking sampling rate in Hz (default 5.0)')
    parser.add_argument('--smoothing', type=float, default=0.8, help='Smoothing window in seconds (default 0.8)')
    parser.add_argument('--deadband', type=float, default=0.04, help='Jitter deadband in normalized [0, 0.3] (default 0.04)')
    parser.add_argument('--name', help='Optional edit plan name')
    parser.add_argument('--mute-audio', action='store_true', help='Mute source audio on reframed clips')
    args = parser.parse_args(argv)
    try:
        report = run_auto_reframe(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
