"""Strict, host-independent validation for the initial Resolve editing subset.

Times are seconds on input and exact 30 fps frames after normalization. A
normalized document is data, not proof of its sources: the workflow must probe
the original plan again before host execution and compare its fingerprint.
"""
import hashlib
import json
import math
from pathlib import Path
import re


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f'{label}: expected object with text keys')
    unknown, missing = set(value) - set(allowed), set(required) - set(value)
    if unknown:
        raise ValueError(f'{label}: unsupported fields {sorted(unknown)}')
    if missing:
        raise ValueError(f'{label}: missing fields {sorted(missing)}')
    return value


def _number(value, label, low, high):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or not low <= value <= high:
        raise ValueError(f'{label}: expected finite number in [{low}, {high}]')
    return value


def _text(value, label, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError(f'{label}: expected nonempty text without control characters, max {maximum}')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'non-finite JSON constant: {value}')


def _local_path(raw, parent, label):
    _text(raw, f'{label}.path', 32760)
    portable = raw.replace('\\', '/')
    rest = portable[2:] if re.match(r'^[A-Za-z]:/', portable) else portable
    if portable.startswith('//') or ':' in rest:
        raise ValueError(f'{label}.path: only local file paths are supported')
    path = Path(raw)
    path = (parent / path).resolve() if not path.is_absolute() else path.resolve()
    if str(path).replace('\\', '/').startswith('//') or not path.is_file():
        raise ValueError(f'{label}.path: missing local file or resolved network path: {path}')
    return str(path)


def _frames(value, label, positive=False):
    value = _number(value, label, 0, 86400)
    frame = round(value * 30)
    if abs(value * 30 - frame) > 1e-6 or (positive and frame <= 0):
        raise ValueError(f'{label}: expected {"positive " if positive else ""}whole-frame time')
    return frame


def _normalize_captions(captions, timeline_frames, normalized=False):
    if type(timeline_frames) is not int or not 1 <= timeline_frames <= 18000:
        raise ValueError('captions: expected timeline duration in 1..18000 frames')
    if not isinstance(captions, list) or len(captions) > 512:
        raise ValueError('captions: expected at most 512 plain-text captions')
    result, end = [], 0
    fields = ('text', 'start', 'duration')
    if normalized:
        fields = (*fields, 'start_frames', 'duration_frames')
    for index, item in enumerate(captions):
        label = f'captions[{index}]'
        _object(item, fields, fields, label)
        text = item['text']
        if not isinstance(text, str):
            raise ValueError(f'{label}.text: expected plain text')
        text = text.replace('\r\n', '\n').replace('\u2028', '\n')
        lines = text.split('\n')
        if (len(text) > 500 or not 1 <= len(lines) <= 2 or any(not line.strip() for line in lines)
                or any((ord(c) < 32 and c != '\n') or 127 <= ord(c) <= 159
                       or 0xD800 <= ord(c) <= 0xDFFF or c == '\u2029' for c in text)):
            raise ValueError(f'{label}.text: expected 1..2 nonblank lines without control characters, max 500')
        if re.search(r'<[^>\n]*>|\{\\[^}\n]*\}', text):
            raise ValueError(f'{label}.text: subtitle style tags are unsupported')
        start = _frames(item['start'], f'{label}.start')
        duration = _frames(item['duration'], f'{label}.duration', True)
        if start < end:
            raise ValueError(f'{label}: captions must be in ascending order without overlap')
        end = start + duration
        if end > timeline_frames:
            raise ValueError(f'{label}: extends beyond timeline')
        value = {'text': text, 'start': start / 30, 'duration': duration / 30,
                 'start_frames': start, 'duration_frames': duration}
        if normalized and _canonical(value) != _canonical(item):
            raise ValueError(f'{label}: noncanonical text or derived fields')
        result.append(value)
    return result


