"""Self-review a cut against its own music when no reference video exists: cut density vs music energy, cuts on beat vs chance, drop hits and the ending (advisory, no score, no pass/fail)."""
import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import file_hash
import music_structure
import runtime_core as core
import style_compare

PEAK_WINDOW_S = 4.0
DROP_TOLERANCE_S = 0.1
MIN_SEGMENT_S = 1.0


def _music_grid(args, video, info, out):
    """Return the music grid mapped onto the cut's timeline (seconds from video start)."""
    if args.structure:
        grid = music_structure.load_rhythm_grid(args.structure)
        offset = float(args.music_offset or 0.0)
        source = {'kind': 'existing_structure', 'file': grid.get('structure_file') or str(args.structure), 'timeline_offset_s': offset}
    else:
        if not info.get('has_audio'):
            raise ValueError('Video has no audio stream; run music-structure on the BGM and pass --structure')
        report = music_structure.run_music_structure(argparse.Namespace(
            input=str(video), out=str(out / 'music'), backend=args.backend, start=0.0, duration=None, gpu=False))
        grid = music_structure.load_rhythm_grid(out / 'music')
        offset = 0.0
        source = {'kind': 'analyzed_from_cut_audio', 'file': str(out / 'music' / 'structure.json'), 'backend': report['backend']}
    span = float(info['duration'])

    def shift(times):
        return sorted(round(offset + float(t), 4) for t in times if 0.0 <= offset + float(t) <= span)

    segments = []
    for s in grid.get('segments', []):
        a, b = max(0.0, offset + float(s['start'])), min(span, offset + float(s['end']))
        if b - a > 1e-3:
            segments.append({'label': s['label'], 'start': round(a, 3), 'end': round(b, 3), 'energy_db': float(s['energy_db'])})
    drops = [{'time': round(offset + float(d['time']), 3), 'type': d.get('type', 'drop')}
             for d in grid.get('drop_points', []) if isinstance(d, dict) and 0.0 < offset + float(d['time']) < span]
    return {
        'source': source,
        'bpm': grid.get('bpm'),
        'beats': shift(grid.get('beats', [])),
        'downbeats': shift(grid.get('downbeats', [])),
        'boundaries': [t for t in shift(grid.get('boundaries', [])) if 0.0 < t < span],
        'segments': segments,
        'drops': drops,
    }


def _nearest(t, targets):
    return min(abs(x - t) for x in targets) if targets else None


PLAN_MATCH_S = 0.1


def _plan_cuts(plan_path, track, span):
    """Shot boundaries on the plan's main video track (timeline seconds), including dissolves/flash-covered cuts."""
    path = Path(plan_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'Plan does not exist: {path}')
    plan = json.loads(path.read_text(encoding='utf-8'))
    track = track or plan.get('main_track') or 'video'
    clips = [c for c in plan.get('clips', []) if isinstance(c, dict) and c.get('track', 'video') == track]
    if not clips:
        raise ValueError(f'Plan has no clips on track {track!r}; pass --plan-track')
    track_end = max(float(c['start']) + float(c['duration']) for c in clips)
    plan_end = max(float(c['start']) + float(c['duration']) for c in plan.get('clips', []) if isinstance(c, dict))
    if abs(plan_end - span) > 0.5:
        raise ValueError(f'Plan clips end at {plan_end:.3f}s but the video is {span:.3f}s; is this the plan that exported this cut?')
    cuts, transitions = [], 0
    bounds = [(float(c['start']), bool(c.get('transition'))) for c in sorted(clips, key=lambda c: float(c['start']))]
    bounds.append((track_end, False))  # main track handing over to an end card / other track is a cut too
    for t, has_transition in bounds:
        t = round(t, 3)
        if 0.05 < t < span - 0.05 and (not cuts or t - cuts[-1] >= style_compare.MIN_CUT_GAP):
            cuts.append(t)
            transitions += 1 if has_transition else 0
    return {'file': str(path), 'sha256': file_hash.sha256(path), 'track': track, 'cuts': cuts, 'with_transition': transitions}


