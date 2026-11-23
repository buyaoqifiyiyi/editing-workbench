"""Candidate-beat cuts or explicit audio marks in a new validated plan."""
import argparse
import copy
from decimal import Decimal
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
import tempfile
import editor

MAX_MARKS = 2048


def _micros(value):
    num = editor.number(value, 'timestamp')
    scaled = Decimal(str(num)) * 1_000_000
    if scaled != int(scaled):
        raise ValueError(f'Timestamp must have exact integer microsecond precision: {value}')
    return int(scaled)


def _is_structure_source(beats_path):
    p = Path(beats_path).resolve()
    return (p.is_file() and p.name == 'structure.json') or (p.is_dir() and (p / 'structure.json').is_file())


def _load_structure_grid(beats_path, music, *, use_downbeats=False):
    import music_structure
    grid = music_structure.load_rhythm_grid(beats_path)
    if Path(music['path']).resolve() != Path(grid['source_path']).resolve():
        raise ValueError('Beat report source must be the exact plan music source')
    raw_times = grid['downbeats'] if use_downbeats else grid['beats']
    if not raw_times:
        kind_label = 'downbeats' if use_downbeats else 'beats'
        raise ValueError(f'music-structure report contains no {kind_label}')
    start_us = round(Decimal(str(grid['start_seconds'])) * 1_000_000)
    dur_us = round(Decimal(str(grid['duration'])) * 1_000_000)
    pts_us = [start_us + round(Decimal(str(t)) * 1_000_000) for t in raw_times]
    if any(a >= b for a, b in zip(pts_us, pts_us[1:])):
        raise ValueError('music-structure beat timestamps must strictly increase')
    return grid, pts_us, start_us, start_us + dur_us


def mark_audio(args, plan, before, source_plan, out, every):
    import beats
    if len(plan['audio']) != 1:
        raise ValueError('Beat mapping requires exactly one music item')
    music = plan['audio'][0]
    if 'beat_marks' in music:
        raise ValueError('Beat mapping will not replace existing beat_marks')
    low = _micros(music['source_in'])
    high = low + _micros(music['duration'])
    use_downbeats = bool(getattr(args, 'use_downbeats', False))
    if _is_structure_source(args.beats):
        _, pts_us, coverage_low_us, coverage_high_us = _load_structure_grid(args.beats, music, use_downbeats=use_downbeats)
        candidates = [value for value in pts_us if low <= value < high and coverage_low_us <= value < coverage_high_us]
    else:
        result = beats.load_result(Path(args.beats).resolve())
        if Path(music['path']).resolve() != Path(result['source']['path']).resolve():
            raise ValueError('Beat report source must be the exact plan music source')
        selection = result['selection']
        rate = selection['sample_rate']
        coverage_low = Fraction(selection['start_us'])
        coverage_high = coverage_low + Fraction(selection['sample_count'] * 1_000_000, rate)
        candidates = []
        for sample in result['beat_samples']:
            point = coverage_low + Fraction(sample * 1_000_000, rate)
            if low <= point < high:
                value = round(point)
                if not (low <= value < high and coverage_low <= value < coverage_high):
                    raise ValueError('Quantized mark falls outside music selection or analyzed coverage')
                candidates.append(value)
    if any(a >= b for a, b in zip(candidates, candidates[1:])):
        raise ValueError('Beat microsecond quantization produced duplicate or reversed marks')
    chosen = candidates[every - 1::every]
    if not 1 <= len(chosen) <= MAX_MARKS:
        raise ValueError(f'Beat mapping requires 1..{MAX_MARKS} selected marks; no truncation or invented beats')
    output = copy.deepcopy(plan)
    output['audio'][0]['beat_marks'] = [value / 1_000_000 for value in chosen]
    output['notes'].append('离线节拍候选已映射为音轨拍点标记；音频对齐与人工听感需人工复核。')
    warnings = write_plan(output, out, source_plan, before)
    return {'status': 'candidate_plan_created', 'mode': 'mark_audio', 'plan': str(out),
            'source_plan': str(source_plan), 'beats': str(Path(args.beats).resolve()),
            'every': every, 'use_downbeats': use_downbeats, 'eligible_candidates': len(candidates),
            'beat_marks_us': chosen, 'input_plan_unchanged': True, 'warnings': warnings,
            'limitations': ['Audio alignment and human listening unverified',
                            'Plan only; no project build or export performed']}


