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
import hyperframes_backend
import overlay_recipes
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
    override = os.environ.get('OPENEDIT_' + name.upper())
    if override is not None:
        override = override.strip()
        if override:
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


def is_filmcraft_hi10p(info):
    """Return True for 10-bit H.264 (Hi10P), which FilmCraft renders as MEDIA UNREADABLE."""
    if not isinstance(info, dict) or info.get('kind') != 'video':
        return False
    codec = str(info.get('video_codec') or '').lower()
    pix_fmt = str(info.get('pix_fmt') or '').lower()
    return codec in {'h264', 'avc', 'avc1'} and any(tok in pix_fmt for tok in ('10le', '10be', 'p10'))


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


def _validate_curve_points(points, label, ch):
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError(f'{label}.{ch} curve requires at least two [x, y] anchors')
    last_x = -1.0
    for pt in points:
        if not isinstance(pt, list) or len(pt) != 2:
            raise ValueError(f'{label}.{ch} curve anchor must be [x, y]')
        x = number(pt[0], f'{label}.{ch}.x', 0, 1)
        number(pt[1], f'{label}.{ch}.y', 0, 1)
        if x <= last_x:
            raise ValueError(f'{label}.{ch} curve x coordinates must strictly increase')
        last_x = x


def _validate_grading(grade, label):
    if not isinstance(grade, dict) or not grade:
        raise ValueError(f'{label}.grading must be a non-empty object')
    allowed = {
        'exposure', 'contrast', 'saturation', 'creative_sat', 'highlights', 'shadows',
        'whites', 'blacks', 'temperature', 'tint', 'vibrance', 'sharpness', 'sharpen',
        'faded_film', 'look_intensity', 'vignette', 'vignette_midpoint',
        'vignette_roundness', 'vignette_feather', 'luma_curve', 'rgb_curves',
        'hue_vs_sat', 'hue_vs_hue', 'hue_vs_luma', 'luma_vs_sat', 'sat_vs_sat',
        'red_hsl', 'green_hsl', 'blue_hsl', 'hsl', 'wheels', 'primary_wheel',
        'input_lut', 'lut', 'look', 'local_circle',
    }
    fields(grade, allowed, f'{label}.grading')
    scalar_ranges = {
        'exposure': (-5.0, 5.0),
        'contrast': (-100.0, 100.0),
        'saturation': (0.0, 200.0),
        'creative_sat': (0.0, 200.0),
        'highlights': (-100.0, 100.0),
        'shadows': (-100.0, 100.0),
        'whites': (-100.0, 100.0),
        'blacks': (-100.0, 100.0),
        'temperature': (-100.0, 100.0),
        'tint': (-100.0, 100.0),
        'vibrance': (-100.0, 100.0),
        'sharpness': (-100.0, 100.0),
        'sharpen': (-100.0, 100.0),
        'faded_film': (0.0, 100.0),
        'look_intensity': (0.0, 200.0),
        'vignette': (-5.0, 5.0),
        'vignette_midpoint': (0.0, 100.0),
        'vignette_roundness': (-100.0, 100.0),
        'vignette_feather': (0.0, 100.0),
    }
    for scalar_key, (low, high) in scalar_ranges.items():
        if scalar_key in grade:
            number(grade[scalar_key], f'{label}.grading.{scalar_key}', low, high)
    curves = {}
    for curve_key in ('luma_curve', 'hue_vs_sat', 'hue_vs_hue', 'hue_vs_luma', 'luma_vs_sat', 'sat_vs_sat'):
        if curve_key in grade:
            curves[curve_key] = grade[curve_key]
    if 'rgb_curves' in grade:
        fields(grade['rgb_curves'], {'red', 'green', 'blue'}, f'{label}.grading.rgb_curves')
        curves.update(grade['rgb_curves'])
    for ch, points in curves.items():
        _validate_curve_points(points, f'{label}.grading', ch)
    for lut_key in ('lut', 'look', 'input_lut'):
        if lut_key in grade:
            check_text(grade[lut_key], f'{label}.grading.{lut_key}', 1024)
    wheel_spec = grade.get('wheels') if 'wheels' in grade else grade.get('primary_wheel')
    if 'wheels' in grade and 'primary_wheel' in grade:
        raise ValueError(f'{label}.grading cannot specify both wheels and primary_wheel')
    if wheel_spec is not None:
        wlabel = f'{label}.grading.wheels'
        fields(wheel_spec, {'shadows', 'shadows_l', 'midtones', 'midtones_l', 'highlights', 'highlights_l'}, wlabel)
        for zone in ('shadows', 'midtones', 'highlights'):
            if zone in wheel_spec:
                vec = wheel_spec[zone]
                if not isinstance(vec, list) or len(vec) != 2:
                    raise ValueError(f'{wlabel}.{zone} must be [x, y]')
                number(vec[0], f'{wlabel}.{zone}[0]', -1.0, 1.0)
                number(vec[1], f'{wlabel}.{zone}[1]', -1.0, 1.0)
            l_key = f'{zone}_l'
            if l_key in wheel_spec:
                number(wheel_spec[l_key], f'{wlabel}.{l_key}', -100.0, 100.0)
    shorthand_hsl = [k for k in ('red_hsl', 'green_hsl', 'blue_hsl') if k in grade]
    if len(shorthand_hsl) > 1 or (shorthand_hsl and 'hsl' in grade):
        raise ValueError(
            f'{label}.grading supports at most one active HSL secondary section; '
            'use hue_vs_sat/hue_vs_hue/hue_vs_luma curves for multi-color grading'
        )
    if 'hsl' in grade:
        hlabel = f'{label}.grading.hsl'
        hsl = grade['hsl']
        fields(hsl, {'hue', 'hue_range', 'sat_min', 'luma_min', 'luma_max', 'soft',
                     'denoise', 'blur', 'temp', 'tint', 'sat', 'hue_shift'}, hlabel)
        hsl_ranges = {
            'hue': (0.0, 360.0), 'hue_range': (1.0, 180.0), 'sat_min': (0.0, 100.0),
            'luma_min': (0.0, 100.0), 'luma_max': (0.0, 100.0), 'soft': (0.0, 100.0),
            'denoise': (0.0, 100.0), 'blur': (0.0, 100.0), 'temp': (-100.0, 100.0),
            'tint': (-100.0, 100.0), 'sat': (0.0, 200.0), 'hue_shift': (-180.0, 180.0),
        }
        for hk, (low, high) in hsl_ranges.items():
            if hk in hsl:
                number(hsl[hk], f'{hlabel}.{hk}', low, high)
    for sk in shorthand_hsl:
        slabel = f'{label}.grading.{sk}'
        sh = grade[sk]
        fields(sh, {'hue', 'hue_shift', 'saturation', 'lightness', 'hue_range', 'soft'}, slabel)
        if 'hue' in sh:
            number(sh['hue'], f'{slabel}.hue', -180.0, 360.0)
        if 'hue_shift' in sh:
            number(sh['hue_shift'], f'{slabel}.hue_shift', -180.0, 180.0)
        if 'saturation' in sh:
            number(sh['saturation'], f'{slabel}.saturation', -100.0, 100.0)
        if 'lightness' in sh:
            number(sh['lightness'], f'{slabel}.lightness', -100.0, 100.0)
        if 'hue_range' in sh:
            number(sh['hue_range'], f'{slabel}.hue_range', 1.0, 180.0)
        if 'soft' in sh:
            number(sh['soft'], f'{slabel}.soft', 0.0, 100.0)
        if 'lightness' in sh and 'hue_vs_luma' in grade:
            raise ValueError(f'{label}.grading cannot specify both {sk}.lightness and hue_vs_luma')
    if 'local_circle' in grade:
        lc = grade['local_circle']
        lclabel = f'{label}.grading.local_circle'
        if not isinstance(lc, dict) or not lc:
            raise ValueError(f'{lclabel} must be a non-empty object')
        fields(lc, {'center', 'center_x', 'center_y', 'size', 'width', 'height', 'radius',
                    'feather', 'opacity', 'expansion', 'inverted', 'invert', 'mode'}, lclabel)
        if 'center' in lc:
            if not isinstance(lc['center'], list) or len(lc['center']) != 2:
                raise ValueError(f'{lclabel}.center must be [x, y]')
            number(lc['center'][0], f'{lclabel}.center[0]', -10000, 20000)
            number(lc['center'][1], f'{lclabel}.center[1]', -10000, 20000)
        for k in ('center_x', 'center_y'):
            if k in lc:
                number(lc[k], f'{lclabel}.{k}', -10000, 10000)
        if 'size' in lc:
            if not isinstance(lc['size'], list) or len(lc['size']) != 2:
                raise ValueError(f'{lclabel}.size must be [width, height]')
            number(lc['size'][0], f'{lclabel}.size[0]', 0.0001, 20000)
            number(lc['size'][1], f'{lclabel}.size[1]', 0.0001, 20000)
        for k in ('width', 'height'):
            if k in lc:
                number(lc[k], f'{lclabel}.{k}', 0.0001, 10.0)
        if 'radius' in lc:
            number(lc['radius'], f'{lclabel}.radius', 0.0001, 10000.0)
        if 'feather' in lc:
            number(lc['feather'], f'{lclabel}.feather', 0.0, 1000.0)
        if 'opacity' in lc:
            number(lc['opacity'], f'{lclabel}.opacity', 0.0, 1.0)
        if 'expansion' in lc:
            number(lc['expansion'], f'{lclabel}.expansion', -1000.0, 1000.0)
        for bkey in ('inverted', 'invert'):
            if bkey in lc and type(lc[bkey]) is not bool:
                raise ValueError(f'{lclabel}.{bkey} must be boolean')
        if 'mode' in lc and lc['mode'] not in ('none', 'add', 'subtract', 'intersect', 'lighten', 'darken', 'difference'):
            raise ValueError(f'{lclabel}.mode is unsupported: {lc["mode"]}')


