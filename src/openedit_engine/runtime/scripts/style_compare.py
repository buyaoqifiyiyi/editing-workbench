"""Compare editing-style fingerprints of a reference video and a candidate cut; list differences for human review (no score, no pass/fail)."""
import argparse
import importlib.metadata
import json
import re
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import file_hash
import media_prep
import runtime_core as core

MIN_CUT_GAP = 0.08
BEAT_TOLERANCE = 0.05
HIST_BINS = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0)


def _detect_cuts(path, start, span, threshold):
    res = core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin',
        '-ss', f'{start:.6f}', '-t', f'{span:.6f}', '-i', str(path), '-an',
        '-vf', f"select='gt(scene,{threshold})',showinfo", '-f', 'null', '-',
    ], timeout=900)
    times = sorted(float(m.group(1)) for m in re.finditer(r'pts_time:([0-9]+(?:\.[0-9]+)?)', res.stderr or ''))
    cuts = []
    for t in times:
        if 0.04 < t < span - 0.04 and (not cuts or t - cuts[-1] >= MIN_CUT_GAP):
            cuts.append(round(t, 3))
    return cuts


def _sample_frames(path, start, span, shots, out_dir, count):
    mids = [(a + b) / 2.0 for a, b in shots]
    if len(mids) < 4:
        mids = [span * (i + 0.5) / 8 for i in range(8)]
    if len(mids) > count:
        mids = [mids[round(i * (len(mids) - 1) / (count - 1))] for i in range(count)]
    out_dir.mkdir(parents=True, exist_ok=False)
    frames = []
    for idx, t in enumerate(mids):
        png = out_dir / f'frame-{idx:03d}.png'
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-ss', f'{start + t:.3f}', '-i', str(path), '-frames:v', '1', '-vf', 'scale=480:-2', str(png),
        ], timeout=60)
        if png.is_file():
            frames.append(png)
    return frames


def _distribution(durs):
    arr = sorted(durs)

    def pct(q):
        return round(arr[min(len(arr) - 1, int(round(q * (len(arr) - 1))))], 3)

    mean = sum(arr) / len(arr)
    cv = statistics.pstdev(arr) / mean if mean > 1e-6 else 0.0
    edges = list(HIST_BINS) + [float('inf')]
    hist = [sum(1 for d in arr if lo <= d < hi) for lo, hi in zip(edges, edges[1:])]
    return {
        'p10_s': pct(0.1), 'p50_s': pct(0.5), 'p90_s': pct(0.9),
        'duration_cv': round(cv, 3),
        'short_share_under_0_5s': round(sum(1 for d in arr if d < 0.5) / len(arr), 3),
        'long_share_over_3s': round(sum(1 for d in arr if d > 3.0) / len(arr), 3),
        'histogram_bins_s': [f'{lo:g}-{hi:g}' if hi != float('inf') else f'{lo:g}+' for lo, hi in zip(edges, edges[1:])],
        'histogram_counts': hist,
    }


def _tone(frames):
    lightness, contrast, chroma = [], [], []
    for p in frames:
        with Image.open(p) as im:
            rgb = np.asarray(im.convert('RGB'), dtype=np.uint8)[::2, ::2]
        lab = media_prep._rgb_to_oklab_pixels(rgb)
        lightness.append(float(lab[:, 0].mean()))
        contrast.append(float(lab[:, 0].std()))
        chroma.append(float(np.median(np.hypot(lab[:, 1], lab[:, 2]))))
    if not lightness:
        return None
    return {
        'oklab_lightness_mean': round(statistics.mean(lightness), 4),
        'oklab_lightness_contrast': round(statistics.mean(contrast), 4),
        'chroma_median': round(statistics.mean(chroma), 4),
        'lightness_spread_across_frames': round(max(lightness) - min(lightness), 4),
    }


def _near_ratio(cuts, targets, tol):
    if not cuts or not len(targets):
        return None
    arr = np.asarray(targets, dtype=np.float64)
    return round(sum(1 for c in cuts if float(np.min(np.abs(arr - c))) <= tol) / len(cuts), 3)