def _merge_cuts(plan_cuts, detected):
    """Plan boundaries are authoritative; detected cuts far from any plan boundary are in-shot cuts (source edits, PiP changes)."""
    extra = [d for d in detected if _nearest(d, plan_cuts) is None or _nearest(d, plan_cuts) > PLAN_MATCH_S]
    matched = sum(1 for p in plan_cuts if detected and _nearest(p, detected) <= PLAN_MATCH_S)
    merged = sorted(set(plan_cuts) | set(extra))
    return merged, {'plan_boundaries': len(plan_cuts), 'plan_boundaries_seen_by_detector': matched,
                    'detected_inside_shots': len(extra), 'total': len(merged)}


def _beat_fit(cuts, beats, downbeats, span, tol):
    def ratio(targets):
        if not cuts or not targets:
            return None, None
        hit = sum(1 for c in cuts if _nearest(c, targets) <= tol)
        # A cut placed at random lands within ±tol of some target with roughly this probability.
        chance = min(1.0, 2.0 * tol * len(targets) / max(span, 1e-6))
        return round(hit / len(cuts), 3), round(chance, 3)

    on_beat, beat_chance = ratio(beats)
    on_down, down_chance = ratio(downbeats)
    off = [{'cut': c, 'nearest_beat_delta_s': round(_nearest(c, beats), 3)} for c in cuts if beats and _nearest(c, beats) > tol]
    entry = {
        'tolerance_s': tol,
        'cuts_on_beat_ratio': on_beat, 'random_cut_expected_ratio': beat_chance,
        'cuts_on_downbeat_ratio': on_down, 'random_cut_expected_downbeat_ratio': down_chance,
        'off_beat_cuts': off,
    }
    if on_beat is not None:
        if on_beat < beat_chance * 1.5:
            entry['observation'] = (f'切点落在拍上的比例 {on_beat:.0%}，随便乱切也大约有 {beat_chance:.0%}，'
                                    f'看不出在卡拍；如果本意是卡点，优先检查 off_beat_cuts 里的 {len(off)} 个切点')
        else:
            entry['observation'] = (f'切点落在拍上的比例 {on_beat:.0%}，明显高于随机水平（约 {beat_chance:.0%}）；'
                                    '这只说明切点位置，不说明镜头之间怎样衔接，衔接看 audit 的 connection_qc')
    return entry


def _density_fit(cuts, segments, span):
    rows = []
    for s in segments:
        a, b = s['start'], s['end']
        if b - a < MIN_SEGMENT_S:
            continue
        n = sum(1 for c in cuts if a <= c < b)
        rows.append({**s, 'energy_db': round(s['energy_db'], 2), 'cuts': n, 'cuts_per_minute': round(n * 60.0 / (b - a), 2)})
    entry = {'segments': rows}
    if not rows:
        entry['status'] = 'skipped'
        entry['reason'] = 'no music segments of at least 1 s'
        return entry
    corr = None
    e, d = [r['energy_db'] for r in rows], [r['cuts_per_minute'] for r in rows]
    if len(rows) >= 3 and len(set(e)) > 1 and len(set(d)) > 1:
        corr = round(float(np.corrcoef(e, d)[0, 1]), 3)
    entry['energy_density_correlation'] = corr
    music_peak = max(rows, key=lambda r: r['energy_db'])
    entry['music_peak'] = {'start': music_peak['start'], 'end': music_peak['end'], 'label': music_peak['label']}
    visual_peak = None
    if span >= PEAK_WINDOW_S and cuts:
        best = max(np.arange(0.0, span - PEAK_WINDOW_S + 1e-6, 0.25),
                   key=lambda s0: sum(1 for c in cuts if s0 <= c < s0 + PEAK_WINDOW_S))
        count = sum(1 for c in cuts if best <= c < best + PEAK_WINDOW_S)
        visual_peak = {'start': round(float(best), 2), 'end': round(float(best) + PEAK_WINDOW_S, 2), 'cuts': count}
    entry['densest_cut_window'] = visual_peak
    notes = []
    if visual_peak and (visual_peak['end'] <= music_peak['start'] or visual_peak['start'] >= music_peak['end']):
        notes.append(f"音乐最响的段落在 {music_peak['start']}–{music_peak['end']} 秒，切得最密的地方在 "
                     f"{visual_peak['start']}–{visual_peak['end']} 秒，两处没重合；如果高潮本意是一次揭示或停留，可忽略")
    if corr is not None and corr < 0:
        notes.append(f'音乐越响的段落反而切得越疏（相关系数 {corr}），确认是否有意反差')
    if notes:
        entry['observation'] = '；'.join(notes)
    return entry


