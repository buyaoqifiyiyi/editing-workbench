#!/usr/bin/env python3
"""Local media analysis and new Jianying drafts; never controls desktop input."""
import argparse
import copy
import configparser
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import native_advanced
import native_text_glow
import native_matting_cache
import native_mask_feather
import native_chroma
import native_mix_mode
import native_text_highlight
import native_hsl_channels
import native_text_alpha
import file_hash
import local_font
import runtime_paths

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
MEDIA_EXT = IMAGE_EXT | {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.gif', '.m4v',
                         '.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}
KEYFRAMES = {'uniform_scale', 'scale_x', 'scale_y', 'position_x', 'position_y', 'rotation', 'alpha'}
TRANSFORMS = {'scale_x', 'scale_y', 'transform_x', 'transform_y', 'rotation', 'alpha',
              'flip_horizontal', 'flip_vertical'}
NATIVE_TYPES = {'filters': 'FilterType', 'scene_effects': 'VideoSceneEffectType',
                'character_effects': 'VideoCharacterEffectType', 'video_intro': 'IntroType',
                'video_outro': 'OutroType', 'video_group': 'GroupAnimationType',
                'text_intro': 'TextIntro', 'text_outro': 'TextOutro', 'text_loop': 'TextLoopAnim',
                'mix_modes': 'MixModeType', 'masks': 'MaskType', 'transitions': 'TransitionType',
                'keyframes': 'KeyframeProperty', 'fonts': 'FontType', 'text_glows': None}
ANIMATION_KINDS = ('text_intro', 'text_outro', 'text_loop', 'video_intro', 'video_outro', 'video_group')
SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_JIANYING_VERSION = '11.5.0.14471'


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read_json(path):
    def reject_constant(value):
        raise ValueError(f'JSON contains non-finite value: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), parse_constant=reject_constant)


def draft_state(path):
    """Classify a draft_content.json by bytes only: missing / plaintext / ciphertext / corrupt.

    Jianying rewrites a draft saved in its UI as base64 ciphertext (measured 2026-10-16, evidence in
    Skill维护层\\来源\\草稿存储权威位置-20261016). That form is not ours to decrypt, overwrite or repair.
    """
    file = Path(path)
    if not file.is_file():
        return 'missing'
    with file.open('rb') as handle:
        head = handle.read(4096)
    if not head.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'{'):
        return 'ciphertext'
    try:
        read_json(file)
    except (ValueError, UnicodeDecodeError):
        return 'corrupt'
    return 'plaintext'


def require_plaintext_draft(path, purpose):
    """Refuse work on a draft that is not our own plaintext JSON, naming the state instead of a parse error."""
    state = draft_state(path)
    if state == 'plaintext':
        return path
    detail = {
        'ciphertext': 'Jianying saved this draft as encrypted bytes; this Skill never decrypts, overwrites or repairs it '
                      '- build a new draft and leave the saved one alone',
        'corrupt': 'the file is not valid JSON; inspect it by hand before reusing this path',
        'missing': 'no such file',
    }[state]
    raise ValueError(f'Refusing {purpose}: {Path(path)} is {state} - {detail}.')


def run(args, timeout=120):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise ValueError(f'{Path(str(args[0])).name} failed: {result.stderr[-2000:]}')
    return result


_LOUDNESS = {}


def measure_loudness(item):
    """Measure the selected source range; discard PCM, native player applies the gain."""
    stat = Path(item['path']).stat()
    key = (str(Path(item['path']).resolve()), stat.st_size, stat.st_mtime_ns,
           us(item['source_in']), us(item['duration']), item['loudness']['target_lufs'])
    if key not in _LOUDNESS:
        _LOUDNESS[key] = _measure_loudness(item)
    return dict(_LOUDNESS[key])


def _measure_loudness(item):
    result = run([tool('ffmpeg'), '-hide_banner', '-nostdin', '-ss', item['source_in'],
                  '-t', item['duration'], '-i', item['path'], '-vn', '-af',
                  'ebur128=peak=sample+true:metadata=1,ametadata=print:key=lavfi.r128.I',
                  '-f', 'null', '-'])
    averages = re.findall(r'lavfi\.r128\.I=([-\d.]+)', result.stderr)
    peaks = re.findall(r'Sample peak:\s*Peak:\s*([-\d.]+) dBFS', result.stderr)
    true_peaks = re.findall(r'True peak:\s*Peak:\s*([-\d.]+) dBFS', result.stderr)
    if not averages or not peaks or not true_peaks:
        raise ValueError('Selected audio range has no measurable gated loudness or peak')
    average, peak = float(averages[-1]), float(peaks[-1])
    if not math.isfinite(average) or not math.isfinite(peak) or average <= -69:
        raise ValueError('Silent or near-silent range cannot be normalized by this measured profile')
    target = item['loudness']['target_lufs']
    true_peak = float(true_peaks[-1])
    if not math.isfinite(true_peak) or true_peak + target - average > -1.1:
        raise ValueError('Native loudness profile would exceed measured peak headroom; no limiter is implemented')
    return {'avg_loudness': average, 'peak_loudness': peak}


def tool_path(name):
    """Resolve an explicit tool, PATH, then an optional skill-local tools/bin."""
    override = os.environ.get('JIANYING_' + name.upper())
    if override:
        candidate = Path(os.path.expandvars(override)).expanduser().resolve()
        return str(candidate) if candidate.is_file() else None
    path = shutil.which(name)
    if path:
        return path
    for root in (SKILL_ROOT, runtime_paths.RAW_ROOT):
        candidate = root / 'tools' / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))
        if candidate.is_file():
            return str(candidate)
    return None


def tool(name):
    path = tool_path(name)
    if not path:
        raise ValueError(f'{name} is unavailable. Set JIANYING_{name.upper()} to its executable, '
                         f'add its directory to PATH, or place it in {SKILL_ROOT / "tools/bin"}. '
                         'An invalid explicit override is not silently ignored.')
    return path


