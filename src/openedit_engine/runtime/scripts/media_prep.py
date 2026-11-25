"""Local media inspection, reference frame extraction, and FFmpeg VFR normalization."""
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys

from PIL import Image, ImageDraw, ImageFont

import file_hash
import runtime_core as core


def _extract_frames(media_path, out_dir, duration, count):
    frames = []
    if count <= 0:
        return frames
    out_dir.mkdir(parents=True, exist_ok=True)
    times = [0.0] if duration <= 0 else [round(duration * (i + 0.5) / count, 3) for i in range(count)]
    for idx, ts in enumerate(times):
        png = out_dir / f'frame-{idx}.png'
        args = [core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n']
        if duration > 0:
            args.extend(['-ss', str(ts)])
        args.extend(['-i', str(media_path), '-frames:v', '1', str(png)])
        core.run(args, timeout=60)
        if png.is_file():
            frames.append({'time': ts, 'path': str(png)})
    return frames


def _create_yunet_detector():
    import auto_reframe
    import cv2

    face_model = auto_reframe.resolve_face_model(None, 'auto')
    if face_model is None or not callable(getattr(cv2, 'FaceDetectorYN_create', None)):
        return None
    return cv2.FaceDetectorYN_create(str(face_model), '', (320, 180), 0.50, 0.3, 100)


def _rgb_to_oklab_pixels(rgb_u8):
    import numpy as np

    arr = np.asarray(rgb_u8, dtype=np.float64).reshape(-1, 3) / 255.0
    lin = np.where(arr <= 0.04045, arr / 12.92, np.power((arr + 0.055) / 1.055, 2.4))
    r, g, b = lin[:, 0], lin[:, 1], lin[:, 2]
    l = np.cbrt(np.maximum(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b, 0.0))
    m = np.cbrt(np.maximum(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b, 0.0))
    s = np.cbrt(np.maximum(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b, 0.0))
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bv = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return np.column_stack((L, a, bv))


def _oklab_to_palette_hex(lab, k=3):
    import numpy as np

    if lab.shape[0] == 0:
        return []
    order = np.argsort(lab[:, 0])
    seeds = [order[int((i + 0.5) * (len(order) - 1) / k)] for i in range(k)]
    centers = lab[seeds].copy()
    for _ in range(7):
        dists = np.sum((lab[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        assign = np.argmin(dists, axis=1)
        for ci in range(k):
            mask = assign == ci
            if np.any(mask):
                centers[ci] = lab[mask].mean(axis=0)
    counts = np.bincount(assign, minlength=k)
    rank = np.argsort(-counts)
    hexes = []
    for ci in rank:
        if counts[ci] == 0:
            continue
        L, a, bv = centers[ci]
        l_ = L + 0.3963377774 * a + 0.2158037573 * bv
        m_ = L - 0.1055613458 * a - 0.0638541728 * bv
        s_ = L - 0.0894841775 * a - 1.2914855480 * bv
        l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
        rgb_lin = np.clip([
            +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
        ], 0.0, 1.0)
        srgb = np.where(rgb_lin <= 0.0031308, rgb_lin * 12.92, 1.055 * np.power(rgb_lin, 1.0 / 2.4) - 0.055)
        r8, g8, b8 = [int(np.clip(round(v * 255.0), 0, 255)) for v in srgb]
        hexes.append(f'#{r8:02X}{g8:02X}{b8:02X}')
    return hexes


def _estimate_shot_scale(face_h_ratio):
    """Classify cinematic shot scale from YuNet face height / frame height ratio (MovieNet taxonomy)."""
    if face_h_ratio is None:
        return 'WS'
    if face_h_ratio >= 0.42:
        return 'ECU'
    if face_h_ratio >= 0.25:
        return 'CU'
    if face_h_ratio >= 0.15:
        return 'MCU'
    if face_h_ratio >= 0.08:
        return 'MS'
    return 'WS'


def _crop_box_from_center(cx, cy, target_aspect, src_aspect=16.0 / 9.0):
    ratio = target_aspect / max(src_aspect, 1e-4)
    if ratio < 1.0:
        w = max(0.18, min(1.0, ratio))
        h = 1.0
    else:
        w = 1.0
        h = max(0.18, min(1.0, 1.0 / ratio))
    x0 = max(0.0, min(1.0 - w, cx - 0.5 * w))
    # Place subject upper-third (around y = 0.38 inside crop box) for nicer portrait headroom
    y0 = max(0.0, min(1.0 - h, cy - 0.38 * h))
    return {'x': round(x0, 4), 'y': round(y0, 4), 'w': round(w, 4), 'h': round(h, 4)}


def _analyze_item_framing(sample_frames, yunet=None, *, duration=None, src_aspect=16.0 / 9.0):
    """Inspect sampled PNGs for YuNet face center/headroom/shot_scale, aesthetics, crop boxes, and trim hints."""
    import auto_reframe
    import cv2
    import numpy as np

    if not sample_frames:
        return None
    centers = []
    headrooms = []
    face_h_ratios = []
    hists = []
    sub_ratios = []
    lap_vars = []
    lumas = []
    clip_ratios = []
    black_ratios = []
    chromas = []
    corrs = []
    face_hits = 0

    for sf in sample_frames:
        p = Path(sf['path'])
        if not p.is_file():
            continue
        with Image.open(p) as raw_im:
            rgb = np.asarray(raw_im.convert('RGB'), dtype=np.uint8)
        sh, sw = rgb.shape[:2]
        src_aspect = float(sw) / max(1.0, float(sh))
        scale = min(1.0, 640.0 / max(sw, sh))
        dw = max(32, int(round(sw * scale)))
        dh = max(32, int(round(sh * scale)))
        bgr_s = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (dw, dh), interpolation=cv2.INTER_AREA)

        pt = None
        if yunet is not None:
            yunet.setInputSize((dw, dh))
            _, faces = yunet.detect(bgr_s)
            if faces is not None and len(faces) > 0:
                pt = auto_reframe._pick_face_center(faces, dw, dh, centers[-1] if centers else (0.5, 0.45))
                if pt is not None:
                    face_hits += 1
                    top_ratios = [max(0.0, float(row[1]) / dh) for row in faces if float(row[2]) > 2 and float(row[3]) > 2]
                    fh_list = [max(0.0, float(row[3]) / dh) for row in faces if float(row[2]) > 2 and float(row[3]) > 2]
                    if top_ratios:
                        headrooms.append(min(top_ratios))
                    if fh_list:
                        face_h_ratios.append(max(fh_list))
        if pt is None:
            gray = cv2.cvtColor(bgr_s, cv2.COLOR_BGR2GRAY)
            pt = auto_reframe._saliency_center(gray, prev_center=centers[-1] if centers else (0.5, 0.45))
        centers.append(pt)

        hsv = cv2.cvtColor(bgr_s, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0.0, 1.0, cv2.NORM_MINMAX)
        if hists:
            corrs.append(float(cv2.compareHist(hists[-1], hist, cv2.HISTCMP_CORREL)))
        hists.append(hist)

        gray_full = cv2.cvtColor(bgr_s, cv2.COLOR_BGR2GRAY)
        lap_vars.append(float(cv2.Laplacian(gray_full, cv2.CV_32F).var()))
        gf = gray_full.astype(np.float32)
        lumas.append(float(gf.mean()))
        clip_ratios.append(float((gf >= 248.0).mean()))
        black_ratios.append(float((gf <= 12.0).mean()))

        lab = _rgb_to_oklab_pixels(bgr_s[::4, ::4, ::-1])
        chromas.append(float(np.sqrt(lab[:, 1] ** 2 + lab[:, 2] ** 2).mean()))

        y_bot = max(1, int(dh * 0.84))
        y_mid0, y_mid1 = int(dh * 0.35), int(dh * 0.65)
        gx_bot = cv2.Sobel(gray_full[y_bot:, :], cv2.CV_32F, 1, 0, ksize=3)
        gx_mid = cv2.Sobel(gray_full[y_mid0:y_mid1, :], cv2.CV_32F, 1, 0, ksize=3)
        e_bot = float(np.abs(gx_bot).mean())
        e_mid = max(4.0, float(np.abs(gx_mid).mean()))
        bright_bot = float((gray_full[y_bot:, :] >= 225).mean())
        if e_bot / e_mid > 2.2 and bright_bot > 0.03:
            sub_ratios.append(e_bot / e_mid)

    internal_cut = any(c < 0.55 for c in corrs)
    mean_cx = sum(c[0] for c in centers) / len(centers) if centers else 0.5
    mean_cy = sum(c[1] for c in centers) / len(centers) if centers else 0.5
    min_headroom = round(min(headrooms), 4) if headrooms else None
    max_fh = round(max(face_h_ratios), 4) if face_h_ratios else None
    shot_scale = _estimate_shot_scale(max_fh)
    bias_y = round(float(np.clip(mean_cy - 0.5, -0.35, 0.35)), 3)

    mean_lap = sum(lap_vars) / max(1, len(lap_vars))
    sharpness_score = round(100.0 * (1.0 - math.exp(-mean_lap / 220.0)), 1)
    avg_luma = sum(lumas) / max(1, len(lumas))
    max_clip = max(clip_ratios, default=0.0)
    max_blk = max(black_ratios, default=0.0)
    luma_score = max(0.0, 100.0 - abs(avg_luma - 122.0) * 0.75 - max_clip * 110.0 - max_blk * 85.0)
    avg_chroma = sum(chromas) / max(1, len(chromas))
    chroma_score = max(20.0, min(100.0, 48.0 + avg_chroma * 340.0))
    thirds_dx = min(abs(mean_cx - 1.0 / 3.0), abs(mean_cx - 2.0 / 3.0), abs(mean_cx - 0.5) * 1.15)
    thirds_dy = abs(mean_cy - 0.38)
    framing_score = max(25.0, min(100.0, 100.0 - thirds_dx * 120.0 - thirds_dy * 90.0))
    if min_headroom is not None and min_headroom < 0.06:
        framing_score -= 18.0
    if sub_ratios:
        framing_score -= 12.0
    aesthetic_score = round(
        max(0.0, min(100.0, 0.30 * sharpness_score + 0.25 * luma_score + 0.20 * chroma_score + 0.25 * framing_score)),
        1,
    )

    skip_lead = 0.0
    skip_tail = 0.0
    trim_reason = 'clean'
    can_trim = duration is None or float(duration) > 1.2
    if can_trim and lumas:
        if lumas[0] < 14.0 or clip_ratios[0] >= 0.45:
            skip_lead = 0.25
            trim_reason = 'leading_dark_or_flash'
        elif len(corrs) >= 2 and corrs[0] > 0.992:
            skip_lead = 0.20
            trim_reason = 'leading_static_hold'
        if len(lumas) >= 2 and (lumas[-1] < 14.0 or clip_ratios[-1] >= 0.45):
            skip_tail = 0.25
            trim_reason = 'trailing_dark_or_flash' if trim_reason == 'clean' else f'{trim_reason}+trailing_dark_or_flash'
        elif len(corrs) >= 2 and corrs[-1] > 0.992:
            skip_tail = 0.20
            trim_reason = 'trailing_static_hold' if trim_reason == 'clean' else f'{trim_reason}+trailing_static_hold'

    hazards = []
    if internal_cut:
        hazards.append('possible_internal_cut')
    if sub_ratios:
        hazards.append('possible_hard_subtitle')
    if min_headroom is not None and min_headroom < 0.06:
        hazards.append('tight_headroom')

    return {
        'detector': 'yunet' if face_hits > 0 else 'saliency',
        'sampled_frames': len(centers),
        'face_detected_frames': face_hits,
        'face_height_ratio': max_fh,
        'shot_scale': shot_scale,
        'subject_center': [round(mean_cx, 4), round(mean_cy, 4)],
        'headroom_ratio': min_headroom,
        'recommended_crop_bias_y': bias_y,
        'sharpness_score': sharpness_score,
        'aesthetic_score': aesthetic_score,
        'rule_of_thirds_crop': {
            'target_9_16': _crop_box_from_center(mean_cx, mean_cy, 9.0 / 16.0, src_aspect=src_aspect),
            'target_4_5': _crop_box_from_center(mean_cx, mean_cy, 4.0 / 5.0, src_aspect=src_aspect),
        },
        'trim_suggestion': {
            'skip_leading_s': skip_lead,
            'skip_trailing_s': skip_tail,
            'reason': trim_reason,
        },
        'internal_cut_detected': internal_cut,
        'possible_hard_subtitle': bool(sub_ratios),
        'hazards': hazards,
    }


def _build_inspect_contact_sheet(probed_items, out_path):
    """Build a multi-row visual triage contact sheet showing filename, duration, and sample strip per clip."""
    rows_items = [item for item in probed_items if item.get('sample_frames')]
    if not rows_items:
        return None
    max_cols = max(len(item['sample_frames']) for item in rows_items)
    thumb_w = 240
    first_w = max(1, int(rows_items[0].get('width') or 16))
    first_h = max(1, int(rows_items[0].get('height') or 9))
    thumb_h = max(90, round(thumb_w * first_h / float(first_w)))
    hdr_h, pad = 26, 8
    canvas_w = max_cols * (thumb_w + pad) + pad
    canvas_h = len(rows_items) * (thumb_h + hdr_h + pad) + pad
    canvas = Image.new('RGB', (canvas_w, canvas_h), (14, 16, 22))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 13)
    except Exception:
        font = ImageFont.load_default()

    for r_idx, item in enumerate(rows_items):
        y0 = pad + r_idx * (thumb_h + hdr_h + pad)
        draw.rectangle([pad, y0, canvas_w - pad - 1, y0 + hdr_h - 2], fill=(28, 32, 44))
        name = Path(item.get('path', f'clip_{r_idx}')).name
        dur = float(item.get('duration') or 0.0)
        hazards = (item.get('framing') or {}).get('hazards') or []
        tag_str = f' [!{",".join(hazards)}]' if hazards else ''
        label = f'#{r_idx + 1:03d} {name[:36]} | {item.get("width", 0)}x{item.get("height", 0)} {dur:.2f}s{tag_str}'
        draw.text((pad + 6, y0 + 5), label, fill=(240, 245, 255), font=font)
        for c_idx, sf in enumerate(item['sample_frames']):
            p = Path(sf['path'])
            if not p.is_file():
                continue
            with Image.open(p) as raw_im:
                im = raw_im.convert('RGB').resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            x0 = pad + c_idx * (thumb_w + pad)
            canvas.paste(im, (x0, y0 + hdr_h))
            draw.rectangle([x0, y0 + hdr_h, x0 + thumb_w - 1, y0 + hdr_h + thumb_h - 1], outline=(62, 72, 92))
    canvas.save(out_path, quality=88)
    return {'path': str(out_path), 'items': len(rows_items), 'columns': max_cols}


def inspect_media(input_path, out_dir, frames=3, max_files=200, *, contact_sheet=False, analyze_framing=False):
    """Probe a single local file or media folder, extract sample frames, and optionally triage framing/hazards."""
    core.number(frames, 'frames', 1, 12)
    core.number(max_files, 'max_files', 1, 1000)
    target = Path(input_path).resolve()
    out = core.new_directory(out_dir)

    candidates = []
    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        for root, _, files in core.os.walk(target):
            for name in sorted(files):
                p = Path(root) / name
                if p.suffix.lower() in core.MEDIA_EXT:
                    candidates.append(p)
                    if len(candidates) >= max_files:
                        break
            if len(candidates) >= max_files:
                break
    else:
        raise ValueError(f'Input path not found: {target}')

    yunet = _create_yunet_detector() if analyze_framing else None
    probed = []
    for idx, media_file in enumerate(candidates):
        info = core.probe(media_file)
        item_warnings = []
        if info.get('has_video'):
            info['sample_frames'] = _extract_frames(media_file, out / f'item_{idx}', info.get('duration', 0), int(frames))
            if analyze_framing:
                framing = _analyze_item_framing(info['sample_frames'], yunet=yunet, duration=info.get('duration'))
                if framing is not None:
                    info['framing'] = framing
                    item_warnings.extend(framing['hazards'])
            if info.get('vfr'):
                item_warnings.append('variable_frame_rate')
            if core.is_filmcraft_hi10p(info):
                item_warnings.append('h264_10bit_hi10p_unsupported_by_filmcraft')
        if item_warnings:
            info['warnings'] = item_warnings
        info['needs_normalization'] = any(
            w in ('variable_frame_rate', 'h264_10bit_hi10p_unsupported_by_filmcraft')
            for w in item_warnings
        )
        probed.append(info)

    sheet_meta = None
    if contact_sheet:
        sheet_meta = _build_inspect_contact_sheet(probed, out / 'contact_sheet.jpg')

    report = {
        'status': 'passed',
        'input': str(target),
        'count': len(probed),
        'contact_sheet': sheet_meta,
        'items': probed,
    }
    core.write_json(out / 'inspect.json', report)
    return report


def _load_pyscenedetect():
    """Load optional PySceneDetect 0.7.2 lazily using existing cv2."""
    explicit = os.environ.get('OPENEDIT_PYSCENEDETECT_DIR')
    if explicit:
        explicit_dir = Path(os.path.expandvars(explicit)).expanduser()
        if not explicit_dir.is_dir():
            raise ValueError(f'OPENEDIT_PYSCENEDETECT_DIR does not exist: {explicit_dir}')
        candidates = [explicit_dir]
    else:
        local = Path(os.environ.get('LOCALAPPDATA') or Path.home())
        candidates = [
            local / 'OpenEditEngine' / 'optional' / 'pyscenedetect-0.7.2',
        ]
    for cand in candidates:
        if cand.is_dir():
            resolved = str(cand.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            break
    try:
        import cv2  # noqa: F401
        import scenedetect
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:
        raise ValueError(
            '--detector pyscenedetect requires optional PySceneDetect 0.7.2 and cv2; '
            f'default --detector ffmpeg remains unaffected ({exc})'
        ) from None
    return scenedetect, open_video, SceneManager, ContentDetector


def _extract_reference_pyscenedetect(target, info, frames_dir, *, start, span, content_threshold, min_scene_seconds, max_frames, modules):
    scenedetect, open_video, SceneManager, ContentDetector = modules
    window_end = start + span
    fps = float(info.get('fps') or 30.0)
    video = open_video(str(target))
    try:
        if getattr(video, 'frame_rate', None):
            fps = float(video.frame_rate)
        if start > 0:
            video.seek(start)
        min_scene_len = max(1, round(min_scene_seconds * fps))
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=content_threshold, min_scene_len=min_scene_len))
        scene_manager.detect_scenes(video, end_time=window_end)
        raw_scenes = scene_manager.get_scene_list(start_in_scene=True)
    finally:
        cap = getattr(video, 'capture', None)
        if cap is not None:
            cap.release()

    scenes = []
    for tc_start, tc_end in raw_scenes:
        s = max(start, min(window_end, float(tc_start.seconds)))
        e = max(s, min(window_end, float(tc_end.seconds)))
        if e - s >= 0.01:
            scenes.append({
                'index': len(scenes),
                'start': round(s, 3),
                'end': round(e, 3),
                'duration': round(e - s, 3),
            })
    if not scenes:
        scenes = [{'index': 0, 'start': round(start, 3), 'end': round(window_end, 3), 'duration': round(span, 3)}]

    cuts = [sc['start'] for sc in scenes[1:]]
    durations = [sc['duration'] for sc in scenes]
    median_dur = round(float(statistics.median(durations)), 3)
    cuts_per_min = round(len(cuts) * 60.0 / span, 2) if span > 0 else 0.0

    if len(scenes) <= max_frames:
        picked = scenes
    else:
        picked = [scenes[round(i * (len(scenes) - 1) / (max_frames - 1))] for i in range(max_frames)]

    extracted = []
    max_seek = max(start, window_end - 0.01)
    for idx, sc in enumerate(picked):
        ts = round(min(max_seek, sc['start'] + min(0.1, sc['duration'] * 0.25)), 3)
        png = frames_dir / f'scene-{idx:03d}.png'
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-ss', str(ts), '-i', str(target), '-frames:v', '1', str(png),
        ], timeout=60)
        if png.is_file():
            extracted.append(str(png))

    return {
        'detector': 'pyscenedetect',
        'detector_version': getattr(scenedetect, '__version__', '0.7.2'),
        'detector_params': {'content_threshold': content_threshold, 'min_scene_seconds': min_scene_seconds},
        'scenes': scenes,
        'cuts': cuts,
        'scene_count': len(scenes),
        'cut_count': len(cuts),
        'median_scene_seconds': median_dur,
        'cuts_per_minute': cuts_per_min,
        'frames': extracted,
        'boundaries': [
            'PySceneDetect ContentDetector provides candidate visual cuts inside the requested window only.',
            'Window edges may clip leading/trailing shots; does not inspect camera motion, effects, or audio.',
        ],
    }


