"""Create a new editable plan with safely reusable overlay video tracks."""
import argparse
import copy
import hashlib
import json
import tempfile
from pathlib import Path

import editor

GROUPS = ('clips', 'audio', 'captions', 'effects', 'filters')


def overlaps(a, b):
    return editor.us(a['start']) < editor.end_us(b) and editor.us(b['start']) < editor.end_us(a)


def tracks_by_name(plan):
    result = {}
    for group in GROUPS:
        for item in plan[group]:
            result.setdefault(item['track'], []).append(item)
    return result


def ordering_pairs(plan):
    """All overlapping item pairs and their relative original layer order."""
    order = {track: i for i, track in enumerate(plan['layer_order'])}
    items = [(group, i, item) for group in GROUPS for i, item in enumerate(plan[group])]
    result = {}
    for i, (group_a, index_a, a) in enumerate(items):
        for group_b, index_b, b in items[i + 1:]:
            if overlaps(a, b):
                if a['track'] == b['track']:
                    raise ValueError('Overlapping items must not share a track')
                result[(group_a, index_a, group_b, index_b)] = order[a['track']] < order[b['track']]
    return result


def compact(plan):
    """Work on validated plan; retain main, tasks and groups as barriers.

    shortcut: greedy whole-track reuse; revisit only if a real project needs
    segment-level packing or composite-aware motion with native verification.
    """
    result = copy.deepcopy(plan)
    if 'layer_order' not in result:
        return result, {'status': 'unchanged', 'reason': 'Explicit layer_order required; default rendering mode stays unchanged',
                        'merges': [], 'clip_mapping': []}
    order = result['layer_order']
    original_pairs = ordering_pairs(result)
    original_clips = [clip['track'] for clip in result['clips']]
    grouped = {name for spec in result['composites'] for name in spec['tracks']}
    barriers = {x['track'] for group in ('effects','filters') for x in result[group]}
    main_end = max(editor.end_us(x) for x in result['clips'] if x['track'] == 'video')
    protected = {'video'} | grouped | {x['track'] for x in result.get('jianying_tasks', [])} | {
        x['track'] for x in result.get('manual_tasks', [])}
    # An existing multi-clip lane may already represent a deliberate reusable
    # editing role (e.g. left page, dialogue). Preserve it instead of making
    # the timeline less legible simply to minimize the track count.
    clip_counts = {}
    for clip in result['clips']:
        clip_counts[clip['track']] = clip_counts.get(clip['track'], 0) + 1
    protected |= {name for name, count in clip_counts.items() if count > 1}
    protected |= {c['track'] for c in result['clips'] if any(k in c for k in
                   ('transition', 'matting_cache', 'mix_mode', 'chroma', 'speed_curve', 'motion_easing'))}
    merges = []
    # Only video-overlay tracks are candidates. Moving a higher lane down past
    # any active intermediate lane can change the stacking of rendered pixels.
    for moving in list(order):
        if moving not in order or moving in protected:
            continue
        clips = [x for x in result['clips'] if x['track'] == moving]
        if not clips:
            continue
        j = order.index(moving)
        for target in order[:j]:
            if target in protected or not any(x['track'] == target for x in result['clips']):
                continue
            i = order.index(target)
            between = order[i + 1:j]
            if barriers.intersection(between):
                continue
            # Group members remain contiguous; crossing the whole group is
            # safe only when the moving lane is absent for the group's entire
            # outer range, including intervals when inner tracks are empty.
            crossed = [spec for spec in result['composites'] if set(spec['tracks']).intersection(between)]
            if any(overlaps(item, {'start': group_start / 1_000_000,
                                   'duration': group_duration / 1_000_000})
                   for spec in crossed
                   for group_start, group_duration in [editor.composite_range(spec, main_end)]
                   for item in clips):
                continue
            lanes = tracks_by_name(result)
            if any(overlaps(a, b) for a in clips for b in lanes[target]):
                continue
            if any(overlaps(a, b) for a in clips for name in between for b in lanes[name]):
                continue
            for item in clips:
                item['track'] = target
            order.remove(moving)
            merges.append({'from': moving, 'into': target})
            break
    if ordering_pairs(result) != original_pairs:
        raise ValueError('Track compaction would change overlapping item layer order')
    # A shared lane no longer has one effect role; label it honestly while the
    # report retains every original track name and per-clip mapping.
    renamed = {}
    for target in dict.fromkeys(m['into'] for m in merges):
        index = 1
        while f'复用叠层{index}' in order or f'复用叠层{index}' in renamed.values():
            index += 1
        renamed[target] = f'复用叠层{index}'
    for old, new in renamed.items():
        order[order.index(old)] = new
        for clip in result['clips']:
            if clip['track'] == old:
                clip['track'] = new
    if ordering_pairs(result) != original_pairs:
        raise ValueError('Track renaming would change overlapping item layer order')
    mapping = [{'clip_index': i, 'from': before, 'to': clip['track'], 'start': clip['start'],
                'duration': clip['duration'], 'reason': clip.get('reason', '')}
               for i, (before, clip) in enumerate(zip(original_clips, result['clips'])) if before != clip['track']]
    return result, {'status': 'compacted' if merges else 'unchanged',
                    'reason': None if merges else 'No whole-track merge passed the safety constraints',
                    'merges': merges, 'renamed_tracks': renamed, 'clip_mapping': mapping}


def run(plan_path, out):
    source, output = Path(plan_path).resolve(), Path(out).resolve()
    report = output.with_name(output.stem + '.layout.json')
    if source in (output, report) or output.exists() or report.exists():
        raise ValueError('Choose a new plan and report path; existing files are never overwritten')
    if not output.parent.is_dir():
        raise ValueError('Output parent must already exist')
    before = source.read_bytes()
    plan, _, _ = editor.validate_plan(source)
    result, details = compact(plan)
    if details['status'] == 'unchanged':
        return {**details, 'input': str(source), 'output': None}
    result['name'] = plan['name'][:90] + '-轨道整理'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                         dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, ensure_ascii=False, indent=2)
        normalized, _, warnings = editor.validate_plan(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if source.read_bytes() != before:
        raise ValueError('Source plan changed during track compaction')
    payload = json.dumps(normalized, ensure_ascii=False, indent=2)
    audit = {**details, 'input': str(source), 'input_sha256': hashlib.sha256(before).hexdigest(),
             'output': str(output), 'output_sha256': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
             'tracks_before': len(plan['layer_order']), 'tracks_after': len(normalized['layer_order']),
             'scope': 'whole video-overlay tracks only; explicit ordering; grouped and task tracks protected',
             'creative_content': 'unchanged', 'warnings': warnings,
             'native_visual_review': 'not_performed', 'human_editing_and_save_reopen': 'not_performed'}
    created = []
    try:
        with output.open('x', encoding='utf-8', newline='\n') as handle:
            created.append(output)
            handle.write(payload)
        with report.open('x', encoding='utf-8', newline='\n') as handle:
            created.append(report)
            json.dump(audit, handle, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError):
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {'status': details['status'], 'input': str(source), 'output': str(output), 'report': str(report),
            'tracks_before': audit['tracks_before'], 'tracks_after': audit['tracks_after'],
            'merge_count': len(details['merges']), 'source_unchanged': True,
            'native_visual_review': 'not_performed', 'human_editing_and_save_reopen': 'not_performed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        editor.emit(run(args.plan, args.out))
        return 0
    except (ValueError, TypeError, OSError, KeyError, ImportError) as exc:
        editor.emit({'status': 'failed', 'error': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