def apply(args):
    import beats
    source_plan = Path(args.plan).resolve()
    requested_out = Path(args.out).absolute()
    if requested_out.exists() or requested_out.is_symlink():
        raise ValueError('Beat application requires a new output file; never overwrite a plan')
    out = requested_out.resolve()
    every = args.every
    if type(every) is not int or not 1 <= every <= 16:
        raise ValueError('Beat interval must be an integer from 1 to 16')
    if source_plan == out or out.exists():
        raise ValueError('Beat application requires a new output file; never overwrite a plan')
    if not out.parent.is_dir():
        raise ValueError('Beat output parent directory must already exist')
    before = source_plan.read_bytes()
    plan, _, _ = editor.validate_plan(source_plan)
    if getattr(args, 'mark_audio', False):
        return mark_audio(args, plan, before, source_plan, out, every)
    if len(plan['clips']) < 2 or any(plan.get(k) for k in
            ('captions', 'effects', 'filters', 'composites', 'missing_assets')):
        raise ValueError('Beat application requires at least two plain primary clips and no captions/effects/composites/missing assets')
    cursor = 0
    forbidden = ('transform', 'keyframes', 'motion_easing', 'animations', 'effects', 'filters',
                 'grading', 'mask', 'chroma', 'background', 'mix_mode', 'transition', 'speed_curve', 'matting_cache')
    for clip in plan['clips']:
        if (clip['track'] != 'video' or editor.us(clip['start']) != cursor or clip['speed'] != 1 or
                clip['volume'] != 0 or any(clip.get(k) for k in forbidden)):
            raise ValueError('Beat application admits ordered contiguous muted primary video clips at speed=1 without transforms/effects/keys/transitions')
        cursor = editor.end_us(clip)
    if len(plan['audio']) != 1:
        raise ValueError('Beat application requires exactly one independent music item')
    music = plan['audio'][0]
    if (music['track'] != 'music' or music['start'] != 0 or music['speed'] != 1 or
            any(music.get(k) for k in ('fade_in', 'fade_out', 'keyframes', 'denoise', 'loudness'))):
        raise ValueError('Beat application requires music starting at zero, speed=1, without fades/keys/denoise/loudness')
    offset = editor.us(music['source_in'])
    original_duration = editor.us(music['duration'])
    use_downbeats = bool(getattr(args, 'use_downbeats', False))
    fps = Fraction(str(plan['canvas']['fps']))
    if _is_structure_source(args.beats):
        _, pts_us, cov_low, cov_high = _load_structure_grid(args.beats, music, use_downbeats=use_downbeats)
        candidates_us = [pt - offset for pt in pts_us if 0 < (pt - offset) < original_duration]
        chosen_us = candidates_us[every - 1::every][:len(plan['clips'])]
        if len(chosen_us) != len(plan['clips']):
            raise ValueError('Insufficient candidate boundaries; no invented beats or looping source clips')
        frame_numbers = [0] + [round(Fraction(pt_us, 1_000_000) * fps) for pt_us in chosen_us]
        if any(a >= b for a, b in zip(frame_numbers, frame_numbers[1:])):
            raise ValueError('Beat frame quantization produced duplicate or reversed boundaries')
        frame_times = [Fraction(frame * 1_000_000, 1) / fps for frame in frame_numbers]
        boundaries = [round(point) for point in frame_times]
        detection_start = Fraction(cov_low - offset, 1)
        detection_end = Fraction(cov_high - offset, 1)
        eligible_count = len(candidates_us)
        selected_us_list = chosen_us
    else:
        result = beats.load_result(Path(args.beats).resolve())
        media = Path(result['source']['path']).resolve()
        if Path(music['path']).resolve() != media:
            raise ValueError('Beat report source must be the exact plan music source')
        selection = result['selection']
        rate = selection['sample_rate']
        candidates = []
        for sample in result['beat_samples']:
            # Samples are indices inside the analyzed source selection; cuts use the music-local clock.
            if not 0 <= sample < selection['sample_count']:
                raise ValueError('Beat sample falls outside the analyzed selection')
            numerator = (selection['start_us'] - offset) * rate + sample * 1_000_000
            if 0 < numerator < original_duration * rate:
                candidates.append(numerator)
        chosen = candidates[every - 1::every][:len(plan['clips'])]
        if len(chosen) != len(plan['clips']):
            raise ValueError('Insufficient candidate boundaries; no invented beats or looping source clips')
        frame_numbers = [0] + [round(Fraction(numerator, rate * 1_000_000) * fps) for numerator in chosen]
        if any(a >= b for a, b in zip(frame_numbers, frame_numbers[1:])):
            raise ValueError('Beat frame quantization produced duplicate or reversed boundaries')
        frame_times = [Fraction(frame * 1_000_000, 1) / fps for frame in frame_numbers]
        boundaries = [round(point) for point in frame_times]
        detection_start = Fraction(selection['start_us'] - offset, 1)
        detection_end = detection_start + Fraction(selection['sample_count'] * 1_000_000, rate)
        eligible_count = len(candidates)
        selected_us_list = [round(Fraction(numerator, rate)) for numerator in chosen]
    if any(not (0 < point < original_duration and detection_start <= point < detection_end)
           for point in frame_times[1:] + boundaries[1:]):
        raise ValueError('Quantized cuts fall outside the music range or analyzed coverage')
    output = copy.deepcopy(plan)
    output['name'] = plan['name'][:90] + '-节拍候选'
    for index, clip in enumerate(output['clips']):
        clip['start'] = boundaries[index] / 1_000_000
        clip['duration'] = (boundaries[index + 1] - boundaries[index]) / 1_000_000
    output['audio'][0]['duration'] = boundaries[-1] / 1_000_000
    output['notes'].append('按离线节拍候选生成；未经试听，仅作为节奏切点方案。')
    warnings = write_plan(output, out, source_plan, before)
    return {'status': 'candidate_plan_created', 'plan': str(out), 'source_plan': str(source_plan),
            'beats': str(Path(args.beats).resolve()), 'every': every, 'use_downbeats': use_downbeats,
            'eligible_candidates': eligible_count,
            'selected_candidate_us': selected_us_list,
            'frame_numbers': frame_numbers, 'boundaries_us': boundaries,
            'duration_us': boundaries[-1], 'original_music_duration_us': original_duration,
            'input_plan_unchanged': True, 'warnings': warnings,
            'limitations': ['Offline candidate timing only; listening and editorial quality unverified',
                            'No project build or export performed']}


def write_plan(output, out, source_plan, before):
    # Validate beside the final file so its already-absolute media paths retain their meaning.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                         dir=out.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(output, handle, ensure_ascii=False, indent=2)
        normalized, _, warnings = editor.validate_plan(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if source_plan.read_bytes() != before:
        raise ValueError('Input plan changed during beat application')
    payload = json.dumps(normalized, ensure_ascii=False, indent=2)
    created = False
    try:
        with out.open('x', encoding='utf-8') as handle:
            created = True
            handle.write(payload)
    except BaseException:
        if created:
            out.unlink(missing_ok=True)
        raise
    return warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--beats', type=Path, required=True)
    parser.add_argument('--every', type=int, default=4)
    parser.add_argument('--use-downbeats', action='store_true',
                        help='Consume downbeats instead of all beats when --beats points to music-structure output')
    parser.add_argument('--mark-audio', action='store_true',
                        help='Map existing candidates to limited native source-clock audio beat_marks; do not cut clips')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        editor.emit(apply(args))
        return 0
    except (ValueError, TypeError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_plan_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