def _drop_fit(cuts, drops):
    rows = []
    for d in drops:
        delta = _nearest(d['time'], cuts)
        rows.append({**d, 'nearest_cut_delta_s': None if delta is None else round(delta, 3),
                     'cut_near': delta is not None and delta <= DROP_TOLERANCE_S})
    entry = {'tolerance_s': DROP_TOLERANCE_S, 'drops': rows}
    missed = [r['time'] for r in rows if not r['cut_near']]
    if missed:
        entry['observation'] = (f"{len(missed)}/{len(rows)} 个音乐爆点附近 {DROP_TOLERANCE_S} 秒内没有切点："
                                f"{', '.join(f'{t}s' for t in missed)}；可以是有意不切，但要确认画面动作或特效在接这个点")
    return entry


def _quarter_energy(segments, span):
    if not segments:
        return None
    out = []
    for q in range(4):
        a, b = span * q / 4, span * (q + 1) / 4
        weight = total = 0.0
        for s in segments:
            w = max(0.0, min(b, s['end']) - max(a, s['start']))
            weight += w
            total += w * s['energy_db']
        out.append(round(total / weight, 2) if weight > 0 else None)
    return out if all(v is not None for v in out) else None


def _ending_fit(cuts, grid, span, motion):
    q_cpm = [round(sum(1 for c in cuts if span * q / 4 <= c < span * (q + 1) / 4) * 60.0 / (span / 4), 2) for q in range(4)]
    q_energy = _quarter_energy(grid['segments'], span)
    last_shot = round(span - (cuts[-1] if cuts else 0.0), 3)
    shots = [b - a for a, b in zip([0.0] + cuts, cuts + [span])]
    marks = sorted(set(grid['downbeats']) | set(grid['boundaries']))
    end_gap = round(min(abs(span - t) for t in marks), 3) if marks else None
    entry = {
        'quarter_cuts_per_minute': q_cpm,
        'quarter_music_energy_db': q_energy,
        'quarter_motion_speed': motion.get('quarter_speed') if motion.get('status') == 'measured' else None,
        'last_shot_s': last_shot,
        'median_shot_s': round(statistics.median(shots), 3) if shots else None,
        'end_to_nearest_bar_or_section_s': end_gap,
    }
    notes = []
    music_holds = q_energy is not None and q_energy[3] >= statistics.mean(q_energy[:3]) - 1.5
    cuts_fade = statistics.mean(q_cpm[:3]) > 0 and q_cpm[3] < 0.6 * statistics.mean(q_cpm[:3])
    motion_q = entry['quarter_motion_speed']
    motion_fade = motion_q is not None and statistics.mean(motion_q[:3]) > 0 and motion_q[3] < 0.7 * statistics.mean(motion_q[:3])
    if music_holds and (cuts_fade or motion_fade):
        what = '和'.join(x for x, on in (('切换', cuts_fade), ('动作', motion_fade)) if on)
        notes.append(f'音乐在最后四分之一还在高位，画面{what}却明显变少，结尾可能显得泄气')
    last_label = grid['segments'][-1]['label'] if grid['segments'] else None
    if end_gap is not None and end_gap > 0.25 and last_label != 'outro':
        notes.append(f'成片结束点离最近的小节/乐段边界还有 {end_gap} 秒，音乐可能被拦腰截断')
    if notes:
        entry['observation'] = '；'.join(notes)
    return entry


