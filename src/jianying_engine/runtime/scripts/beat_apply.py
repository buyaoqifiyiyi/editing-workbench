"""Explicit candidate-beat cuts into a new plan; never changes the input or builds a draft."""
import argparse
import copy
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
import tempfile
import editor


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
    if len(plan['clips']) < 2 or any(plan.get(k) for k in
            ('captions', 'effects', 'filters', 'composites', 'jianying_tasks', 'missing_assets')):
        raise ValueError('Beat application requires at least two plain primary clips and no captions/effects/composites/tasks/missing assets')
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
    result = beats.load_result(Path(args.beats).resolve())
    media = Path(result['source']['path']).resolve()
    if Path(music['path']).resolve() != media:
        raise ValueError('Beat report source must be the exact plan music source')
    selection = result['selection']
    rate = selection['sample_rate']
    offset = editor.us(music['source_in'])
    original_duration = editor.us(music['duration'])
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
    fps = Fraction(str(plan['canvas']['fps']))
    frame_numbers = [0] + [round(Fraction(numerator, rate * 1_000_000) * fps) for numerator in chosen]
    if any(a >= b for a, b in zip(frame_numbers, frame_numbers[1:])):
        raise ValueError('Beat frame quantization produced duplicate or reversed boundaries')
    frame_times = [Fraction(frame * 1_000_000, 1) / fps for frame in frame_numbers]
    boundaries = [round(point) for point in frame_times]
    detection_start = Fraction(selection['start_us'] - offset, 1)
    detection_end = detection_start + Fraction(selection['sample_count'] * 1_000_000, rate)
    if any(not (0 < point < original_duration and detection_start <= point < detection_end)
           for point in frame_times[1:] + boundaries[1:]):
        raise ValueError('Quantized cuts fall outside the music range or analyzed coverage')
    output = copy.deepcopy(plan)
    output['name'] = plan['name'][:90] + '-节拍候选'
    for index, clip in enumerate(output['clips']):
        clip['start'] = boundaries[index] / 1_000_000
        clip['duration'] = (boundaries[index + 1] - boundaries[index]) / 1_000_000
    output['audio'][0]['duration'] = boundaries[-1] / 1_000_000
    output['notes'].append('按离线节拍候选生成；未经试听，不代表精准音乐拍点或剪映官方原生 beat marker。')
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
    return {'status': 'candidate_plan_created', 'plan': str(out), 'source_plan': str(source_plan),
            'beats': str(Path(args.beats).resolve()), 'every': every, 'eligible_candidates': len(candidates),
            'selected_candidate_us': [round(Fraction(numerator, rate)) for numerator in chosen],
            'frame_numbers': frame_numbers, 'boundaries_us': boundaries,
            'duration_us': boundaries[-1], 'original_music_duration_us': original_duration,
            'input_plan_unchanged': True, 'warnings': warnings,
            'limitations': ['Offline candidate timing only; listening and editorial quality unverified',
                            'No official native beat markers, draft build, UI save/reopen or export performed']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--beats', type=Path, required=True)
    parser.add_argument('--every', type=int, default=4)
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
