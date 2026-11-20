"""Core pure open-source runtime primitives, media probe, and open edit-plan validation."""
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import file_hash
import runtime_common
import runtime_paths

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'}
AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.aiff'}
VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.gif', '.m4v', '.mxf', '.ts'}
MEDIA_EXT = IMAGE_EXT | AUDIO_EXT | VIDEO_EXT

TICKS_PER_SEC = 254_016_000_000
KEYFRAMES = {'uniform_scale', 'scale_x', 'scale_y', 'position_x', 'position_y', 'rotation', 'alpha'}
TRANSFORMS = {'scale_x', 'scale_y', 'transform_x', 'transform_y', 'rotation', 'alpha',
              'flip_horizontal', 'flip_vertical', 'position'}
EASINGS = {'linear', 'bezier', 'hold', 'easeIn', 'easeOut', 'smoothstep'}

SKILL_ROOT = Path(__file__).resolve().parents[1]


def py():
    """Return python executable matching pythonw sibling fallback."""
    exe = Path(sys.executable)
    if exe.name.lower() == 'pythonw.exe' and exe.with_name('python.exe').is_file():
        return str(exe.with_name('python.exe'))
    return str(exe)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def reject_constant(value):
    raise ValueError(f'JSON contains non-finite value: {value}')


def json_pairs(pairs):
    return runtime_common.unique_json_pairs(pairs)


def read_json(path, *, unique_keys=False, max_bytes=None):
    target = Path(path)
    if max_bytes is not None and target.stat().st_size > max_bytes:
        raise ValueError(f'JSON exceeds {max_bytes} bytes')
    kwargs = {'parse_constant': reject_constant}
    if unique_keys:
        kwargs['object_pairs_hook'] = json_pairs
    return json.loads(target.read_text(encoding='utf-8-sig'), **kwargs)


def new_directory(path):
    target = Path(path).resolve()
    target.mkdir(parents=True, exist_ok=False)
    return target


def tool_path(name):
    for prefix in ('OPENEDIT_', 'JIANYING_'):
        override = os.environ.get(prefix + name.upper())
        if override is not None:
            override = override.strip()
            if not override:
                continue
            candidate = Path(override)
            return str(candidate.resolve()) if candidate.is_file() else None
    located = shutil.which(name)
    if located and Path(located).is_file():
        return str(Path(located).resolve())
    suffix = '.exe' if os.name == 'nt' else ''
    for root in (SKILL_ROOT, runtime_paths.RAW_ROOT):
        local = root / 'tools/bin' / (name + suffix)
        if local.is_file():
            return str(local.resolve())
    return None


def tool(name):
    found = tool_path(name)
    if not found:
        raise ValueError(f'Required open-source media tool unavailable: {name}')
    return found


def run(args, timeout=120):
    result = subprocess.run(
        [str(x) for x in args],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if result.returncode:
        raise ValueError(f'{Path(str(args[0])).name} failed: {result.stderr[-2000:]}')
    return result


def us(seconds):
    return round(seconds * 1_000_000)


def end_us(item):
    return us(item['start']) + us(item['duration'])


def number(value, label, low=0, high=None):
    return runtime_common.check_number(value, label, low, high, style='editor')


def fields(obj, allowed, label, required=()):
    return runtime_common.check_object(obj, allowed, required, label)


def check_text(value, label, maximum=4000):
    return runtime_common.check_text(value, label, maximum)


def _frames_fraction(seconds, label, exact=True, fps=30):
    if exact and abs(round(seconds * fps) - seconds * fps) > 1e-6:
        raise ValueError(f'{label} must align to 1/{fps}s frame grid')
    return round(seconds * fps)


def local_media_path(value, base_dir, kind='file'):
    if not isinstance(value, str) or not value.strip() or '://' in value or '\0' in value:
        raise ValueError(f'Invalid local path for {kind}')
    path = Path(value)
    if not path.is_absolute():
        path = Path(base_dir) / path
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f'Media file does not exist for {kind}: {resolved}')
    return str(resolved)


def snap_range(item, label='item'):
    """Quantize start and duration to exact integer microseconds; mutates start/duration."""
    start = number(item.get('start'), f'{label}.start', 0, 86400)
    duration = number(item.get('duration'), f'{label}.duration', 0.000001, 86400)
    start_u, end_u = us(start), us(start + duration)
    if end_u <= start_u:
        raise ValueError(f'{label}.duration rounds below one microsecond')
    item['start'], item['duration'] = start_u / 1_000_000, (end_u - start_u) / 1_000_000
    return start_u, end_u