def _audio_alignment(path, info, start, span, cuts, median_shot):
    if not info.get('has_audio'):
        return {'status': 'skipped', 'reason': 'no audio stream'}
    if span < 2.0:
        return {'status': 'skipped', 'reason': 'window shorter than 2 s'}
    try:
        import beats
        import librosa
        y = beats.decode(str(path), start, span)
        # trim=False: librosa's default trims "weak" trailing beats, which dropped the last 4-14 s of
        # beats on continuous music and left the chart's beat ticks ending well before the window.
        tempo, frames = librosa.beat.beat_track(y=y, sr=beats.RATE, hop_length=beats.HOP, units='frames', trim=False)
        beat_t = [float(t) for t in librosa.frames_to_time(frames, sr=beats.RATE, hop_length=beats.HOP)]
        onset_t = [float(t) for t in librosa.onset.onset_detect(y=y, sr=beats.RATE, hop_length=beats.HOP, units='time')]
    except Exception as exc:  # audio decode/analysis is advisory; keep the visual comparison
        return {'status': 'skipped', 'reason': f'audio analysis failed: {exc}'}
    bpm = float(np.asarray(tempo).reshape(-1)[0]) if beat_t else 0.0
    return {
        'status': 'measured',
        'librosa': importlib.metadata.version('librosa'),
        'tempo_bpm_candidate': round(bpm, 2),
        'beat_count': len(beat_t),
        'onset_count': len(onset_t),
        'tolerance_s': BEAT_TOLERANCE,
        'cuts_on_beat_ratio': _near_ratio(cuts, beat_t, BEAT_TOLERANCE),
        'cuts_on_onset_ratio': _near_ratio(cuts, onset_t, BEAT_TOLERANCE),
        'median_shot_in_beats': round(median_shot * bpm / 60.0, 2) if bpm > 0 else None,
        'beat_times': [round(t, 3) for t in beat_t],
    }


def _motion(path, start, span):
    import motion_flow

    quarters = []
    for q in range(4):
        r = motion_flow.analyze_video(path, sample_fps=10.0, start_s=start + span * q / 4, end_s=start + span * (q + 1) / 4)
        quarters.append(round(float(r['overall']['speed']), 4))
    return {'status': 'measured', 'quarter_speed': quarters, 'mean_speed': round(statistics.mean(quarters), 4)}


def fingerprint(path, out_dir, *, start, duration, threshold, frames, motion, audio):
    target = Path(path).expanduser().resolve()
    info = core.probe(target)
    if info['kind'] != 'video':
        raise ValueError(f'Style comparison requires a local video: {target}')
    core.number(start, 'start', 0, max(0.0, info['duration']))
    limit = info['duration'] - start
    span = limit if duration is None else core.number(duration, 'duration', 0.5, limit)
    cuts = _detect_cuts(target, start, span, threshold)
    bounds = [0.0] + cuts + [round(span, 3)]
    shots = list(zip(bounds, bounds[1:]))
    durs = [round(max(0.02, b - a), 3) for a, b in shots]
    picked = _sample_frames(target, start, span, shots, out_dir / 'frames', frames)
    rhythm = media_prep._compute_editing_rhythm([str(p) for p in picked], cuts, span, start=0.0)
    scales = {}
    for item in rhythm['shot_scale_sequence']:
        scales[item['shot_scale']] = scales.get(item['shot_scale'], 0) + 1
    width, height = int(info.get('width') or 0), int(info.get('height') or 0)
    return {
        'input': str(target),
        'sha256': file_hash.sha256(target),
        'resolution': [width, height],
        'aspect_ratio': round(width / height, 3) if height else None,
        'start': start,
        'window_s': round(span, 3),
        'cuts': cuts,
        'shots': [[round(a, 3), round(b, 3)] for a, b in shots],
        'rhythm': {k: v for k, v in rhythm.items() if k != 'shot_scale_sequence'},
        'shot_distribution': _distribution(durs),
        'shot_scale_counts': scales,
        'tone': _tone(picked),
        'audio_alignment': _audio_alignment(target, info, start, span, cuts, rhythm['median_shot_s']) if audio else {'status': 'skipped', 'reason': 'disabled'},
        'motion': _motion(target, start, span) if motion else {'status': 'skipped', 'reason': 'disabled'},
        'frames': [str(p) for p in picked],
    }