_LABEL_COLORS = {'intro': (70, 90, 120), 'verse': (80, 120, 170), 'buildup': (170, 140, 70),
                 'chorus': (210, 110, 80), 'outro': (90, 90, 110)}


def _chart(report, out_path):
    W, pad = 1280, 20
    img = Image.new('RGB', (W, 320), (16, 18, 24))
    draw = ImageDraw.Draw(img)
    font, small = style_compare._font(15), style_compare._font(11)
    span = max(1e-6, report['window_s'])
    grid, cuts = report['music'], report['cuts']
    x0, x1 = pad, W - pad

    def x(t):
        return x0 + (x1 - x0) * t / span

    b = report['beat_fit']
    head = f"MUSIC FIT  window={span:.1f}s  cuts={len(cuts)}  bpm={grid.get('bpm')}"
    if b.get('cuts_on_beat_ratio') is not None:
        head += f"  on-beat={b['cuts_on_beat_ratio']:.0%} (random~{b['random_cut_expected_ratio']:.0%})"
    draw.text((pad, pad), head, fill=(240, 245, 255), font=font)
    top, h = pad + 34, 90
    energies = [s['energy_db'] for s in grid['segments']] or [0.0]
    lo, hi = min(energies), max(energies)
    for s in grid['segments']:
        frac = 1.0 if hi - lo < 1e-6 else 0.25 + 0.75 * (s['energy_db'] - lo) / (hi - lo)
        draw.rectangle([x(s['start']), top + h * (1 - frac), max(x(s['start']) + 1, x(s['end']) - 1), top + h],
                       fill=_LABEL_COLORS.get(s['label'], (100, 100, 100)))
    peak = report['density_fit'].get('densest_cut_window')
    if peak:
        draw.rectangle([x(peak['start']), top - 4, x(peak['end']), top + h + 4], outline=(94, 196, 160), width=2)
    for c in cuts:
        draw.line([x(c), top, x(c), top + h], fill=(235, 238, 245), width=1)
    for t in grid['beats']:
        draw.line([x(t), top + h + 4, x(t), top + h + 10], fill=(240, 200, 80))
    for t in grid['downbeats']:
        draw.line([x(t), top + h + 4, x(t), top + h + 16], fill=(255, 225, 120), width=2)
    for d in report['drop_fit']['drops']:
        cx = x(d['time'])
        draw.polygon([(cx - 6, top - 14), (cx + 6, top - 14), (cx, top - 4)], fill=(94, 196, 160) if d['cut_near'] else (235, 80, 80))
    draw.text((x0, top + h + 20), 'bars = music sections (height = loudness, orange = chorus)   white = cuts   '
              'yellow = beats/downbeats   triangles = drops (red = no cut nearby)   green box = densest 4 s of cuts',
              fill=(150, 158, 172), font=small)
    e = report['ending_fit']
    by, bw = top + h + 46, (W - pad * 4) // 3
    style_compare._bars(draw, pad, by, bw, 100, e['quarter_cuts_per_minute'], max(e['quarter_cuts_per_minute']) or 1.0,
                        (94, 196, 160), 'cuts/min by quarter', small, [f'Q{q + 1}' for q in range(4)])
    if e['quarter_music_energy_db']:
        qe = e['quarter_music_energy_db']
        base = min(qe) - 3.0
        style_compare._bars(draw, pad * 2 + bw, by, bw, 100, [v - base for v in qe], max(v - base for v in qe) or 1.0,
                            (210, 110, 80), 'music loudness by quarter (relative)', small, [f'Q{q + 1}' for q in range(4)])
    if e['quarter_motion_speed']:
        qm = e['quarter_motion_speed']
        style_compare._bars(draw, pad * 3 + bw * 2, by, bw, 100, qm, max(qm) or 1.0, (236, 120, 96),
                            'motion by quarter', small, [f'Q{q + 1}' for q in range(4)])
    img.save(out_path, quality=92)
    return str(out_path)