def source_limit_us(info):
    if info['kind'] == 'image':
        return 3600 * 1_000_000
    return info['duration_us']


def composite_range(spec, total):
    return us(spec.get('start', 0)), us(spec.get('duration', total / 1_000_000))


def validate_parameter_keys(keys, duration, label, low=0, high=10, source_in=0, speed=1):
    """Validate strictly increasing clip-local parameter keyframes starting at time 0."""
    if not isinstance(keys, list) or not keys:
        raise ValueError(f'{label}.keyframes must be a non-empty array')
    last_local, last_source = -1, -1
    for key in keys:
        fields(key, {'time', 'value'}, label + '.keyframe', ('time', 'value'))
        t = number(key['time'], label + '.keyframe.time', 0, duration - 0.000001)
        number(key['value'], label + '.keyframe.value', low, high)
        u = us(t)
        su = us(source_in + t * speed)
        if last_local == -1 and u != 0:
            raise ValueError(f'{label}.keyframes must start at time zero')
        if u <= last_local or su <= last_source:
            raise ValueError(f'{label} keyframes must remain strictly increasing in local and source time')
        last_local, last_source = u, su


def _parse_fps(rate_text):
    if not rate_text or '/' not in rate_text:
        return 0.0
    num, den = rate_text.split('/', 1)
    try:
        den_i = int(den)
        return float(Fraction(int(num), den_i)) if den_i else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path):
    """Probe local audio, video, or image metadata using ffprobe."""
    target = Path(path).resolve()
    if not target.is_file():
        raise ValueError(f'Media file missing: {target}')
    ext = target.suffix.lower()
    if ext not in MEDIA_EXT:
        raise ValueError(f'Unsupported media file type: {ext} ({target.name})')

    out = run([
        tool('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(target)
    ])
    data = json.loads(out.stdout)
    streams = data.get('streams', [])
    fmt = data.get('format', {})
    vstreams = [s for s in streams if s.get('codec_type') == 'video']
    astreams = [s for s in streams if s.get('codec_type') == 'audio']

    if not vstreams and not astreams:
        raise ValueError(f'No audio or video streams found in {target}')

    if ext in IMAGE_EXT or (vstreams and ext in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif'}):
        vs = vstreams[0]
        return {
            'path': str(target),
            'kind': 'image',
            'width': int(vs.get('width', 0)),
            'height': int(vs.get('height', 0)),
            'fps': 0.0,
            'duration': 0.0,
            'duration_us': 0,
            'has_video': True,
            'has_audio': False,
            'video_codec': vs.get('codec_name'),
            'audio_codec': None,
            'pix_fmt': vs.get('pix_fmt'),
            'has_alpha': 'a' in (vs.get('pix_fmt') or ''),
            'vfr': False,
            'sample_rate': None,
            'channels': None,
        }

    if vstreams and ext not in AUDIO_EXT:
        vs = vstreams[0]
        raw_dur = vs.get('duration') or fmt.get('duration') or '0'
        dur = float(raw_dur)
        avg_fps = _parse_fps(vs.get('avg_frame_rate'))
        r_fps = _parse_fps(vs.get('r_frame_rate'))
        fps = avg_fps or r_fps or 30.0
        vfr = bool(avg_fps and r_fps and abs(avg_fps - r_fps) > 0.05)
        pix_fmt = vs.get('pix_fmt') or ''
        return {
            'path': str(target),
            'kind': 'video',
            'width': int(vs.get('width', 0)),
            'height': int(vs.get('height', 0)),
            'fps': round(fps, 6),
            'duration': dur,
            'duration_us': round(dur * 1_000_000),
            'has_video': True,
            'has_audio': bool(astreams),
            'video_codec': vs.get('codec_name'),
            'audio_codec': astreams[0].get('codec_name') if astreams else None,
            'pix_fmt': pix_fmt,
            'has_alpha': bool(re.search(r'(yuva|gbrap|rgba|bgra|argb|abgr)', pix_fmt)),
            'vfr': vfr,
            'sample_rate': int(astreams[0].get('sample_rate', 0)) if astreams else None,
            'channels': int(astreams[0].get('channels', 0)) if astreams else None,
        }

    aus = astreams[0]
    if 'duration_ts' in aus and 'time_base' in aus and '/' in str(aus['time_base']):
        tb_num, tb_den = map(int, str(aus['time_base']).split('/', 1))
        dur = float(Fraction(int(aus['duration_ts']) * tb_num, tb_den)) if tb_den else float(aus.get('duration') or fmt.get('duration') or 0)
    else:
        dur = float(aus.get('duration') or fmt.get('duration') or 0)
    return {
        'path': str(target),
        'kind': 'audio',
        'width': 0,
        'height': 0,
        'fps': 0.0,
        'duration': dur,
        'duration_us': round(dur * 1_000_000),
        'has_video': False,
        'has_audio': True,
        'video_codec': None,
        'audio_codec': aus.get('codec_name'),
        'pix_fmt': None,
        'has_alpha': False,
        'vfr': False,
        'sample_rate': int(aus.get('sample_rate', 0)) if aus.get('sample_rate') else None,
        'channels': int(aus.get('channels', 0)) if aus.get('channels') else None,
    }


def _validate_keyframes(keys, duration, source_in=0.0, speed=1.0, label='item'):
    if not isinstance(keys, list):
        raise ValueError(f'{label}.keyframes must be an array')
    seen = set()
    for key in keys:
        fields(key, {'property', 'time', 'value', 'interpolation'}, f'{label}.keyframe', ('property', 'time', 'value'))
        prop = key['property']
        if prop not in KEYFRAMES:
            raise ValueError(f'Unsupported keyframe property: {prop}')
        t = number(key['time'], f'{label}.keyframe.time', 0, duration)
        number(
            key['value'],
            f'{label}.keyframe.value',
            0.000001 if 'scale' in prop else 0 if prop == 'alpha' else -10000,
            1 if prop == 'alpha' else 10000,
        )
        if 'interpolation' in key and key['interpolation'] not in EASINGS:
            raise ValueError(f'Unsupported keyframe interpolation: {key["interpolation"]}')
        ident = (prop, us(t))
        if ident in seen:
            raise ValueError(f'Duplicate keyframe property/time on {label}')
        seen.add(ident)


def _validate_grading(grade, label):
    if not isinstance(grade, dict) or not grade:
        raise ValueError(f'{label}.grading must be a non-empty object')
    allowed = {
        'exposure', 'contrast', 'saturation', 'creative_sat', 'highlights', 'shadows',
        'whites', 'blacks', 'temperature', 'tint', 'vibrance', 'sharpness', 'vignette',
        'luma_curve', 'rgb_curves', 'red_hsl', 'green_hsl', 'blue_hsl', 'hsl',
        'wheels', 'primary_wheel', 'lut', 'look', 'local_circle',
    }
    fields(grade, allowed, f'{label}.grading')
    for scalar_key in ('exposure', 'contrast', 'saturation', 'creative_sat', 'highlights', 'shadows',
                       'whites', 'blacks', 'temperature', 'tint', 'vibrance', 'sharpness', 'vignette'):
        if scalar_key in grade:
            number(grade[scalar_key], f'{label}.grading.{scalar_key}', -200, 300)
    curves = {}
    if 'luma_curve' in grade:
        curves['luma'] = grade['luma_curve']
    if 'rgb_curves' in grade:
        fields(grade['rgb_curves'], {'red', 'green', 'blue'}, f'{label}.grading.rgb_curves')
        curves.update(grade['rgb_curves'])
    for ch, points in curves.items():
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError(f'{label}.grading curve {ch} requires at least two [x, y] anchors')
        last_x = -1.0
        for pt in points:
            if not isinstance(pt, list) or len(pt) != 2:
                raise ValueError(f'{label}.grading curve {ch} anchor must be [x, y]')
            x = number(pt[0], f'{label}.grading.{ch}.x', 0, 1)
            number(pt[1], f'{label}.grading.{ch}.y', 0, 1)
            if x <= last_x:
                raise ValueError(f'{label}.grading curve {ch} x coordinates must strictly increase')
            last_x = x


def _validate_transform(transform, label):
    fields(transform, TRANSFORMS, f'{label}.transform')
    for k, val in transform.items():
        if k.startswith('flip_'):
            if type(val) is not bool:
                raise ValueError(f'{label}.transform.{k} must be boolean')
        elif k == 'position':
            if not isinstance(val, list) or len(val) != 2:
                raise ValueError(f'{label}.transform.position must be [x, y]')
            number(val[0], f'{label}.transform.position[0]', -10000, 20000)
            number(val[1], f'{label}.transform.position[1]', -10000, 20000)
        else:
            number(
                val,
                f'{label}.transform.{k}',
                0.000001 if k.startswith('scale_') else 0 if k == 'alpha' else -10000,
                1 if k == 'alpha' else 10000,
            )


def _register_track(track, track_type, item, label, tracks, track_types):
    if not isinstance(track, str) or not track.strip():
        raise ValueError(f'{label}.track must be a non-empty string')
    if track in track_types and track_types[track] != track_type:
        raise ValueError(f'Track name used by different media types: {track}')
    track_types[track] = track_type
    tracks.setdefault(track, []).append(item)


def _validate_captions(plan, base_dir, tracks, track_types):
    captions = plan.setdefault('captions', [])
    if not isinstance(captions, list):
        raise ValueError('captions must be an array')
    for index, item in enumerate(captions):
        label = f'captions[{index}]'
        allowed = {
            'text', 'start', 'duration', 'track', 'kind', 'size', 'color', 'font', 'font_style', 'font_path',
            'x', 'y', 'position', 'border', 'stroke', 'stroke_color', 'stroke_width',
            'background', 'background_color', 'shadow', 'align', 'style', 'bold', 'italic',
            'highlights', 'keyframes', 'motion_easing', 'transform', 'scale', 'rotation', 'opacity',
        }
        fields(item, allowed, label, ('text', 'start', 'duration'))
        if not isinstance(item['text'], str) or not item['text'].strip() or len(item['text']) > 4000:
            raise ValueError(f'{label}.text must be non-empty text (<= 4000 chars)')
        number(item['start'], f'{label}.start', 0, 86400)
        duration = number(item['duration'], f'{label}.duration', 0.000001, 86400)
        snap_range(item, label)
        duration = item['duration']
        kind = item.setdefault('kind', 'graphic')
        if kind not in ('graphic', 'subtitle'):
            raise ValueError(f'{label}.kind must be graphic or subtitle')
        item.setdefault('track', 'C1' if kind == 'subtitle' else 'captions')
        _register_track(item['track'], 'text', item, label, tracks, track_types)
        item.setdefault('x', 0.0)
        item.setdefault('y', -0.7)
        item.setdefault('bold', False)
        item.setdefault('border', True)
        item.setdefault('color', '#FFFFFF')
        item.setdefault('size', 48)
        item.setdefault('transform', {})
        item.setdefault('style', {})
        number(item['x'], f'{label}.x', -10000, 10000)
        number(item['y'], f'{label}.y', -10000, 10000)
        number(item['size'], f'{label}.size', 1, 500)
        if type(item['bold']) is not bool:
            raise ValueError(f'{label}.bold must be boolean')
        if type(item['border']) is not bool:
            raise ValueError(f'{label}.border must be boolean')
        if not isinstance(item['style'], dict):
            raise ValueError(f'{label}.style must be an object')
        _validate_transform(item['transform'], label)
        for color_key in ('color', 'stroke_color', 'background_color'):
            if color_key in item:
                if not isinstance(item[color_key], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', item[color_key]):
                    raise ValueError(f'{label}.{color_key} must be #RRGGBB or #RRGGBBAA')
        if 'font_path' in item:
            item['font_path'] = local_media_path(item['font_path'], base_dir, f'{label}.font_path')
        if 'highlights' in item:
            spans = item['highlights']
            if not isinstance(spans, list) or not spans:
                raise ValueError(f'{label}.highlights must be a non-empty array')
            for span in spans:
                fields(span, {'start', 'end', 'color'}, f'{label}.highlight', ('start', 'end', 'color'))
                if (type(span['start']) is not int or type(span['end']) is not int or
                        not 0 <= span['start'] < span['end'] <= len(item['text'])):
                    raise ValueError(f'{label}.highlight requires integer 0 <= start < end <= len(text)')
                if not isinstance(span['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', span['color']):
                    raise ValueError(f'{label}.highlight.color must be #RRGGBB')
            spans.sort(key=lambda s: s['start'])
            if any(a['end'] > b['start'] for a, b in zip(spans, spans[1:])):
                raise ValueError(f'{label}.highlights spans must not overlap')
        if 'keyframes' in item:
            _validate_keyframes(item['keyframes'], duration, label=label)


def _validate_overlays(plan, base_dir, canvas, tracks, track_types):
    overlays = plan.setdefault('overlays', [])
    if not isinstance(overlays, list):
        raise ValueError('overlays must be an array')
    seen_names = set()
    for index, ov in enumerate(overlays):
        label = f'overlays[{index}]'
        fields(ov, {'id', 'name', 'track', 'start', 'duration', 'width', 'height', 'fps',
                    'layers', 'script', 'effects', 'out_format', 'gpu'},
               label, ('start', 'duration'))
        name = ov.setdefault('name', ov.get('id') or f'overlay_{index + 1}')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}', name):
            raise ValueError(f'{label}.name must be 1..64 safe identifier characters')
        if name in seen_names:
            raise ValueError(f'Duplicate overlay name: {name}')
        seen_names.add(name)
        ov.setdefault('track', 'overlay')
        number(ov['start'], f'{label}.start', 0, 86400)
        number(ov['duration'], f'{label}.duration', 0.04, 3600)
        snap_range(ov, label)
        _register_track(ov['track'], 'video', ov, label, tracks, track_types)
        ov.setdefault('width', canvas['width'])
        ov.setdefault('height', canvas['height'])
        ov.setdefault('fps', canvas['fps'])
        number(ov['width'], f'{label}.width', 64, 7680)
        number(ov['height'], f'{label}.height', 64, 4320)
        number(ov['fps'], f'{label}.fps', 1, 120)
        if 'layers' not in ov and 'script' not in ov:
            raise ValueError(f'{label} must specify layers or script')
        if 'layers' in ov and not isinstance(ov['layers'], list):
            raise ValueError(f'{label}.layers must be an array')
        for li, layer in enumerate(ov.get('layers', [])):
            if not isinstance(layer, dict):
                raise ValueError(f'{label}.layers[{li}] must be an object')
            if layer.get('type') == 'footage' and 'path' in layer:
                layer['path'] = local_media_path(layer['path'], base_dir, f'{label}.layers[{li}].path')


def provenance(plan):
    """Verify .provenance.json sidecar checksums & review flags for selected media."""
    paths = [x['path'] for group in ('clips', 'audio') for x in plan.get(group, [])]
    for ov in plan.get('overlays', []):
        for layer in ov.get('layers', []):
            if layer.get('type') == 'footage' and layer.get('path'):
                paths.append(layer['path'])
    records = []
    for material in dict.fromkeys(paths):
        path = Path(material)
        sidecar = path.with_name(path.name + '.provenance.json')
        if sidecar.is_file():
            item = read_json(sidecar)
            if (not isinstance(item, dict)
                    or item.get('license_verification') != 'agent_review_recorded'
                    or not item.get('review_note')
                    or item.get('sha256') != file_hash.sha256(path)):
                raise ValueError(f'Material provenance missing review or checksum mismatch: {path.name}')
            records.append(item)
        else:
            records.append({
                'local_path': str(path),
                'license_verification': 'not_recorded',
                'attribution': '',
                'rights_clearance': 'not_verified',
            })
    return records


def source_delivery(records):
    """Render concise Markdown ledger of verified & unrecorded source provenance."""
    lines = [
        '# 素材来源与授权记录',
        '',
        '来源记录与核查备注不构成完整权利担保；画面中人物、商标及最终用途仍需核对。',
        '',
    ]
    for item in records:
        lines += [
            f'## {Path(item["local_path"]).name}',
            '',
            f'- 核查状态：{item["license_verification"]}',
            f'- 来源/署名：{item.get("attribution") or "未登记；不推断为免费素材"}',
            f'- 核查备注：{item.get("review_note", "未记录")}',
            f'- SHA256：{item.get("sha256", "未记录")}',
            '',
        ]
        if item.get('license') == 'by-sa':
            lines += ['- CC BY-SA：使用者已记录相同方式分享安排，需按核查备注执行。', '']
    return '\n'.join(lines) + '\n'


def validate_plan(plan_path, user_data=None):
    """Validate an open edit-plan.json (v1/v2) and probe all referenced local media files."""
    del user_data  # Pure open-source engine does not bind external proprietary user data.
    plan_file = Path(plan_path).resolve()
    raw = read_json(plan_file)
    if not isinstance(raw, dict):
        raise ValueError('Edit plan must be a JSON object')
    plan = json.loads(json.dumps(raw, ensure_ascii=False))
    allowed_root = {
        'schema_version', 'name', 'mode', 'backend', 'canvas', 'clips', 'audio', 'captions',
        'overlays', 'motion_graphics', 'effects', 'filters', 'composites', 'layer_order',
        'notes', 'missing_assets', 'export', 'allow_vip_effects',
    }
    fields(plan, allowed_root, 'edit-plan', ('schema_version', 'name', 'canvas', 'clips'))
    if type(plan['schema_version']) is not int or plan['schema_version'] not in (1, 2):
        raise ValueError('Edit plan requires integer schema_version 1 or 2')
    check_text(plan['name'], 'plan.name', 200)
    plan.setdefault('notes', [])
    if not isinstance(plan['notes'], list):
        raise ValueError('notes must be an array')
    plan.setdefault('missing_assets', [])
    if not isinstance(plan['missing_assets'], list):
        raise ValueError('missing_assets must be an array')

    canvas = plan['canvas']
    fields(canvas, {'width', 'height', 'fps'}, 'canvas', ('width', 'height', 'fps'))
    if any(type(canvas[k]) is not int for k in ('width', 'height')):
        raise ValueError('canvas width and height must be integers')
    number(canvas['width'], 'canvas.width', 64, 7680)
    number(canvas['height'], 'canvas.height', 64, 4320)
    number(canvas['fps'], 'canvas.fps', 1, 120)

    if 'motion_graphics' in plan and 'overlays' not in plan:
        plan['overlays'] = plan.pop('motion_graphics')

    clips = plan['clips']
    if not isinstance(clips, list) or not clips:
        raise ValueError('clips must be a non-empty array')

    probes = {}
    tracks, track_types = {}, {}
    warnings = []
    base_dir = plan_file.parent

    allowed_clip = {
        'path', 'start', 'duration', 'source_in', 'speed', 'reverse', 'volume', 'track', 'fit',
        'transform', 'keyframes', 'motion_easing', 'mix_mode', 'blend_mode', 'transition',
        'grading', 'effects', 'filters', 'mask', 'background', 'animations', 'reason', 'notes',
        'loudness', 'denoise', 'chroma',
    }
    for index, clip in enumerate(clips):
        label = f'clips[{index}]'
        fields(clip, allowed_clip, label, ('path', 'start', 'duration'))
        clip['path'] = local_media_path(clip['path'], base_dir, label)
        info = probes.setdefault(clip['path'], probe(clip['path']))
        if info['kind'] not in ('video', 'image'):
            raise ValueError(f'{label}: expected video or image source, got {info["kind"]}')
        if info.get('vfr'):
            warnings.append(f'{label}: variable frame rate detected ({Path(clip["path"]).name}); normalize-media recommended')
        number(clip['start'], f'{label}.start', 0, 86400)
        number(clip['duration'], f'{label}.duration', 0.000001, 86400)
        snap_range(clip, label)
        duration = clip['duration']
        source_in = number(clip.setdefault('source_in', 0.0), f'{label}.source_in', 0, 86400)
        clip['source_in'] = us(source_in) / 1_000_000
        source_in = clip['source_in']
        speed = number(clip.setdefault('speed', 1.0), f'{label}.speed', 0.05, 32.0)
        number(clip.setdefault('volume', 1.0 if info.get('has_audio') else 0.0), f'{label}.volume', 0, 4.0)
        clip.setdefault('track', 'video')
        _register_track(clip['track'], 'video', clip, label, tracks, track_types)
        if clip.setdefault('fit', 'contain') not in ('contain', 'cover', 'fill'):
            raise ValueError(f'{label}.fit must be contain, cover, or fill')
        if info['kind'] == 'video':
            source_out = source_in + duration * speed
            if source_out > info['duration'] + 0.05:
                raise ValueError(f'{label}: source range [{source_in:.3f}, {source_out:.3f}] exceeds media duration {info["duration"]:.3f}s')
        clip.setdefault('transform', {})
        _validate_transform(clip['transform'], label)
        if 'keyframes' in clip:
            _validate_keyframes(clip['keyframes'], duration, source_in, speed, label=label)
        if 'motion_easing' in clip and clip['motion_easing'] not in EASINGS:
            raise ValueError(f'{label}.motion_easing must be one of {sorted(EASINGS)}')
        if 'grading' in clip:
            _validate_grading(clip['grading'], label)
        if 'transition' in clip:
            tr = clip['transition']
            if isinstance(tr, dict):
                fields(tr, {'name', 'duration', 'frames', 'edge'}, f'{label}.transition', ('name',))
                check_text(tr['name'], f'{label}.transition.name', 128)
                if 'duration' in tr:
                    number(tr['duration'], f'{label}.transition.duration', 0.001, duration)
                if 'frames' in tr:
                    if type(tr['frames']) is not int or tr['frames'] < 1:
                        raise ValueError(f'{label}.transition.frames must be a positive integer')
                if 'edge' in tr and tr['edge'] not in ('in', 'out'):
                    raise ValueError(f'{label}.transition.edge must be in or out')
            elif not isinstance(tr, str) or not tr.strip():
                raise ValueError(f'{label}.transition must be an effect name or object')

    audio_list = plan.setdefault('audio', [])
    if not isinstance(audio_list, list):
        raise ValueError('audio must be an array')
    allowed_audio = {
        'path', 'start', 'duration', 'source_in', 'speed', 'volume', 'track',
        'fade_in', 'fade_out', 'keyframes', 'beat_marks', 'loudness', 'denoise',
    }
    for index, item in enumerate(audio_list):
        label = f'audio[{index}]'
        fields(item, allowed_audio, label, ('path', 'start', 'duration'))
        item['path'] = local_media_path(item['path'], base_dir, label)
        info = probes.setdefault(item['path'], probe(item['path']))
        if not info.get('has_audio'):
            raise ValueError(f'{label}: source has no audio stream')
        number(item['start'], f'{label}.start', 0, 86400)
        number(item['duration'], f'{label}.duration', 0.000001, 86400)
        snap_range(item, label)
        duration = item['duration']
        source_in = number(item.setdefault('source_in', 0.0), f'{label}.source_in', 0, 86400)
        item['source_in'] = us(source_in) / 1_000_000
        source_in = item['source_in']
        speed = number(item.setdefault('speed', 1.0), f'{label}.speed', 0.25, 4.0)
        number(item.setdefault('volume', 1.0), f'{label}.volume', 0, 4.0)
        item.setdefault('track', 'voice')
        _register_track(item['track'], 'audio', item, label, tracks, track_types)
        if info['duration'] > 0 and source_in + duration * speed > info['duration'] + 0.05:
            raise ValueError(f'{label}: selected audio range exceeds source duration {info["duration"]:.3f}s')
        if 'keyframes' in item:
            validate_parameter_keys(item['keyframes'], duration, label, 0, 10, source_in, speed)

    _validate_captions(plan, base_dir, tracks, track_types)
    _validate_overlays(plan, base_dir, canvas, tracks, track_types)
    plan.setdefault('effects', [])
    plan.setdefault('filters', [])
    plan.setdefault('composites', [])
    for group, ttype in (('effects', 'effect'), ('filters', 'filter')):
        if not isinstance(plan[group], list):
            raise ValueError(f'{group} must be an array')
        for index, eff in enumerate(plan[group]):
            if isinstance(eff, dict) and 'track' in eff:
                _register_track(eff['track'], ttype, eff, f'{group}[{index}]', tracks, track_types)

    for track, items in tracks.items():
        if all(isinstance(x, dict) and 'start' in x and 'duration' in x for x in items):
            ordered = sorted(items, key=lambda x: x['start'])
            for prev, curr in zip(ordered, ordered[1:]):
                if end_us(prev) > us(curr['start']):
                    raise ValueError(f'Overlapping items on track: {track} at {curr["start"]:.6f}s')

    if 'layer_order' in plan:
        order = plan['layer_order']
        if (not isinstance(order, list)
                or not all(isinstance(x, str) and x.strip() for x in order)
                or len(order) != len(set(order))
                or set(order) != set(track_types)):
            raise ValueError('layer_order must contain each actual track name exactly once')

    return plan, probes, warnings