def _validate_mask_points(pts, label):
    if not isinstance(pts, list) or len(pts) < 3:
        raise ValueError(f'{label} must be an array of at least 3 [x, y] points')
    for idx, pt in enumerate(pts):
        if not isinstance(pt, list) or len(pt) != 2:
            raise ValueError(f'{label}[{idx}] must be [x, y]')
        number(pt[0], f'{label}[{idx}][0]', -10000, 20000)
        number(pt[1], f'{label}[{idx}][1]', -10000, 20000)


def _validate_mask(mask, duration, canvas, label):
    del canvas
    mlabel = f'{label}.mask'
    allowed = {
        'shape', 'type', 'effect', 'center', 'center_x', 'center_y', 'size',
        'width', 'height', 'path', 'points', 'corner_radius', 'feather',
        'opacity', 'expansion', 'inverted', 'invert', 'mode', 'keyframes',
    }
    fields(mask, allowed, mlabel)
    if not mask:
        raise ValueError(f'{mlabel} must be a non-empty object')
    raw_shape = mask.get('shape') or mask.get('type')
    if raw_shape is None:
        raw_shape = 'polygon' if ('path' in mask or 'points' in mask) else 'rectangle'
    shape_alias = {'rect': 'rectangle', 'circle': 'ellipse'}.get(raw_shape, raw_shape)
    if shape_alias not in ('rectangle', 'ellipse', 'polygon', 'bezier'):
        raise ValueError(f'{mlabel}.shape must be rectangle, ellipse, polygon, or bezier')
    if 'effect' in mask:
        check_text(mask['effect'], f'{mlabel}.effect', 128)
    if 'center' in mask:
        if not isinstance(mask['center'], list) or len(mask['center']) != 2:
            raise ValueError(f'{mlabel}.center must be [x, y]')
        number(mask['center'][0], f'{mlabel}.center[0]', -10000, 20000)
        number(mask['center'][1], f'{mlabel}.center[1]', -10000, 20000)
    for k in ('center_x', 'center_y'):
        if k in mask:
            number(mask[k], f'{mlabel}.{k}', -10000, 10000)
    if 'size' in mask:
        if not isinstance(mask['size'], list) or len(mask['size']) != 2:
            raise ValueError(f'{mlabel}.size must be [width, height]')
        number(mask['size'][0], f'{mlabel}.size[0]', 0.0001, 20000)
        number(mask['size'][1], f'{mlabel}.size[1]', 0.0001, 20000)
    for k in ('width', 'height'):
        if k in mask:
            number(mask[k], f'{mlabel}.{k}', 0.0001, 10.0)
    if 'path' in mask or 'points' in mask:
        if 'path' in mask and 'points' in mask:
            raise ValueError(f'{mlabel} cannot specify both path and points')
        _validate_mask_points(mask.get('path') if 'path' in mask else mask.get('points'), f'{mlabel}.path')
    elif shape_alias in ('polygon', 'bezier'):
        raise ValueError(f'{mlabel} {shape_alias} requires path or points')
    if 'corner_radius' in mask:
        number(mask['corner_radius'], f'{mlabel}.corner_radius', 0.0, 2000.0)
    if 'feather' in mask:
        number(mask['feather'], f'{mlabel}.feather', 0.0, 1000.0)
    if 'opacity' in mask:
        number(mask['opacity'], f'{mlabel}.opacity', 0.0, 1.0)
    if 'expansion' in mask:
        number(mask['expansion'], f'{mlabel}.expansion', -1000.0, 1000.0)
    for bkey in ('inverted', 'invert'):
        if bkey in mask and type(mask[bkey]) is not bool:
            raise ValueError(f'{mlabel}.{bkey} must be boolean')
    if 'mode' in mask and mask['mode'] not in ('none', 'add', 'subtract', 'intersect', 'lighten', 'darken', 'difference'):
        raise ValueError(f'{mlabel}.mode is unsupported: {mask["mode"]}')
    if 'keyframes' in mask:
        kfs = mask['keyframes']
        if not isinstance(kfs, list) or not kfs:
            raise ValueError(f'{mlabel}.keyframes must be a non-empty array')
        for idx, kf in enumerate(kfs):
            klabel = f'{mlabel}.keyframes[{idx}]'
            fields(kf, {'time', 'feather', 'opacity', 'expansion', 'path', 'center', 'size'}, klabel, ('time',))
            number(kf['time'], f'{klabel}.time', 0.0, duration)
            if not any(k in kf for k in ('feather', 'opacity', 'expansion', 'path', 'center', 'size')):
                raise ValueError(f'{klabel} must update at least one mask property')
            if 'feather' in kf:
                number(kf['feather'], f'{klabel}.feather', 0.0, 1000.0)
            if 'opacity' in kf:
                number(kf['opacity'], f'{klabel}.opacity', 0.0, 1.0)
            if 'expansion' in kf:
                number(kf['expansion'], f'{klabel}.expansion', -1000.0, 1000.0)
            if 'path' in kf:
                _validate_mask_points(kf['path'], f'{klabel}.path')
            if 'center' in kf:
                if not isinstance(kf['center'], list) or len(kf['center']) != 2:
                    raise ValueError(f'{klabel}.center must be [x, y]')
                number(kf['center'][0], f'{klabel}.center[0]', -10000, 20000)
                number(kf['center'][1], f'{klabel}.center[1]', -10000, 20000)
            if 'size' in kf:
                if not isinstance(kf['size'], list) or len(kf['size']) != 2:
                    raise ValueError(f'{klabel}.size must be [width, height]')
                number(kf['size'][0], f'{klabel}.size[0]', 0.0001, 20000)
                number(kf['size'][1], f'{klabel}.size[1]', 0.0001, 20000)