def _markdown(report):
    cs = report['cut_source']
    if cs['kind'] == 'plan_plus_detector':
        cut_line = (f"- 切点来源：工程主轨 {cs['plan_boundaries']} 个镜头边界（其中 {cs['plan_boundaries_with_transition']} 个带转场，"
                    f"画面检测只认出 {cs['plan_boundaries_seen_by_detector']} 个）+ 镜头内切换 {cs['detected_inside_shots']} 个，共 {cs['total']} 个")
    else:
        cut_line = f"- 切点来源：仅画面检测，共 {cs['total']} 个（叠化、闪白下的切点可能漏掉；有工程文件时加 --plan）"
    lines = ['# 音乐自审（无参考片）', '', f"- 成片：`{report['input']}`", f"- 音乐来源：{report['music']['source']['kind']}", cut_line, '',
             '## 值得看的地方', '']
    noted = [(label, report[key]['observation']) for key, label in
             (('beat_fit', '卡拍'), ('density_fit', '切点密度与音乐起伏'), ('drop_fit', '音乐爆点'), ('ending_fit', '结尾'))
             if report[key].get('observation')]
    lines += [f'- {label}：{text}' for label, text in noted] or ['- 没有发现明显和音乐脱节的地方。']
    if report.get('connection_hint'):
        lines.append(f"- 镜头衔接：{report['connection_hint']}")
    off = report['beat_fit'].get('off_beat_cuts') or []
    if off:
        lines += ['', '## 没落在拍上的切点', '', '| 切点（秒） | 离最近的拍（秒） |', '|---|---|']
        lines += [f"| {o['cut']} | {o['nearest_beat_delta_s']} |" for o in off]
    rows = report['density_fit'].get('segments') or []
    if rows:
        lines += ['', '## 各乐段切点密度', '', '| 乐段 | 时间（秒） | 响度 dB | 切点数 | 次/分 |', '|---|---|---|---|---|']
        lines += [f"| {r['label']} | {r['start']}–{r['end']} | {r['energy_db']} | {r['cuts']} | {r['cuts_per_minute']} |" for r in rows]
    lines += ['', '## 边界', ''] + [f'- {b}' for b in report['limitations']]
    return '\n'.join(lines) + '\n'


