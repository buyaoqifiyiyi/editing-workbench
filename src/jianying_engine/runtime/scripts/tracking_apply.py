"""Apply a measured offline trajectory to native image or bounded text position keys in a new plan."""
import argparse
from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import editor
import migrate


def validate(plan):
    with tempfile.TemporaryDirectory(prefix='jianying-tracking-apply-') as temporary:
        path = Path(temporary) / 'plan.json'
        editor.write_json(path, plan)
        return editor.validate_plan(path)


def execute(args):
    source_plan, output = Path(args.plan).resolve(), Path(args.out).resolve()
    if output.exists() or output == source_plan:
        raise FileExistsError(f'Output already exists; choose a new plan: {output}')
    plan, _, missing = migrate.remap_plan(source_plan, [])
    if missing:
        raise ValueError('Plan has missing media or nested local files; migrate them first')
    if plan.get('composites'):
        raise ValueError('Tracking application does not support composites')
    if type(args.source_index) is not int or not 0 <= args.source_index < len(plan['clips']):
        raise ValueError('Source index must select an existing clip')
    caption_index = getattr(args, 'caption_index', None)
    clip_index = getattr(args, 'target_index', None)
    is_text = caption_index is not None
    if is_text and clip_index is not None:
        raise ValueError('Choose exactly one target-index or caption-index')
    target_group = 'captions' if is_text else 'clips'
    target_index = caption_index if is_text else clip_index
    if type(target_index) is not int or not 0 <= target_index < len(plan.get(target_group, [])):
        raise ValueError('Target index must select an existing clip or caption')
    if not is_text and args.source_index == target_index:
        raise ValueError('Source and target must be different clips')
    offset_x = editor.number(getattr(args, 'offset_x', 0), 'offset-x', -1e6, 1e6)
    offset_y = editor.number(getattr(args, 'offset_y', 0), 'offset-y', -1e6, 1e6)
    if not is_text and (offset_x or offset_y):
        raise ValueError('Pixel offsets require a caption target')
    normalized, probes, _ = validate(plan)
    source = normalized['clips'][args.source_index]
    target = normalized[target_group][target_index]
    info = probes[source['path']]
    width, height = normalized['canvas']['width'], normalized['canvas']['height']
    if info['kind'] != 'video' or (info['width'], info['height']) != (width, height):
        raise ValueError('Source must be a video with pixel dimensions matching the canvas')
    if source['fit'] != 'contain' or source['transform'] or any(source.get(key) for key in
            ('keyframes', 'animations', 'motion_easing', 'speed_curve', 'transition', 'mask', 'effects')):
        raise ValueError('Source needs contain and untransformed linear geometry: no motion, crop, effects or speed_curve')
    if 'speed_curve' in plan['clips'][args.source_index]:
        raise ValueError('speed_curve cannot use linear tracking time mapping')
    if (target['start'], target['duration']) != (source['start'], source['duration']):
        raise ValueError('Target must match source start/duration')
    if is_text:
        if (target['x'] != 0 or target['y'] != 0 or target['transform'] or
                any(key in target for key in ('font', 'text_curve', 'glow')) or
                any(target.get(key) for key in ('keyframes', 'motion_easing', 'animations'))):
            raise ValueError('Caption target requires x=0/y=0, default font and no transform/motion/animations/curve/glow; use pixel offsets')
        if '\n' in target['text'] or '\r' in target['text'] or target['style'].get('vertical') or target['style'].get('auto_wrapping') is not False:
            raise ValueError('Caption target requires horizontal single-line text with style.auto_wrapping=false')
    else:
        target_info = probes[target['path']]
        if target_info['kind'] != 'image' or target['track'] == 'video' or target['fit'] != 'contain':
            raise ValueError('Target must be a contain image on an overlay track, not the video main track')
        if target['source_in'] != 0 or target['speed'] != 1:
            raise ValueError('Image target needs source_in=0 and speed=1')
        if set(target['transform']) - {'scale_x', 'scale_y', 'alpha'} or any(target.get(key) for key in
                ('keyframes', 'motion_easing', 'animations', 'speed_curve', 'transition', 'effects')):
            raise ValueError('Target must be static; only static scale/alpha transforms are retained')

    report = editor.read_json(Path(args.tracking).resolve())
    if not isinstance(report, dict) or report.get('schema_version') != 1 or report.get('method') != 'opencv_csrt':
        raise ValueError('Expected schema_version=1 opencv_csrt tracking report')
    if report.get('status') != 'tracked' or report.get('lost_at') is not None:
        raise ValueError('Lost or incomplete tracking cannot be applied')
    identity = report.get('source')
    if not isinstance(identity, dict) or not isinstance(identity.get('path'), str) or not Path(identity['path']).is_absolute():
        raise ValueError('Tracking source must have an absolute media path')
    media = Path(source['path'])
    with media.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if (Path(identity['path']).resolve() != media.resolve() or identity.get('bytes') != media.stat().st_size or
            not isinstance(identity.get('sha256'), str) or identity['sha256'].lower() != digest or
            (identity.get('width'), identity.get('height')) != (width, height)):
        raise ValueError('Tracking source path/hash/size/dimensions do not match the selected video')
    coverage_start = editor.us(editor.number(report.get('coverage_start'), 'coverage_start'))
    coverage_end = editor.us(editor.number(report.get('coverage_end'), 'coverage_end'))
    source_in = editor.us(source['source_in'])
    source_end = source_in + editor.us(source['duration'] * source['speed'])
    if not coverage_start <= source_in < source_end <= coverage_end <= editor.source_limit_us(info):
        raise ValueError('Tracking coverage must contain the full selected source interval within the video')
    samples = report.get('samples')
    if not isinstance(samples, list) or not samples:
        raise ValueError('Tracking samples must be a nonempty array')
    times, centers = [], []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError('Every tracking sample must be an object')
        time = editor.us(editor.number(sample.get('time'), 'sample time'))
        if times and time <= times[-1]:
            raise ValueError('Tracking samples must be strictly ordered in media microseconds')
        bbox = sample.get('bbox')
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError('Each bbox must have x, y, width and height')
        x, y, w, h = [editor.number(value, 'bbox coordinate', 0) for value in bbox]
        if w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError('Tracking bbox must remain inside the source pixels with positive size')
        times.append(time)
        centers.append((2 * (x + w / 2) / width - 1, 1 - 2 * (y + h / 2) / height))
    if times[0] != coverage_start or times[-1] > coverage_end:
        raise ValueError('Tracking sample endpoints disagree with coverage')

    def position(moment):
        left = bisect_right(times, moment) - 1
        if left < 0 or moment > coverage_end:
            raise ValueError('Tracking interpolation would extrapolate outside measured coverage')
        if left == len(times) - 1:
            return centers[left]  # Hold the last tracked frame only inside its declared display interval.
        ratio = (moment - times[left]) / (times[left + 1] - times[left])
        return tuple(a + (b - a) * ratio for a, b in zip(centers[left], centers[left + 1]))

    duration = editor.us(source['duration'])
    local_times = {0, duration - 1}
    local_times.update(round((time - source_in) / source['speed']) for time in times
                       if source_in < time < source_end)
    keys = []
    for local in sorted(time for time in local_times if 0 <= time < duration):
        # Source time is reconstructed from the actual quantized target key, not an unrounded float cue.
        moment = min(source_end, source_in + round(local * source['speed']))
        x, y = position(moment)
        if is_text:
            x, y = x + 2 * offset_x / width, y + 2 * offset_y / height
            if not -1 < x < 1 or not -1 < y < 1:
                raise ValueError('Caption anchor falls outside the canvas; reduce pixel offsets')
        keys.extend({'property': axis, 'time': local / 1_000_000, 'value': value}
                    for axis, value in (('position_x', x), ('position_y', y)))
    plan[target_group][target_index]['keyframes'] = keys
    validate(plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return {'status': 'passed', 'output_plan': str(output), 'keyframes': len(keys),
            'target_group': target_group, 'target_index': target_index,
            'pixel_offset': {'x_right': offset_x, 'y_up': offset_y},
            'source_modified': False, 'plan_validation': 'passed', 'ui_compatibility': 'unverified',
            'boundaries': [('Original editable text with local position_x/y keys; glyph bounds/readability require visual review.'
                            if is_text else 'Original image overlay with editable position_x/y keys; no baked video.'),
                           'Linear interpolation between samples; last-frame hold only inside declared coverage.',
                           'No playback/export or reliable privacy masking claim.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('--tracking', required=True)
    parser.add_argument('--source-index', required=True, type=int)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--target-index', type=int, help='Existing static overlay image in clips')
    target.add_argument('--caption-index', type=int, help='Existing static default-font single-line caption, x=y=0 and auto_wrapping=false')
    parser.add_argument('--offset-x', type=float, default=0, help='Caption-only horizontal canvas pixels, positive right')
    parser.add_argument('--offset-y', type=float, default=0, help='Caption-only vertical canvas pixels, positive up')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        editor.emit(execute(args))
        return 0
    except (ValueError, OSError, TypeError, KeyError, ImportError) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