def _media_info(raw, label, audio_only):
    if not isinstance(raw, dict):
        raise ValueError(f'{label}: probe must return an object')
    expected = 'audio' if audio_only else 'video'
    if raw.get('kind') != expected or type(raw.get('has_audio')) is not bool:
        raise ValueError(f'{label}: expected {expected} and boolean has_audio')
    info = {'kind': expected,
            'duration': _number(raw.get('duration'), f'{label} source duration', 1e-9, 86400 * 365),
            'has_audio': raw['has_audio']}
    if not audio_only:
        for axis in ('width', 'height'):
            if type(raw.get(axis)) is not int or not 1 <= raw[axis] <= 16384:
                raise ValueError(f'{label}: invalid source {axis}')
            info[axis] = raw[axis]
        info['fps'] = _number(raw.get('fps'), f'{label} source fps', 30, 30)
    if audio_only and not info['has_audio']:
        raise ValueError(f'{label}: source has no audio')
    if info['has_audio']:
        if type(raw.get('channels')) is not int or raw['channels'] != 2:
            raise ValueError(f'{label}: audio must be stereo')
        if type(raw.get('sample_rate')) is not int or raw['sample_rate'] != 48000:
            raise ValueError(f'{label}: audio sample rate must be 48000')
        info.update(channels=2, sample_rate=48000)
    return info


def _normalize(plan, parent, probe):
    _object(plan, ('schema_version', 'backend', 'name', 'canvas', 'clips', 'audio', 'captions'),
            ('schema_version', 'backend', 'name', 'canvas', 'clips'), 'plan')
    if type(plan['schema_version']) is not int or plan['schema_version'] != 1 or plan['backend'] != 'resolve':
        raise ValueError('expected schema_version=1 and backend="resolve"')
    name = _text(plan['name'], 'name', 120)
    canvas = _object(plan['canvas'], ('width', 'height', 'fps'), ('width', 'height', 'fps'), 'canvas')
    for axis in ('width', 'height'):
        if type(canvas[axis]) is not int or not 16 <= canvas[axis] <= 1920 or canvas[axis] % 2:
            raise ValueError(f'canvas.{axis}: expected even integer 16..1920')
    width, height = canvas['width'], canvas['height']
    if min(width, height) > 1080:
        raise ValueError('canvas: limited to 1080p landscape or portrait')
    if type(canvas['fps']) is not int or canvas['fps'] != 30:
        raise ValueError('canvas.fps: initial subset requires integer 30')
    if not isinstance(plan['clips'], list) or not 1 <= len(plan['clips']) <= 256:
        raise ValueError('clips: expected 1..256 sequential video clips')
    audio = plan.get('audio', [])
    if not isinstance(audio, list) or len(audio) > 16:
        raise ValueError('audio: expected at most 16 independent stereo audio clips')
    result = {'schema_version': 1, 'backend': 'resolve', 'name': name,
              'canvas': dict(canvas), 'clips': [], 'audio': [], 'captions': []}
    cache = {}
    data_levels = {}

    def media(item, label, audio_only):
        fields = ('path', 'source_in', 'duration', 'start', 'volume')
        _object(item, fields if audio_only else (*fields, 'transform', 'data_level'), fields[:4], label)
        path = _local_path(item['path'], parent, label)
        if path not in cache:
            cache[path] = probe(path)
        info = _media_info(cache[path], label, audio_only)
        normalized = {'path': path, 'info': info}
        if not audio_only:
            level = item.get('data_level', 'auto')
            if not isinstance(level, str) or level not in ('auto', 'full'):
                raise ValueError(f'{label}.data_level: expected auto or full')
            source = Path(path)
            if source in data_levels and data_levels[source] != level:
                raise ValueError(f'{label}.data_level: inconsistent data level for shared source')
            data_levels[source] = level
            normalized['data_level'] = level
        for field in ('source_in', 'duration', 'start'):
            frame = _frames(item[field], f'{label}.{field}', field == 'duration')
            normalized[field + '_frames'] = frame
            normalized[field] = frame / 30
        if normalized['source_in'] + normalized['duration'] > info['duration'] + 1e-7:
            raise ValueError(f'{label}: source selection exceeds measured duration')
        normalized['volume'] = _number(item.get('volume', 1), f'{label}.volume', 0, 2)
        if 0 < normalized['volume'] < 1e-5:
            raise ValueError(f'{label}.volume: nonzero gain must be at least 1e-5 (-100 dB)')
        return normalized

    cursor = 0
    limits = {'scale': (.01, 10), 'x': (-3.5 * width, 4.5 * width),
              'y': (-3.5 * height, 4.5 * height), 'rotation': (-360, 360), 'opacity': (0, 1)}
    for index, clip in enumerate(plan['clips']):
        label = f'clips[{index}]'
        value = media(clip, label, False)
        if value['start_frames'] != cursor:
            raise ValueError(f'{label}: clips must be sequential without gaps or overlap')
        cursor += value['duration_frames']
        if cursor > 600 * 30:
            raise ValueError('plan: initial subset is limited to 600 seconds')
        transform = _object(clip.get('transform', {}), limits, (), f'{label}.transform')
        value['transform'] = {'scale': 1, 'x': width / 2, 'y': height / 2, 'rotation': 0, 'opacity': 1}
        for key, number in transform.items():
            value['transform'][key] = _number(number, f'{label}.transform.{key}', *limits[key])
        result['clips'].append(value)
    result.update(duration_frames=cursor, duration=cursor / 30)
    for index, item in enumerate(audio):
        value = media(item, f'audio[{index}]', True)
        if value['start_frames'] + value['duration_frames'] > cursor:
            raise ValueError(f'audio[{index}]: extends beyond timeline')
        result['audio'].append(value)
    result['captions'] = _normalize_captions(plan.get('captions', []), cursor)
    return result