def _rel(a, b):
    if a is None or b is None:
        return None
    return round((b - a) / max(abs(a), 1e-6), 3)


def _corr(a, b):
    if len(set(a)) < 2 or len(set(b)) < 2:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def _differences(ref, cand):
    out = []

    def add(metric, label, rv, cv, *, rel=None, absdiff=None, note=None):
        entry = {'metric': metric, 'label': label, 'reference': rv, 'candidate': cv}
        if rv is not None and cv is not None and not isinstance(rv, (str, list)):
            entry['relative_change'] = _rel(rv, cv)
            entry['delta'] = round(cv - rv, 4)
            flagged = (rel is not None and entry['relative_change'] is not None and abs(entry['relative_change']) >= rel) or \
                      (absdiff is not None and abs(cv - rv) >= absdiff)
        else:
            flagged = rv != cv
        if flagged and note:
            entry['observation'] = note(rv, cv)
        out.append(entry)

    more_less = lambda rv, cv, word: f'{word}{"更长" if cv > rv else "更短"}（参考 {rv}，成片 {cv}）'
    rr, cr = ref['rhythm'], cand['rhythm']
    rd, cd = ref['shot_distribution'], cand['shot_distribution']
    add('median_shot_s', '镜头时长中位数（秒）', rr['median_shot_s'], cr['median_shot_s'], rel=0.3,
        note=lambda a, b: more_less(a, b, '成片的典型镜头'))
    add('cuts_per_minute', '每分钟切点数', rr['cuts_per_minute'], cr['cuts_per_minute'], rel=0.3,
        note=lambda a, b: f'成片切换{"更密" if b > a else "更疏"}（参考 {a}，成片 {b} 次/分）')
    add('duration_cv', '镜头时长离散度', rd['duration_cv'], cd['duration_cv'], absdiff=0.25,
        note=lambda a, b: f'成片镜头长短{"变化更大" if b > a else "更均匀"}，参考 {a}，成片 {b}')
    add('short_share_under_0_5s', '0.5 秒以下短镜占比', rd['short_share_under_0_5s'], cd['short_share_under_0_5s'], absdiff=0.15,
        note=lambda a, b: f'短促镜头占比参考 {a:.0%}，成片 {b:.0%}')
    add('long_share_over_3s', '3 秒以上长镜占比', rd['long_share_over_3s'], cd['long_share_over_3s'], absdiff=0.15,
        note=lambda a, b: f'长停留镜头占比参考 {a:.0%}，成片 {b:.0%}')
    add('pacing_profile', '四段节奏走向', rr['pacing_profile'], cr['pacing_profile'],
        note=lambda a, b: f'参考节奏走向是 {a}，成片是 {b}')
    corr = _corr(rr['pacing_quarters_cpm'], cr['pacing_quarters_cpm'])
    out.append({'metric': 'pacing_quarters_cpm', 'label': '四段切点密度（次/分）',
                'reference': rr['pacing_quarters_cpm'], 'candidate': cr['pacing_quarters_cpm'], 'shape_correlation': corr,
                **({'observation': f'四段疏密走向与参考不一致（相关系数 {corr}）'} if corr is not None and corr < 0.3 else {})})

    ra, ca = ref['audio_alignment'], cand['audio_alignment']
    if ra['status'] == 'measured' and ca['status'] == 'measured':
        add('cuts_on_beat_ratio', f'切点落在拍点 ±{int(BEAT_TOLERANCE * 1000)}ms 的比例', ra['cuts_on_beat_ratio'], ca['cuts_on_beat_ratio'], absdiff=0.2,
            note=lambda a, b: f'切点踩拍比例参考 {a:.0%}，成片 {b:.0%}')
        add('cuts_on_onset_ratio', f'切点落在声音起音 ±{int(BEAT_TOLERANCE * 1000)}ms 的比例', ra['cuts_on_onset_ratio'], ca['cuts_on_onset_ratio'], absdiff=0.2,
            note=lambda a, b: f'切点对齐声音起音的比例参考 {a:.0%}，成片 {b:.0%}')
        add('median_shot_in_beats', '典型镜头时长（拍）', ra['median_shot_in_beats'], ca['median_shot_in_beats'], rel=0.4,
            note=lambda a, b: f'参考平均约 {a} 拍一镜，成片约 {b} 拍一镜')

    rt, ct = ref['tone'], cand['tone']
    if rt and ct:
        add('oklab_lightness_mean', '平均明度（Oklab L）', rt['oklab_lightness_mean'], ct['oklab_lightness_mean'], absdiff=0.06,
            note=lambda a, b: f'成片整体{"更亮" if b > a else "更暗"}（参考 {a}，成片 {b}）')
        add('oklab_lightness_contrast', '画面内明暗反差', rt['oklab_lightness_contrast'], ct['oklab_lightness_contrast'], absdiff=0.04,
            note=lambda a, b: f'成片反差{"更强" if b > a else "更弱"}（参考 {a}，成片 {b}）')
        add('chroma_median', '饱和度（Oklab 色度中位数）', rt['chroma_median'], ct['chroma_median'], absdiff=0.03,
            note=lambda a, b: f'成片{"更鲜艳" if b > a else "更素"}（参考 {a}，成片 {b}）')
        add('lightness_spread_across_frames', '镜头之间的明度起伏', rt['lightness_spread_across_frames'], ct['lightness_spread_across_frames'], absdiff=0.12,
            note=lambda a, b: f'镜头间明暗起伏参考 {a}，成片 {b}')
    out.append({'metric': 'dominant_palette_hex', 'label': '主色调', 'reference': rr['dominant_palette_hex'],
                'candidate': cr['dominant_palette_hex']})
    out.append({'metric': 'shot_scale_counts', 'label': '景别抽样分布（按人脸大小估计）',
                'reference': ref['shot_scale_counts'], 'candidate': cand['shot_scale_counts']})

    rm, cm = ref['motion'], cand['motion']
    if rm['status'] == 'measured' and cm['status'] == 'measured':
        add('mean_motion_speed', '画面运动强度（光流）', rm['mean_speed'], cm['mean_speed'], rel=0.4,
            note=lambda a, b: f'成片画面运动{"更强" if b > a else "更弱"}（参考 {a}，成片 {b}）')
        mc = _corr(rm['quarter_speed'], cm['quarter_speed'])
        out.append({'metric': 'quarter_motion_speed', 'label': '四段运动强度', 'reference': rm['quarter_speed'],
                    'candidate': cm['quarter_speed'], 'shape_correlation': mc,
                    **({'observation': f'四段动静走向与参考不一致（相关系数 {mc}）'} if mc is not None and mc < 0.3 else {})})
    if ref['aspect_ratio'] != cand['aspect_ratio']:
        out.append({'metric': 'aspect_ratio', 'label': '画幅比例', 'reference': ref['aspect_ratio'], 'candidate': cand['aspect_ratio'],
                    'observation': '两片画幅不同，构图与运动强度只能粗略对照'})
    return out