def _compute_editing_rhythm(extracted_frames, cuts, span, *, start=0.0):
    """Build reference editing rhythm fingerprint (ASL, quarter pacing curve, Oklab palette, shot scales)."""
    import cv2
    import numpy as np

    clean_span = max(0.1, float(span))
    rel_cuts = sorted({
        round(max(0.0, min(clean_span, float(t) - float(start))), 3)
        for t in (cuts or [])
        if 0.02 < (float(t) - float(start)) < clean_span - 0.02
    })
    boundaries = [0.0] + rel_cuts + [round(clean_span, 3)]
    shot_durs = [round(max(0.02, b - a), 3) for a, b in zip(boundaries, boundaries[1:])]
    asl = round(float(sum(shot_durs) / len(shot_durs)), 3)
    med_s = round(float(statistics.median(shot_durs)), 3)
    min_s = round(min(shot_durs), 3)
    max_s = round(max(shot_durs), 3)
    total_cpm = round(len(rel_cuts) * 60.0 / clean_span, 2)

    q_len = clean_span / 4.0
    quarters = []
    for qi in range(4):
        q0, q1 = qi * q_len, (qi + 1) * q_len
        q_cuts = sum(1 for t in rel_cuts if q0 <= t < q1 or (qi == 3 and t == clean_span))
        quarters.append(round(q_cuts * 60.0 / max(q_len, 1e-3), 2))
    if quarters[3] > quarters[0] * 1.35 + 1.0:
        profile = 'accelerating'
    elif quarters[0] > quarters[3] * 1.35 + 1.0:
        profile = 'decelerating'
    elif max(quarters[1], quarters[2]) > max(quarters[0], quarters[3]) * 1.25 + 1.0:
        profile = 'climax_middle'
    else:
        profile = 'steady'

    yunet = _create_yunet_detector()
    lab_samples = []
    mono_count = 0
    shot_scale_seq = []
    step_f = max(1, len(extracted_frames) // 16) if len(extracted_frames) > 16 else 1
    sampled_paths = extracted_frames[::step_f][:16]
    for f_idx, fp in enumerate(sampled_paths):
        p = Path(fp)
        if not p.is_file():
            continue
        with Image.open(p) as im:
            small = im.convert('RGB')
            small.thumbnail((240, 240))
            rgb = np.asarray(small, dtype=np.uint8)
        lab = _rgb_to_oklab_pixels(rgb[::3, ::3])
        lab_samples.append(lab)
        chroma_med = float(np.median(np.sqrt(lab[:, 1] ** 2 + lab[:, 2] ** 2)))
        if chroma_med < 0.035:
            mono_count += 1
        face_h_ratio = None
        if yunet is not None:
            bgr_s = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            yunet.setInputSize((rgb.shape[1], rgb.shape[0]))
            _, faces = yunet.detect(bgr_s)
            if faces is not None and len(faces) > 0:
                fhs = [max(0.0, float(row[3]) / max(1, rgb.shape[0])) for row in faces if float(row[2]) > 2 and float(row[3]) > 2]
                if fhs:
                    face_h_ratio = round(max(fhs), 4)
        shot_scale_seq.append({
            'frame': p.name,
            'shot_scale': _estimate_shot_scale(face_h_ratio),
            'luma_mean': round(float(rgb.mean()), 1),
        })

    palette = _oklab_to_palette_hex(np.vstack(lab_samples), k=3) if lab_samples else []
    mono_ratio = round(mono_count / max(1, len(shot_scale_seq)), 3)

    return {
        'shot_count': len(shot_durs),
        'cut_count': len(rel_cuts),
        'asl_seconds': asl,
        'median_shot_s': med_s,
        'min_shot_s': min_s,
        'max_shot_s': max_s,
        'cuts_per_minute': total_cpm,
        'pacing_quarters_cpm': quarters,
        'pacing_profile': profile,
        'dominant_palette_hex': palette,
        'monochrome_ratio': mono_ratio,
        'shot_scale_sequence': shot_scale_seq,
    }


def extract_reference(input_path, out_dir, *, start=0.0, duration=None, detector='ffmpeg', threshold=None, content_threshold=None, min_scene_seconds=None, max_frames=24):
    """Extract bounded scene-change frames and editing rhythm fingerprint from a local reference video."""
    if detector not in ('ffmpeg', 'pyscenedetect'):
        raise ValueError(f'Unsupported reference detector: {detector}')
    if detector == 'ffmpeg':
        if content_threshold is not None or min_scene_seconds is not None:
            raise ValueError('--content-threshold and --min-scene-seconds are only valid with --detector pyscenedetect')
        threshold = 0.35 if threshold is None else core.number(threshold, 'threshold', 0.01, 1.0)
        psd_modules = None
    else:
        if threshold is not None:
            raise ValueError('--threshold is only valid with --detector ffmpeg; use --content-threshold with pyscenedetect')
        content_threshold = 27.0 if content_threshold is None else core.number(content_threshold, 'content_threshold', 0.1, 255.0)
        min_scene_seconds = 0.3 if min_scene_seconds is None else core.number(min_scene_seconds, 'min_scene_seconds', 0.04, 60.0)
        psd_modules = _load_pyscenedetect()

    target = Path(input_path).resolve()
    info = core.probe(target)
    if info['kind'] != 'video':
        raise ValueError('Reference extraction requires a local video file')
    core.number(start, 'start', 0, max(0.0, info['duration']))
    limit = info['duration'] - start
    span = limit if duration is None else core.number(duration, 'duration', 0.1, limit)
    max_frames = int(core.number(max_frames, 'max_frames', 2, 96))

    out = core.new_directory(out_dir)
    frames_dir = out / 'frames'
    frames_dir.mkdir()
    report = {
        'status': 'passed',
        'input': str(target),
        'sha256': file_hash.sha256(target),
        'duration': info['duration'],
        'start': start,
        'window': span,
    }
    if detector == 'pyscenedetect':
        report.update(_extract_reference_pyscenedetect(
            target, info, frames_dir,
            start=start, span=span,
            content_threshold=content_threshold,
            min_scene_seconds=min_scene_seconds,
            max_frames=max_frames,
            modules=psd_modules,
        ))
        cut_times = report.get('cuts', [])
    else:
        res = core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin', '-n',
            '-ss', str(start), '-t', str(span), '-i', str(target),
            '-vf', f"select='gt(scene,{threshold})+isnan(prev_selected_t)',showinfo",
            '-fps_mode', 'vfr', '-frames:v', str(max_frames), str(frames_dir / 'cut-%03d.png'),
        ], timeout=180)
        parsed_pts = [
            start + float(m.group(1))
            for m in re.finditer(r'pts_time:([0-9]+(?:\.[0-9]+)?)', (res.stderr or '') + (res.stdout or ''))
        ]
        cut_times = [t for t in parsed_pts if t - start > 0.04]
        extracted = sorted(str(p) for p in frames_dir.glob('*.png'))
        if not extracted:
            step = max(span / max_frames, 0.25)
            core.run([
                core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-ss', str(start), '-t', str(span), '-i', str(target),
                '-vf', f'fps=1/{step:.4f}', '-frames:v', str(max_frames), str(frames_dir / 'sample-%03d.png'),
            ], timeout=180)
            extracted = sorted(str(p) for p in frames_dir.glob('*.png'))
        report.update({
            'detector': 'ffmpeg',
            'detector_params': {'threshold': threshold},
            'frames': extracted,
        })

    report['editing_rhythm'] = _compute_editing_rhythm(report.get('frames', []), cut_times, span, start=start)
    core.write_json(out / 'reference.json', report)
    return report