def validate_plan(plan_path, probe):
    """Read strict JSON and probe local sources; return an independent normalized plan."""
    path = Path(_local_path(str(plan_path), Path.cwd(), 'plan'))
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('plan: JSON exceeds 4 MiB')
    try:
        plan = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_pairs,
                          parse_constant=_constant)
    except RecursionError as exc:
        raise ValueError('plan: JSON nesting exceeds parser limit') from exc
    return _normalize(plan, path.parent, probe)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('normalized plan must contain finite JSON data') from exc


def validate_normalized(plan):
    """Revalidate shape/derived fields, not media authenticity; never trust receipt data."""
    _object(plan, ('schema_version', 'backend', 'name', 'canvas', 'clips', 'audio', 'captions',
                   'duration_frames', 'duration'),
            ('schema_version', 'backend', 'name', 'canvas', 'clips', 'audio', 'captions',
             'duration_frames', 'duration'), 'normalized plan')
    raw = {k: v for k, v in plan.items() if k not in ('duration_frames', 'duration', 'clips', 'audio')}
    infos = {}
    for group in ('clips', 'audio'):
        if not isinstance(plan[group], list):
            raise ValueError(f'{group}: expected list')
        raw[group] = []
        for index, item in enumerate(plan[group]):
            fields = ('path', 'source_in', 'duration', 'start', 'volume', 'info',
                      'source_in_frames', 'duration_frames', 'start_frames')
            fields = fields if group == 'audio' else (*fields, 'transform', 'data_level')
            _object(item, fields, fields, f'{group}[{index}]')
            path = item['path']
            _text(path, f'{group}[{index}].path', 32760)
            if _local_path(path, Path.cwd(), f'{group}[{index}]') != path:
                raise ValueError('normalized plan: source paths must be canonical absolute paths')
            if path in infos and _canonical(infos[path]) != _canonical(item['info']):
                raise ValueError('normalized plan: inconsistent source info for shared path')
            infos[path] = item['info']
            raw[group].append({k: v for k, v in item.items()
                               if k != 'info' and not k.endswith('_frames')})
    captions = _normalize_captions(plan['captions'], plan['duration_frames'], normalized=True)
    raw['captions'] = [{k: v for k, v in item.items() if not k.endswith('_frames')} for item in captions]
    result = _normalize(raw, Path.cwd(), lambda path: infos[path])
    if _canonical(result) != _canonical(plan):
        raise ValueError('normalized plan: noncanonical source info, paths or derived fields')
    return result


def compile_srt(plan):
    """Return plain SRT from a normalized plan without media or filesystem access."""
    captions = _normalize_captions(plan['captions'], plan['duration_frames'], normalized=True)

    def timestamp(frame):
        # Round each absolute frame time, never accumulate rounded durations.
        milliseconds = (frame * 1000 + 15) // 30
        seconds, milliseconds = divmod(milliseconds, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'

    blocks = []
    for index, caption in enumerate(captions, 1):
        start = caption['start_frames']
        end = start + caption['duration_frames']
        blocks.append(f'{index}\n{timestamp(start)} --> {timestamp(end)}\n{caption["text"]}\n\n')
    return ''.join(blocks)


def normalized_fingerprint(plan):
    """SHA256 identity of a revalidated canonical normalized document, not media hashes."""
    return hashlib.sha256(_canonical(validate_normalized(plan)).encode('utf-8')).hexdigest()
