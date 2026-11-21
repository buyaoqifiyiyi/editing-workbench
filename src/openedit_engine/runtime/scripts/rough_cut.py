"""One-command rough-cut orchestrator: auto-cut (silence removal) -> Whisper transcription -> editable/karaoke captions."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import auto_cut
import file_hash
import karaoke
import runtime_core as core
import transcribe


def _build_rough_cut_captions(plan, tr_report, *, track, karaoke_mode, karaoke_color, max_chars, caption_size, caption_y):
    """Map word/segment transcript cues strictly inside kept auto-cut clips with zero overlap."""
    segments = tr_report.get('segments') or []
    if not segments:
        return [], 0

    duration_s = float((tr_report.get('audio') or {}).get('seconds') or 0.0)
    raw_words = []
    try:
        if (tr_report.get('params') or {}).get('word_timestamps') is True and duration_s > 0.0:
            raw_words = transcribe.word_cues(segments, duration_s)
    except ValueError:
        raw_words = []

    units_all = []
    if raw_words:
        units_all = raw_words
    else:
        for idx, seg in enumerate(segments):
            txt = str(seg.get('text') or '').strip()
            if txt and float(seg['end']) > float(seg['start']):
                units_all.append({
                    'text': txt,
                    'start': float(seg['start']),
                    'end': float(seg['end']),
                    'segment_index': idx,
                })

    if not units_all:
        return [], 0

    all_cues = []
    for clip in plan['clips']:
        c_start_us = core.us(float(clip['start']))
        c_dur_us = core.us(float(clip['duration']))
        src_in_us = core.us(float(clip.get('source_in', 0.0)))
        speed = float(clip.get('speed', 1.0))
        src_out_us = src_in_us + round(c_dur_us * speed)

        clip_units = []
        last_tl_end = c_start_us - 1
        for unit in units_all:
            ua = core.us(float(unit['start']))
            ub = core.us(float(unit['end']))
            center = (ua + ub) // 2
            if not (src_in_us <= center <= src_out_us):
                continue
            ca = max(src_in_us, ua)
            cb = min(src_out_us, ub)
            if cb - ca < 10_000:
                continue
            tl_a = c_start_us + round((ca - src_in_us) / speed)
            tl_b = c_start_us + round((cb - src_in_us) / speed)
            tl_a = max(c_start_us, tl_a)
            tl_b = min(c_start_us + c_dur_us, tl_b)
            if last_tl_end >= c_start_us and tl_a <= last_tl_end:
                tl_a = last_tl_end + 1000
            if tl_b - tl_a < 10_000:
                continue
            last_tl_end = tl_b
            text_piece = str(unit['text']).replace('\r', ' ').replace('\n', ' ')
            if not text_piece.strip():
                continue
            clip_units.append({
                'text': text_piece,
                'start': tl_a / 1_000_000,
                'end': tl_b / 1_000_000,
                'group': int(unit.get('segment_index', 0)),
            })

        if not clip_units:
            continue

        if karaoke_mode:
            effective_max = max(max_chars, max(len(u['text']) for u in clip_units))
            states = karaoke.compile_cues(
                clip_units,
                mode=karaoke_mode,
                color=karaoke_color,
                max_chars=min(80, effective_max),
                max_words=8,
                joiner='',
                max_gap=1.0,
            )
            for st in states:
                cue = {k: v for k, v in st.items() if k != 'end'}
                cue['duration'] = st['end'] - st['start']
                cue['track'] = track
                cue['y'] = caption_y
                if caption_size is not None:
                    cue['size'] = caption_size
                core.snap_range(cue, 'rough-cut karaoke cue')
                all_cues.append(cue)
        else:
            # Group short words into readable phrase blocks within each clip.
            blocks, cur, cur_len = [], [], 0
            for u in clip_units:
                gap = (u['start'] - cur[-1]['end']) if cur else 0.0
                if cur and (u['group'] != cur[-1]['group'] or gap > 0.7 or cur_len + len(u['text']) > max_chars):
                    blocks.append(cur)
                    cur, cur_len = [], 0
                cur.append(u)
                cur_len += len(u['text'])
            if cur:
                blocks.append(cur)
            for blk in blocks:
                txt = ''.join(u['text'] for u in blk).strip()
                if not txt:
                    continue
                cue = {
                    'text': txt,
                    'start': blk[0]['start'],
                    'duration': max(0.05, blk[-1]['end'] - blk[0]['start']),
                    'track': track,
                    'y': caption_y,
                }
                if caption_size is not None:
                    cue['size'] = caption_size
                core.snap_range(cue, 'rough-cut caption')
                all_cues.append(cue)

    return all_cues, len(units_all)


def run_rough_cut(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    caption_track = core.check_text(args.caption_track, 'caption_track', 64)
    caption_y = core.number(args.caption_y, 'caption_y', -1.0, 1.0)
    caption_size = core.number(args.caption_size, 'caption_size', 1.0, 500.0) if args.caption_size is not None else None
    max_chars = int(core.number(args.karaoke_max_chars, 'karaoke_max_chars', 4, 80))

    info = core.probe(source)
    if info['kind'] != 'video' or not info.get('has_audio'):
        raise ValueError('rough-cut requires a local video file with an audible audio stream')

    plan_name = core.check_text(args.name, 'name', 160) if args.name else f'{source.stem[:110]}_roughcut'
    out = core.new_directory(out_cand)
    autocut_dir = out / 'autocut'

    ac_args = SimpleNamespace(
        input=str(source),
        out=str(autocut_dir),
        detector=args.detector,
        vad_model=args.vad_model,
        threshold=args.threshold,
        min_silence=args.min_silence,
        min_speech=args.min_speech,
        pad=args.pad,
        fade=args.fade,
        name=plan_name,
        width=args.width,
        height=args.height,
        fps=args.fps,
        gpu=getattr(args, 'gpu', False),
    )
    ac_report = auto_cut.run_auto_cut(ac_args)
    if not ac_report.get('plan_path'):
        report = {
            'status': 'passed',
            'operation': 'rough-cut',
            'input': str(source),
            'sha256': file_hash.sha256(source),
            'out': str(out),
            'auto_cut': ac_report,
            'transcription': None,
            'captions_count': 0,
            'plan_path': None,
            'note': ac_report.get('note') or 'No speech segments remained after auto-cut; rough-cut plan not written.',
        }
        core.write_json(out / 'rough-cut.json', report)
        return report

    plan = core.read_json(Path(ac_report['plan_path']))
    tr_report = None
    captions_count = 0
    note = None

    if not args.skip_captions:
        tr_dir = out / 'transcript'
        tr_args = SimpleNamespace(
            input=str(source),
            out=str(tr_dir),
            model=args.whisper_model,
            language=args.language,
            beam_size=args.beam_size,
            compute_type=args.compute_type,
            threads=0,
            no_vad=False,
            word_timestamps=True,
            max_seconds=None,
            verbose=False,
            device=args.device,
            gpu=args.gpu,
        )
        tr_report = transcribe.run_transcription(tr_args)
        cues, _ = _build_rough_cut_captions(
            plan,
            tr_report,
            track=caption_track,
            karaoke_mode=args.karaoke_mode,
            karaoke_color=args.karaoke_color,
            max_chars=max_chars,
            caption_size=caption_size,
            caption_y=caption_y,
        )
        if cues:
            plan['captions'] = cues
            order = list(plan.get('layer_order') or ['video'])
            if caption_track not in order:
                order.append(caption_track)
            plan['layer_order'] = order
            captions_count = len(cues)
        else:
            note = 'Transcription produced 0 mappable speech segments inside kept clips; delivered silence-cut plan without captions.'

    plan.setdefault('notes', []).append(
        f'Generated by rough-cut (auto-cut={ac_report["detector_used"]}, kept={ac_report["kept_count"]}, captions={captions_count}).'
    )
    final_plan_path = out / 'edit-plan.json'
    core.write_json(final_plan_path, plan)
    core.validate_plan(final_plan_path)

    report = {
        'status': 'passed',
        'operation': 'rough-cut',
        'input': str(source),
        'sha256': file_hash.sha256(source),
        'out': str(out),
        'plan_path': str(final_plan_path),
        'kept_clips': len(plan['clips']),
        'kept_duration': ac_report['kept_duration'],
        'removed_duration': ac_report['removed_duration'],
        'captions_count': captions_count,
        'karaoke_mode': args.karaoke_mode,
        'skip_captions': bool(args.skip_captions),
        'auto_cut_report': str(autocut_dir / 'cuts.json'),
        'transcript_report': str(out / 'transcript' / 'transcript-report.json') if tr_report else None,
        'note': note,
        'boundaries': [
            'Combines silence removal + offline transcription + clip-clamped captions into one validated edit-plan.json.',
            'Review ASR wording and cut breathing pauses in FilmCraft or edit-plan.json before final export.',
        ],
    }
    core.write_json(out / 'rough-cut.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video file with spoken audio')
    parser.add_argument('--out', required=True, help='New output directory for edit-plan.json and rough-cut.json')
    parser.add_argument('--name', help='Optional edit plan name')
    parser.add_argument('--detector', choices=('auto', 'silero', 'ffmpeg'), default='auto')
    parser.add_argument('--vad-model', help='Optional Silero VAD ONNX file')
    parser.add_argument('--threshold', type=float, default=None)
    parser.add_argument('--min-silence', type=float, default=0.45)
    parser.add_argument('--min-speech', type=float, default=0.25)
    parser.add_argument('--pad', type=float, default=0.15)
    parser.add_argument('--fade', type=float, default=0.02)
    parser.add_argument('--skip-captions', action='store_true', help='Skip Whisper transcription and output silence-cut plan only')
    parser.add_argument('--whisper-model', help='Optional faster-whisper model dir')
    parser.add_argument('--language', default='zh', help="'auto' or language code such as zh/en (default zh)")
    parser.add_argument('--device', choices=('cpu', 'cuda', 'auto'), default='cpu')
    parser.add_argument('--gpu', action='store_true', help='Prefer CUDA GPU for Silero VAD & Whisper')
    parser.add_argument('--beam-size', type=int, default=5)
    parser.add_argument('--compute-type', default=None, help='int8/float32 on CPU; float16/int8_float16 on CUDA')
    parser.add_argument('--caption-track', default='captions')
    parser.add_argument('--karaoke-mode', choices=('active', 'cumulative'), default=None)
    parser.add_argument('--karaoke-color', default='#FFD54F')
    parser.add_argument('--karaoke-max-chars', type=int, default=14)
    parser.add_argument('--caption-size', type=float, default=None)
    parser.add_argument('--caption-y', type=float, default=-0.7)
    parser.add_argument('--width', type=int, default=None)
    parser.add_argument('--height', type=int, default=None)
    parser.add_argument('--fps', type=float, default=None)
    args = parser.parse_args(argv)
    try:
        report = run_rough_cut(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