def _normalize_single_video(src, dst, info, *, fps, width, height, codec, crf):
    if info['kind'] != 'video':
        raise ValueError('normalize-media requires a local video input')
    if dst.exists() or dst.is_symlink():
        raise ValueError(f'Output file already exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    fps = core.number(fps, 'fps', 1, 120)
    crf = int(core.number(crf, 'crf', 0, 51))
    vf_parts = [f'fps={fps}']
    if width is not None or height is not None:
        w = int(core.number(width or -2, 'width', -2, 7680))
        h = int(core.number(height or -2, 'height', -2, 4320))
        vf_parts.append(f'scale={w}:{h}:flags=lanczos')

    cmd = [
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-i', str(src), '-vf', ','.join(vf_parts),
    ]
    if codec == 'prores':
        cmd.extend(['-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le'])
    elif codec == 'h264':
        cmd.extend(['-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf), '-pix_fmt', 'yuv420p'])
    else:
        raise ValueError(f'Unsupported normalization codec: {codec}')

    if info.get('has_audio'):
        cmd.extend(['-c:a', 'pcm_s16le' if codec == 'prores' else 'aac', '-ar', '48000', '-ac', '2'])
        if codec == 'h264':
            cmd.extend(['-b:a', '192k'])
    else:
        cmd.append('-an')
    cmd.extend(['-movflags', '+faststart', str(dst)])
    core.run(cmd, timeout=600)

    # Verify strict decode on normalized output.
    core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(dst), '-f', 'null', '-'], timeout=180)
    out_info = core.probe(dst)
    return {
        'status': 'passed',
        'source': str(src),
        'output': str(dst),
        'sha256': file_hash.sha256(dst),
        'probe': out_info,
    }


def normalize_media(input_path, out_path, *, fps=30, width=None, height=None, codec='h264', crf=16, force=False, max_files=200):
    """Normalize single file or batch-scan a folder of VFR / Hi10P videos to CFR H.264/AAC or ProRes."""
    src = Path(input_path).resolve()
    if src.is_file():
        info = core.probe(src)
        return _normalize_single_video(
            src, Path(out_path).resolve(), info,
            fps=fps, width=width, height=height, codec=codec, crf=crf,
        )
    if not src.is_dir():
        raise ValueError(f'Input path not found: {src}')

    max_files = int(core.number(max_files, 'max_files', 1, 1000))
    out_dir = core.new_directory(out_path)
    out_ext = '.mov' if codec == 'prores' else '.mp4'
    candidates = []
    truncated = False
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            cand = Path(root) / name
            if cand.suffix.lower() in core.VIDEO_EXT:
                candidates.append(cand)
                if len(candidates) >= max_files:
                    truncated = True
                    break
        if truncated:
            break

    items, path_map, migrate_args = [], {}, []
    used_paths = set()
    normalized_count = 0
    skipped_count = 0
    for media_file in candidates:
        info = core.probe(media_file)
        if info.get('kind') != 'video':
            continue
        reasons = []
        if info.get('vfr'):
            reasons.append('variable_frame_rate')
        if core.is_filmcraft_hi10p(info):
            reasons.append('h264_10bit_hi10p_unsupported_by_filmcraft')
        if force:
            reasons.append('forced')
        if width is not None or height is not None:
            reasons.append('resize_requested')

        rel = media_file.relative_to(src)
        if not reasons:
            skipped_count += 1
            items.append({
                'source': str(media_file),
                'relative': rel.as_posix(),
                'action': 'skipped_compliant',
                'reasons': [],
                'output': str(media_file),
                'probe': info,
            })
            continue

        dst = (out_dir / rel).with_suffix(out_ext)
        suffix_idx = 1
        while dst in used_paths or dst.exists():
            dst = dst.with_name(f'{dst.stem}_{suffix_idx}{out_ext}')
            suffix_idx += 1
        used_paths.add(dst)

        norm_res = _normalize_single_video(
            media_file, dst, info,
            fps=fps, width=width, height=height, codec=codec, crf=crf,
        )
        normalized_count += 1
        path_map[str(media_file)] = norm_res['output']
        migrate_args.append(f'{media_file}={norm_res["output"]}')
        items.append({
            'source': str(media_file),
            'relative': rel.as_posix(),
            'action': 'normalized',
            'reasons': reasons,
            'output': norm_res['output'],
            'sha256': norm_res['sha256'],
            'probe': norm_res['probe'],
        })

    report = {
        'status': 'passed',
        'mode': 'batch',
        'input': str(src),
        'out': str(out_dir),
        'truncated': truncated,
        'summary': {
            'scanned': len(items),
            'normalized': normalized_count,
            'skipped_compliant': skipped_count,
        },
        'path_map': path_map,
        'migrate_args': migrate_args,
        'items': items,
    }
    core.write_json(out_dir / 'prepare-report.json', report)
    return report
