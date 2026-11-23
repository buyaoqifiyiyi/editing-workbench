"""End-to-end project audit: plan validation, source overlap check, strict decode, loudness QC, beat alignment, and contact sheet."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

import audio_master
import file_hash
import runtime_core as core


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


def _build_contact_sheet(export_path, v1_clips, total_dur, sheet_frames, out_path):
    dur = max(0.05, float(total_dur))
    raw_times = [min(max(0.0, float(c['start']) + min(0.05, float(c['duration']) * 0.25)), max(0.0, dur - 0.04)) for c in v1_clips]
    if len(raw_times) < sheet_frames:
        need = sheet_frames - len(raw_times)
        step = dur / float(need + 1)
        for i in range(1, need + 1):
            raw_times.append(round(min(max(0.0, dur - 0.04), i * step), 3))
    # Deduplicate close samples while keeping cut coverage priority
    picked = []
    for ts in sorted(raw_times):
        if not picked or abs(ts - picked[-1]) >= 0.03:
            picked.append(ts)
    if len(picked) > sheet_frames:
        idxs = [round(i * (len(picked) - 1) / (sheet_frames - 1)) for i in range(sheet_frames)]
        picked = [picked[i] for i in dict.fromkeys(idxs)]
    if not picked:
        picked = [0.0]

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
                im = raw_im.convert('RGB').resize((tile_w, tile_h), Image.Resampling.LANCZOS)
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

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    plan, probes, plan_warnings = core.validate_plan(plan_path)
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
    if args.contact_sheet and export_path is not None and export_probe and export_probe.get('has_video'):
        contact_sheet = _build_contact_sheet(
            export_path, v1_clips, export_probe.get('duration') or timeline_dur,
            sheet_frames, out_dir / 'qc_contact_sheet.jpg',
        )

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