def run(args):
    threshold = core.number(args.threshold, 'threshold', 0.01, 1.0)
    tol = core.number(args.tolerance, 'tolerance', 0.01, 0.25)
    out = Path(args.out).resolve()
    if out.exists() or out.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')
    video = Path(args.input).expanduser().resolve()
    if not video.is_file():
        raise ValueError(f'Input video does not exist: {video}')
    info = core.probe(video)
    if info['kind'] != 'video':
        raise ValueError(f'Music fit requires a local video: {video}')
    span = round(float(info['duration']), 3)
    if span < 2.0:
        raise ValueError('Video is shorter than 2 s')
    before = file_hash.sha256(video)
    plan_info = _plan_cuts(args.plan, args.plan_track, span) if args.plan else None  # reject a wrong plan before creating output
    out = core.new_directory(out)
    grid = _music_grid(args, video, info, out)
    detected = style_compare._detect_cuts(video, 0.0, span, threshold)
    if plan_info:
        cuts, counts = _merge_cuts(plan_info['cuts'], detected)
        cut_source = {'kind': 'plan_plus_detector', 'plan': plan_info['file'], 'plan_sha256': plan_info['sha256'],
                      'track': plan_info['track'], 'plan_boundaries_with_transition': plan_info['with_transition'], **counts}
    else:
        cuts = detected
        cut_source = {'kind': 'detector_only', 'total': len(cuts)}
    motion = {'status': 'skipped', 'reason': 'disabled'} if args.skip_motion else style_compare._motion(video, 0.0, span)
    if file_hash.sha256(video) != before:
        raise ValueError('Input changed during analysis; reject output')
    if plan_info and file_hash.sha256(plan_info['file']) != plan_info['sha256']:
        raise ValueError('Plan changed during analysis; reject output')
    report = {
        'status': 'measured_for_review',
        'operation': 'music-fit',
        'input': str(video),
        'sha256': before,
        'window_s': span,
        'detector': {'name': 'ffmpeg scene', 'threshold': threshold, 'min_cut_gap_s': style_compare.MIN_CUT_GAP},
        'cut_source': cut_source,
        'detected_cuts': detected,
        'music': grid,
        'cuts': cuts,
        'beat_fit': _beat_fit(cuts, grid['beats'], grid['downbeats'], span, tol),
        'density_fit': _density_fit(cuts, grid['segments'], span),
        'drop_fit': _drop_fit(cuts, grid['drops']),
        'ending_fit': _ending_fit(cuts, grid, span, motion),
        'motion': motion,
        'aesthetic_judgment': 'not_performed',
        'human_watching': 'not_performed',
        'limitations': [
            '这是“画面和音乐配不配”的自查提示，不是好坏评分；高潮可以用停留或一次揭示处理，不必每处都快切或卡拍。',
            '默认从成片自带的混音里分析音乐，原声、对白和音效会干扰乐段与爆点判断；有干净 BGM 时用 music-structure 分析后传 --structure。',
            ('切点以 --plan 工程主轨的镜头边界为准（叠化、闪白盖住的切点也算），再补上画面检测到的镜头内切换；工程必须就是导出这条成片的那份，否则结论无效。'
             if plan_info else
             'FFmpeg 场景检测只给候选硬切，闪白、叠化、相近画面之间的切换会漏检，快速运镜可能误检；有工程文件时传 --plan 读取真实切点。'),
            '没有切点的地方也可能靠画面动作或特效在接拍。',
            '拍点、乐段和爆点是离线候选检测，可能落在半拍/倍拍；未试听不代表卡点听感。',
        ],
    }
    if plan_info and len(plan_info['cuts']) >= 6 and plan_info['with_transition'] == 0:
        report['connection_hint'] = (f"工程主轨 {len(plan_info['cuts'])} 个切点没有一个带转场；卡拍再准也可能全是硬切，"
                                     '交付前用 audit --plan 看 connection_qc，并对照衔接表逐个切点确认')
    report['chart'] = _chart(report, out / 'music_fit.jpg')
    core.write_json(out / 'music-fit.json', report)
    (out / 'music-fit.md').write_text(_markdown(report), encoding='utf-8')
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Exported cut to review')
    parser.add_argument('--out', required=True, help='New output directory')
    parser.add_argument('--structure', default=None, help='Existing music-structure output (structure.json or dir) for the BGM; default analyzes the cut audio')
    parser.add_argument('--music-offset', type=float, default=0.0, help='Timeline second where --structure time 0 lands (default 0)')
    parser.add_argument('--backend', choices=('auto', 'beat-this', 'allin1', 'spectral'), default='auto', help='music-structure backend when analyzing the cut audio')
    parser.add_argument('--threshold', type=float, default=0.35, help='FFmpeg scene threshold in [0.01, 1.0] (default 0.35)')
    parser.add_argument('--tolerance', type=float, default=0.05, help='Seconds a cut may miss a beat and still count as on beat (default 0.05)')
    parser.add_argument('--skip-motion', action='store_true', help='Skip optical-flow motion for the ending check')
    parser.add_argument('--plan', default=None, help='edit-plan.json that exported this cut; its main-track shot boundaries (incl. dissolves and flash-covered cuts) become the cut list, plus detected in-shot cuts')
    parser.add_argument('--plan-track', default=None, help='Main video track name in --plan (default: the plan main_track, else video)')
    try:
        report = run(parser.parse_args(argv))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    summary = {k: report[k] for k in ('status', 'operation', 'window_s', 'chart')}
    summary['observations'] = {k: report[k].get('observation') for k in ('beat_fit', 'density_fit', 'drop_fit', 'ending_fit')}
    if report.get('connection_hint'):
        summary['observations']['connection'] = report['connection_hint']
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