def _font(size):
    for name in ('C:/Windows/Fonts/consola.ttf', 'DejaVuSansMono.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _bars(draw, x, y, w, h, values, vmax, color, title, font, labels=None):
    draw.text((x, y), title, fill=(200, 208, 222), font=font)
    top = y + 16
    n = max(1, len(values))
    bw = w / n
    for i, v in enumerate(values):
        bh = 0 if vmax <= 0 else (h - 14) * float(v) / vmax
        draw.rectangle([x + i * bw + 2, top + h - 14 - bh, x + (i + 1) * bw - 2, top + h - 14], fill=color)
        if labels:
            draw.text((x + i * bw + 2, top + h - 12), labels[i], fill=(150, 158, 172), font=font)


def _chart(sides, out_path):
    W, pad, row_h = 1280, 20, 300
    img = Image.new('RGB', (W, pad * 2 + row_h * len(sides)), (16, 18, 24))
    draw = ImageDraw.Draw(img)
    font, small = _font(15), _font(11)
    cpm_max = max(max(fp['rhythm']['pacing_quarters_cpm']) for _, fp in sides) or 1.0
    hist_max = max(max(fp['shot_distribution']['histogram_counts']) for _, fp in sides) or 1
    motion_max = max([max(fp['motion']['quarter_speed']) for _, fp in sides if fp['motion']['status'] == 'measured'] or [0.0]) or 1.0
    for i, (label, fp) in enumerate(sides):
        y = pad + i * row_h
        r = fp['rhythm']
        draw.text((pad, y), f"{label}  window={fp['window_s']:.1f}s  shots={r['shot_count']}  median={r['median_shot_s']}s  "
                            f"cuts/min={r['cuts_per_minute']}  profile={r['pacing_profile']}", fill=(240, 245, 255), font=font)
        x0, x1, sy, sh = pad, W - pad, y + 26, 40
        span = max(1e-6, fp['window_s'])
        for k, (a, b) in enumerate(fp['shots']):
            xa, xb = x0 + (x1 - x0) * a / span, x0 + (x1 - x0) * b / span
            draw.rectangle([xa, sy, max(xa + 1, xb - 1), sy + sh], fill=(70, 110, 170) if k % 2 == 0 else (120, 160, 215))
        aa = fp['audio_alignment']
        for t in aa.get('beat_times', []):
            x = x0 + (x1 - x0) * t / span
            draw.line([x, sy + sh + 3, x, sy + sh + 11], fill=(240, 200, 80))
        draw.text((x0, sy + sh + 13), 'shots (normalized time)   yellow ticks = candidate beats', fill=(150, 158, 172), font=small)
        by, bw = sy + sh + 36, (W - pad * 5) // 4
        _bars(draw, pad, by, bw, 150, r['pacing_quarters_cpm'], cpm_max, (94, 196, 160), 'cuts/min by quarter', small,
              [f'Q{q + 1}' for q in range(4)])
        d = fp['shot_distribution']
        # Compact labels: full "0.25-0.5" is wider than one 7-bin column and overlapped its neighbour.
        _bars(draw, pad * 2 + bw, by, bw, 150, d['histogram_counts'], hist_max, (130, 150, 230), 'shot length histogram (s)', small,
              [lbl.replace('0.', '.') for lbl in d['histogram_bins_s']])
        if fp['motion']['status'] == 'measured':
            _bars(draw, pad * 3 + bw * 2, by, bw, 150, fp['motion']['quarter_speed'], motion_max, (236, 120, 96), 'motion by quarter', small,
                  [f'Q{q + 1}' for q in range(4)])
        px = pad * 4 + bw * 3
        draw.text((px, by), 'palette', fill=(200, 208, 222), font=small)
        for k, hx in enumerate(r['dominant_palette_hex']):
            rgb = tuple(int(hx[j:j + 2], 16) for j in (1, 3, 5))
            draw.rectangle([px + k * 64, by + 18, px + k * 64 + 56, by + 74], fill=rgb, outline=(65, 76, 96))
            draw.text((px + k * 64, by + 78), hx, fill=(150, 158, 172), font=small)
    img.save(out_path, quality=92)
    return str(out_path)


def _markdown(report):
    lines = ['# 风格对照', '', f"- 参考：`{report['reference']['input']}`", f"- 成片：`{report['candidate']['input']}`", '',
             '## 值得看的差异', '']
    noted = [d for d in report['differences'] if d.get('observation')]
    lines += [f"- {d['label']}：{d['observation']}" for d in noted] or ['- 各项指标与参考接近，没有超出提示范围的差异。']
    lines += ['', '## 全部指标', '', '| 指标 | 参考 | 成片 |', '|---|---|---|']
    lines += [f"| {d['label']} | {d['reference']} | {d['candidate']} |" for d in report['differences']]
    lines += ['', '## 边界', ''] + [f'- {b}' for b in report['limitations']]
    return '\n'.join(lines) + '\n'


def run(args):
    threshold = core.number(args.threshold, 'threshold', 0.01, 1.0)
    frames = int(core.number(args.frames, 'frames', 4, 32))
    out = Path(args.out).resolve()
    if out.exists() or out.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')
    ref_path, cand_path = Path(args.reference).expanduser().resolve(), Path(args.candidate).expanduser().resolve()
    for label, p in (('reference', ref_path), ('candidate', cand_path)):
        if not p.is_file():
            raise ValueError(f'{label} video does not exist: {p}')
    before = {str(p): file_hash.sha256(p) for p in {ref_path, cand_path}}
    out = core.new_directory(out)
    opts = dict(threshold=threshold, frames=frames, motion=not args.skip_motion, audio=not args.skip_audio)
    ref = fingerprint(ref_path, out / 'reference', start=args.reference_start, duration=args.reference_duration, **opts)
    cand = fingerprint(cand_path, out / 'candidate', start=args.candidate_start, duration=args.candidate_duration, **opts)
    if any(file_hash.sha256(Path(p)) != h for p, h in before.items()):
        raise ValueError('Input changed during comparison; reject output')
    report = {
        'status': 'compared_for_review',
        'operation': 'style-compare',
        'detector': {'name': 'ffmpeg scene', 'threshold': threshold, 'min_cut_gap_s': MIN_CUT_GAP},
        'reference': ref,
        'candidate': cand,
        'differences': _differences(ref, cand),
        'chart': _chart([('REFERENCE', ref), ('CANDIDATE', cand)], out / 'style_compare.jpg'),
        'aesthetic_judgment': 'not_performed',
        'human_watching': 'not_performed',
        'limitations': [
            '差异只说明“和参考哪里不同”，不是好坏评分；是否要向参考靠拢由剪辑目标和使用者决定。',
            'FFmpeg 场景检测只给候选硬切，闪白、叠化、快速运镜可能误检或漏检；窗口两端的镜头可能被截断。',
            '拍点与起音来自 librosa 候选检测，可能落在半拍/倍拍；未试听不代表卡点听感。',
            '运动强度含切点处的光流尖峰，两片画幅不同时只能粗略对照。',
            '景别按 YuNet 人脸大小估计，动漫人脸常检测不到而记为 WS。',
        ],
    }
    core.write_json(out / 'style-compare.json', report)
    (out / 'style-compare.md').write_text(_markdown(report), encoding='utf-8')
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, help='Reference video the edit should be compared against')
    parser.add_argument('--candidate', required=True, help='Candidate export to compare')
    parser.add_argument('--out', required=True, help='New output directory')
    parser.add_argument('--reference-start', type=float, default=0.0)
    parser.add_argument('--reference-duration', type=float, default=None)
    parser.add_argument('--candidate-start', type=float, default=0.0)
    parser.add_argument('--candidate-duration', type=float, default=None)
    parser.add_argument('--threshold', type=float, default=0.35, help='FFmpeg scene threshold in [0.01, 1.0] (default 0.35)')
    parser.add_argument('--frames', type=int, default=16, help='Sampled frames per video for tone/palette/shot scale in [4, 32]')
    parser.add_argument('--skip-motion', action='store_true', help='Skip optical-flow motion intensity')
    parser.add_argument('--skip-audio', action='store_true', help='Skip beat/onset alignment')
    try:
        report = run(parser.parse_args(argv))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps({k: v for k, v in report.items() if k not in ('reference', 'candidate')}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