def _validate_chroma(chroma, label):
    clabel = f'{label}.chroma'
    allowed = {
        'method', 'color', 'key_color', 'setting', 'tolerance', 'strength',
        'transparency', 'highlight', 'shadow', 'pedestal', 'choke', 'soften',
        'feather', 'spill', 'contrast', 'mid_point', 'desaturate', 'range',
        'spill_luma', 'thin', 'output',
    }
    fields(chroma, allowed, clabel)
    if not chroma:
        raise ValueError(f'{clabel} must be a non-empty object')
    method = chroma.get('method', 'ultra_key')
    if method not in ('ultra_key', 'color_key'):
        raise ValueError(f'{clabel}.method must be ultra_key or color_key')
    for ck in ('color', 'key_color'):
        if ck in chroma:
            if not isinstance(chroma[ck], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', chroma[ck]):
                raise ValueError(f'{clabel}.{ck} must be #RRGGBB or #RRGGBBAA')
    if 'setting' in chroma and chroma['setting'] not in ('Default', 'Relaxed', 'Aggressive', 'Custom'):
        raise ValueError(f'{clabel}.setting must be Default, Relaxed, Aggressive, or Custom')
    if 'output' in chroma and chroma['output'] not in ('Composite', 'Alpha Channel', 'Color Channel'):
        raise ValueError(f'{clabel}.output must be Composite, Alpha Channel, or Color Channel')
    if 'tolerance' in chroma:
        number(chroma['tolerance'], f'{clabel}.tolerance', 0.0, 255.0 if method == 'color_key' else 100.0)
    if 'thin' in chroma:
        number(chroma['thin'], f'{clabel}.thin', -5.0, 5.0)
    for fk in ('strength', 'feather', 'transparency', 'highlight', 'shadow', 'pedestal',
               'choke', 'soften', 'spill', 'contrast', 'mid_point', 'desaturate', 'range', 'spill_luma'):
        if fk in chroma:
            number(chroma[fk], f'{clabel}.{fk}', 0.0, 100.0)


def _validate_loudness(spec, label):
    llabel = f'{label}.loudness'
    if isinstance(spec, (int, float)) and type(spec) is not bool:
        number(spec, llabel, -60.0, 0.0)
        return
    if not isinstance(spec, dict) or not spec:
        raise ValueError(f'{llabel} must be a target LUFS number in [-60, 0] or configuration object')
    fields(spec, {'target_lufs', 'target', 'type', 'enabled'}, llabel)
    if 'enabled' in spec and type(spec['enabled']) is not bool:
        raise ValueError(f'{llabel}.enabled must be boolean')
    if 'type' in spec and spec['type'] not in ('dialogue', 'music', 'sfx', 'ambience'):
        raise ValueError(f'{llabel}.type must be dialogue, music, sfx, or ambience')
    target_val = spec.get('target_lufs') if 'target_lufs' in spec else spec.get('target')
    if target_val is not None:
        number(target_val, f'{llabel}.target_lufs', -60.0, 0.0)


def _validate_effect_entries(entries, label):
    if not isinstance(entries, list):
        raise ValueError(f'{label} must be an array')
    for idx, eff in enumerate(entries):
        if isinstance(eff, str):
            check_text(eff, f'{label}[{idx}]', 128)
        elif isinstance(eff, dict):
            fields(eff, {'name', 'params'}, f'{label}[{idx}]', ('name',))
            check_text(eff['name'], f'{label}[{idx}].name', 128)
            if 'params' in eff and not isinstance(eff['params'], dict):
                raise ValueError(f'{label}[{idx}].params must be an object')
        else:
            raise ValueError(f'{label}[{idx}] must be an effect name or object')


def _validate_composites(composites, track_types, canvas):
    if not isinstance(composites, list):
        raise ValueError('composites must be an array')
    allowed = {
        'name', 'tracks', 'start', 'duration', 'transform', 'keyframes',
        'motion_easing', 'blend_mode', 'mix_mode', 'mask', 'grading', 'effects', 'filters',
    }
    for idx, spec in enumerate(composites):
        label = f'composites[{idx}]'
        fields(spec, allowed, label, ('tracks',))
        if 'name' in spec:
            check_text(spec['name'], f'{label}.name', 128)
        tlist = spec['tracks']
        if not isinstance(tlist, list) or not tlist or any(not isinstance(t, str) or not t.strip() for t in tlist):
            raise ValueError(f'{label}.tracks must be a non-empty array of track names')
        unknown_tracks = [t for t in tlist if t not in track_types]
        if unknown_tracks:
            raise ValueError(f'{label}.tracks references unknown track(s): {", ".join(unknown_tracks)}')
        start = number(spec.get('start', 0.0), f'{label}.start', 0.0, 86400.0)
        duration = number(spec.get('duration', 86400.0), f'{label}.duration', 0.000001, 86400.0)
        if 'start' in spec:
            spec['start'] = us(start) / 1_000_000
        if 'duration' in spec:
            spec['duration'] = us(duration) / 1_000_000
        if 'transform' in spec:
            _validate_transform(spec['transform'], label)
        if 'keyframes' in spec:
            _validate_keyframes(spec['keyframes'], duration, label=label)
        if 'motion_easing' in spec and spec['motion_easing'] not in EASINGS:
            raise ValueError(f'{label}.motion_easing must be one of {sorted(EASINGS)}')
        if 'mask' in spec:
            _validate_mask(spec['mask'], duration, canvas, label)
        if 'grading' in spec:
            _validate_grading(spec['grading'], label)
        if 'effects' in spec:
            _validate_effect_entries(spec['effects'], f'{label}.effects')
        if 'filters' in spec:
            _validate_effect_entries(spec['filters'], f'{label}.filters')


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
        fields(ov, {'id', 'name', 'backend', 'track', 'start', 'duration', 'width', 'height', 'fps',
                    'layers', 'script', 'recipe', 'params', 'effects', 'out_format', 'gpu', 'motion_blur',
                    'html', 'variables', 'check', 'allow_network', 'font_path'},
               label, ('start', 'duration'))
        if 'motion_blur' in ov and type(ov['motion_blur']) is not bool:
            raise ValueError(f'{label}.motion_blur must be boolean')
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
        backend = ov.get('backend', 'effectcraft')
        if backend not in ('effectcraft', 'hyperframes'):
            raise ValueError(f'{label}.backend must be effectcraft or hyperframes')
        if backend == 'hyperframes':
            hyperframes_backend.validate_overlay_spec(ov, base_dir=base_dir, label=label)
        else:
            for bad_key in sorted(hyperframes_backend.HYPERFRAMES_ONLY_KEYS & set(ov)):
                raise ValueError(f'{label}.{bad_key} is only supported with backend="hyperframes"')
            if 'recipe' in ov:
                if 'layers' in ov or 'script' in ov:
                    raise ValueError(f'{label} cannot mix recipe with layers or script')
                overlay_recipes.resolve_recipe_media_paths(
                    ov, lambda raw, key: local_media_path(raw, base_dir, f'{label}.params.{key}')
                )
                overlay_recipes.validate_recipe_spec(ov)
            else:
                if 'layers' not in ov and 'script' not in ov:
                    raise ValueError(f'{label} must specify layers, script, or recipe')
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
        for rpath in overlay_recipes.iter_recipe_media_paths(ov):
            paths.append(rpath)
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
        if is_filmcraft_hi10p(info):
            warnings.append(
                f'{label}: 10-bit H.264 ({info.get("pix_fmt")}) detected ({Path(clip["path"]).name}); '
                'FilmCraft renders Hi10P as MEDIA UNREADABLE — run normalize-media (8-bit yuv420p) before build'
            )
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
        if 'mask' in clip:
            _validate_mask(clip['mask'], duration, canvas, label)
        if 'chroma' in clip:
            _validate_chroma(clip['chroma'], label)
        if 'effects' in clip:
            _validate_effect_entries(clip['effects'], f'{label}.effects')
        if 'filters' in clip:
            _validate_effect_entries(clip['filters'], f'{label}.filters')
        if 'loudness' in clip:
            _validate_loudness(clip['loudness'], label)
            if not (info.get('has_audio') and float(clip.get('volume', 0)) > 0):
                warnings.append(f'{label}.loudness: clip has no active audio stream (volume=0 or no audio track); loudness matching skipped')
        for legacy_key in ('denoise', 'animations', 'background'):
            if legacy_key in clip:
                warnings.append(f'{label}.{legacy_key}: not compiled on timeline; use offline denoise-audio or EffectCraft overlays')
        if 'transition' in clip:
            tr = clip['transition']
            if isinstance(tr, dict):
                fields(tr, {'name', 'duration', 'frames', 'edge', 'align', 'reverse', 'params'}, f'{label}.transition', ('name',))
                check_text(tr['name'], f'{label}.transition.name', 128)
                if 'duration' in tr:
                    number(tr['duration'], f'{label}.transition.duration', 0.001, duration)
                if 'frames' in tr:
                    if type(tr['frames']) is not int or tr['frames'] < 1:
                        raise ValueError(f'{label}.transition.frames must be a positive integer')
                if 'edge' in tr and tr['edge'] not in ('in', 'out'):
                    raise ValueError(f'{label}.transition.edge must be in or out')
                if 'align' in tr and tr['align'] not in ('center', 'start', 'end'):
                    raise ValueError(f'{label}.transition.align must be center, start, or end')
                if 'reverse' in tr and type(tr['reverse']) is not bool:
                    raise ValueError(f'{label}.transition.reverse must be boolean')
                if 'params' in tr:
                    tr_params = tr['params']
                    if not isinstance(tr_params, dict):
                        raise ValueError(f'{label}.transition.params must be an object')
                    for pk, pv in tr_params.items():
                        check_text(pk, f'{label}.transition.params key', 64)
                        plabel = f'{label}.transition.params.{pk}'
                        if isinstance(pv, bool):
                            continue
                        if isinstance(pv, (int, float)):
                            if not math.isfinite(pv):
                                raise ValueError(f'{plabel} must be a finite number')
                        elif isinstance(pv, str):
                            check_text(pv, plabel, 128)
                        elif isinstance(pv, list) and 2 <= len(pv) <= 4:
                            for idx, elem in enumerate(pv):
                                number(elem, f'{plabel}[{idx}]', -1e6, 1e6)
                        else:
                            raise ValueError(f'{plabel} must be a finite number, boolean, short string, or 2-4 number vector')
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
        fade_in = number(item['fade_in'], f'{label}.fade_in', 0.0, duration) if 'fade_in' in item else 0.0
        fade_out = number(item['fade_out'], f'{label}.fade_out', 0.0, duration) if 'fade_out' in item else 0.0
        if fade_in + fade_out > duration + 1e-6:
            raise ValueError(f'{label}: fade_in + fade_out exceeds audio duration {duration:.3f}s')
        if 'loudness' in item:
            _validate_loudness(item['loudness'], label)
        if 'beat_marks' in item:
            marks = item['beat_marks']
            if not isinstance(marks, list):
                raise ValueError(f'{label}.beat_marks must be an array')
            source_end = source_in + duration * speed
            last_mark = -1
            for mi, mark in enumerate(marks):
                val = number(mark, f'{label}.beat_marks[{mi}]', source_in - 1e-6, source_end + 1e-6)
                mu = us(val)
                if mu <= last_mark:
                    raise ValueError(f'{label}.beat_marks must be strictly increasing')
                last_mark = mu
        if 'denoise' in item:
            warnings.append(f'{label}.denoise: use offline denoise-audio command to generate cleaned WAV before build')
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

    _validate_composites(plan['composites'], track_types, canvas)

    if 'layer_order' in plan:
        order = plan['layer_order']
        if (not isinstance(order, list)
                or not all(isinstance(x, str) and x.strip() for x in order)
                or len(order) != len(set(order))
                or set(order) != set(track_types)):
            raise ValueError('layer_order must contain each actual track name exactly once')

    return plan, probes, warnings
