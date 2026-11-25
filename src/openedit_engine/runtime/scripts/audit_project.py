"""End-to-end project audit: plan validation, source overlap check, strict decode, loudness QC, beat alignment, and contact sheet."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import statistics
import struct
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import audio_master
import file_hash
import runtime_core as core
import runtime_paths

_TEXT_PARAM_KEYS = frozenset({
    'title', 'subtitle', 'text', 'label', 'sublabel', 'badge', 'period',
    'kpi_label', 'pill_text', 'note', 'rec_label', 'top_right', 'bottom_left',
    'top_code', 'bottom_code', 'left', 'right', 'value', 'display', 'header',
    'left_header', 'right_header',
})


def _parse_sfnt_cmap(data, offset, codepoints):
    if offset + 12 > len(data):
        return
    num_tables = struct.unpack_from('>H', data, offset + 4)[0]
    cmap_off = None
    cmap_len = 0
    for i in range(num_tables):
        rec_off = offset + 12 + i * 16
        if rec_off + 16 > len(data):
            break
        tag, _, tbl_off, tbl_len = struct.unpack_from('>4sIII', data, rec_off)
        if tag == b'cmap':
            cmap_off, cmap_len = tbl_off, tbl_len
            break
    if cmap_off is None or cmap_off + 4 > len(data) or cmap_off + cmap_len > len(data):
        return
    _, num_sub = struct.unpack_from('>HH', data, cmap_off)
    for i in range(num_sub):
        sub_rec = cmap_off + 4 + i * 8
        if sub_rec + 8 > len(data):
            break
        plat_id, enc_id, rel_off = struct.unpack_from('>HHI', data, sub_rec)
        if not ((plat_id == 3 and enc_id in (1, 10)) or (plat_id == 0 and enc_id in (3, 4))):
            continue
        sub_off = cmap_off + rel_off
        if sub_off + 4 > len(data):
            continue
        fmt = struct.unpack_from('>H', data, sub_off)[0]
        if fmt == 4 and sub_off + 14 <= len(data):
            seg_count_x2 = struct.unpack_from('>H', data, sub_off + 6)[0]
            seg_count = seg_count_x2 // 2
            end_base = sub_off + 14
            start_base = end_base + seg_count_x2 + 2
            if start_base + seg_count_x2 > len(data):
                continue
            for j in range(seg_count):
                e_cp = struct.unpack_from('>H', data, end_base + j * 2)[0]
                s_cp = struct.unpack_from('>H', data, start_base + j * 2)[0]
                if s_cp == 0xFFFF and e_cp == 0xFFFF:
                    continue
                if s_cp <= e_cp:
                    codepoints.update(range(s_cp, e_cp + 1))
        elif fmt == 12 and sub_off + 16 <= len(data):
            num_groups = struct.unpack_from('>I', data, sub_off + 12)[0]
            grp_base = sub_off + 16
            if grp_base + num_groups * 12 > len(data):
                continue
            for j in range(num_groups):
                s_cp, e_cp, _ = struct.unpack_from('>III', data, grp_base + j * 12)
                e_clamped = min(e_cp, 0x10FFFF)
                if s_cp <= e_clamped:
                    codepoints.update(range(s_cp, e_clamped + 1))


def _ttf_codepoints(font_path):
    """Extract supported Unicode codepoints from a local TTF/OTF/TTC cmap table via stdlib struct."""
    try:
        data = Path(font_path).read_bytes()
    except OSError:
        return None
    if len(data) < 12:
        return None
    codepoints = set()
    magic = data[:4]
    if magic == b'ttcf':
        num_fonts = struct.unpack_from('>I', data, 8)[0]
        for i in range(min(num_fonts, 32)):
            off_pos = 12 + i * 4
            if off_pos + 4 > len(data):
                break
            font_off = struct.unpack_from('>I', data, off_pos)[0]
            _parse_sfnt_cmap(data, font_off, codepoints)
    elif magic in (b'\x00\x01\x00\x00', b'OTTO', b'true'):
        _parse_sfnt_cmap(data, 0, codepoints)
    else:
        return None
    return codepoints if codepoints else None


def _iter_text_param_strings(obj, out_list):
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in _TEXT_PARAM_KEYS and isinstance(val, str) and val.strip():
                out_list.append(val)
            elif isinstance(val, (dict, list)):
                _iter_text_param_strings(val, out_list)
    elif isinstance(obj, list):
        for item in obj:
            _iter_text_param_strings(item, out_list)


def _collect_plan_texts(plan):
    collected = []
    for idx, cap in enumerate(plan.get('captions') or []):
        if isinstance(cap, dict) and isinstance(cap.get('text'), str) and cap['text'].strip():
            collected.append((f'captions[{idx}]', cap['text'], cap.get('font_path')))
    for idx, ov in enumerate(plan.get('overlays') or []):
        if not isinstance(ov, dict):
            continue
        ov_font = ov.get('font_path')
        param_texts = []
        if isinstance(ov.get('params'), dict):
            _iter_text_param_strings(ov['params'], param_texts)
        for text_val in param_texts:
            collected.append((f'overlays[{idx}].params', text_val, ov_font))
        for lidx, layer in enumerate(ov.get('layers') or []):
            if isinstance(layer, dict) and layer.get('type') == 'text' and isinstance(layer.get('text'), str) and layer['text'].strip():
                collected.append((f'overlays[{idx}].layers[{lidx}]', layer['text'], layer.get('font_path') or ov_font))
    return collected


def _check_font_glyphs(plan, extra_font_path=None):
    override_font = None
    if extra_font_path:
        override_font = Path(os.path.expanduser(os.path.expandvars(str(extra_font_path)))).resolve()
        if not override_font.is_file():
            raise ValueError(f'Font file does not exist: {override_font}')
    default_font = override_font or runtime_paths.discover_cjk_font_file()

    texts = _collect_plan_texts(plan)
    cmap_cache = {}
    checked_fonts = []
    missing = []

    def _get_cmap(fpath):
        key = str(Path(fpath).resolve())
        if key not in cmap_cache:
            cmap_cache[key] = _ttf_codepoints(key)
            if key not in checked_fonts:
                checked_fonts.append(key)
        return cmap_cache[key], key

    for source_label, text, item_font in texts:
        target_font = item_font or default_font
        if target_font is None or not Path(target_font).is_file():
            continue
        cps, font_key = _get_cmap(target_font)
        if cps is None:
            continue
        absent = []
        seen_ch = set()
        for ch in text:
            if ch.isspace() or ord(ch) < 32:
                continue
            if ord(ch) not in cps and ch not in seen_ch:
                seen_ch.add(ch)
                absent.append(f'{ch} (U+{ord(ch):04X})')
        if absent:
            missing.append({
                'font': font_key,
                'source': source_label,
                'missing_chars': absent[:16],
                'sample': text[:48],
            })

    return {
        'checked_fonts': checked_fonts,
        'checked_strings': len(texts),
        'missing': missing,
    }


def _analyze_visual_frame(img_rgb, ts):
    arr = np.asarray(img_rgb, dtype=np.float32)
    luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    h = luma.shape[0]
    band = max(1, h // 10)
    clip_ratio = float((luma >= 250.0).mean())
    black_ratio = float((luma <= 10.0).mean())
    luma_mean = float(luma.mean())
    luma_std = float(luma.std())

    sub = arr[::8, ::8, :].reshape(-1, 3) / 255.0
    lin = np.where(sub <= 0.04045, sub / 12.92, np.power((sub + 0.055) / 1.055, 2.4))
    l_v = np.cbrt(np.maximum(0.4122215 * lin[:, 0] + 0.5363325 * lin[:, 1] + 0.0514460 * lin[:, 2], 0.0))
    m_v = np.cbrt(np.maximum(0.2119035 * lin[:, 0] + 0.6806995 * lin[:, 1] + 0.1073970 * lin[:, 2], 0.0))
    s_v = np.cbrt(np.maximum(0.0883025 * lin[:, 0] + 0.2817188 * lin[:, 1] + 0.6299787 * lin[:, 2], 0.0))
    ok_l = float((0.2104543 * l_v + 0.7936178 * m_v - 0.0040720 * s_v).mean())
    ok_a = float((1.9779985 * l_v - 2.4285922 * m_v + 0.4505937 * s_v).mean())
    ok_b = float((0.0259040 * l_v + 0.7827718 * m_v - 0.8086758 * s_v).mean())

    return {
        'time': round(float(ts), 3),
        'highlight_clip_ratio': round(clip_ratio, 4),
        'near_black_ratio': round(black_ratio, 4),
        'luma_mean': round(luma_mean, 2),
        'luma_std': round(luma_std, 2),
        'oklab_mean': [round(ok_l, 4), round(ok_a, 4), round(ok_b, 4)],
        'top_band_luma': round(float(luma[:band, :].mean()), 2),
        'bottom_band_luma': round(float(luma[-band:, :].mean()), 2),
        'is_blowout': bool(clip_ratio >= 0.35),
        'is_black': bool(black_ratio >= 0.95),
        'is_flat': bool(luma_std < 6.0),
    }


def _analyze_pacing_qc(v1_clips):
    """Advisory editing rhythm & cadence telemetry for primary V1 timeline clips."""
    durs = [max(0.0, float(c['duration'])) for c in v1_clips]
    if not durs:
        return {
            'clip_count': 0,
            'asl_seconds': 0.0,
            'min_duration_s': 0.0,
            'max_duration_s': 0.0,
            'duration_cv': 0.0,
            'micro_cuts_under_0_25s': 0,
            'long_holds_over_6s': 0,
            'notes': [],
        }
    mean_d = sum(durs) / len(durs)
    var_d = sum((d - mean_d) ** 2 for d in durs) / len(durs)
    cv = (var_d ** 0.5) / mean_d if mean_d > 1e-6 else 0.0
    micro = sum(1 for d in durs if d < 0.25)
    holds = sum(1 for d in durs if d > 6.0)
    notes = []
    if len(durs) >= 6 and cv < 0.08:
        notes.append('uniform_metronome_cadence_consider_variation')
    if micro > 0:
        notes.append(f'{micro}_micro_cut_under_250ms')
    if holds > 0:
        notes.append(f'{holds}_long_hold_over_6s')
    return {
        'clip_count': len(durs),
        'asl_seconds': round(mean_d, 3),
        'min_duration_s': round(min(durs), 3),
        'max_duration_s': round(max(durs), 3),
        'duration_cv': round(cv, 3),
        'micro_cuts_under_0_25s': micro,
        'long_holds_over_6s': holds,
        'notes': notes,
    }


def _sample_export_times(v1_clips, total_dur, sheet_frames):
    dur = max(0.05, float(total_dur))
    raw_times = [min(max(0.0, float(c['start']) + min(0.05, float(c['duration']) * 0.25)), max(0.0, dur - 0.04)) for c in v1_clips]
    if len(raw_times) < sheet_frames:
        need = sheet_frames - len(raw_times)
        step = dur / float(need + 1)
        for i in range(1, need + 1):
            raw_times.append(round(min(max(0.0, dur - 0.04), i * step), 3))
    picked = []
    for ts in sorted(raw_times):
        if not picked or abs(ts - picked[-1]) >= 0.03:
            picked.append(ts)
    if len(picked) > sheet_frames:
        idxs = [round(i * (len(picked) - 1) / (sheet_frames - 1)) for i in range(sheet_frames)]
        picked = [picked[i] for i in dict.fromkeys(idxs)]
    return picked or [0.0]


def _strict_decode(path):
    try:
        core.run(
            [core.tool('ffmpeg'), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-i', str(path), '-f', 'null', '-'],
            timeout=300,
        )
        return 'passed', None
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        return 'failed', str(exc)


def _check_source_overlaps(plan, probes, overlap_tol):
    v1_clips = sorted(
        (c for c in plan.get('clips', []) if c.get('track', 'video') == 'video'),
        key=lambda c: float(c['start']),
    )
    used = defaultdict(list)
    overlaps = []
    for clip in v1_clips:
        p = str(Path(clip['path']).resolve())
        info = probes.get(clip['path']) or probes.get(p) or {}
        if info.get('kind') == 'image':
            continue
        s_in = float(clip.get('source_in', 0.0))
        spd = float(clip.get('speed', 1.0))
        s_out = s_in + float(clip['duration']) * spd
        for prev_in, prev_out, prev_start in used[p]:
            if max(s_in, prev_in) < min(s_out, prev_out) - overlap_tol:
                overlaps.append({
                    'path': p,
                    'name': Path(p).name,
                    'first': {'timeline_start': round(prev_start, 6), 'source_in': round(prev_in, 6), 'source_out': round(prev_out, 6)},
                    'second': {'timeline_start': round(float(clip['start']), 6), 'source_in': round(s_in, 6), 'source_out': round(s_out, 6)},
                })
        used[p].append((s_in, s_out, float(clip['start'])))
    return v1_clips, overlaps


def _check_rhythm_alignment(plan, v1_clips, beats_arg):
    import music_structure

    grid = music_structure.load_rhythm_grid(beats_arg)
    src_path = Path(grid['source_path']).resolve()
    audio_list = plan.get('audio') or []
    matched_audio = next(
        (a for a in audio_list if Path(a['path']).resolve() == src_path),
        audio_list[0] if audio_list else None,
    )
    if matched_audio is not None:
        a_start = float(matched_audio.get('start', 0.0))
        a_in = float(matched_audio.get('source_in', 0.0))
        a_spd = float(matched_audio.get('speed', 1.0))
        music_matched = Path(matched_audio['path']).resolve() == src_path
    else:
        a_start, a_in, a_spd, music_matched = 0.0, 0.0, 1.0, False

    raw_pts = set()
    for t in grid.get('beats', []):
        raw_pts.add(float(t))
    for t in grid.get('downbeats', []):
        raw_pts.add(float(t))
    for dp in grid.get('drop_points', []):
        if isinstance(dp, dict) and 'time' in dp:
            raw_pts.add(float(dp['time']))

    s0 = float(grid.get('start_seconds', 0.0))
    targets = sorted({
        round(a_start + (s0 + t - a_in) / max(1e-6, a_spd), 6)
        for t in raw_pts
        if a_start + (s0 + t - a_in) / max(1e-6, a_spd) >= -1e-6
    })
    if not targets:
        raise ValueError(f'No timeline rhythm targets could be mapped from: {beats_arg}')

    fps = float(plan['canvas']['fps'])
    frame_tol_ms = 1000.0 / max(1.0, fps) + 0.5
    cut_deltas = []
    for clip in v1_clips[1:]:
        st = float(clip['start'])
        nearest = min(targets, key=lambda t: abs(t - st))
        delta_ms = round(abs(st - nearest) * 1000.0, 2)
        cut_deltas.append({
            'cut_time': round(st, 6),
            'clip': Path(clip['path']).name,
            'nearest_target': nearest,
            'delta_ms': delta_ms,
        })
    deltas_only = [d['delta_ms'] for d in cut_deltas]
    return {
        'beats_kind': grid['kind'],
        'music_source_matched': music_matched,
        'target_count': len(targets),
        'v1_cuts_checked': len(cut_deltas),
        'frame_tolerance_ms': round(frame_tol_ms, 2),
        'median_delta_ms': round(statistics.median(deltas_only), 2) if deltas_only else 0.0,
        'max_delta_ms': round(max(deltas_only), 2) if deltas_only else 0.0,
        'within_frame_count': sum(1 for d in deltas_only if d <= frame_tol_ms),
        'cuts': cut_deltas,
    }


def _inspect_sampled_frames(export_path, v1_clips, total_dur, sheet_frames, tmp_dir):
    picked = _sample_export_times(v1_clips, total_dur, sheet_frames)
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = core.tool('ffmpeg')
    metrics = []
    try:
        for idx, ts in enumerate(picked):
            png = tmp_dir / f'f_{idx:03d}.png'
            pre = max(0.0, ts - 1.0)
            post = ts - pre
            core.run(
                [ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', f'{pre:.6f}', '-i', str(export_path),
                 '-ss', f'{post:.6f}', '-frames:v', '1', str(png)],
                timeout=60,
            )
            with Image.open(png) as raw_im:
                metrics.append(_analyze_visual_frame(raw_im.convert('RGB'), ts))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return picked, metrics


def _build_contact_sheet(export_path, v1_clips, total_dur, sheet_frames, out_path, metrics_out=None):
    picked = _sample_export_times(v1_clips, total_dur, sheet_frames)

    info = core.probe(export_path)
    src_w = max(1, int(info.get('width') or 1920))
    src_h = max(1, int(info.get('height') or 1080))
    cols = min(4, len(picked))
    rows = (len(picked) + cols - 1) // cols
    tile_w = 400
    tile_h = max(90, round(tile_w * src_h / float(src_w)))
    header_h, pad = 24, 6
    canvas = Image.new(
        'RGB',
        (cols * (tile_w + pad) + pad, rows * (tile_h + header_h + pad) + pad),
        (16, 18, 24),
    )
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 13)
    except Exception:
        font = ImageFont.load_default()

    tmp_dir = out_path.parent / '_tmp_audit_frames'
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg = core.tool('ffmpeg')
        for idx, ts in enumerate(picked):
            png = tmp_dir / f'f_{idx:03d}.png'
            pre = max(0.0, ts - 1.0)
            post = ts - pre
            core.run(
                [ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', f'{pre:.6f}', '-i', str(export_path),
                 '-ss', f'{post:.6f}', '-frames:v', '1', str(png)],
                timeout=60,
            )
            with Image.open(png) as raw_im:
                rgb_im = raw_im.convert('RGB')
                if metrics_out is not None:
                    metrics_out.append(_analyze_visual_frame(rgb_im, ts))
                im = rgb_im.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            r, c = divmod(idx, cols)
            x0 = pad + c * (tile_w + pad)
            y0 = pad + r * (tile_h + header_h + pad)
            canvas.paste(im, (x0, y0 + header_h))
            draw.rectangle([x0, y0 + header_h, x0 + tile_w - 1, y0 + header_h + tile_h - 1], outline=(65, 76, 96))
            draw.rectangle([x0, y0, x0 + tile_w - 1, y0 + header_h - 2], fill=(28, 32, 42))
            draw.text((x0 + 6, y0 + 4), f't={ts:.3f}s (#{idx + 1})', fill=(240, 245, 255), font=font)
        canvas.save(out_path, quality=90)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {'path': str(out_path), 'frames': len(picked), 'times': [round(t, 3) for t in picked]}


def _analyze_motion_qc(export_path, v1_clips, total_dur):
    """Sample 10fps grayscale energy via FFmpeg pipe to flag static frozen spans and cut momentum."""
    ffmpeg = core.tool('ffmpeg')
    cmd = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-i', str(export_path),
        '-vf', 'fps=10,scale=160:90,format=gray',
        '-f', 'rawvideo', '-pix_fmt', 'gray', '-',
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=180,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    frame_bytes = 160 * 90
    raw = proc.stdout or b''
    n_frames = len(raw) // frame_bytes
    if n_frames < 2:
        return {
            'sample_fps': 10.0,
            'sampled_frames': int(n_frames),
            'mean_motion_energy': 0.0,
            'p90_motion_energy': 0.0,
            'dynamic_ratio': 0.0,
            'frozen_segments': [],
            'cut_continuity': [],
            'advisory_notes': [],
        }

    frames = np.frombuffer(raw[:n_frames * frame_bytes], dtype=np.uint8).reshape(n_frames, 90, 160)
    diffs = np.mean(np.abs(frames[1:].astype(np.float32) - frames[:-1].astype(np.float32)), axis=(1, 2))
    static_eps = 0.45

    frozen = []
    run_start = None
    for idx, val in enumerate(diffs):
        if float(val) < static_eps:
            if run_start is None:
                run_start = idx
        elif run_start is not None:
            length = idx - run_start
            if length >= 8:
                frozen.append({
                    'start': round(run_start * 0.1, 2),
                    'end': round((idx + 1) * 0.1, 2),
                    'duration': round(length * 0.1, 2),
                    'mean_diff': round(float(np.mean(diffs[run_start:idx])), 3),
                })
            run_start = None
    if run_start is not None:
        length = len(diffs) - run_start
        if length >= 8:
            frozen.append({
                'start': round(run_start * 0.1, 2),
                'end': round((len(diffs) + 1) * 0.1, 2),
                'duration': round(length * 0.1, 2),
                'mean_diff': round(float(np.mean(diffs[run_start:])), 3),
            })

    dur = max(0.1, float(total_dur))
    cut_continuity = []
    for clip in v1_clips[1:]:
        t_cut = float(clip['start'])
        if not (0.2 <= t_cut <= max(0.2, dur - 0.25)):
            continue
        c_idx = int(round(t_cut * 10.0))
        pre_sl = diffs[max(0, c_idx - 3):max(0, c_idx - 1)]
        post_sl = diffs[min(len(diffs), c_idx + 1):min(len(diffs), c_idx + 3)]
        pre_e = round(float(np.mean(pre_sl)), 3) if pre_sl.size else 0.0
        post_e = round(float(np.mean(post_sl)), 3) if post_sl.size else 0.0
        jump_e = round(float(diffs[min(len(diffs) - 1, max(0, c_idx - 1))]), 3)
        if pre_e < static_eps and post_e < static_eps:
            flag = 'stagnant'
        elif abs(pre_e - post_e) > 2.5 and min(pre_e, post_e) < static_eps:
            flag = 'dead_stop'
        else:
            flag = 'ok'
        cut_continuity.append({
            'time': round(t_cut, 3),
            'pre_energy': pre_e,
            'post_energy': post_e,
            'cut_jump': jump_e,
            'flag': flag,
        })

    notes = []
    if frozen:
        notes.append(f'{len(frozen)}_frozen_segment_over_0_8s')
    if any(c['flag'] != 'ok' for c in cut_continuity):
        notes.append('low_motion_continuity_around_cuts')

    return {
        'sample_fps': 10.0,
        'sampled_frames': int(n_frames),
        'mean_motion_energy': round(float(np.mean(diffs)), 3),
        'p90_motion_energy': round(float(np.percentile(diffs, 90)), 3),
        'dynamic_ratio': round(float(np.mean(diffs >= static_eps)), 4),
        'frozen_segments': frozen,
        'cut_continuity': cut_continuity,
        'advisory_notes': notes,
    }


def _build_motion_review_artifacts(export_path, v1_clips, total_dur, out_dir, has_audio):
    """Render compact 360p/15fps motion_review.mp4 with timestamp HUD plus cut_strips.jpg burst sheet."""
    ffmpeg = core.tool('ffmpeg')
    review_mp4 = out_dir / 'motion_review.mp4'

    vf_parts = ['fps=15', 'scale=640:360:force_original_aspect_ratio=decrease', 'pad=640:360:(ow-iw)/2:(oh-ih)/2']
    font_file = Path('C:/Windows/Fonts/consola.ttf')
    if not font_file.is_file():
        font_file = runtime_paths.discover_cjk_font_file()
    if font_file is not None and Path(font_file).is_file():
        font_esc = str(Path(font_file).resolve()).replace('\\', '/').replace(':', '\\:')
        vf_parts.append(
            f"drawtext=fontfile='{font_esc}':text='%{{pts\\:hms}}':x=14:y=14:"
            f"fontsize=18:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=5"
        )

    cmd = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-i', str(export_path),
        '-vf', ','.join(vf_parts),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '28', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
    ]
    if has_audio:
        cmd.extend(['-c:a', 'aac', '-b:a', '96k'])
    else:
        cmd.append('-an')
    cmd.append(str(review_mp4))
    core.run(cmd, timeout=240)
    dec_status, dec_err = _strict_decode(review_mp4)
    if dec_status != 'passed':
        raise RuntimeError(f'motion_review.mp4 strict decode failed: {dec_err}')

    dur = max(0.08, float(total_dur))
    cut_times = [float(c['start']) for c in v1_clips[1:] if 0.04 < float(c['start']) < dur - 0.04]
    if not cut_times:
        cut_times = [round(dur * f, 3) for f in (0.25, 0.5, 0.75) if dur > 0.4] or [round(dur * 0.5, 3)]
    if len(cut_times) > 6:
        idxs = [round(i * (len(cut_times) - 1) / 5) for i in range(6)]
        cut_times = [cut_times[i] for i in dict.fromkeys(idxs)]

    info = core.probe(export_path)
    src_w = max(1, int(info.get('width') or 1920))
    src_h = max(1, int(info.get('height') or 1080))
    offsets = (-0.12, -0.03, 0.03, 0.12)
    tile_w = 320
    tile_h = max(80, round(tile_w * src_h / float(src_w)))
    header_h, pad = 26, 6
    canvas = Image.new(
        'RGB',
        (len(offsets) * (tile_w + pad) + pad, len(cut_times) * (tile_h + header_h + pad) + pad),
        (16, 18, 24),
    )
    draw = ImageDraw.Draw(canvas)
    try:
        pil_font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 13)
    except Exception:
        pil_font = ImageFont.load_default()

    strips_path = out_dir / 'cut_strips.jpg'
    tmp_dir = out_dir / '_tmp_cut_strips'
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for r_idx, ct in enumerate(cut_times):
            y0 = pad + r_idx * (tile_h + header_h + pad)
            for c_idx, off in enumerate(offsets):
                ts = max(0.0, min(max(0.0, dur - 0.04), ct + off))
                png = tmp_dir / f'c_{r_idx}_{c_idx}.png'
                pre = max(0.0, ts - 1.0)
                post = ts - pre
                core.run([
                    ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', f'{pre:.6f}', '-i', str(export_path),
                    '-ss', f'{post:.6f}', '-frames:v', '1', str(png),
                ], timeout=60)
                with Image.open(png) as raw_im:
                    tile = raw_im.convert('RGB').resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                x0 = pad + c_idx * (tile_w + pad)
                canvas.paste(tile, (x0, y0 + header_h))
                border = (244, 63, 94) if off in (-0.03, 0.03) else (65, 76, 96)
                draw.rectangle([x0, y0 + header_h, x0 + tile_w - 1, y0 + header_h + tile_h - 1], outline=border)
                draw.rectangle([x0, y0, x0 + tile_w - 1, y0 + header_h - 2], fill=(28, 32, 42))
                draw.text((x0 + 6, y0 + 5), f'cut={ct:.2f}s ({off * 1000:+.0f}ms)', fill=(240, 245, 255), font=pil_font)
        canvas.save(strips_path, quality=90)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        'video': {
            'path': str(review_mp4),
            'sha256': file_hash.sha256(review_mp4),
            'bytes': review_mp4.stat().st_size,
            'width': 640,
            'height': 360,
            'fps': 15.0,
            'strict_decode': dec_status,
        },
        'cut_strips': {
            'path': str(strips_path),
            'cuts': [round(t, 3) for t in cut_times],
            'offsets_ms': [round(o * 1000) for o in offsets],
        },
    }


def run_audit(args):
    plan_path = Path(os.path.expanduser(os.path.expandvars(str(args.plan)))).resolve()
    if not plan_path.is_file():
        raise ValueError(f'Plan file does not exist: {plan_path}')
    overlap_tol = core.number(args.overlap_tolerance, 'overlap_tolerance', 0.0, 1.0)
    sheet_frames = int(args.sheet_frames)
    if not 4 <= sheet_frames <= 120:
        raise ValueError(f'--sheet-frames must be in [4, 120], got {sheet_frames}')

    export_path = None
    if args.export:
        export_path = Path(os.path.expanduser(os.path.expandvars(str(args.export)))).resolve()
        if not export_path.is_file():
            raise ValueError(f'Export media file does not exist: {export_path}')

    build_dir = None
    if args.build_dir:
        build_dir = Path(os.path.expanduser(os.path.expandvars(str(args.build_dir)))).resolve()
        if not build_dir.is_dir():
            raise ValueError(f'Build directory does not exist: {build_dir}')

    if args.contact_sheet and export_path is None:
        raise ValueError('--contact-sheet requires --export <media_file>')
    if getattr(args, 'visual_qc', False) and export_path is None:
        raise ValueError('--visual-qc requires --export <media_file>')
    if getattr(args, 'motion_review', False) and export_path is None:
        raise ValueError('--motion-review requires --export <media_file>')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    plan, probes, plan_warnings = core.validate_plan(plan_path)
    font_glyphs = _check_font_glyphs(plan, extra_font_path=getattr(args, 'font_check', None))
    v1_clips, overlaps = _check_source_overlaps(plan, probes, overlap_tol)

    rhythm_alignment = None
    if args.beats:
        rhythm_alignment = _check_rhythm_alignment(plan, v1_clips, args.beats)

    strict_decode = {}
    attention_reasons = []
    export_probe = None
    audio_qc = None

    if export_path is not None:
        export_probe = core.probe(export_path)
        export_probe['sha256'] = file_hash.sha256(export_path)
        export_probe['bytes'] = export_path.stat().st_size
        dec_status, dec_err = _strict_decode(export_path)
        strict_decode['export'] = dec_status
        if dec_status != 'passed':
            attention_reasons.append(f'Export strict decode failed: {dec_err}')

        if export_probe.get('has_audio'):
            preset_cfg = audio_master.PRESETS[args.preset]
            pcm = audio_master.measure_pcm_stats(export_path)
            ebur = audio_master._measure_ebur128(
                export_path,
                i_target=preset_cfg['target_lufs'],
                tp_target=preset_cfg['true_peak'],
                lra_target=preset_cfg['lra'],
            )
            before_i = ebur['integrated_lufs']
            tp = ebur['true_peak_dbtp']
            clipped = pcm['full_scale_clipped_samples']
            ultra_quiet = before_i is None or before_i < -70.0
            i_ok = (not ultra_quiet) and abs(before_i - preset_cfg['target_lufs']) <= 1.5
            tp_ok = tp is not None and tp <= preset_cfg['true_peak'] + 0.3
            clip_ok = clipped == 0
            qc_warnings = []
            if ultra_quiet:
                qc_warnings.append('Exported audio integrated loudness is below -70 LUFS (near-silent).')
            elif not i_ok:
                qc_warnings.append(f'Loudness {before_i:+.2f} LUFS deviates >1.5 LU from {args.preset} target ({preset_cfg["target_lufs"]:+.1f}).')
            if not tp_ok:
                qc_warnings.append(f'True peak {tp} dBTP exceeds limit {preset_cfg["true_peak"]}.')
            if not clip_ok:
                qc_warnings.append(f'Detected {clipped} full-scale clipped PCM samples.')
            audio_qc = {
                'preset': args.preset,
                'targets': preset_cfg,
                'integrated_lufs': before_i,
                'true_peak_dbtp': tp,
                'lra_lu': ebur['lra_lu'],
                'pcm': pcm,
                'pass_qc': bool(i_ok and tp_ok and clip_ok),
                'warnings': qc_warnings,
            }
            if not audio_qc['pass_qc']:
                attention_reasons.extend(qc_warnings)

    if build_dir is not None:
        ov_root = build_dir / 'overlays'
        if ov_root.is_dir():
            for mov in sorted(ov_root.glob('*/*.mov')):
                rel_key = mov.relative_to(build_dir).as_posix()
                dec_status, dec_err = _strict_decode(mov)
                strict_decode[rel_key] = dec_status
                if dec_status != 'passed':
                    attention_reasons.append(f'Overlay {rel_key} strict decode failed: {dec_err}')

    if overlaps:
        attention_reasons.append(f'Detected {len(overlaps)} duplicate source interval overlap(s) on primary video track.')
    if rhythm_alignment and rhythm_alignment['v1_cuts_checked'] > 0:
        r_tol = max(25.0, rhythm_alignment['frame_tolerance_ms'])
        if rhythm_alignment['median_delta_ms'] > r_tol:
            attention_reasons.append(
                f'Median V1 cut delta ({rhythm_alignment["median_delta_ms"]} ms) exceeds tolerance ({r_tol:.1f} ms).'
            )

    spans = (
        [float(c['start']) + float(c['duration']) for c in plan.get('clips', [])]
        + [float(a['start']) + float(a['duration']) for a in plan.get('audio', [])]
        + [float(o['start']) + float(o['duration']) for o in plan.get('overlays', [])]
    )
    timeline_dur = round(max([0.0] + spans), 4)
    plan_stats = {
        'duration_s': timeline_dur,
        'canvas': plan['canvas'],
        'total_clips': len(plan.get('clips', [])),
        'v1_cuts': len(v1_clips),
        'audio_items': len(plan.get('audio', [])),
        'overlays': len(plan.get('overlays', [])),
        'composites': len(plan.get('composites', [])),
        'captions': len(plan.get('captions', [])),
        'lut_graded_clips': sum(1 for c in plan.get('clips', []) if isinstance(c.get('grading'), dict) and ('lut' in c['grading'] or 'look' in c['grading'])),
        'variable_speed_clips': sum(1 for c in plan.get('clips', []) if abs(float(c.get('speed', 1.0)) - 1.0) > 1e-4),
        'beat_marks': sum(len(a.get('beat_marks', [])) for a in plan.get('audio', [])),
    }

    out_dir = core.new_directory(out_cand)
    contact_sheet = None
    motion_qc = None
    motion_review = None
    frame_metrics = []
    if export_path is not None and export_probe and export_probe.get('has_video'):
        eff_dur = export_probe.get('duration') or timeline_dur
        motion_qc = _analyze_motion_qc(export_path, v1_clips, eff_dur)
        if args.contact_sheet:
            contact_sheet = _build_contact_sheet(
                export_path, v1_clips, eff_dur,
                sheet_frames, out_dir / 'qc_contact_sheet.jpg', metrics_out=frame_metrics,
            )
        elif getattr(args, 'visual_qc', False):
            _, frame_metrics = _inspect_sampled_frames(
                export_path, v1_clips, eff_dur,
                sheet_frames, out_dir / '_tmp_visual_qc',
            )
        if getattr(args, 'motion_review', False):
            motion_review = _build_motion_review_artifacts(
                export_path, v1_clips, eff_dur, out_dir, bool(export_probe.get('has_audio')),
            )

    blowout_frames = [m for m in frame_metrics if m['is_blowout']]
    black_frames = [m for m in frame_metrics if m['is_black']]
    flat_frames = [m for m in frame_metrics if m['is_flat'] and not m['is_black']]
    vqc_warnings = []
    if blowout_frames:
        times_str = ', '.join(f'{m["time"]:.2f}s({m["highlight_clip_ratio"]:.0%})' for m in blowout_frames[:4])
        vqc_warnings.append(f'Highlight clipping blowout detected at {times_str}.')
    if black_frames:
        times_str = ', '.join(f'{m["time"]:.2f}s' for m in black_frames[:4])
        vqc_warnings.append(f'Near-black frame detected at {times_str}.')
    if font_glyphs['missing']:
        first_m = font_glyphs['missing'][0]
        vqc_warnings.append(f'Font missing glyphs in {first_m["source"]}: {", ".join(first_m["missing_chars"])}.')
    attention_reasons.extend(vqc_warnings)

    oklab_deltas = []
    for fa, fb in zip(frame_metrics, frame_metrics[1:]):
        la, aa, ba = fa['oklab_mean']
        lb, ab, bb = fb['oklab_mean']
        oklab_deltas.append(round(((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2) ** 0.5, 4))

    visual_qc = {
        'sampled_frames': len(frame_metrics),
        'max_highlight_clip_ratio': round(max((m['highlight_clip_ratio'] for m in frame_metrics), default=0.0), 4),
        'max_near_black_ratio': round(max((m['near_black_ratio'] for m in frame_metrics), default=0.0), 4),
        'max_adjacent_oklab_delta': round(max(oklab_deltas, default=0.0), 4),
        'blowout_frames': blowout_frames,
        'black_frames': black_frames,
        'flat_frames': flat_frames,
        'font_glyphs': font_glyphs,
        'pass_qc': not (blowout_frames or black_frames or font_glyphs['missing']),
        'warnings': vqc_warnings,
    }
    pacing_qc = _analyze_pacing_qc(v1_clips)

    report = {
        'status': 'attention' if attention_reasons else 'passed',
        'operation': 'audit',
        'plan': str(plan_path),
        'plan_warnings': plan_warnings,
        'plan_stats': plan_stats,
        'source_overlaps_count': len(overlaps),
        'source_overlaps': overlaps,
        'export_probe': export_probe,
        'strict_decode': strict_decode,
        'audio_qc': audio_qc,
        'visual_qc': visual_qc,
        'pacing_qc': pacing_qc,
        'motion_qc': motion_qc,
        'motion_review': motion_review,
        'rhythm_alignment': rhythm_alignment,
        'contact_sheet': contact_sheet,
        'attention_reasons': attention_reasons,
        'continuous_audiovisual_review': 'unverified',
    }
    core.write_json(out_dir / 'audit-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--plan', required=True, help='Path to validated edit-plan.json')
    parser.add_argument('--out', required=True, help='New output directory for audit-report.json and optional contact sheet')
    parser.add_argument('--export', default=None, help='Optional exported .mp4/.mov path for strict decode, loudness QC, and contact sheet')
    parser.add_argument('--build-dir', default=None, help='Optional build output directory to verify all rendered overlays/*.mov')
    parser.add_argument('--beats', default=None, help='Optional music-structure (structure.json or dir) or beats dir for cut alignment check')
    parser.add_argument('--preset', choices=tuple(audio_master.PRESETS), default='streaming', help='Audio loudness target preset (default streaming)')
    parser.add_argument('--contact-sheet', action='store_true', help='Generate qc_contact_sheet.jpg from cut points and uniform frames (requires --export)')
    parser.add_argument('--visual-qc', action='store_true', help='Inspect sampled export frames for highlight blowout/black frames (also runs with --contact-sheet)')
    parser.add_argument('--motion-review', action='store_true', help='Render compact 360p/15fps motion_review.mp4 with timestamp HUD and cut_strips.jpg (requires --export)')
    parser.add_argument('--font-check', default=None, help='Optional TTF/OTF/TTC font path to check caption/overlay glyph coverage')
    parser.add_argument('--sheet-frames', type=int, default=16, help='Target frames in [4, 120] for contact sheet (default 16)')
    parser.add_argument('--overlap-tolerance', type=float, default=0.01, help='Tolerance in seconds for source interval overlap check (default 0.01)')
    args = parser.parse_args(argv)
    try:
        report = run_audit(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