def number(value, label, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if value < low or (high is not None and value > high):
        raise ValueError(f'{label} is outside its supported range')
    return value


def us(value):
    return round(value * 1_000_000)


def snap_range(item, label):
    """Quantize start and end to whole microseconds; duration becomes end - start.

    pyJianYingDraft compares integer ranges (end = start + duration), so validating
    us(start + duration) against un-snapped floats can pass while build overlaps by 1us.
    """
    start, end = us(item['start']), us(item['start'] + item['duration'])
    if end <= start:
        raise ValueError(f'{label}.duration rounds below one microsecond')
    item['start'], item['duration'] = start / 1_000_000, (end - start) / 1_000_000


def end_us(item):
    return us(item['start']) + us(item['duration'])


_SOURCE_LIMITS = {}


def source_limit_us(info):
    """Upper source bound in microseconds, using the backend's own mediainfo duration when available."""
    path = Path(info['path'])
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _SOURCE_LIMITS:
        limit = us(info['duration']) + 1000
        try:
            backend = load_backend()
        except (ValueError, ImportError):
            backend = None  # validate stays usable without the backend; build reloads it and rechecks.
        if backend is not None:
            material = (backend.AudioMaterial if info['kind'] == 'audio' else backend.VideoMaterial)(str(path))
            limit = min(limit, material.duration)
        _SOURCE_LIMITS[key] = limit
    return _SOURCE_LIMITS[key]


def fields(obj, allowed, label, required=()):
    if not isinstance(obj, dict):
        raise ValueError(f'{label} must be an object')
    unknown = set(obj) - set(allowed)
    missing = set(required) - set(obj)
    if unknown or missing:
        raise ValueError(f'{label}: unknown fields {sorted(unknown)}, missing fields {sorted(missing)}')


def validate_parameter_keys(keys, duration, label, low, high, source_in=0, speed=1, rotation=False):
    if not isinstance(keys, list) or not keys:
        raise ValueError(label + ' must be a nonempty array')
    last, source_last = -1, -1
    for key in keys:
        names = {'time', 'value', 'property'} if rotation else {'time', 'value'}
        fields(key, names, label, names)
        if rotation and key['property'] not in ('rotation', 'position_x', 'position_y', 'uniform_scale'):
            raise ValueError('Only measured composite rotation, position_x/position_y or uniform_scale keyframes are supported')
        number(key['time'], label + '.time', 0, duration - 0.000001)
        number(key['value'], label + '.value', low, high)
        timestamp, mapped = us(key['time']), us(source_in + key['time'] * speed)
        if last == -1 and timestamp != 0:
            raise ValueError(label + ' must start at time zero')
        if timestamp <= last or mapped <= source_last:
            raise ValueError(label + ' must remain strictly increasing after source-time mapping')
        last, source_last = timestamp, mapped


def native_user_data():
    setting = draft_location()['setting_file']
    if not setting:
        raise ValueError('Native resources require configured Jianying user data')
    return Path(setting).parent.parent


def configured_user_data():
    """Jianying user data for local-resource checks; never raises.

    Order: the configured draft location first (the same source native_user_data() reads), then the
    JIANYING_USER_DATA override already honored for draft paths and doctor, then the default install
    location when that is the only one with an effect cache. Without the fallbacks a machine whose
    settings mirror is missing keeps every planned animation out of the draft even though the packages
    are on disk, and names every one of them in the warning as if the cache had rejected it.
    """
    try:
        primary = native_user_data()
    except Exception:
        primary = None
    try:
        if effect_cache_dir(primary) is not None:
            return primary
    except Exception:
        pass
    candidates = []
    override = os.environ.get('JIANYING_USER_DATA')
    if override:
        candidates.append(Path(os.path.expandvars(override)).expanduser())
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(Path(local) / 'JianyingPro' / 'User Data')
    for candidate in candidates:
        if effect_cache_dir(candidate) is not None:
            return candidate
    return primary


def resource_user_data(relative=None):
    """User data root for native resource families; raises when the machine has none configured.

    Candidate order matches configured_user_data(), so the layer that decides whether a resource is
    usable and the layer that writes its cache path into the draft read one directory. With `relative`
    (a family's own cache path under the user data root) the first candidate that actually holds it
    wins, so a mirror carrying an empty Cache/effect falls back to the machine's install instead of
    failing with "<resource> unavailable" while the package sits on disk. Without `relative` the probe
    stays the cache directory. When no candidate has it the configured directory is returned unchanged,
    so a genuinely missing package still fails loudly with the family's own message.

    ponytail: animations still take the directory-level probe (animation names differ per item and each
    bundle is probed by id after the root is chosen); thread a relative in per name if that ever bites.
    """
    try:
        primary = native_user_data()
    except Exception:
        primary = None
    candidates = [Path(primary)] if primary is not None else []
    override = os.environ.get('JIANYING_USER_DATA')
    if override:
        candidates.append(Path(os.path.expandvars(override)).expanduser())
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(Path(local) / 'JianyingPro' / 'User Data')
    for candidate in candidates:
        try:
            if relative is not None:
                if (candidate / relative).exists():
                    return candidate
            elif effect_cache_dir(candidate) is not None:
                return candidate
        except Exception:
            pass
    return Path(primary) if primary is not None else native_user_data()


def effect_cache_dir(user_data):
    """The local effect cache, or None when this machine has not created it yet.

    A machine that never downloaded an effect package has no Cache/effect directory at all. Reporting
    that as "every name is missing" reads like a per-name verdict, so callers say cache absent instead.
    """
    if not user_data:
        return None
    cache = Path(user_data) / 'Cache' / 'effect'
    return cache if cache.is_dir() else None


def validate_grading(item, info):
    grade = item['grading']
    if native_hsl_channels.requested(grade):
        native_hsl_channels.validate(item, info)
        return
    fields(grade, {'red_hsl', 'blue_hsl', 'green_hsl', 'luma_curve', 'rgb_curves', 'local_circle'}, 'grading')
    if not grade or not ({'red_hsl', 'blue_hsl', 'green_hsl', 'luma_curve', 'rgb_curves'} & set(grade)):
        raise ValueError('Grading must specify measured HSL or three-anchor curves')
    for field in ('blue_hsl', 'green_hsl'):
        if field not in grade:
            continue
        spec = grade[field]
        allowed = {'saturation', 'hue', 'lightness'}
        fields(spec, allowed, 'grading.' + field)
        if len(spec) != 1:
            raise ValueError('Blue/green HSL requires exactly one measured control')
        control = next(iter(spec))
        if field == 'green_hsl' and control == 'lightness':
            native_advanced.green_lightness_runtime()
        values = (-30, 0, 30) if control in ('hue', 'lightness') else (-60, 0, 60)
        number(spec[control], 'grading.' + field + '.' + control, values[0], values[-1])
        paired = (set(grade) == {'blue_hsl', 'green_hsl'} and
                  grade['blue_hsl'] == {'saturation': -60} and grade['green_hsl'] == {'saturation': -60})
        measured_video = (info['kind'] == 'video' and field in ('blue_hsl', 'green_hsl') and
                          control == 'saturation' and set(grade) == {field} and
                          not info.get('has_audio') and info['fps'] == 30 and
                          item.get('source_in', 0) == 0.5 and item.get('speed', 1) == 1 and
                          item.get('volume', 0) == 0)
        if (spec[control] not in values or (set(grade) != {field} and not paired) or
                (info['kind'] != 'image' and not measured_video) or (info['width'], info['height']) != (640, 360) or
                item.get('fit', 'contain') != 'contain' or item.get('transform') or
                any(item.get(k) for k in ('keyframes', 'motion_easing', 'transition', 'effects', 'filters',
                                          'animations', 'mix_mode', 'chroma', 'background', 'mask', 'matting_cache'))):
            raise ValueError('Blue/green HSL admits 640x360 contain static images or bounded silent blue/green-saturation video only')
    if 'red_hsl' in grade:
        fields(grade['red_hsl'], {'hue', 'saturation', 'lightness'}, 'grading.red_hsl')
        for key in ('hue', 'saturation', 'lightness'):
            number(grade['red_hsl'].setdefault(key, 0), 'grading.red_hsl.' + key, -100, 100)
    curves = {'luma': grade['luma_curve']} if 'luma_curve' in grade else {}
    if 'rgb_curves' in grade:
        rgb = grade['rgb_curves']
        fields(rgb, {'red', 'green', 'blue'}, 'grading.rgb_curves')
        if not rgb:
            raise ValueError('RGB curves must include at least one channel')
        curves.update(rgb)
    for channel, points in curves.items():
        if not isinstance(points, list) or len(points) != 3 or points[0] != [0, 0] or points[-1] != [1, 1]:
            raise ValueError(f'Measured {channel} curve requires three anchors with endpoints [0,0] and [1,1]')
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError('Curve anchors must be [x,y]')
            for value in point:
                number(value, 'grading curve ' + channel, 0, 1)
        if not 0 < points[1][0] < 1:
            raise ValueError('Curve anchor x must strictly increase')
    if 'local_circle' in grade:
        if 'mask' in item:
            raise ValueError('Local grading mask cannot coexist with a video mask')
        mask = grade['local_circle']
        fields(mask, {'width', 'height', 'feather'}, 'grading.local_circle', ('width', 'height'))
        number(mask['width'], 'grading.local_circle.width', 0.000001, info['width'] * 4)
        number(mask['height'], 'grading.local_circle.height', 0.000001, info['height'] * 4)
        number(mask.setdefault('feather', 0), 'grading.local_circle.feather', 0, 100)
    # Do not deliver grading that references a missing cache resource.
    native_advanced.color_materials(grade, info, None)


def validate_visual(item, label):
    fields(item.setdefault('transform', {}), TRANSFORMS, label + '.transform')
    for k, value in item['transform'].items():
        if k.startswith('flip_'):
            if type(value) is not bool:
                raise ValueError(f'{label}.transform.{k} must be a boolean')
        else:
            number(value, label + '.transform.' + k, 0.000001 if k.startswith('scale_') else 0 if k == 'alpha' else -10000,
                   1 if k == 'alpha' else 10000)
    if not isinstance(item.setdefault('keyframes', []), list):
        raise ValueError(f'{label}.keyframes must be an array')
    seen, source_seen, properties = set(), set(), set()
    for key in item['keyframes']:
        fields(key, {'property', 'time', 'value'}, 'keyframe', ('property', 'time', 'value'))
        prop = key['property']
        if prop not in KEYFRAMES:
            raise ValueError(f'Unsupported keyframe property: {prop}')
        number(key['time'], 'keyframe.time', 0, item['duration'])
        number(key['value'], 'keyframe.value', 0.000001 if 'scale' in prop else 0 if prop == 'alpha' else -10000,
               1 if prop == 'alpha' else 10000)
        identity = (prop, us(key['time']))
        if identity in seen:
            raise ValueError('Duplicate keyframe property/time')
        seen.add(identity)
        if 'source_in' in item:
            source_identity = (prop, us(item['source_in'] + key['time'] * item['speed']))
            if source_identity in source_seen:
                raise ValueError('Video source-time keyframes must remain distinct after speed mapping')
            source_seen.add(source_identity)
        properties.add(prop)
    if 'uniform_scale' in properties and properties & {'scale_x', 'scale_y'}:
        raise ValueError('Cannot mix uniform_scale with scale_x/scale_y keyframes')


def native_font(name, allow_vip=True, user_data=None):
    catalog = read_json(SKILL_ROOT / 'assets/native-fonts-11.5.json')
    if not isinstance(name, str) or name not in catalog:
        raise ValueError('Font not admitted; run capabilities --kind fonts')
    spec = catalog[name]
    member = native_enum(load_backend(), 'fonts', name, allow_vip).value
    if any(getattr(member, key) != spec[key] for key in ('resource_id', 'effect_id', 'md5', 'is_vip')):
        raise ValueError('Admitted font metadata changed')
    root = Path(user_data) if user_data is not None else resource_user_data(spec['cache_relative'])
    path = root / spec['cache_relative']
    if not file_hash.matches(path, spec['sha256']):
        raise ValueError('Admitted font cache unavailable or changed: ' + str(path))
    return {'id': spec['resource_id'], 'path': path.resolve().as_posix()}


def jianying_install():
    """Directory of the installed Jianying build: the uninstall registry entry first, then a recorded preview manifest."""
    candidates = []
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Uninstall\JianyingPro') as key:
                uninstall = winreg.QueryValueEx(key, 'UninstallString')[0]
                version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
        except OSError:
            uninstall, version = '', ''
        root = Path(os.path.expandvars(uninstall.strip().strip('"'))).parent if uninstall else None
        candidates += [root / version, root] if root else []
    for manifest in sorted((SKILL_ROOT / 'assets').glob('native-preview-*.json'), reverse=True):
        recorded = read_json(manifest).get('install')
        if recorded:
            candidates.append(Path(recorded))
    return next((path for path in candidates if (path / 'Resources' / 'Font' / 'SystemFont').is_dir()), None)


DEFAULT_FONT_CACHE = {}


def default_text_font():
    """The entry Jianying itself writes for text that names no font; None when this machine has no install.

    Jianying renders text without this path as placeholder glyphs, so the default is written explicitly
    instead of left for the app to repair on save.
    """
    if not DEFAULT_FONT_CACHE:
        install = jianying_install()
        font = (install / 'Resources' / 'Font' / 'SystemFont' / 'zh-hans.ttf') if install else None
        DEFAULT_FONT_CACHE['font'] = {'path': font.resolve().as_posix()} if font and font.is_file() else None
    return DEFAULT_FONT_CACHE['font']


def text_segment(item, target, backend):
    style = dict(item.get('style', {}))
    def rgb(color):
        return tuple(int(color[i:i+2], 16) / 255 for i in (1, 3, 5))
    border_color = style.pop('border_color', '#000000')
    border_width = style.pop('border_width', 40)
    shadow = style.pop('shadow', None)
    if shadow is not None:
        shadow = dict(shadow)
        shadow['color'] = rgb(shadow.get('color', '#000000'))
    transform = {'transform_x': item['x'], 'transform_y': item['y'], **item.get('transform', {})}
    segment = backend.TextSegment(item['text'], target,
        style=backend.TextStyle(size=item['size'], color=rgb(item['color']), bold=item['bold'],
                                align=1, auto_wrapping=style.pop('auto_wrapping', True), **style),
        clip_settings=backend.ClipSettings(**transform),
        border=backend.TextBorder(color=rgb(border_color), width=border_width) if item['border'] else None,
        shadow=backend.TextShadow(**shadow) if shadow is not None else None)
    # 0.3.0 gives text a MediaSegment speed reference but never registers its speed material.
    # Text has no source time range; remove this orphan while keeping subsequent animation refs.
    segment.extra_material_refs.remove(segment.speed.global_id)
    for key in sorted(item.get('keyframes', []), key=lambda k: k['time']):
        segment.add_keyframe(getattr(backend.KeyframeProperty, key['property']), us(key['time']), key['value'])
    return segment


def bind_text_font(material, item, default_font=None, local_assets=None, draft_root=None):
    rich = json.loads(material['content'])
    native_text_highlight.apply(rich, item)
    if 'font_path' in item:
        if local_assets is None or draft_root is None:
            raise ValueError('Local font binding requires a verified asset and draft root')
        local_font.bind_material(material, local_assets[item['font_path']], draft_root)
        return material
    if 'font' in item:
        rich['styles'][0]['font'] = native_font(item['font'])
    elif default_font is not None:
        material['font_path'] = default_font['path']
    material['content'] = json.dumps(rich, ensure_ascii=False)
    return material


def probe(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f'Missing local media: {path}')
    data = json.loads(run([tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format',
                           '-of', 'json', str(path)]).stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    duration = data.get('format', {}).get('duration')
    if duration is None:
        duration = max((float(s.get('duration', 0)) for s in videos + audios), default=0)
    duration = float(duration)
    number(duration, 'media duration')
    kind = 'image' if path.suffix.lower() in IMAGE_EXT and videos else 'video' if videos else 'audio'
    if not videos and not audios:
        raise ValueError(f'No supported media stream: {path}')
    info = {'path': str(path), 'kind': kind, 'duration': 0 if kind == 'image' else duration,
            'has_audio': bool(audios)}
    if audios:
        info.update(sample_rate=int(audios[0].get('sample_rate', 0)), channels=audios[0].get('channels', 0))
    if videos:
        v = videos[0]
        numerator, denominator = v.get('avg_frame_rate', '0/1').split('/')
        info.update(width=v['width'], height=v['height'],
                    fps=float(numerator) / float(denominator) if float(denominator) else 0)
    if kind != 'image' and duration <= 0:
        raise ValueError(f'Media duration could not be measured: {path}')
    return info


def frame(path, timestamp, destination):
    seek = [] if Path(path).suffix.lower() in IMAGE_EXT else ['-ss', f'{timestamp:.6f}']
    run([tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
         *seek, '-i', path, '-frames:v', '1',
         '-vf', 'scale=960:960:force_original_aspect_ratio=decrease', '-q:v', '3', destination])
    if not Path(destination).is_file() or not Path(destination).stat().st_size:
        raise ValueError(f'Frame extraction produced no image at {timestamp}s')


def new_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def draft_location(user_data=None):
    if user_data is None:
        override = os.environ.get('JIANYING_USER_DATA')
        local = os.environ.get('LOCALAPPDATA')
        if not local and not override:
            return {'active_draft_root': None, 'setting_file': None, 'homepage_index': None}
        user_data = Path(os.path.expandvars(override)).expanduser() if override else Path(local) / 'JianyingPro' / 'User Data'
        if override and not user_data.is_dir():
            raise ValueError(f'JIANYING_USER_DATA directory is unavailable: {user_data}')
    user_data = Path(user_data)
    setting = user_data / 'Config' / 'globalSetting'
    default_root = user_data / 'Projects' / 'com.lveditor.draft'
    custom = ''
    if setting.is_file():
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(setting.read_text(encoding='utf-8-sig'))
        # Qt QSettings escapes Windows separators in this INI value.
        custom = config.get('General', 'currentCustomDraftPath', fallback='').strip().strip('"').replace('\\\\', '\\')
    active = Path(custom) if custom else default_root
    if custom and not active.is_dir():
        raise ValueError(f'Configured Jianying draft directory is unavailable: {active}')
    return {'active_draft_root': str(active.resolve()) if active.is_dir() else None,
            'setting_file': str(setting), 'homepage_index': str(default_root / 'root_meta_info.json')}


def wait_for_registration(destination, index_path, seconds):
    deadline = time.monotonic() + seconds
    while index_path:
        try:
            entries = read_json(index_path)['all_draft_store']
            matches = [x for x in entries if Path(x.get('draft_fold_path', '')).resolve() == destination]
            if len(matches) == 1:
                return {'status': 'registered', 'draft_id': matches[0]['draft_id'], 'index': index_path}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # The native writer may be replacing the index during this read.
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return {'status': 'unverified', 'index': index_path}


def jianying_running():
    """Read-only process probe; the draft watcher that registers new folders exists only while Jianying runs."""
    if os.name != 'nt':
        return False
    try:
        listing = run(['tasklist', '/FI', 'IMAGENAME eq JianyingPro.exe', '/NH'], timeout=30).stdout
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False
    return 'JianyingPro.exe' in listing


def draft_tree_digest(root):
    """{relative path: [bytes, sha256]} so a round-trip can prove the folder came back byte for byte."""
    root = Path(root)
    return {item.relative_to(root).as_posix(): [item.stat().st_size, file_hash.sha256(item)]
            for item in sorted(root.rglob('*')) if item.is_file()}


def refresh_registration(draft, wait_seconds=30, settle_seconds=8, watcher_required=True):
    """Re-create an existing draft folder so the running Jianying watcher registers it.

    Jianying 11.6 registers folders whose creation it observes; a folder that already existed when
    the watcher started stays invisible, and restarting Jianying does not register it. The folder is
    copied aside, removed and copied back byte for byte, so an adopted (encrypted) draft keeps the
    app's own files. Nothing else is written: the homepage index is only read.
    """
    destination = Path(draft).resolve()
    settings = draft_location()
    active = settings['active_draft_root']
    if not active:
        raise ValueError('No configured Jianying draft root; run doctor and confirm where drafts are stored')
    active = Path(active).resolve()
    if destination.parent != active or not destination.is_dir():
        raise ValueError(f'Draft must be an existing directory directly inside the configured draft root: {active}')
    if not (destination / 'draft_meta_info.json').is_file():
        raise ValueError(f'Not a draft directory (no draft_meta_info.json): {destination}')
    if watcher_required and not jianying_running():
        raise ValueError('Jianying is not running: its draft watcher registers folders only while it runs. Open Jianying, then retry.')
    index = settings['homepage_index']
    known = wait_for_registration(destination, index, 0)
    if known['status'] == 'registered':
        return {'draft': str(destination), 'refresh': 'already_registered', 'global_index_modified': False,
                'registration': known, 'homepage_visibility': 'pending_ui_verification'}
    before = draft_tree_digest(destination)
    with tempfile.TemporaryDirectory(prefix='.jianying-refresh-') as backup:
        staged = Path(backup) / 'draft'
        shutil.copytree(destination, staged)
        if draft_tree_digest(staged) != before:
            raise ValueError('Backup copy differs from the draft; refresh cancelled')
        shutil.rmtree(destination)
        time.sleep(settle_seconds)
        shutil.copytree(staged, destination)
        if draft_tree_digest(destination) != before:
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(staged, destination)
            raise ValueError('Draft changed during the round-trip; the previous copy was restored and registration was not refreshed')
    registration = wait_for_registration(destination, index, wait_seconds)
    return {'draft': str(destination), 'refresh': 'recreated', 'files': len(before), 'byte_identical': True,
            'global_index_modified': False, 'registration': registration,
            'homepage_visibility': 'pending_ui_verification' if registration['status'] == 'registered' else 'unverified'}


def doctor():
    versions = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for subkey in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                           r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                try:
                    with winreg.OpenKey(hive, subkey) as root:
                        for index in range(winreg.QueryInfoKey(root)[0]):
                            try:
                                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as item:
                                    name = winreg.QueryValueEx(item, 'DisplayName')[0]
                                    if '剪映' in name or 'jianying' in name.lower():
                                        versions.append({'name': name, 'version': winreg.QueryValueEx(item, 'DisplayVersion')[0]})
                            except OSError:
                                continue
                except OSError:
                    continue
    location_error = None
    try:
        location = draft_location()
    except (ValueError, OSError, configparser.Error) as exc:
        location_error = str(exc)
        location = {'active_draft_root': None, 'setting_file': None, 'homepage_index': None}
    candidates = [location['active_draft_root']] if location['active_draft_root'] else []
    local = os.environ.get('LOCALAPPDATA')
    if local:
        for rel in ('JianyingPro/User Data/Projects/com.lveditor.draft',
                    'JianyingPro/User Data/com.lveditor.draft'):
            p = Path(local) / rel
            if p.is_dir() and str(p.resolve()) not in candidates:
                candidates.append(str(p))
    try:
        backend = importlib.metadata.version('pyJianYingDraft')
    except importlib.metadata.PackageNotFoundError:
        backend = None
    backend_error = None
    try:
        load_backend()
    except (ImportError, OSError, ValueError) as exc:
        backend_error = f'{type(exc).__name__}: {exc}'
    backend_importable = backend_error is None
    try:
        import pymediainfo
        media_library = bool(pymediainfo.MediaInfo.can_parse())
    except (ImportError, OSError):
        media_library = False
    tools = {name: tool_path(name) for name in ('ffmpeg', 'ffprobe')}
    execution = {}
    for name, path in tools.items():
        try:
            execution[name] = bool(path and run([path, '-version'], timeout=10).stdout.strip())
        except (ValueError, OSError, subprocess.TimeoutExpired):
            execution[name] = False
    ready = os.name == 'nt' and all(execution.values()) and backend == '0.3.0' and backend_importable and media_library
    actions = []
    if os.name != 'nt':
        actions.append({'code': 'unsupported_os', 'instruction': '当前实现面向Windows剪映；其他系统需单独适配，不能声明迁移通过。'})
    for name in tools:
        if not execution[name]:
            actions.append({'code': name + '_unavailable', 'instruction':
                            f'安装可运行的{name}，设置当前会话JIANYING_{name.upper()}为其完整路径，或加入PATH；重新运行doctor。'})
    if backend != '0.3.0' or not backend_importable or not media_library:
        actions.append({'code': 'dependencies', 'instruction':
                        f'当前解释器为 {sys.executable}。使用外部虚拟环境，在PowerShell运行 & "{sys.executable}" -m pip install -r "{SKILL_ROOT / "scripts/requirements.txt"}" -c "{SKILL_ROOT / "scripts/constraints.txt"}"；不要在引擎runtime目录创建.venv，也不修改全局Python。',
                        'detail': backend_error})
    if location_error or not location['active_draft_root']:
        actions.append({'code': 'draft_directory', 'instruction':
                        '核实本机剪映设置中的草稿保存位置；旧设备盘符不存在时，在剪映设置中选可用目录。特殊用户数据位置可设置JIANYING_USER_DATA；不自动修改全局设置或猜目录。',
                        'detail': location_error})
    known_version = any(v['version'] == REFERENCE_JIANYING_VERSION for v in versions)
    if not versions:
        actions.append({'code': 'installation_unconfirmed', 'instruction':
                        '未从注册表确认Windows剪映安装；核实安装与实际运行版本，暂不声明剪映兼容或发布就绪。'})
    elif not known_version:
        actions.append({'code': 'version_unverified', 'instruction':
                        f'安装版本与高级结构参考{REFERENCE_JIANYING_VERSION}不同；ready只表示运行依赖就绪。'
                        '按能力主表和当前工程回执核实各功能；已有其他版本的分项证据仍有效，不能推定全功能通过或全部不可用。不自动升级/降级剪映。'})
    return {'python': sys.executable, 'python_version': sys.version.split()[0],
            'skill_root': str(SKILL_ROOT), 'platform': sys.platform,
            **tools, 'tool_execution': execution,
            'backend': backend, 'expected_backend': '0.3.0', 'mediainfo_available': media_library,
            'backend_importable': backend_importable, 'backend_error': backend_error,
            'jianying_installed': versions, 'draft_root_candidates': candidates,
            **location, 'draft_location_error': location_error,
            'publication_ready': bool(ready and versions and location['active_draft_root']),
            'advanced_schema_reference_version': REFERENCE_JIANYING_VERSION,
            'advanced_schema_version_match': known_version,
            'ready_scope': 'runtime_dependencies_only',
            'compatibility': {'all_features_verified': False,
                              'feature_evidence': 'consult_capabilities_and_project_receipts',
                              'ui_save_reopen': 'feature_and_project_specific',
                              'running_version_confirmed': False},
            'migration_instructions': actions,
            'background_only': True, 'ui_compatibility': 'unverified',
            'ready': ready}


def inspect_media(args):
    source = Path(args.input).resolve()
    if not source.exists():
        raise ValueError(f'Input does not exist: {source}')
    files = [source] if source.is_file() else sorted(p for p in source.rglob('*')
                                                   if p.is_file() and p.suffix.lower() in MEDIA_EXT)
    if not files:
        raise ValueError('No supported media files found')
    output = new_directory(args.out)
    records = []
    for index, path in enumerate(files[:args.max_files]):
        try:
            item = probe(path)
            item['frames'] = []
            if item['kind'] != 'audio':
                duration = item['duration']
                points = [0] if item['kind'] == 'image' else [duration * (i + 0.5) / args.frames for i in range(args.frames)]
                for j, point in enumerate(points):
                    image = output / f'media-{index:04d}-frame-{j:02d}.jpg'
                    frame(str(path), point, str(image))
                    item['frames'].append({'time': round(point, 6), 'path': str(image)})
            item['status'] = 'probed'
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            item = {'path': str(path), 'status': 'failed', 'error': str(exc)}
        records.append(item)
    report = {'files_found': len(files), 'files_processed': len(records),
              'truncated': len(files) > len(records), 'semantic_analysis': 'not_performed',
              'status': 'partial' if any(r['status'] == 'failed' for r in records) else 'success', 'media': records}
    write_json(output / 'media-index.json', report)
    return {'status': report['status'], 'index': str(output / 'media-index.json'),
            'files_processed': len(records), 'truncated': report['truncated']}


def analyze_reference(args):
    info = probe(args.input)
    if info['kind'] != 'video':
        raise ValueError('Reference must be a local video')
    number(args.start, 'start')
    number(args.duration, 'analysis duration', 0.01, 300)
    number(args.threshold, 'scene threshold', 0.01, 1)
    if args.start >= info['duration']:
        raise ValueError('Analysis start is past the end of the reference')
    span = min(args.duration, info['duration'] - args.start)
    result = run([tool('ffmpeg'), '-hide_banner', '-nostdin', '-ss', args.start,
                  '-i', info['path'], '-t', span, '-an',
                  '-vf', rf'select=gt(scene\,{args.threshold}),showinfo', '-f', 'null', '-'], timeout=600)
    cuts = sorted(set(round(args.start + float(x), 6)
                      for x in re.findall(r'pts_time:([0-9.eE+-]+)', result.stderr)))
    end_sample = max(args.start, args.start + span - max(0.1, 1 / (info['fps'] or 30)))
    # Bounded sampling: candidate cuts are hypotheses, never a verified shot list.
    uniform = [args.start + (end_sample - args.start) * i / (args.max_frames - 1)
               for i in range(args.max_frames)]
    points = sorted(set(round(t, 6) for t in uniform +
                        [max(args.start, min(end_sample, c + delta)) for c in cuts for delta in (-0.12, 0.12)]))
    if len(points) > args.max_frames:
        points = [points[round(i * (len(points) - 1) / (args.max_frames - 1))] for i in range(args.max_frames)]
    output = new_directory(args.out)
    frames = []
    for i, point in enumerate(points):
        target = output / f'reference-{i:03d}-{point:.3f}s.jpg'
        frame(info['path'], point, str(target))
        frames.append({'time': point, 'path': str(target)})
    audio_path = None
    if info['has_audio']:
        audio_path = output / 'audio.wav'
        run([tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
             '-ss', args.start, '-i', info['path'], '-t', span, '-vn', '-ac', '1',
             '-ar', '16000', '-c:a', 'pcm_s16le', str(audio_path)], timeout=600)
    report = {'source': info, 'window_start': args.start, 'window_duration': span,
              'entire_video_analyzed': args.start == 0 and span >= info['duration'],
              'scene_threshold': args.threshold, 'cut_candidates': cuts, 'frames': frames,
              'audio': str(audio_path) if audio_path else None, 'audio_time_origin': args.start,
              'semantic_analysis': 'not_performed', 'transcription': 'not_performed',
              'ocr': 'not_performed', 'beat_analysis': 'not_performed'}
    write_json(output / 'analysis.json', report)
    return {'analysis': str(output / 'analysis.json'), 'frames': len(frames),
            'cut_candidates': len(cuts), 'entire_video_analyzed': report['entire_video_analyzed'],
            'audio': report['audio'], 'next': 'View the actual frames and analyze audio; cuts are only candidates.'}


def native_enum(backend, kind, name, allow_vip=True):
    enum = getattr(backend, NATIVE_TYPES[kind])
    if not isinstance(name, str) or name not in enum.__members__:
        raise ValueError(f'Unknown {kind} name: {name}; run capabilities --kind {kind}')
    value = enum[name]
    if getattr(value.value, 'is_vip', False) and not allow_vip:
        raise ValueError(f'VIP resource {kind}/{name}: disabled by allow_vip_effects=false')
    return value


def validate_native(plan, mask_resource_root=None):
    """Validate native parameters only. Never render or substitute external effects."""
    used, backend = set(), None
    allow_vip = plan.setdefault('allow_vip_effects', True)
    if type(allow_vip) is not bool:
        raise ValueError('allow_vip_effects must be a boolean')
    def enum(kind, name):
        nonlocal backend
        backend = backend or load_backend()
        used.add(kind)
        return native_enum(backend, kind, name, allow_vip)
    def effect(item):
        fields(item, {'kind', 'name', 'params', 'keyframes'}, 'effect', ('name',))
        if item.setdefault('kind', 'scene') not in ('scene', 'character'):
            raise ValueError('effect.kind must be scene or character')
        member = enum(item['kind'] + '_effects', item['name'])
        params = item.setdefault('params', {})
        allowed = {p.name for p in member.value.params}
        fields(params, allowed, 'effect.params')
        for name, value in params.items():
            if value is not None:
                number(value, 'effect.params.' + name, 0, 100)
    def filter_(item):
        fields(item, {'name', 'intensity'}, 'filter', ('name',))
        enum('filters', item['name'])
        number(item.setdefault('intensity', 100), 'filter.intensity', 0, 100)
    for group in ('clips', 'captions'):
        for item in plan[group]:
            animations = item.setdefault('animations', [])
            if not isinstance(animations, list):
                raise ValueError('animations must be an array')
            stages, durations = set(), {}
            for animation in animations:
                fields(animation, {'stage', 'name', 'duration'}, 'animation', ('stage', 'name'))
                stage = animation['stage']
                if stage not in ('intro', 'outro', 'group' if group == 'clips' else 'loop') or stage in stages:
                    raise ValueError('Invalid or duplicate animation stage')
                member = enum(('video_' if group == 'clips' else 'text_') + stage, animation['name'])
                if stage == 'loop':
                    if 'duration' in animation:
                        raise ValueError('Text loop duration is computed between intro and outro; omit duration')
                else:
                    value = item['duration'] if stage == 'group' else member.value.duration / 1_000_000
                    number(animation.setdefault('duration', min(value, item['duration'])),
                           'animation.duration', 0.000001, item['duration'])
                    durations[stage] = us(animation['duration'])
                stages.add(stage)
            if 'group' in stages and len(stages) > 1:
                raise ValueError('Video group animation cannot coexist with intro/outro')
            occupied = durations.get('intro', 0) + durations.get('outro', 0)
            if occupied > us(item['duration']) or ('loop' in stages and occupied >= us(item['duration'])):
                raise ValueError('Intro/outro overlap or leave no duration for loop')
            if group == 'captions':
                continue
            for key, check in (('effects', effect), ('filters', filter_)):
                values = item.setdefault(key, [])
                if not isinstance(values, list):
                    raise ValueError(key + ' must be an array')
                seen = set()
                for value in values:
                    check(value)
                    if 'keyframes' in value:
                        raise ValueError('Effect keyframes currently require a standalone blur track')
                    identity = (value.get('kind'), value['name'])
                    if identity in seen:
                        raise ValueError('Duplicate clip effect/filter')
                    seen.add(identity)
            if 'mix_mode' in item:
                native_mix_mode.validate_spec(item, plan)
                native_mix_mode.resource(item['mix_mode']['name'])
                used.add('mix_mode')
            if 'background' in item:
                fields(item['background'], {'type', 'blur', 'color'}, 'background', ('type',))
                background = item['background']
                if item['track'] != 'video' or background['type'] not in ('blur', 'color'):
                    raise ValueError('Background filling requires main video and type blur/color')
                if background['type'] == 'blur':
                    if 'color' in background:
                        raise ValueError('Blur background does not accept color')
                    number(background.setdefault('blur', 0.0625), 'background.blur', 0, 1)
                else:
                    if 'blur' in background:
                        raise ValueError('Color background does not accept blur')
                    if not isinstance(background.get('color'), str) or not re.fullmatch(r'#[0-9a-fA-F]{8}', background['color']):
                        raise ValueError('Background color must be #RRGGBBAA')
                used.add('background')
            if 'chroma' in item:
                native_chroma.validate_spec(item, plan)
                native_chroma.resource()
                used.add('chroma')
            if 'mask' in item:
                mask = item['mask']
                fields(mask, {'name', 'experimental', 'center_x', 'center_y', 'size', 'rotation',
                              'feather', 'invert', 'rect_width', 'round_corner', 'keyframes', 'rendering'}, 'mask', ('name', 'experimental'))
                if mask['experimental'] is not True:
                    raise ValueError('Native mask is experimental on newer Jianying; experimental must be true')
                enum('masks', mask['name'])
                if mask['name'] not in ('矩形', '圆形'):
                    raise ValueError('Only measured rectangle and zero-feather circle masks are admitted; other shapes remain research-only')
                for name, low, high, default in (('center_x', -1e6, 1e6, 0), ('center_y', -1e6, 1e6, 0),
                    ('size', 0.000001, 4, 0.5), ('rotation', -36000, 36000, 0), ('feather', 0, 100, 0)):
                    number(mask.setdefault(name, default), 'mask.' + name, low, high)
                if type(mask.setdefault('invert', False)) is not bool:
                    raise ValueError('mask.invert must be boolean')
                if mask['name'] == '圆形' and mask['feather'] != 0:
                    raise ValueError('Measured circle mask requires feather=0; nonzero feather rendering is not admitted')
                derived = 'rendering' in mask
                if derived and (mask['rendering'] != native_mask_feather.MODE or mask['name'] != '矩形'):
                    raise ValueError('mask.rendering requires red_coverage_v1 rectangle')
                if mask['feather'] != 0 and not derived:
                    raise ValueError('mask.feather must be 0; nonzero feather rendering is not admitted')
                for name in ('rect_width', 'round_corner'):
                    if name in mask:
                        if mask['name'] != '矩形':
                            raise ValueError('rect_width/round_corner require a rectangle mask')
                        number(mask[name], 'mask.' + name, 0.000001 if name == 'rect_width' else 0,
                               4 if name == 'rect_width' else 100)
                if 'keyframes' in mask:
                    if mask['name'] not in ('矩形', '圆形') or not isinstance(mask['keyframes'], list) or not mask['keyframes']:
                        raise ValueError('Dynamic mask keyframes require a measured rectangle/circle and a nonempty array')
                    last = {}
                    source_last = {}
                    for key in mask['keyframes']:
                        fields(key, {'property', 'time', 'value'}, 'mask.keyframe', ('property', 'time', 'value'))
                        prop = key['property']
                        if prop not in native_advanced.MASK_PROPERTIES:
                            raise ValueError('Unsupported mask keyframe property')
                        if mask['name'] == '圆形' and prop == 'feather':
                            raise ValueError('Circle feather keyframes are not admitted')
                        if prop == 'feather' and key['value'] != 0 and not derived:
                            raise ValueError('mask.feather keyframes must remain 0; nonzero feather rendering is not admitted')
                        number(key['time'], 'mask.keyframe.time', 0, item['duration'] - 0.000001)
                        low, high = ((0, 100) if prop == 'feather' else (0.000001, 1e6) if prop in ('width', 'height') else
                                     (-36000, 36000) if prop == 'rotation' else (-1e6, 1e6))
                        number(key['value'], 'mask.keyframe.value', low, high)
                        timestamp = us(key['time'])
                        if prop not in last and timestamp != 0:
                            raise ValueError('Each mask keyframe property must start at time 0')
                        if prop in last and timestamp <= last[prop]:
                            raise ValueError('Mask keyframes must be strictly increasing per property')
                        source_timestamp = native_advanced.source_keyframe_time(item, key['time'])
                        if prop in source_last and source_timestamp <= source_last[prop]:
                            raise ValueError('Mask source-time keyframes must remain distinct after speed mapping')
                        last[prop] = timestamp
                        source_last[prop] = source_timestamp
                if mask['name'] == '圆形':
                    native_advanced.cached_material('common_mask', None)
                elif mask['name'] == '矩形':
                    if derived:
                        native_mask_feather.validate_spec(item, plan)
                        native_mask_feather.check_runtime()
                        if mask_resource_root is None:
                            native_mask_feather.source_path()
                        else:
                            native_mask_feather.verify_directory(mask_resource_root)
                    else:
                        native_advanced.cached_material('rectangle_mask', None)
    for key, check in (('effects', effect), ('filters', filter_)):
        for item in plan[key]:
            selection = {k: v for k, v in item.items() if k not in ('start', 'duration', 'track')}
            check(selection)
            item.update(selection)
    return sorted(used)


def effect_params(backend, item):
    member = native_enum(backend, item['kind'] + '_effects', item['name'], True)
    return member, [item['params'].get(p.name) for p in member.value.params]


def apply_animations(segment, item, backend, text=False):
    animations = sorted(item['animations'], key=lambda a: {'intro': 0, 'outro': 1, 'group': 2, 'loop': 3}[a['stage']])
    for animation in animations:
        kind = ('text_' if text else 'video_') + animation['stage']
        member = native_enum(backend, kind, animation['name'], True)
        segment.add_animation(member, us(animation['duration']) if 'duration' in animation else None)
        if text and animation['stage'] == 'loop':
            # 0.3.0 starts loops at intro.start, not intro.end. Correct native timing, not pixels.
            start = sum(us(a['duration']) for a in animations if a['stage'] == 'intro')
            outro = sum(us(a['duration']) for a in animations if a['stage'] == 'outro')
            loop = segment.animations_instance.animations[-1]
            loop.start, loop.duration = start, us(item['duration']) - start - outro


def planned_animation_id(backend, group, animation):
    kind = ('video_' if group == 'clips' else 'text_') + animation['stage']
    return str(native_enum(backend, kind, animation['name'], True).value.effect_id)


def cached_animation_id(backend, kind, name):
    """The effect id whose cached bundle decides whether this animation can ship at all."""
    return str(native_enum(backend, kind, name, True).value.effect_id)


def animation_availability(backend, user_data, kinds=ANIMATION_KINDS):
    """Split every animation enum name by whether the local effect cache resolves its bundle now.

    Measured 2026-10-17 on 11.6: of 737 animation names only 20 carry a package here, because the app
    downloads packages lazily, per use. Only the available names can be recommended without a retry.

    A machine without a Cache/effect directory at all (a fresh installation that never downloaded an
    effect) has no per-name verdict to give; every name comes back missing and effect_cache_dir says why.
    """
    cache = effect_cache_dir(user_data) or Path(user_data) / 'Cache' / 'effect'
    available, missing = {}, {}
    for kind in kinds:
        enum = getattr(backend, NATIVE_TYPES[kind])
        here = [name for name in enum.__members__
                if native_advanced.animation_bundle(cache, str(enum.__members__[name].value.effect_id)) is not None]
        available[kind] = here
        missing[kind] = [name for name in enum.__members__ if name not in here]
    return available, missing


def animation_cache_status(user_data):
    """'local_cache_absent' when this machine has never created the effect cache, else 'local_cache_checked'."""
    return 'local_cache_checked' if effect_cache_dir(user_data) else 'local_cache_absent'


def animation_preflight(plan, backend, user_data):
    """Report every planned animation the local cache cannot resolve before the build drops it.

    Only a cache-resolved entry survives building, and an unresolved one used to disappear silently;
    this names it at validate time together with the names that do resolve locally. The status is
    'local_cache_absent' when the cache itself is missing, so a first run is not read as 737 verdicts.
    """
    available, _ = animation_availability(backend, user_data)
    unresolvable = []
    for group in ('clips', 'captions'):
        for item in plan.get(group, []):
            for animation in item.get('animations', []):
                kind = ('video_' if group == 'clips' else 'text_') + animation['stage']
                name = animation['name']
                if name not in available[kind]:
                    unresolvable.append(f"{group}/{animation['stage']}/{name}(id {cached_animation_id(backend, kind, name)})")
    return available, unresolvable, animation_cache_status(user_data)


def drop_unavailable_animations(plan, backend, user_data=None):
    """Keep only the planned animations whose effect bundle the local effect cache resolves.

    剪映 resolves an animation at Cache/effect/<id>/<hash>. An entry without a bundle is not drawn at
    all: the app marks the segment as lost and keeps hunting for the resource while the draft opens
    (measured 2026-10-16 on 11.6: `path` empty in the app's own save, bundle never downloaded). Such
    an animation therefore never enters the draft; dropping it here also keeps the derived text loop
    timing correct, because apply_animations reads the surviving intros and outros.
    """
    user_data = user_data if user_data is not None else configured_user_data()
    cache = effect_cache_dir(user_data)
    if cache is None:
        # No cache yet no longer means "all 737 names are unavailable": naming it that way made a
        # first-run machine look like a machine that rejected every request.
        if user_data:
            print('Animation packages were not checked: this user data has no Cache/effect directory yet.', file=sys.stderr)
        return []
    dropped = []
    for group in ('clips', 'captions'):
        for item in plan.get(group, []):
            requested = item.get('animations', [])
            kept = []
            for animation in requested:
                identifier = planned_animation_id(backend, group, animation)
                if native_advanced.animation_bundle(cache, identifier) is None:
                    dropped.append({'group': group, 'stage': animation['stage'],
                                    'name': animation['name'], 'id': identifier})
                else:
                    kept.append(animation)
            if len(kept) != len(requested):
                item['animations'] = kept
    return dropped


def apply_native(segment, item, backend):
    apply_animations(segment, item, backend)
    for effect in item['effects']:
        member, params = effect_params(backend, effect)
        segment.add_effect(member, params)
    for filter_ in item['filters']:
        segment.add_filter(native_enum(backend, 'filters', filter_['name'], True), filter_['intensity'])
    if 'mix_mode' in item:
        segment.set_mix_mode(native_enum(backend, 'mix_modes', item['mix_mode']['name'], True))
    if 'chroma' in item:
        segment.add_chroma(**{k: v for k, v in item['chroma'].items() if k != 'rendering'})
    if 'background' in item:
        background = dict(item['background'])
        segment.add_background_filling(background.pop('type'), **background)
    if 'mask' in item:
        mask = dict(item['mask'])
        mask.pop('experimental')
        mask.pop('keyframes', None)
        mask.pop('rendering', None)
        segment.add_mask(native_enum(backend, 'masks', mask.pop('name'), True), **mask)


def native_catalog(args):
    if args.kind == 'text_glows':
        admitted=read_json(SKILL_ROOT/'assets/native-text-glow-11.5.json')
        matches=[name for name,spec in admitted.items() if args.search in name and not (args.free_only and spec['is_vip'])]
        result={'text_glows':matches[:args.limit],'total_matches':len(matches),'truncated':len(matches)>args.limit,'resource_availability':'identity_checked_on_use'}
        if args.details:result['details']=[{'name':name,'label':admitted[name]['label'],'is_vip':admitted[name]['is_vip'],
            'parameters':{'color':'#RRGGBB','strength':[.01,1],'range':[.01,1]},'scope':'static single-line admitted-font white text only'} for name in matches[:args.limit]]
        return result
    backend = load_backend()
    values = list(getattr(backend, NATIVE_TYPES[args.kind]))
    if args.kind == 'keyframes':
        values = [v for v in values if v.name in KEYFRAMES]
    if args.kind == 'fonts':
        admitted = read_json(SKILL_ROOT / 'assets/native-fonts-11.5.json')
        values = [v for v in values if v.name in admitted]
    matches = [v for v in values if args.search in v.name and
               (not args.free_only or not getattr(v.value, 'is_vip', False))]
    selected = matches[:args.limit]
    result = {args.kind: [v.name for v in selected], 'total_matches': len(matches),
              'truncated': len(matches) > len(selected), 'resource_availability': 'unverified'}
    if args.kind in ANIMATION_KINDS and args.user_data:
        available, missing = animation_availability(backend, args.user_data, (args.kind,))
        result['available_here'] = available[args.kind]
        result['missing_here_count'] = len(missing[args.kind])
        result['resource_availability'] = animation_cache_status(args.user_data)
    if args.details:
        result['details'] = [{'name': v.name, 'label': getattr(v.value, 'name', getattr(v.value, 'title', v.name)),
            'is_vip': getattr(v.value, 'is_vip', None),
            'parameters': [{'name': p.name, 'input_min': 0, 'input_max': 100,
                            'native_min': p.min_value, 'native_max': p.max_value,
                            'native_default': p.default_value} for p in getattr(v.value, 'params', [])],
            'default_duration_seconds': getattr(v.value, 'duration', 0) / 1_000_000 or None,
            'experimental': args.kind == 'masks'} for v in selected]
    return result


def validate_plan(path, user_data=None, mask_resource_root=None):
    plan_path = Path(path).resolve()
    plan = copy.deepcopy(read_json(plan_path))
    fields(plan, {'schema_version', 'name', 'mode', 'canvas', 'clips', 'audio', 'captions', 'notes', 'missing_assets',
                  'effects', 'filters', 'allow_vip_effects', 'jianying_tasks', 'layer_order', 'composites'},
           'plan', ('schema_version', 'name', 'mode', 'canvas', 'clips'))
    if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    name = plan['name']
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith(('.', ' ')):
        raise ValueError('Invalid Windows project name')
    if name in ('.', '..') or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', name):
        raise ValueError('Reserved Windows project name')
    if plan['mode'] not in ('script', 'reference', 'hybrid'):
        raise ValueError('mode must be script, reference or hybrid')
    fields(plan['canvas'], {'width', 'height', 'fps'}, 'canvas', ('width', 'height', 'fps'))
    for k in ('width', 'height', 'fps'):
        value = plan['canvas'][k]
        if type(value) is not int:
            raise ValueError(f'canvas.{k} must be an integer')
        number(value, f'canvas.{k}', 1 if k == 'fps' else 2, 120 if k == 'fps' else 16384)
        if k != 'fps' and value % 2:
            raise ValueError(f'canvas.{k} must be even')
    for key in ('clips', 'audio', 'captions', 'notes', 'missing_assets', 'effects', 'filters', 'jianying_tasks', 'composites'):
        value = plan.setdefault(key, [])
        if not isinstance(value, list):
            raise ValueError(f'{key} must be an array')
    if not plan['clips']:
        raise ValueError('At least one main video clip is required')
    for key in ('notes', 'missing_assets'):
        if not all(isinstance(x, str) for x in plan[key]):
            raise ValueError(f'{key} entries must be strings')
    probes, tracks, track_types = {}, {}, {}
    common = {'path', 'start', 'duration', 'source_in', 'speed', 'volume', 'track', 'reason'}
    for group, track_type, default_track in (('clips', 'video', 'video'), ('audio', 'audio', 'voice'), ('captions', 'text', 'captions')):
        for index, item in enumerate(plan[group]):
            label = f'{group}[{index}]'
            allowed = (common | {'fit', 'transform', 'keyframes', 'motion_easing', 'speed_curve', 'transition', 'effects', 'filters', 'animations',
                                 'mix_mode', 'chroma', 'background', 'mask', 'grading', 'matting_cache'} if group == 'clips' else
                       common | {'fade_in', 'fade_out', 'keyframes', 'denoise', 'loudness'} if group == 'audio' else
                       {'text', 'start', 'duration', 'track', 'size', 'color', 'x', 'y', 'bold', 'border', 'animations',
                        'style', 'transform', 'keyframes', 'motion_easing', 'text_curve', 'font', 'font_path', 'glow',
                        'highlights'})
            fields(item, allowed, label, ('start', 'duration', 'text' if group == 'captions' else 'path'))
            number(item['start'], label + '.start')
            number(item['duration'], label + '.duration', 0.000001)
            snap_range(item, label)
            track = item.setdefault('track', default_track)
            if not isinstance(track, str) or not track.strip():
                raise ValueError(f'{label}.track must be a non-empty string')
            if track in track_types and track_types[track] != track_type:
                raise ValueError(f'Track name used by different media types: {track}')
            track_types[track] = track_type
            tracks.setdefault(track, []).append(item)
            if 'reason' in item and not isinstance(item['reason'], str):
                raise ValueError(f'{label}.reason must be text')
            if group == 'captions':
                if not isinstance(item['text'], str) or not item['text'].strip() or '\x00' in item['text']:
                    raise ValueError(f'{label}.text must be non-empty text')
                if 'font' in item and 'font_path' in item:
                    raise ValueError(f'{label}: font and font_path are mutually exclusive')
                if 'font_path' in item:
                    if not isinstance(item['font_path'], str) or not item['font_path'] or '://' in item['font_path'] or '\x00' in item['font_path']:
                        raise ValueError(f'{label}.font_path must be a local TTF or OTF path')
                    if plan.get('composites'):
                        raise ValueError('Local font text inside native composites is not verified')
                    source = (plan_path.parent / item['font_path']).resolve()
                    item['font_path'] = str(source)
                    local_font.inspect(source)
                    if (item.get('animations') or item.get('transform') or item.get('keyframes')
                            or 'motion_easing' in item or 'text_curve' in item
                            or 'glow' in item or 'highlights' in item
                            or 'alpha' in item.get('style', {}) or item.get('style', {}).get('vertical')):
                        raise ValueError('Local font initially supports static horizontal text only; '
                                         'animations, motion, highlights, alpha, text effects and vertical layout are unverified')
                if 'font' in item:
                    native_font(item['font'], plan.get('allow_vip_effects', True))
                if 'font' in item or 'font_path' in item:
                    if any(ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF for c in item['text']):
                        raise ValueError('Local and admitted fonts currently require BMP text without surrogate characters')
                number(item.setdefault('size', 8), label + '.size', 1, 100)
                for axis in ('x', 'y'):
                    number(item.setdefault(axis, -0.8 if axis == 'y' else 0), label + '.' + axis, -10, 10)
                if not isinstance(item.setdefault('color', '#FFFFFF'), str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', item['color']):
                    raise ValueError(f'{label}.color must be #RRGGBB')
                for flag in ('bold', 'border'):
                    if type(item.setdefault(flag, flag == 'border')) is not bool:
                        raise ValueError(f'{label}.{flag} must be a boolean')
                validate_visual(item, label)
                if 'highlights' in item:
                    native_text_highlight.validate(item, plan)
                if 'glow' in item:
                    glow=item['glow']
                    fields(glow, {'name','color','strength','range'}, label+'.glow', ('name','color','strength','range'))
                    if not isinstance(glow['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',glow['color']):
                        raise ValueError('Text glow color must be #RRGGBB')
                    number(glow['strength'],label+'.glow.strength',.01,1)
                    number(glow['range'],label+'.glow.range',.01,1)
                    style=item.get('style',{})
                    fields(style,{'auto_wrapping'},label+'.style',('auto_wrapping',))
                    if ('font' not in item or item['border'] or item['bold'] or item['color']!='#FFFFFF' or item.get('animations') or
                            item.get('transform') or 'text_curve' in item or
                            style.get('auto_wrapping') is not False or set(style)-{'auto_wrapping'} or
                            any(ord(c)>0xFFFF or 0xD800<=ord(c)<=0xDFFF for c in item['text']) or '\n' in item['text'] or '\r' in item['text']):
                        raise ValueError('Text glow requires an admitted font and single-line white BMP text, border/bold off, style.auto_wrapping=false only')
                    keys=sorted(item['keyframes'],key=lambda k:k['time'])
                    if keys and (len(keys)!=2 or any(k['property']!='position_x' or not -.35<=k['value']<=.35 for k in keys) or
                                 keys[0]['time']!=0 or us(keys[-1]['time'])>=us(item['duration']) or item['x']!=0):
                        raise ValueError('Text glow motion requires two position_x keys within +/-0.35, first at 0 and last before text end, x=0; linear or admitted smoothstep only')
                    native_text_glow.material(glow,None,plan.get('allow_vip_effects',True))
                if 'font' in item and (item.get('animations') or item.get('transform') or
                        any(k['property'] != 'position_x' for k in item['keyframes'])):
                    raise ValueError('Admitted font permits static layout and position_x keys; other motion combinations are unverified')
                if 'motion_easing' in item:
                    keys = item['keyframes']
                    if (item['motion_easing'] != 'smoothstep' or len(keys) != 2 or
                            any(k['property'] != 'position_x' for k in keys) or
                            keys[0]['time'] != 0 or keys[-1]['time'] >= item['duration'] or
                            item.get('animations') or item.get('transform') or 'text_curve' in item or
                            item.get('style', {}).get('vertical')):
                        raise ValueError('Measured text motion_easing requires smoothstep with two position_x keys, first at 0 and last before text end; no animations, transform, text_curve or vertical layout')
                if 'text_curve' in item:
                    curve = item['text_curve']
                    fields(curve, {'angle', 'experimental'}, label + '.text_curve', ('angle',))
                    if 'experimental' in curve and type(curve['experimental']) is not bool:
                        raise ValueError(f'{label}.text_curve.experimental must be a boolean if supplied')
                    number(curve['angle'], label + '.text_curve.angle', 0, 360)
                if any(k['property'] == 'alpha' for k in item['keyframes']):
                    native_text_alpha.validate(item, plan)
                if {'transform_x', 'transform_y'} & set(item['transform']):
                    raise ValueError('Text position uses x/y or position keyframes, not transform_x/transform_y')
                style = item.setdefault('style', {})
                fields(style, {'alpha', 'italic', 'vertical', 'letter_spacing', 'line_spacing', 'auto_wrapping',
                               'border_color', 'border_width', 'shadow'}, label + '.style')
                for flag in ('italic', 'vertical', 'auto_wrapping'):
                    if flag in style and type(style[flag]) is not bool:
                        raise ValueError(f'{label}.style.{flag} must be a boolean')
                for k in ('letter_spacing', 'line_spacing'):
                    if k in style:
                        if type(style[k]) is not int:
                            raise ValueError(f'{label}.style.{k} must be an integer')
                        number(style[k], label + '.style.' + k, -100, 100)
                for k, high in (('alpha', 1), ('border_width', 100)):
                    if k in style:
                        number(style[k], label + '.style.' + k, 0, high)
                if 'border_color' in style and (not isinstance(style['border_color'], str) or
                        not re.fullmatch(r'#[0-9a-fA-F]{6}', style['border_color'])):
                    raise ValueError('Text border color must be #RRGGBB')
                if 'shadow' in style:
                    shadow = style['shadow']
                    fields(shadow, {'color', 'alpha', 'diffuse', 'distance', 'angle'}, 'text.shadow')
                    if 'color' in shadow and (not isinstance(shadow['color'], str) or
                            not re.fullmatch(r'#[0-9a-fA-F]{6}', shadow['color'])):
                        raise ValueError('Text shadow color must be #RRGGBB')
                    for k, low, high in (('alpha', 0, 1), ('diffuse', 0, 100), ('distance', 0, 100), ('angle', -180, 180)):
                        if k in shadow:
                            number(shadow[k], 'text.shadow.' + k, low, high)
                continue
            if not isinstance(item['path'], str) or not item['path'] or '://' in item['path']:
                raise ValueError(f'{label}.path must be a local media path')
            source = (plan_path.parent / item['path']).resolve()
            item['path'] = str(source)
            if item['path'] not in probes:
                probes[item['path']] = probe(source)
            info = probes[item['path']]
            number(item.setdefault('source_in', 0), label + '.source_in')
            item['source_in'] = us(item['source_in']) / 1_000_000
            if 'speed_curve' in item:
                points = item['speed_curve']
                if not isinstance(points, list) or len(points) not in (2, 3):
                    raise ValueError('speed_curve requires two or three source-normalized points')
                for point in points:
                    fields(point, {'x', 'y'}, label + '.speed_curve point', ('x', 'y'))
                    number(point['x'], 'speed_curve.x', 0, 1)
                    number(point['y'], 'speed_curve.y', 0.5, 2)
                if points[0]['x'] != 0 or points[-1]['x'] != 1 or any(a['x'] >= b['x'] for a, b in zip(points, points[1:])):
                    raise ValueError('speed_curve x must strictly increase from 0 to 1')
                average = native_advanced.average_curve_speed(points)
                if 'speed' in item:
                    number(item['speed'], label + '.speed', 0.5, 2)
                    if abs(item['speed'] - average) > 1e-9:
                        raise ValueError('speed must equal the speed_curve derived average; normally omit speed')
                item['speed'] = average
                excluded = ('keyframes', 'motion_easing', 'transition', 'effects', 'filters', 'animations',
                            'chroma', 'background', 'grading', 'mix_mode')
                if (info['kind'] != 'video' or info.get('has_audio') or item.get('volume', 0) != 0 or
                        any(item.get(k) for k in excluded)):
                    raise ValueError('Measured speed_curve requires video without an audio stream, volume=0 and no clip effects, keys or transitions')
                if 'mask' in item:
                    mask = item['mask']
                    if (not isinstance(mask, dict) or mask.get('name') != '矩形' or
                            any(mask.get(k, 0) != 0 for k in ('feather', 'rotation', 'round_corner')) or
                            mask.get('invert', False) is not False):
                        raise ValueError('Measured speed_curve mask requires a rectangle with feather/rotation/round_corner=0, invert=false')
                    if 'keyframes' in mask:
                        keys = mask['keyframes']
                        axis = keys[0].get('property') if isinstance(keys, list) and keys else None
                        dimensions = (info['width'], info['height'])
                        window_scale = 2 if dimensions == (1280, 720) else 1
                        limits = {'center_x': (-24 * window_scale, 24 * window_scale),
                                  'width': (520 * window_scale, 600 * window_scale)}.get(axis)
                        if (not isinstance(keys, list) or len(keys) != 2 or limits is None or
                                any(k.get('property') != axis for k in keys) or keys[0].get('time') != 0 or
                                any(not isinstance(k.get('value'), (int, float)) or not limits[0] <= k['value'] <= limits[1] for k in keys) or
                                dimensions not in ((640, 360), (1280, 720)) or abs(info['fps'] - 60) > .001 or
                                plan['canvas'] != {'width': info['width'], 'height': info['height'], 'fps': 60} or
                                item.get('fit', 'contain') != 'contain' or item.get('transform') or
                                mask.get('size', .5) != .9 or mask.get('center_y', 0) != 0 or
                                axis == 'center_x' and (mask.get('rect_width', mask.get('size', .5)) != .9 or mask.get('center_x', 0) != keys[0]['value']) or
                                axis == 'width' and (mask.get('center_x', 0) != 0 or mask.get('rect_width', mask.get('size', .5)) != keys[0]['value'] / info['width'])):
                            raise ValueError('Curve dynamic rectangle admits matching source/canvas 640x360 or 1280x720 at 60fps, contain with no transform, two center_x +/-24 or width 520..600 points at 640; doubled source-pixel limits at 1280, height 0.9, centered Y, initial static geometry matching the first key')
            number(item.setdefault('speed', 1), label + '.speed', 0.01, 100)
            number(item.setdefault('volume', 0 if group == 'clips' else 1), label + '.volume', 0, 10)
            if group == 'clips' and info['kind'] == 'audio':
                raise ValueError(f'{label} requires video or image')
            if group == 'audio' and info['kind'] != 'audio':
                raise ValueError(f'{label} requires an audio-only file; extract a copy first')
            if info['kind'] == 'image':
                if item['source_in'] != 0 or item['speed'] != 1:
                    raise ValueError('Still images require source_in=0, speed=1')
            elif us(item['source_in']) + us(item['duration'] * item['speed']) > source_limit_us(info):
                raise ValueError(f'{label}: source range exceeds measured media duration')
            if group == 'audio':
                if 'denoise' in item:
                    spec = item['denoise']
                    fields(spec, {'model_path'}, label + '.denoise', ('model_path',))
                    if not isinstance(spec['model_path'], str) or not spec['model_path'] or '://' in spec['model_path']:
                        raise ValueError('denoise.model_path must be the measured local native model')
                    spec['model_path'] = str((plan_path.parent / spec['model_path']).resolve())
                    if info.get('sample_rate') != 44100 or info.get('channels') != 2 or item['speed'] != 1:
                        raise ValueError('Measured realtime denoise currently requires 44100 Hz stereo audio at speed=1')
                    native_advanced.denoise_material(spec)
                for k in ('fade_in', 'fade_out'):
                    number(item.setdefault(k, 0), label + '.' + k)
                if item['fade_in'] + item['fade_out'] > item['duration']:
                    raise ValueError(f'{label}: fades exceed clip duration')
                if 'loudness' in item:
                    fields(item['loudness'], {'target_lufs'}, label + '.loudness', ('target_lufs',))
                    if item['loudness']['target_lufs'] != -23 or isinstance(item['loudness']['target_lufs'], bool):
                        raise ValueError('Only the measured native -23 LUFS target is admitted')
                    if (info.get('sample_rate') != 44100 or info.get('channels') != 2 or item['speed'] != 1 or
                            item['volume'] != 1 or item['fade_in'] or item['fade_out'] or
                            item.get('keyframes') or 'denoise' in item):
                        raise ValueError('Measured loudness requires 44100 Hz stereo, speed=1, gain=1 and no fades, keys or denoise')
                    if item['duration'] < 3:
                        raise ValueError('Measured native loudness profile requires at least 3 seconds')
                    measure_loudness(item)
                if 'keyframes' in item:
                    validate_parameter_keys(item['keyframes'], item['duration'], label + '.keyframes', 0, 10,
                                            item['source_in'], item['speed'])
                continue
            if item.setdefault('fit', 'contain') not in ('contain', 'cover'):
                raise ValueError(f'{label}.fit must be contain or cover')
            validate_visual(item, label)
            if 'matting_cache' in item:
                spec=item['matting_cache']
                fields(spec,{'manifest','sha256'},label+'.matting_cache',('manifest','sha256'))
                if not isinstance(spec['manifest'],str) or not spec['manifest'] or '://' in spec['manifest'] or not isinstance(spec['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',spec['sha256']):
                    raise ValueError('Matting cache requires a local manifest and SHA256')
                spec['manifest']=str((plan_path.parent/spec['manifest']).resolve())
                if (info['kind']!='video' or item['speed']!=1 or item['volume']!=0 or item['fit']!='contain' or
                        any(item.get(k) for k in ('speed_curve','transition','effects','filters','animations','mix_mode','chroma','background','mask','grading')) or
                        set(item['transform'])-{'scale_x','scale_y','transform_x','transform_y'} or
                        item['transform'].get('scale_x',1)!=1 or item['transform'].get('scale_y',1)!=1 or item['transform'].get('transform_y',0)!=0 or
                        not -.32<=item['transform'].get('transform_x',0)<=.32):
                    raise ValueError('Existing matting admits muted speed=1 contain video with scale=1, y=0, x within +/-0.32 only')
                keys=sorted(item['keyframes'],key=lambda k:k['time'])
                axis=keys[0]['property'] if keys else None
                if keys and (len(keys)!=2 or axis not in ('position_x','position_y') or
                             any(k['property']!=axis or not ((-.32<=k['value']<=.32) if axis=='position_x' else (0<=k['value']<=.24)) for k in keys) or
                             keys[0]['time']!=0 or us(keys[-1]['time'])>=us(item['duration']) or
                             ('transform_x' if axis=='position_x' else 'transform_y') in item['transform'] or
                             (axis=='position_y' and item['transform'].get('transform_x',0)!=0)):
                    raise ValueError('Existing matting motion requires two single-axis keys: position_x +/-0.32 at y=0, or position_y 0..0.24 at x=0; first 0, last before clip end, omit active-axis static transform; linear or smoothstep only')
                native_matting_cache.binding(item,plan['canvas'],info)
            if 'motion_easing' in item:
                keys = sorted(item['keyframes'], key=lambda k: k['time'])
                properties = {k['property'] for k in keys}
                allowed_axes = {'position_x', 'position_y'} if info['kind'] == 'video' else {'position_x'}
                if (item['motion_easing'] != 'smoothstep' or info['kind'] not in ('image', 'video') or
                        len(keys) != 2 or len(properties) != 1 or not properties <= allowed_axes or
                        keys[0]['time'] != 0 or us(keys[1]['time']) >= us(item['duration'])):
                    raise ValueError('Measured motion_easing requires two smoothstep points on one axis: image position_x, video position_x/position_y; first at 0 and last before clip end')
                if info['kind'] == 'video' and (item['speed'] != 1 or any(item.get(k) for k in
                        ('speed_curve', 'transition', 'effects', 'filters', 'animations', 'mix_mode',
                         'chroma', 'background', 'mask', 'grading'))):
                    raise ValueError('Measured video motion_easing requires speed=1 and no clip effects, masks, grading or transitions')
            if 'grading' in item:
                if isinstance(item['grading'], dict) and ({'blue_hsl', 'green_hsl'} | native_hsl_channels.CHANNELS.keys()) & item['grading'].keys() and (len(plan['clips']) != 1 or
                        plan['canvas'] != {'width': 640, 'height': 360, 'fps': 30} or
                        item['start'] != 0 or item['duration'] != 2 or
                        any(plan.get(k) for k in ('audio', 'captions', 'effects', 'filters', 'composites'))):
                    raise ValueError('Measured HSL requires one standalone two-second clip on a matching 640x360/30 canvas, with no other tracks; input types and controls must match the admitted channel subset')
                validate_grading(item, info)
            if 'transition' in item:
                fields(item['transition'], {'name', 'duration'}, 'transition', ('name', 'duration'))
                if not isinstance(item['transition']['name'], str):
                    raise ValueError('Transition name must be a string')
                number(item['transition']['duration'], 'transition.duration', 0.000001)
    for group, track_type in (('effects', 'effect'), ('filters', 'filter')):
        for item in plan[group]:
            fields(item, {'start', 'duration', 'track', 'name', 'kind', 'params', 'keyframes'} if group == 'effects' else
                         {'start', 'duration', 'track', 'name', 'intensity'}, group, ('start', 'duration', 'name'))
            number(item['start'], group + '.start')
            number(item['duration'], group + '.duration', 0.000001)
            snap_range(item, group)
            if 'keyframes' in item:
                if item['name'] != '模糊' or item.get('kind', 'scene') != 'scene':
                    raise ValueError('Only measured standalone scene blur supports effect keyframes')
                validate_parameter_keys(item['keyframes'], item['duration'], 'effect.keyframes', 0, 100)
            track = item.setdefault('track', group)
            if not isinstance(track, str) or not track.strip() or (track in track_types and track_types[track] != track_type):
                raise ValueError('Invalid or conflicting native effect/filter track name')
            track_types[track] = track_type
            tracks.setdefault(track, []).append(item)
    native_features = validate_native(plan, mask_resource_root)
    for item in plan['clips']:
        if native_chroma.requested(item):
            native_chroma.validate_media(item, probes[item['path']])
        if native_mix_mode.requested(item):
            native_mix_mode.check_media(item, plan, probes)
    for item in plan['clips']:
        if ('matting_cache' in item or 'speed_curve' in item or 'motion_easing' in item and probes[item['path']]['kind'] == 'video'):
            if any(x['start'] < item['start'] + item['duration'] and
                   item['start'] < x['start'] + x['duration'] for x in plan['effects'] + plan['filters']):
                raise ValueError('Measured speed_curve/video motion_easing does not admit overlapping independent effects or filters')
    for effect in plan['effects']:
        if effect.get('keyframes'):
            native_advanced.blur_material(effect, None)
    for track, items in tracks.items():
        items.sort(key=lambda x: x['start'])
        for before, after in zip(items, items[1:]):
            if end_us(before) > us(after['start']):
                raise ValueError(f'Overlapping clips on track: {track} at {after["start"]:.6f}s')
        for index, item in enumerate(items):
            if 'transition' in item:
                if index == len(items) - 1 or abs(end_us(item) - us(items[index + 1]['start'])) > 1:
                    raise ValueError('Transition needs an immediately adjacent following clip')
                if ('matting_cache' in items[index + 1] or 'speed_curve' in items[index + 1] or 'motion_easing' in items[index + 1] and
                        probes[items[index + 1]['path']]['kind'] == 'video'):
                    raise ValueError('Measured speed_curve/video motion_easing does not admit an incoming transition')
                if item['transition']['duration'] > min(item['duration'], items[index + 1]['duration']) / 2:
                    raise ValueError('Transition is too long for adjacent clips')
    main = tracks.get('video', [])
    if track_types.get('video') != 'video' or not main or us(main[0]['start']) != 0:
        raise ValueError('Main video track must be named video and start at 0')
    cursor = 0
    for item in main:
        if abs(us(item['start']) - cursor) > 1:
            raise ValueError(f'Gap in main video track at {cursor / 1_000_000:.6f}s; use an explicit black image if intentional')
        cursor = end_us(item)
    if any(end_us(x) > cursor for group in ('clips', 'audio', 'captions', 'effects', 'filters') for x in plan[group]):
        raise ValueError('Overlay, audio or text extends past main video')
    for task in plan['jianying_tasks']:
        fields(task, {'operation', 'track', 'start', 'duration', 'instructions'}, 'jianying_task',
               ('operation', 'track', 'start', 'duration', 'instructions'))
        if task['operation'] not in ('smart_cutout', 'tracking'):
            raise ValueError('jianying_task.operation must be smart_cutout or tracking')
        if not isinstance(task['instructions'], str) or not task['instructions'].strip():
            raise ValueError('jianying_task.instructions must explain the native action')
        if not isinstance(task['track'], str) or track_types.get(task['track']) != 'video':
            raise ValueError('jianying_task must target an existing video track')
        number(task['start'], 'jianying_task.start')
        number(task['duration'], 'jianying_task.duration', 0.000001)
        snap_range(task, 'jianying_task')
        task_start, task_end = us(task['start']), us(task['start']) + us(task['duration'])
        if not any(us(c['start']) <= task_start and us(c['start']) + us(c['duration']) >= task_end for c in tracks[task['track']]):
            raise ValueError('jianying_task must fit within a target clip')
    transitions = [x['transition']['name'] for x in plan['clips'] if 'transition' in x]
    if transitions:
        backend = load_backend()
        for name in transitions:
            if name not in backend.TransitionType.__members__:
                raise ValueError(f'Unknown transition enum: {name}; run capabilities')
    if 'layer_order' in plan:
        order = plan['layer_order']
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order) or len(order) != len(set(order)) or set(order) != set(track_types):
            raise ValueError('layer_order must contain each actual track name exactly once, from background to foreground')
        visual_order = [x for x in order if track_types[x] in ('video', 'text')]
        if visual_order[0] != 'video':
            raise ValueError('Main video must remain the lowest visual layer')
    order = plan.get('layer_order', list(dict.fromkeys(
        x['track'] for group in ('audio', 'clips', 'captions', 'effects', 'filters') for x in plan[group])))
    # Build always places video before the other video tracks.
    if 'layer_order' not in plan:
        video_names = [x for x in order if track_types[x] == 'video']
        order = [x for x in order if track_types[x] == 'audio'] + ['video'] + [x for x in video_names if x != 'video'] + [
            x for x in order if track_types[x] not in ('audio', 'video')]
    grouped, composite_names = set(), set()
    dynamic_curves = [item for item in plan['clips'] if 'speed_curve' in item and item.get('mask', {}).get('keyframes')]
    for spec in plan['composites']:
        fields(spec, {'name', 'tracks', 'perspective', 'start', 'duration', 'keyframes', 'motion_easing'}, 'composite', ('name', 'tracks'))
        if ('start' in spec) != ('duration' in spec):
            raise ValueError('Local composites require start and duration together')
        number(spec.get('start', 0), 'composite.start', 0)
        number(spec.get('duration', cursor / 1_000_000), 'composite.duration', 0.000001)
        if 'start' in spec:
            snap_range(spec, 'composite')
        group_start, group_duration = native_advanced.composite_range(spec, cursor)
        if group_start + group_duration > cursor:
            raise ValueError('Composite extends past main video')
        if 'keyframes' in spec:
            validate_parameter_keys(spec['keyframes'], group_duration / 1_000_000, 'composite.keyframes', -36000, 36000, rotation=True)
            properties = {k['property'] for k in spec['keyframes']}
            if len(properties) != 1:
                raise ValueError('Composite motion supports one measured property at a time')
            if properties & {'position_x', 'position_y', 'uniform_scale'}:
                if 'perspective' in spec or len(spec['keyframes']) != 2:
                    raise ValueError('Composite translation/uniform_scale requires two points and no perspective')
                for key in spec['keyframes']:
                    limits = (0.6, 1.2) if key['property'] == 'uniform_scale' else (-1, 1)
                    number(key['value'], 'composite.' + key['property'], *limits)
        if 'motion_easing' in spec:
            keys = spec.get('keyframes', [])
            if (spec['motion_easing'] != 'smoothstep' or len(keys) != 2 or
                    any(k['property'] != 'position_x' for k in keys) or
                    keys[0]['time'] != 0 or 'perspective' in spec or 'start' not in spec):
                raise ValueError('Composite easing requires smoothstep with two horizontal points starting at zero, no perspective')
            for key in keys:
                number(key['value'], 'composite.eased_position_x', -.35, .35)
        if 'perspective' in spec:
            perspective = spec['perspective']
            if 'static_up_right' in perspective:
                fields(perspective, {'static_up_right'}, 'perspective', ('static_up_right',))
                corner = perspective['static_up_right']
                if not isinstance(corner, list) or len(corner) != 2:
                    raise ValueError('Static upper-right requires [x,y]')
                for value in corner:
                    number(value, 'static_up_right coordinate', -1, 1)
                source_info = probe(plan['clips'][0]['path'])
                if (perspective['static_up_right'] not in ([1, -1], [0.5, -0.5]) or
                        plan['canvas'] != {'width': 640, 'height': 360, 'fps': 30} or
                        len(plan['clips']) != 1 or len(plan['captions']) != 1 or
                        group_duration != 2000000 or spec.get('start', 0) != 0 or
                        spec.get('keyframes') or spec.get('motion_easing') or plan['audio'] or plan['effects'] or plan['filters'] or
                        source_info['kind'] != 'image' or (source_info['width'], source_info['height']) != (640, 360) or len(plan['composites']) != 1 or len(spec['tracks']) != 2):
                    raise ValueError('Static upper-right admits standalone 640x360 image/text two-second composites and two measured positions only')
                for item in plan['clips'] + plan['captions']:
                    if item['start'] != 0 or item['duration'] != 2 or any(item.get(k) for k in ('transform','keyframes','animations','effects','filters','grading','mask','chroma','background','mix_mode','motion_easing')):
                        raise ValueError('Static upper-right requires static full-range image and text')
                image, text = plan['clips'][0], plan['captions'][0]
                if (image.get('fit', 'contain') != 'contain' or
                        any(text.get(k) for k in ('text_curve', 'glow', 'font', 'style'))):
                    raise ValueError('Static upper-right admits contain image and plain default-font text only')
            else:
                fields(perspective, {'keyframes'}, 'perspective', ('keyframes',))
                if not isinstance(perspective['keyframes'], list) or not perspective['keyframes']:
                    raise ValueError('Perspective requires a nonempty keyframe array')
                last = {}
                for key in perspective['keyframes']:
                    fields(key, {'property', 'time', 'values'}, 'perspective.keyframe', ('property', 'time', 'values'))
                    if key['property'] not in native_advanced.CORNER_PIN_PROPERTIES:
                        raise ValueError('Only measured corner pin keyframe properties are supported')
                    number(key['time'], 'perspective.keyframe.time', 0, group_duration / 1_000_000 - 0.000001)
                    if not isinstance(key['values'], list) or len(key['values']) != 2:
                        raise ValueError('Perspective values must be [x, y]')
                    for value in key['values']:
                        number(value, 'perspective.keyframe.value', -10, 10)
                    timestamp = us(key['time'])
                    prop = key['property']
                    if prop not in last and timestamp != 0:
                        raise ValueError('Perspective keyframes must start at time zero')
                    if prop in last and timestamp <= last[prop]:
                        raise ValueError('Perspective keyframes must strictly increase')
                    last[prop] = timestamp
                if native_advanced.other_corner_requested(spec):
                    native_advanced.validate_other_corner(plan, spec, group_duration, probe(plan['clips'][0]['path']))
        if not isinstance(spec['name'], str) or not spec['name'].strip() or spec['name'] in composite_names:
            raise ValueError('Composite names must be nonempty and unique')
        names = spec['tracks']
        if dynamic_curves:
            keys = spec.get('keyframes', [])
            subject = dynamic_curves[0]
            group_axis = keys[0]['property'] if keys else None
            limits = {'position_x': (-.3, .3), 'position_y': (-.15, .15),
                      'uniform_scale': (.9, 1.1)}.get(group_axis)
            canvas_720 = plan['canvas'] == {'width': 1280, 'height': 720, 'fps': 60}
            if canvas_720:
                limits = {'position_x': (-.2, .2), 'position_y': (-.1, .1),
                          'uniform_scale': (.9, 1.1)}.get(group_axis)
            valid_names = isinstance(names, list) and all(isinstance(name, str) and name in order for name in names)
            foreground_range = (valid_names and all(order.index(name) > order.index(subject['track']) for name in names) and
                                us(subject['start']) == group_start and us(subject['duration']) == group_duration)
            rear_static = canvas_720 and 'keyframes' not in spec and 'motion_easing' not in spec
            lead = us(subject['start']) - group_start
            trail = group_start + group_duration - us(subject['start']) - us(subject['duration'])
            ordered_windows = sorted(dynamic_curves, key=lambda item: us(item['start']))
            two_shot_static = (rear_static and valid_names and len(ordered_windows) == 2 and
                               ordered_windows[0]['track'] == ordered_windows[1]['track'] and
                               ordered_windows[0]['path'] == ordered_windows[1]['path'] and
                               tuple(us(item['duration']) for item in ordered_windows) in
                               ((2200000, 2200000), (1600000, 2400000), (2400000, 1600000)) and
                               us(ordered_windows[0]['source_in'] + ordered_windows[0]['duration'] * ordered_windows[0]['speed']) <= us(ordered_windows[1]['source_in']) and
                               us(ordered_windows[0]['start']) + us(ordered_windows[0]['duration']) == us(ordered_windows[1]['start']) and
                               sum(item['track'] == subject['track'] for item in plan['clips']) == 2 and
                               all(order.index(name) > order.index(subject['track']) for name in names) and
                               900000 <= us(ordered_windows[0]['start']) - group_start <= 1200000 and
                               800000 <= group_start + group_duration - us(ordered_windows[1]['start']) - us(ordered_windows[1]['duration']) <= 1100000)
            if two_shot_static:
                lead = us(ordered_windows[0]['start']) - group_start
                trail = group_start + group_duration - us(ordered_windows[1]['start']) - us(ordered_windows[1]['duration'])
            foreground_static = (rear_static and valid_names and
                                 all(order.index(name) > order.index(subject['track']) for name in names) and
                                 900000 <= lead <= 1200000 and 800000 <= trail <= 1100000)
            rear_interval = (500000 <= lead <= 600000 and 400000 <= trail <= 500000) or (
                rear_static and 900000 <= lead <= 1200000 and 800000 <= trail <= 1100000)
            rear_envelope = (canvas_720 and valid_names and (rear_static or group_axis in ('position_x', 'position_y', 'uniform_scale')) and
                             (group_axis == 'position_x' or 'motion_easing' not in spec) and
                             all(order.index(name) < order.index(subject['track']) for name in names) and
                             rear_interval)
            if (plan['canvas'] not in ({'width': 640, 'height': 360, 'fps': 60},
                                       {'width': 1280, 'height': 720, 'fps': 60}) or
                    (len(dynamic_curves) != 1 and not two_shot_static) or len(plan['composites']) != 1 or
                    'start' not in spec or 'perspective' in spec or spec.get('motion_easing') not in (None, 'smoothstep') or
                    (not rear_static and (len(keys) != 2 or limits is None or any(k['property'] != group_axis for k in keys) or
                     any(not limits[0] <= k['value'] <= limits[1] for k in keys))) or
                    (group_axis != 'position_x' and 'motion_easing' in spec) or
                    (not rear_static and keys[0]['time'] != 0) or not (foreground_range or rear_envelope or foreground_static) or
                    (rear_static and not (rear_envelope or foreground_static))):
                raise ValueError('Curve dynamic rectangle combination requires one independent window and one matching-range foreground contain-image/static-white-cached-font group: two X linear/smoothstep points within +/-0.3, two Y linear points within +/-0.15, or two uniform-scale linear points within 0.9..1.1 at 640x360; matching 1280x720 only two X linear/smoothstep points within +/-0.2 or two Y linear points within +/-0.1 or two uniform-scale linear points within 0.9..1.1, no perspective; 1280x720 additionally permits one rear X linear/smoothstep group within +/-0.2, Y default-linear group within +/-0.1, or uniform-scale default-linear group within 0.9..1.1, or static group omitting keyframes/easing, enclosing the window with 0.5..0.6s lead and 0.4..0.5s trail; static-only additionally admits 0.9..1.2s lead and 0.8..1.1s trail together; foreground static without keys/easing additionally admits that long interval; exactly two adjacent same-source same-track dynamic windows with durations (2.2,2.2), (1.6,2.4), or (2.4,1.6) seconds and ordered nonoverlap source ranges may share one foreground long static envelope')
        if any('matting_cache' in item and item['track'] in names for item in plan['clips']):
            raise ValueError('Existing matting cache inside composites is unverified')
        vertical_subjects = [item for item in plan['clips'] if 'matting_cache' in item and
                             any(k['property'] == 'position_y' for k in item['keyframes'])]
        if vertical_subjects:
            keys = spec.get('keyframes', [])
            group_axis = keys[0]['property'] if keys else None
            limits = {'position_x': (-.35, .35), 'position_y': (-.25, .25),
                      'uniform_scale': (.7, 1.1)}.get(group_axis)
            if (len(vertical_subjects) != 1 or len(plan['composites']) != 1 or
                    vertical_subjects[0].get('motion_easing') != 'smoothstep' or
                    'start' not in spec or 'perspective' in spec or len(keys) != 2 or
                    limits is None or any(k['property'] != group_axis for k in keys) or keys[0]['time'] != 0 or
                    any(not limits[0] <= k['value'] <= limits[1] for k in keys) or
                    us(vertical_subjects[0]['start']) != group_start or
                    us(vertical_subjects[0]['duration']) != group_duration or
                    not isinstance(names, list) or
                    any(name not in order or order.index(name) >= order.index(vertical_subjects[0]['track']) for name in names)):
                raise ValueError('Vertical matting combination requires one independent smoothstep subject and one matching-range behind two-point group: X +/-0.35 linear/smoothstep, Y +/-0.25 linear, or uniform scale 0.7..1.1 linear')
        if any('glow' in item and item['track'] in names for item in plan['captions']):
            raise ValueError('Text glow inside composites is unverified')
        if (not isinstance(names, list) or len(names) < 2 or not all(isinstance(x, str) for x in names) or
                len(names) != len(set(names)) or any(track_types.get(x) not in ('video', 'text', 'audio') for x in names) or
                track_types.get(names[0]) != 'video'):
            raise ValueError('Composite requires distinct video/text/audio tracks, beginning with video')
        if 'motion_easing' in spec or vertical_subjects or dynamic_curves:
            images = [x for x in plan['clips'] if x['track'] in names]
            texts = [x for x in plan['captions'] if x['track'] in names]
            if (len(names) != 2 or len(images) != 1 or len(texts) != 1 or
                    images[0]['track'] != names[0] or texts[0]['track'] != names[1] or
                    probes[images[0]['path']]['kind'] != 'image' or images[0]['fit'] != 'contain' or images[0]['transform'] or
                    any(us(x['start']) != group_start or us(x['duration']) != group_duration for x in images + texts) or
                    not texts[0].get('font') or any(c in texts[0]['text'] for c in '\r\n') or
                    texts[0]['color'].upper() != '#FFFFFF' or texts[0]['border'] or texts[0]['bold'] or texts[0]['transform'] or
                    set(texts[0]['style']) - {'auto_wrapping'} or texts[0]['style'].get('auto_wrapping', False) or
                    any(us(x['start']) < group_start + group_duration and group_start < us(x['start']) + us(x['duration'])
                        for x in plan['effects'] + plan['filters'])):
                raise ValueError('Composite easing, vertical matting or dynamic curve-window combination admits one contain image and one plain white cached-font single-line caption, both spanning the group, with no overlapping effects/filters')
        if any('motion_easing' in item and item['track'] in names for item in plan['clips'] + plan['captions']):
            raise ValueError('Measured motion_easing is not admitted inside composites')
        if any('speed_curve' in item and item['track'] in names for item in plan['clips']):
            raise ValueError('Measured speed_curve is not admitted inside composites')
        if dynamic_curves or any(k['property'] in ('position_x', 'position_y', 'uniform_scale') for k in spec.get('keyframes', [])):
            for item in plan['clips']:
                if item['track'] in names and (item['speed'] != 1 or item.get('keyframes') or
                        any(item.get(k) for k in ('effects', 'filters', 'animations', 'mask', 'grading', 'chroma', 'background', 'mix_mode', 'transition'))):
                    raise ValueError('Composite translation/uniform_scale admits ordinary speed=1 media and static transforms only')
            if any(item['track'] in names and (item.get('keyframes') or item.get('animations') or item.get('text_curve')) for item in plan['captions']):
                raise ValueError('Composite translation/uniform_scale admits static text only')
            if any(item['track'] in names for item in plan['audio']):
                raise ValueError('Composite translation/uniform_scale audio combination is unverified')
        if any('loudness' in x and x['track'] in names for x in plan['audio']):
            raise ValueError('Measured loudness is restricted to standalone audio, outside composites')
        for name in names:
            if not all(group_start <= us(x['start']) and us(x['start']) + us(x['duration']) <= group_start + group_duration for x in tracks[name]):
                raise ValueError('Selected composite tracks must lie entirely inside the group range; split clips first')
        indexes = [order.index(x) for x in names]
        if indexes != list(range(indexes[0], indexes[0] + len(names))) or grouped.intersection(names):
            raise ValueError('Composite tracks must be contiguous in layer order, ordered, and not shared')
        if any(t['track'] in names for t in plan['jianying_tasks']):
            raise ValueError('Pending native tasks must be completed before grouping their tracks')
        grouped.update(names)
        composite_names.add(spec['name'])
    warnings = []
    if any(x['fit'] == 'cover' for x in plan['clips']):
        warnings.append('Cover fit may crop the subject; visually check framing.')
    if transitions:
        warnings.append('Transitions require visual/resource validation inside Jianying.')
    if native_features:
        warnings.append('Native effects/animations remain editable draft parameters; resource download, entitlement and rendering are unverified.')
    if 'masks' in native_features:
        warnings.append('Native rectangle and zero-feather circle geometry measured on 11.5; current project rendering and other shapes require separate verification. No prerender fallback.')
    if any(native_mask_feather.requested(item) for item in plan['clips']):
        warnings.append('red_coverage_v1 is integrated for the measured 11.6 rectangle subset with draft-owned authenticated resources; UI save/reopen and current-project playback/export remain unverified.')
    if plan['composites']:
        warnings.append('Native composites retain editable internal tracks; local groups rebase target times. Current draft rendering is unverified.')
    if plan['jianying_tasks']:
        warnings.append('Smart cutout/tracking tasks are pending inside Jianying; they were NOT executed or prerendered.')
    if any(item.get('animations') for item in plan['clips'] + plan['captions']):
        user_data = user_data if user_data is not None else configured_user_data()
        if user_data is None:
            warnings.append('Animation availability was not checked: no configured local effect cache.')
        elif effect_cache_dir(user_data) is None:
            warnings.append('Animation packages are not cached on this machine yet — this user data has no Cache/effect '
                            'directory, so build drops every planned animation until the app downloads one. Opening a '
                            'dropped animation once inside Jianying, then rebuilding, ships it.')
        else:
            available, unresolvable, status = animation_preflight(plan, load_backend(), user_data)
            if unresolvable:
                locally = '; '.join(f"{kind}: {'、'.join(names) or 'none'}" for kind, names in available.items())
                warnings.append('Build drops animations whose package this machine does not cache yet — '
                                f"{', '.join(unresolvable)}. Locally resolvable names now: {locally}")
    return plan, probes, warnings


def load_backend():
    try:
        version = importlib.metadata.version('pyJianYingDraft')
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError(f'Install the backend in an external virtual environment using its Python -m pip install -r "{SKILL_ROOT / "scripts/requirements.txt"}" -c "{SKILL_ROOT / "scripts/constraints.txt"}"; current interpreter: {sys.executable}') from exc
    if version != '0.3.0':
        raise ValueError(f'Unsupported backend version {version}; expected 0.3.0')
    import pyJianYingDraft
    return pyJianYingDraft


def srt_time(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'


def provenance(plan):
    """Include selected assets only; private/local originals remain unclassified."""
    records = []
    for material in dict.fromkeys(x['path'] for group in ('clips', 'audio') for x in plan[group]):
        path = Path(material)
        sidecar = path.with_name(path.name + '.provenance.json')
        if sidecar.is_file():
            item = read_json(sidecar)
            if (item.get('license_verification') != 'agent_review_recorded' or not item.get('review_note')
                    or item.get('sha256') != file_hash.sha256(path)):
                raise ValueError(f'Material provenance missing review or checksum mismatch: {path.name}')
            records.append(item)
        else:
            records.append({'local_path': str(path), 'license_verification': 'not_recorded',
                            'attribution': '', 'rights_clearance': 'not_verified'})
    return records


def source_delivery(records):
    lines = ['# 素材来源与授权记录', '',
             '来源记录与核查备注不构成完整权利担保；画面中人物、商标及最终用途仍需核对。', '']
    for item in records:
        lines += [f'## {Path(item["local_path"]).name}', '',
                  f'- 核查状态：{item["license_verification"]}',
                  f'- 来源/署名：{item.get("attribution") or "未登记；不推断为免费素材"}',
                  f'- 核查备注：{item.get("review_note", "未记录")}',
                  f'- SHA256：{item.get("sha256", "未记录")}', '']
        if item.get('license') == 'by-sa':
            lines += ['- CC BY-SA：使用者已记录相同方式分享安排，需按核查备注执行。', '']
    return '\n'.join(lines) + '\n'


def delivery(plan, path, warnings):
    lines = [f'# {plan["name"]} 剪辑交付', '',
             ('已生成剪映原生草稿；仍有下列素材或剪辑操作缺口。' if plan['missing_assets'] or plan.get('jianying_tasks') else
              '剪辑已完成：已生成剪映原生草稿，并通过计划校验、草稿结构与原生参数回读。'),
             '导出和最终检查由用户完成，不属于 Skill 的未完成项。', '',
             f'- 草稿目录：`{path}`', '- 计划：`edit-plan.json`', '- 字幕：`字幕.srt`',
             '- 验证：`verification.json`（结构检查，不代表应用兼容）',
             '- 素材来源与署名：`素材来源.md`、`素材来源.json`（未登记的素材不会被标为免费）',
             '- 原生效果与待办：`原生效果.md`（效果参数保留在草稿，未烘焙素材）', '',
             '## 时间轴', '', '| 时间范围（秒） | 轨道 | 素材 | 源入点 | 变速 | 选择依据 |',
             '|---|---|---|---|---|---|']
    for c in sorted(plan['clips'], key=lambda x: (x['start'], x['track'])):
        reason = c.get('reason', '').replace('|', '\\|').replace('\n', ' ')
        material = Path(c['path']).name.replace('|', '\\|')
        lines.append(f'| {c["start"]:.3f}–{c["start"]+c["duration"]:.3f} | {c["track"]} | {material} | {c["source_in"]:.3f} | {c["speed"]} | {reason} |')
    lines += ['', '## 取舍与素材缺口', ''] + [f'- {x}' for x in plan['notes']]
    lines += [f'- 缺少：{x}' for x in plan['missing_assets']] if plan['missing_assets'] else ['- 未记录素材缺口。']
    if plan.get('composites'):
        lines += ['', '## 原生复合片段', ''] + [f'- {s["name"]}：' +
                                                (f'{s["start"]:.3f}–{s["start"] + s["duration"]:.3f} 秒' if 'start' in s else '完整时间轴') +
                                                f'，内部轨道 {", ".join(s["tracks"])}。'
                                                for s in plan['composites']]
    if plan.get('jianying_tasks'):
        lines += ['', '## 剪映内仍待完成', ''] + [
            f'- {t["operation"]}：轨道 {t["track"]}，{t["start"]:.3f}–{t["start"]+t["duration"]:.3f} 秒。{t["instructions"]}'
            for t in plan['jianying_tasks']]
    for clip in plan['clips']:
        if native_hsl_channels.requested(clip.get('grading')):
            name=next(iter(clip['grading']));spec=clip['grading'][name]
            titles={'saturation':'饱和度','lightness':'明度','hue':'色相'}
            detail='；'.join(f'{native_hsl_channels.CHANNELS[name][1]}{titles[k]}：{spec[k]}' for k in ('hue','saturation','lightness') if k in spec)
            lines += ['', '## 其他HSL子集', '', f'- {detail}；仅静态PNG受限子集。',
                      '- 邻近颜色也可能改变，不等于语义主体颜色隔离。']
        if 'green_hsl' in clip.get('grading', {}):
            green = clip['grading']['green_hsl']
            control = '明度' if 'lightness' in green else '色相' if 'hue' in green else '饱和度'
            lines += ['', '## 绿色 HSL 子集', '',
                      f'- 绿色{control}：{next(iter(green.values()))}；参数见原生效果.md。',
                      '- 绿色通道也会影响邻近黄色和青色，实际构图与颜色由当前工程单独检查。']
        if 'blue_hsl' in clip.get('grading', {}):
            blue = clip['grading']['blue_hsl']
            control = '明度' if 'lightness' in blue else '色相' if 'hue' in blue else '饱和度'
            value = next(iter(blue.values()))
            lines += ['', '## 蓝色 HSL 子集', '',
                      f'- 蓝色{control}：{value}；静图或已准入静音视频子集，参数见原生效果.md。',
                      '- 蓝色通道也会影响邻近青色和部分紫色，实际构图与颜色由当前工程单独检查。']
    local_fonts = [item['font_path'] for item in plan['captions'] if 'font_path' in item]
    if local_fonts:
        lines += ['', '## 本地字体依赖', '',
                  f'- 已复制 {len(set(local_fonts))} 份静态 TTF/OTF 到草稿 `Resources/local-fonts/`。',
                  '- 字体文件是用户提供的本地依赖；字体版权、嵌入与商业使用许可由使用者核对。',
                  '- 当前只准入静态横排文字，未验证动画、特效、重点词、透明度和竖排组合。']
    lines += ['', '## Skill 检查', '', '- 素材探测、计划校验、草稿结构及原生参数回读：通过。',
              '', '## 用户负责的最终步骤', '',
              '- 在剪映打开工程，检查构图、资源显示、参数可调、声音接缝与节奏，保存重开并导出。',
              '- 当前未执行这些用户步骤；结构检查不代表已验证实际渲染或完成成片导出。']
    if warnings:
        lines += ['', '## 用户检查重点', ''] + [f'- {x}' for x in warnings]
    return '\n'.join(lines) + '\n'


def native_delivery(plan):
    lines = ['# 剪映原生效果与待办', '',
             '效果由剪映渲染，Skill 检查草稿参数和引用；用户负责最终确认参数可调与视觉呈现，并导出。',
             '原始素材保持引用；没有外部预渲染、没有用生成的视频替换效果。', '']
    for group in ('clips', 'captions', 'audio', 'effects', 'filters'):
        for item in plan[group]:
            settings = ({key: item[key] for key in ('animations', 'effects', 'filters', 'mix_mode', 'chroma', 'mask', 'grading', 'matting_cache', 'background', 'style', 'transform', 'keyframes', 'motion_easing', 'speed_curve', 'text_curve', 'font', 'font_path', 'glow', 'highlights', 'volume', 'fade_in', 'fade_out', 'denoise', 'loudness')
                         if key in item} if group in ('clips', 'captions', 'audio') else
                        {key: item[key] for key in ('name', 'kind', 'params', 'intensity', 'keyframes') if key in item})
            if settings:
                lines += [f'## {item["track"]} · {item["start"]:.3f}–{item["start"]+item["duration"]:.3f} 秒',
                          '', '```json', json.dumps(settings, ensure_ascii=False, indent=2), '```', '']
    if plan.get('composites'):
        lines += ['## 后台原生复合配置', '', '```json', json.dumps({'composites': plan['composites']}, ensure_ascii=False, indent=2), '```', '']
    lines += ['## 剪映内待办（尚未执行）', '']
    lines += [f'- {t["operation"]} · {t["track"]} · {t["start"]} 秒：{t["instructions"]}'
              for t in plan['jianying_tasks']] or ['- 未记录智能抠像/自动跟踪待办。']
    return '\n'.join(lines) + '\n'


def verify_native(content, plan, draft_root=None, local_assets=None):
    """Check actual material references, parameter values and animation timing after serialization."""
    backend = load_backend()
    user_data = configured_user_data()
    animation_cache = (Path(user_data) / 'Cache' / 'effect') if user_data else None
    local_assets = local_font.inspect_plan(plan) if local_assets is None else local_assets
    if local_assets and draft_root is None:
        raise ValueError('Local font verification requires the draft root')
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    for track in content['tracks']:
        for segment in track['segments']:
            if segment['material_id'] not in materials or any(r not in materials for r in segment.get('extra_material_refs', [])):
                raise ValueError('Draft readback contains an unresolved material reference')
    segments = {(track['name'], s['target_timerange']['start'], s['target_timerange']['duration']): s
                for track in content['tracks'] for s in track['segments']}
    counts = {}
    glow_count=native_text_glow.verify(content,plan,None)
    if glow_count:counts['text_glows']=glow_count
    def equal(actual, expected, label):
        if isinstance(expected, float):
            valid = isinstance(actual, (int, float)) and math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-8)
        else:
            valid = actual == expected
        if not valid:
            raise ValueError(f'Native draft readback mismatch: {label}')
    def check_effect(actual, item):
        if item.get('keyframes'):
            expected = native_advanced.blur_material(item, None)
            equal(actual.get('effect_id'), expected['effect_id'], 'native 11.5 blur effect id')
            equal(actual.get('resource_id'), expected['resource_id'], 'native 11.5 blur resource id')
            return  # The extension validator checks every resource/parameter field.
        member, params = effect_params(backend, item)
        equal(actual.get('effect_id'), member.value.effect_id, 'effect id')
        definitions = member.value.params
        equal(len(actual.get('adjust_params', [])), len(definitions), 'effect parameter count')
        for p, supplied, actual_param in zip(definitions, params, actual.get('adjust_params', [])):
            expected = p.default_value if supplied is None else p.min_value + (p.max_value - p.min_value) * supplied / 100
            equal(actual_param.get('name'), p.name, 'effect parameter name')
            equal(actual_param.get('value'), float(expected), 'effect parameter value')
    for group in ('clips', 'captions', 'effects', 'filters'):
        for item in plan[group]:
            segment = segments[(item['track'], us(item['start']), us(item['duration']))]
            refs = [materials[r] for r in segment.get('extra_material_refs', []) if r in materials]
            if group in ('effects', 'filters'):
                actual = materials.get(segment['material_id'], {})
                if group == 'effects':
                    check_effect(actual, item)
                    equal(actual.get('apply_target_type'), 2, 'standalone effect scope')
                else:
                    equal(actual.get('effect_id'), native_enum(backend, 'filters', item['name'], True).value.effect_id, 'track filter id')
                    equal(actual.get('value'), item['intensity'] / 100.0, 'track filter intensity')
                counts['track_' + group] = counts.get('track_' + group, 0) + 1
                continue
            requested = [a for a in item['animations']
                         if native_advanced.animation_bundle(animation_cache,
                                                             planned_animation_id(backend, group, a)) is not None]
            actual_animations = [a for r in refs for a in r.get('animations', [])]
            equal(len(actual_animations), len(requested), 'animation count')
            durations = {a['stage']: us(a['duration']) for a in requested if 'duration' in a}
            for a in requested:
                stage = a['stage']
                kind = ('video_' if group == 'clips' else 'text_') + stage
                member = native_enum(backend, kind, a['name'], True)
                actual = next((v for v in actual_animations if v['type'] == {'intro': 'in', 'outro': 'out', 'group': 'group', 'loop': 'loop'}[stage]), {})
                start = us(item['duration']) - durations[stage] if stage == 'outro' else durations.get('intro', 0) if stage == 'loop' else 0
                duration = (us(item['duration']) - durations.get('intro', 0) - durations.get('outro', 0)
                            if stage == 'loop' else durations[stage])
                for key, value in (('id', member.value.effect_id), ('start', start), ('duration', duration)):
                    equal(actual.get(key), value, 'animation ' + key)
            if requested:
                counts[group + '_animations'] = counts.get(group + '_animations', 0) + len(requested)
            if group == 'captions':
                material = materials[segment['material_id']]
                default_font = default_text_font()
                local_asset = local_assets.get(item.get('font_path'))
                expected_path = (local_font.path_for(draft_root, local_asset).as_posix()
                                 if local_asset else
                                 default_font['path'] if ('font' not in item and default_font) else None)
                equal(material.get('font_path'), expected_path, 'default or local text font path')
                if any(material.get(k) for k in ('font_id',)):
                    raise ValueError('Unrequested top-level font metadata')
                rich = json.loads(material['content'])
                actual_fonts = [s.get('font') for s in rich['styles'] if 'font' in s]
                expected_font = (local_font.expected_font(local_asset, draft_root) if local_asset else
                                 native_font(item['font']) if 'font' in item else None)
                equal(actual_fonts, [expected_font] if expected_font else [], 'text font identity/path')
                if expected_font:
                    counts['local_fonts' if local_asset else 'text_fonts'] = (
                        counts.get('local_fonts' if local_asset else 'text_fonts', 0) + 1)
                if 'text_curve' in item:
                    material = materials[segment['material_id']]
                    equal(material.get('text_curve'), {'enable': True, 'angle': item['text_curve']['angle']}, 'text curve')
                    expected = text_segment(item, backend.Timerange(us(item['start']), us(item['duration'])), backend)
                    equal(material.get('check_flag'), expected.export_material()['check_flag'] | 128, 'text curve style flags')
                    counts['text_curves'] = counts.get('text_curves', 0) + 1
                expected = text_segment(item, backend.Timerange(us(item['start']), us(item['duration'])), backend)
                expected_material = bind_text_font(expected.export_material(), item, local_assets=local_assets,
                                                   draft_root=draft_root)
                equal(material.get('content'), expected_material.get('content'), 'text content/styles')
                if 'highlights' in item:
                    counts['caption_highlights'] = counts.get('caption_highlights', 0) + len(item['highlights'])
                if (item.get('style') or item.get('transform') or item.get('keyframes') or 'font' in item
                        or 'font_path' in item or 'highlights' in item):
                    expected = text_segment(item, backend.Timerange(us(item['start']), us(item['duration'])), backend)
                    material = materials[segment['material_id']]
                    expected_material = bind_text_font(expected.export_material(), item, local_assets=local_assets,
                                                       draft_root=draft_root)
                    for key in ('content', 'global_alpha', 'typesetting', 'letter_spacing', 'line_spacing'):
                        equal(material.get(key), expected_material.get(key), 'text ' + key)
                    exported = expected.export_json()
                    native_text_alpha.adapt(exported, item)
                    native_advanced.apply_motion_curve(exported, item)
                    native_advanced.verify_motion_curves(segment, item)
                    for key in ('clip', 'common_keyframes', 'uniform_scale'):
                        actual_value, expected_value = segment.get(key), exported.get(key)
                        if key == 'common_keyframes':
                            def without_ids(value):
                                if isinstance(value, dict):
                                    return {k: without_ids(v) for k, v in value.items() if k not in ('id', 'keyframe_list_id')}
                                return [without_ids(v) for v in value] if isinstance(value, list) else value
                            actual_value, expected_value = without_ids(actual_value), without_ids(expected_value)
                        equal(actual_value, expected_value, 'text ' + key)
                    counts['text_design'] = counts.get('text_design', 0) + 1
                continue
            for item_effect in item['effects']:
                effect_id = native_enum(backend, item_effect['kind'] + '_effects', item_effect['name'], True).value.effect_id
                actual = next((r for r in refs if r.get('effect_id') == effect_id and r.get('type') in ('video_effect', 'face_effect')), {})
                check_effect(actual, item_effect)
                counts['clip_effects'] = counts.get('clip_effects', 0) + 1
            for filter_ in item['filters']:
                effect_id = native_enum(backend, 'filters', filter_['name'], True).value.effect_id
                actual = next((r for r in refs if r.get('effect_id') == effect_id and r.get('type') == 'filter'), {})
                equal(actual.get('value'), filter_['intensity'] / 100.0, 'clip filter intensity')
                counts['clip_filters'] = counts.get('clip_filters', 0) + 1
            for key, resource_type in (('mix_mode', 'mix_mode'), ('chroma', 'chroma'), ('mask', 'mask'), ('background', 'canvas_' + item.get('background', {}).get('type', ''))):
                if key not in item:
                    continue
                if key == 'background':
                    resource_type = 'canvas_blur' if item[key]['type'] == 'blur' else 'canvas_color'
                actual = next((r for r in refs if r.get('type') == resource_type), {})
                if not actual:
                    raise ValueError('Native material reference missing: ' + key)
                counts[key] = counts.get(key, 0) + 1
                if key == 'mix_mode':
                    equal(actual.get('effect_id'), native_enum(backend, 'mix_modes', item[key]['name'], True).value.effect_id, 'mix mode id')
                elif key == 'chroma':
                    equal(actual.get('color'), item[key]['color'].lower(), 'chroma color')
                    for name in ('intensity', 'shadow', 'edge_smooth', 'spill'):
                        equal(actual.get(name + '_value'), item[key][name] / 100.0, 'chroma ' + name)
                elif key == 'background':
                    name = 'blur' if item[key]['type'] == 'blur' else 'color'
                    equal(actual.get(name), item[key][name].lower() if name == 'color' else item[key][name], 'background ' + name)
                else:
                    m = item[key]
                    metadata = native_enum(backend, 'masks', m['name'], True).value
                    resource = (native_advanced.templates()['mask']['resource_id'] if m['name'] == '矩形' else
                                native_advanced.cached_material('common_mask', None)['resource_id'] if m['name'] == '圆形' else metadata.resource_id)
                    equal(actual.get('resource_id'), resource, 'mask resource')
                    source = materials[segment['material_id']]
                    width, height = source['width'], source['height']
                    mask_width = m.get('rect_width', m['size']) if m['name'] == '矩形' else m['size'] * height * metadata.default_aspect_ratio / width
                    for k, value in (('height', m['size']), ('rotation', m['rotation']), ('invert', m['invert']),
                                     ('feather', m['feather'] / 100), ('roundCorner', m.get('round_corner', 0) / 100),
                                     ('centerX', m['center_x'] / (width / 2)), ('centerY', m['center_y'] / (height / 2)),
                                     ('width', mask_width), ('aspectRatio', metadata.default_aspect_ratio)):
                        equal(actual.get('config', {}).get(k), value, 'mask ' + k)
    return {'mode': 'jianying_native_parameters', 'parameter_readback': 'passed',
            'counts': counts, 'prerendered_effects': False, 'resources_and_rendering': 'unverified',
            'experimental_masks': counts.get('mask', 0), 'pending_jianying_tasks': len(plan.get('jianying_tasks', []))}


def verify_structure(content, plan, physical_root=None, final_root=None):
    if 'layer_order' in plan or plan.get('composites'):
        if content.get('free_render_index_mode_on') is not True or content.get('render_index_track_mode_on') is not False:
            raise ValueError('Explicit layer order requires free render index mode')
    outer_tracks = len(content['tracks'])
    perspective_count = native_advanced.verify_perspectives(content, plan)
    if plan.get('composites'):
        content = native_advanced.expanded_content(content, plan)
    expected = [(kind, item) for group, kind in (('clips', 'video'), ('audio', 'audio'), ('captions', 'text'),
                                              ('effects', 'effect'), ('filters', 'filter')) for item in plan.get(group, [])]
    actual = [(track['type'], segment) for track in content['tracks'] for segment in track['segments']]
    if len(actual) != len(expected):
        raise ValueError('Draft readback segment count mismatch')
    def ranges(entries):
        return sorted((kind, us(x['start']), us(x['duration'])) for kind, x in entries)
    actual_ranges = sorted((kind, segment['target_timerange']['start'], segment['target_timerange']['duration'])
                           for kind, segment in actual)
    if actual_ranges != ranges(expected):
        raise ValueError('Draft readback timeline range mismatch')
    expected_duration = max(us(x['start']) + us(x['duration']) for x in plan['clips'] if x['track'] == 'video')
    if content['duration'] != expected_duration or content['canvas_config']['width'] != plan['canvas']['width'] or content['canvas_config']['height'] != plan['canvas']['height']:
        raise ValueError('Draft readback canvas/duration mismatch')
    if 'layer_order' in plan:
        if [track['name'] for track in content['tracks']] != plan['layer_order']:
            raise ValueError('Draft readback layer order mismatch')
        for index, track in enumerate(content['tracks']):
            if any(s.get('render_index') != index for s in track['segments']):
                raise ValueError('Draft readback render order mismatch')
    local_assets = local_font.inspect_plan(plan)
    path_root = final_root or physical_root
    file_root = physical_root or path_root
    native = verify_native(content, plan, path_root, local_assets)
    if local_assets:
        native['local_fonts'] = local_font.verify_bindings(
            content, plan, path_root, local_assets, file_root=file_root)
    native['existing_matting_caches']=native_matting_cache.verify(content,plan)
    native['new_matting_computation']=False
    native['installed_chroma2'] = native_chroma.verify(content, plan)
    native['installed_chroma2_integration'] = 'limited' if native['installed_chroma2'] else 'not_requested'
    native['installed_mix'] = native_mix_mode.verify(content, plan)
    native['installed_mix_integration'] = 'limited' if native['installed_mix'] else 'not_requested'
    native['text_global_alpha'] = native_text_alpha.verify(content, plan)
    native['text_global_alpha_integration'] = 'limited' if native['text_global_alpha'] else 'not_requested'
    native['dynamic_rectangle_masks'] = native_advanced.verify_masks(content, plan,
        None, physical_root, final_root)
    native['rectangle_red_coverage'] = sum(native_mask_feather.requested(item) for item in plan['clips'])
    native['rectangle_red_coverage_integration'] = 'limited' if native['rectangle_red_coverage'] else 'not_requested'
    native['video_composites'] = len(plan.get('composites', []))
    native['corner_pin_perspectives'] = perspective_count
    native['extensions'] = native_advanced.verify_extensions(content, plan, None, measure_loudness)
    native['animation_paths'] = native_advanced.verify_animation_paths(content)
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    by_track = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    for item in plan['clips'] + plan['audio']:
        segment = by_track[item['track'], us(item['start'])]
        source = materials.get(segment['material_id'], {})
        if source.get('path') != item['path']:
            raise ValueError('Draft readback source path mismatch')
        native_advanced.verify_speed(segment, item, content)
        if item in plan['clips'] and item.get('keyframes'):
            native_advanced.verify_motion_curves(segment, item)
            ratios = (plan['canvas']['width'] / source['width'], plan['canvas']['height'] / source['height'])
            base = max(ratios) / min(ratios) if item['fit'] == 'cover' else 1
            expected_motion = {}
            backend = load_backend()
            for key in item['keyframes']:
                prop = 'scale_x' if key['property'] == 'uniform_scale' else key['property']
                native_property = backend.KeyframeProperty[prop].value
                value = key['value'] * base if 'scale' in key['property'] else key['value']
                expected_motion.setdefault(native_property, []).append((us(item['source_in'] + key['time'] * item['speed']), [value]))
            actual_motion = {g['property_type']: [(p['time_offset'], p['values']) for p in g['keyframe_list']]
                             for g in segment['common_keyframes'] if g['property_type'] in expected_motion}
            if actual_motion != {prop: sorted(points) for prop, points in expected_motion.items()}:
                raise ValueError('Video source-time motion keyframe readback mismatch')
        if Path(item['path']).suffix.lower() not in IMAGE_EXT and segment['source_timerange'] != {
                'start': us(item['source_in']), 'duration': us(item['duration'] * item['speed'])}:
            raise ValueError('Draft readback source range mismatch')
    return {'status': 'structure_checked', 'duration_seconds': expected_duration / 1_000_000,
            'editing_status': 'partial' if plan['missing_assets'] or plan.get('jianying_tasks') else 'complete',
            'final_check_owner': 'user', 'export_owner': 'user',
            'segments': len(actual), 'tracks': len(content['tracks']), 'outer_tracks': outer_tracks, 'media_probe': 'passed',
            'plan_validation': 'passed', 'draft_structure': 'passed',
            'jianying_open': 'unverified', 'save_reopen': 'unverified',
            'visual_audio_review': 'unverified', 'native_export': 'unverified', 'native_effects': native}


def build_draft(args):
    plan, probes, warnings = validate_plan(args.plan, configured_user_data())
    sources = provenance(plan)
    backend = load_backend()
    animation_drops = drop_unavailable_animations(plan, backend, configured_user_data())
    destination = Path(args.out).resolve()
    if destination.exists():
        raise FileExistsError(f'Output already exists; choose a new draft version: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jianying-build-', dir=destination.parent, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        script = backend.DraftFolder(str(root)).create_draft(plan['name'], **plan['canvas'], allow_replace=False)
        project = root / plan['name']
        local_assets = local_font.inspect_plan(plan)
        local_font.copy_assets(local_assets, project)
        track_kinds = {}
        for group, kind in (('audio', backend.TrackType.audio), ('clips', backend.TrackType.video),
                            ('captions', backend.TrackType.text), ('effects', backend.TrackType.effect),
                            ('filters', backend.TrackType.filter)):
            names = list(dict.fromkeys(x['track'] for x in plan[group]))
            if group == 'clips':
                names = ['video'] + [name for name in names if name != 'video']
            track_kinds.update((name, kind) for name in names)
        script.append_tracks([backend.TrackSpec(track_kinds[name], name)
                              for name in plan.get('layer_order', list(track_kinds))])
        for group, kind in (('audio', backend.TrackType.audio), ('clips', backend.TrackType.video), ('captions', backend.TrackType.text)):
            for item in sorted(plan[group], key=lambda x: x['start']):
                target = backend.Timerange(us(item['start']), us(item['duration']))
                if group == 'captions':
                    segment = text_segment(item, target, backend)
                    apply_animations(segment, item, backend, text=True)
                else:
                    source = backend.Timerange(us(item['source_in']), us(item['duration'] * item['speed']))
                    if group == 'audio':
                        # Passing speed with source_timerange makes 0.3.0 recompute the target as
                        # round(source/speed), which can drift 1us; derive speed from both ranges like video.
                        segment = backend.AudioSegment(item['path'], target, source_timerange=source, volume=item['volume'])
                        for key in item.get('keyframes', []):
                            segment.add_keyframe(us(item['source_in'] + key['time'] * item['speed']), key['value'])
                        if item['fade_in'] or item['fade_out']:
                            segment.add_fade(us(item['fade_in']), us(item['fade_out']))
                    else:
                        info = probes[item['path']]
                        ratios = (plan['canvas']['width'] / info['width'], plan['canvas']['height'] / info['height'])
                        base = max(ratios) / min(ratios) if item['fit'] == 'cover' else 1
                        transform = dict(item['transform'])
                        for axis in ('scale_x', 'scale_y'):
                            transform[axis] = transform.get(axis, 1) * base
                        segment = backend.VideoSegment(item['path'], target,
                            source_timerange=None if info['kind'] == 'image' else source,
                            volume=item['volume'], clip_settings=backend.ClipSettings(**transform))
                        for key in sorted(item['keyframes'], key=lambda x: x['time']):
                            value = key['value'] * base if 'scale' in key['property'] else key['value']
                            key_time = item['source_in'] + key['time'] * item['speed']
                            segment.add_keyframe(getattr(backend.KeyframeProperty, key['property']), us(key_time), value)
                        if 'transition' in item:
                            tr = item['transition']
                            segment.add_transition(backend.TransitionType[tr['name']], duration=us(tr['duration']))
                        apply_native(segment, item, backend)
                script.add_segment(segment, item['track'])
        for group, kind in (('effects', backend.TrackType.effect), ('filters', backend.TrackType.filter)):
            for item in sorted(plan[group], key=lambda x: x['start']):
                target = backend.Timerange(us(item['start']), us(item['duration']))
                if group == 'effects':
                    member, params = effect_params(backend, item)
                    script.add_effect(member, target, item['track'], params=params)
                else:
                    script.add_filter(native_enum(backend, 'filters', item['name'], True), target,
                                      item['track'], intensity=item['intensity'])
        draft_id = str(uuid.uuid4()).upper()
        script.content['id'] = draft_id
        script.save()
        content = read_json(project / 'draft_content.json')
        text_materials = {m['id']: m for m in content['materials']['texts']}
        text_segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
        default_font = default_text_font()
        if any('font' not in item and 'font_path' not in item for item in plan['captions']) and default_font is None:
            warnings.append('No installed Jianying default font found; text may show placeholder glyphs '
                            'until Jianying repairs the draft itself.')
        for item in plan['captions']:
            segment = text_segments[item['track'], us(item['start'])]
            bind_text_font(text_materials[segment['material_id']], item, default_font,
                           local_assets=local_assets, draft_root=destination)
            native_text_alpha.adapt(segment, item)
        native_text_glow.apply(content,plan,None)
        native_matting_cache.apply(content,plan)
        native_chroma.bind(content,plan)
        native_mix_mode.bind(content,plan)
        native_advanced.apply_masks(content, plan,
            None)
        native_mask_feather.prepare(content, plan, project, destination)
        native_advanced.apply_extensions(content, plan, None, measure_loudness)
        native_advanced.apply_composites(content, plan)
        has_animations = any(item.get('animations') for item in plan['clips'] + plan['captions'])
        animation_paths = native_advanced.bind_animation_paths(content, configured_user_data() if has_animations else None)
        dropped_animations = animation_drops or animation_paths['dropped']
        if dropped_animations:
            warnings.append('Animation bundle is absent from the local effect cache for ' +
                            ', '.join(f"{d['name']}({d['id']})" for d in dropped_animations) +
                            '; it was dropped from the draft. Jianying draws no animation whose resource is '
                            'missing: it marks the segment as lost and keeps looking for it while the draft '
                            'opens, which is slow and needs network. Measured on 2026-10-16 with 11.6.')
        if animation_paths['unverified']:
            warnings.append('Animation bundle could not be resolved for ' +
                            ', '.join(sorted(set(animation_paths['unverified']))) +
                            ': no local Jianying user data, so the app must download the resource on first open.')
        native_advanced.write_sidecars(content, project, destination, write_json)
        write_json(project / 'draft_content.json', content)
        status = verify_structure(content, plan, project, destination)
        native_advanced.verify_sidecars(content, project, destination, read_json)
        meta = read_json(project / 'draft_meta_info.json')
        meta.update(draft_id=draft_id, draft_name=plan['name'], draft_fold_path=str(destination),
                    draft_root_path=str(destination.parent), tm_duration=content['duration'],
                    tm_draft_create=int(time.time() * 1_000_000), tm_draft_modified=int(time.time() * 1_000_000))
        write_json(project / 'draft_meta_info.json', meta)
        write_json(project / 'edit-plan.json', plan)
        write_json(project / 'verification.json', status)
        write_json(project / '素材来源.json', {'schema_version': 1, 'materials': sources})
        (project / '素材来源.md').write_text(source_delivery(sources), encoding='utf-8')
        (project / '原生效果.md').write_text(native_delivery(plan), encoding='utf-8')
        captions = sorted((x for x in plan['captions'] if x['track'] == 'captions'), key=lambda x: x['start'])
        srt = '\n\n'.join(f'{i+1}\n{srt_time(c["start"])} --> {srt_time(c["start"]+c["duration"])}\n'
                           + re.sub(r'\n\s*\n', '\n', c['text'].replace('\r\n', '\n').replace('\r', '\n')).strip('\n')
                           for i, c in enumerate(captions))
        (project / '字幕.srt').write_text(srt + ('\n' if srt else ''), encoding='utf-8')
        (project / '剪辑交付.md').write_text(delivery(plan, destination, warnings), encoding='utf-8')
        write_json(project / '_skill_manifest.json', {
            'generator': 'jianying-editor', 'version': 2, 'backend': 'pyJianYingDraft==0.3.0',
            'draft_content_sha256': hashlib.sha256((project / 'draft_content.json').read_bytes()).hexdigest(),
            'local_fonts': local_font.manifest(local_assets)})
        project.rename(destination)
    return {'draft': str(destination), 'delivery': str(destination / '剪辑交付.md'),
            'verification': status, 'warnings': warnings, 'missing_assets': plan['missing_assets'],
            'animation_paths': animation_paths, 'local_fonts': local_font.manifest(local_assets)}


def publish_draft(args):
    source = Path(args.draft).resolve()
    require_plaintext_draft(source / 'draft_content.json', 'to publish this draft')
    settings = draft_location()
    requested_root = getattr(args, 'draft_root', None) or settings['active_draft_root']
    if not requested_root:
        raise ValueError('No configured draft root found; specify a confirmed --draft-root')
    root = Path(requested_root).resolve()
    offline = getattr(args, 'offline_copy', False)
    if not offline and settings['active_draft_root'] and root != Path(settings['active_draft_root']).resolve():
        raise ValueError(f"Destination differs from Jianying configured draft root: {settings['active_draft_root']}. Use that directory for live publication, or --offline-copy for an archive.")
    wait_seconds = number(getattr(args, 'wait_seconds', 5), 'wait-seconds', 0, 30)
    manifest = read_json(source / '_skill_manifest.json')
    if manifest.get('generator') != 'jianying-editor' or hashlib.sha256((source / 'draft_content.json').read_bytes()).hexdigest() != manifest['draft_content_sha256']:
        raise ValueError('Only an unchanged draft generated by this Skill can be published')
    plan, _, warnings = validate_plan(source / 'edit-plan.json', configured_user_data(), mask_resource_root=source)
    provenance(plan)
    content = read_json(source / 'draft_content.json')
    verify_structure(content, plan, source, source)
    local_assets = local_font.inspect_plan(plan)
    native_advanced.verify_sidecars(content, source, source, read_json)
    if not root.is_dir():
        raise ValueError('Confirmed draft root must already exist')
    if root == source or source in root.parents:
        raise ValueError('Draft root cannot be inside source draft')
    destination = root / plan['name']
    if destination.exists():
        raise FileExistsError('Draft name already exists; build a new named version')
    with tempfile.TemporaryDirectory(prefix='.jianying-copy-', dir=root, ignore_cleanup_errors=True) as temporary:
        staged = Path(temporary) / 'draft'
        shutil.copytree(source, staged)
        if local_assets:
            local_font.rebase(content, plan, destination, local_assets)
        if any(native_mask_feather.requested(item) for item in plan['clips']):
            native_mask_feather.relocate(content, plan, source, destination)
        verify_structure(content, plan, staged, destination)
        meta = read_json(staged / 'draft_meta_info.json')
        meta.update(draft_fold_path=str(destination), draft_root_path=str(root))
        write_json(staged / 'draft_meta_info.json', meta)
        native_advanced.write_sidecars(content, staged, destination, write_json)
        native_advanced.verify_sidecars(content, staged, destination, read_json)
        write_json(staged / 'draft_content.json', content)
        manifest['draft_content_sha256'] = hashlib.sha256((staged / 'draft_content.json').read_bytes()).hexdigest()
        write_json(staged / '_skill_manifest.json', manifest)
        (staged / '剪辑交付.md').write_text(delivery(plan, destination, warnings), encoding='utf-8')
        (staged / '原生效果.md').write_text(native_delivery(plan), encoding='utf-8')
        staged.rename(destination)
    registration = wait_for_registration(destination, settings['homepage_index'], 0 if offline else wait_seconds)
    return {'draft': str(destination), 'copied': True, 'global_index_modified': False,
            'configured_draft_root': settings['active_draft_root'], 'registration': registration,
            'homepage_visibility': 'pending_ui_verification' if registration['status'] == 'registered' else 'unverified',
            'jianying_open': 'unverified', 'restart_required': False if registration['status'] == 'registered' else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Read environment; no desktop interaction')
    p = commands.add_parser('capabilities', help='List exact native effect/animation names and optional parameter metadata')
    p.add_argument('--kind', choices=list(NATIVE_TYPES), default='keyframes')
    p.add_argument('--search', default='')
    p.add_argument('--limit', type=int, choices=range(1, 2001), default=50)
    p.add_argument('--details', action='store_true')
    p.add_argument('--free-only', action='store_true')
    p.add_argument('--user-data', type=Path, help='Jianying user data; enables the local animation package list')
    p = commands.add_parser('inspect', help='Probe local media and extract frames; no semantic analysis')
    p.add_argument('--input', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--frames', type=int, choices=range(1, 13), default=3)
    p.add_argument('--max-files', type=int, choices=range(1, 1001), default=80)
    p = commands.add_parser('reference', help='Extract scene candidates, frames and mono audio')
    p.add_argument('--input', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--start', type=float, default=0)
    p.add_argument('--duration', type=float, default=120)
    p.add_argument('--threshold', type=float, default=0.3)
    p.add_argument('--max-frames', type=int, choices=range(2, 97), default=32)
    p = commands.add_parser('validate', help='Validate media paths and timeline; write nothing')
    p.add_argument('--plan', required=True)
    p.add_argument('--user-data', type=Path, help='Jianying user data to check animation packages against; defaults to the configured one')
    p = commands.add_parser('build', help='Build a new editable draft; refuse existing output')
    p.add_argument('--plan', required=True)
    p.add_argument('--out', required=True)
    p = commands.add_parser('publish', help='Publish to the configured live draft directory without restarting Jianying')
    p.add_argument('--draft', required=True)
    p.add_argument('--draft-root', help='Confirmed destination; defaults to Jianying currentCustomDraftPath')
    p.add_argument('--offline-copy', action='store_true', help='Allow copying outside the configured live draft root')
    p.add_argument('--wait-seconds', type=float, default=5, help='Wait for native homepage registration, at most 30 seconds')
    p = commands.add_parser('refresh-registration', help='Re-create a draft folder inside the live draft root so the running Jianying watcher registers it')
    p.add_argument('--draft', required=True, help='Existing draft directory directly inside the configured draft root')
    p.add_argument('--wait-seconds', type=float, default=30, help='Wait for native homepage registration, at most 30 seconds')
    p = commands.add_parser('native-preview', help='Optional native still previews of a plaintext draft; no UI or algorithm jobs')
    p.add_argument('--install', type=Path, required=True)
    p.add_argument('--draft', type=Path, required=True, help='Plaintext draft_content.json')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--times', type=float, nargs='+', required=True)
    p.add_argument('--runtime', type=Path, help='Defaults to the bundled bun.exe')
    p = commands.add_parser('admit-matting', help='Register an externally reviewed native custom mask cache; no new computation or UI')
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--custom-id',required=True)
    p.add_argument('--review',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            result = doctor()
        elif args.command == 'capabilities':
            result = native_catalog(args)
        elif args.command == 'inspect':
            result = inspect_media(args)
        elif args.command == 'reference':
            result = analyze_reference(args)
        elif args.command == 'validate':
            plan, probes, warnings = validate_plan(args.plan, args.user_data if args.user_data else configured_user_data())
            result = {'valid': True, 'media_files': len(probes), 'warnings': warnings,
                      'missing_assets': plan['missing_assets']}
        elif args.command == 'build':
            result = build_draft(args)
        elif args.command == 'native-preview':
            require_plaintext_draft(args.draft, 'to render native stills from this draft')
            from native_preview import preview
            result = preview(args.install, args.draft, args.out, args.times, args.runtime)
        elif args.command == 'admit-matting':
            result = native_matting_cache.admit(args.source,args.cache,args.custom_id,args.review,args.out,probe(args.source))
        elif args.command == 'refresh-registration':
            result = refresh_registration(args.draft, number(args.wait_seconds, 'wait-seconds', 0, 30))
        else:
            result = publish_draft(args)
        emit(result)
    except (ValueError, OSError, KeyError, TypeError, ImportError, subprocess.TimeoutExpired) as exc:
        emit({'status': 'failed', 'error': str(exc)})
        return 1
    except Exception as exc:  # Keep the JSON contract for unexpected failures (StopIteration, IndexError, configparser...).
        emit({'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'})
        return 1
    return 2 if args.command == 'doctor' and not result['ready'] else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
