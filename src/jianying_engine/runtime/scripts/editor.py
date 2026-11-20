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
import statistics
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
import native_beat
import native_primary_wheel
import native_text_highlight
import native_hsl_channels
import native_text_alpha
import file_hash
import local_font
import runtime_admission
import runtime_common
import runtime_paths

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
MEDIA_EXT = IMAGE_EXT | {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.gif', '.m4v',
                         '.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}
# Transparent overlay videos whose alpha the native player composites exactly (11.6 pixel probe).
ALPHA_VIDEO_FORMATS = {('png', 'rgba'), ('qtrle', 'argb')}
ALPHA_PIX_FMT = re.compile(r'^(yuva|gbrap|rgba|bgra|argb|abgr|ya\d)')
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
    path = Path(item['path']).resolve()
    source_sha256 = file_hash.sha256(path)
    key = (str(path), source_sha256, us(item['source_in']), us(item['duration']),
           item['loudness']['target_lufs'])
    if key not in _LOUDNESS:
        measured = _measure_loudness(item)
        if file_hash.sha256(path) != source_sha256:
            raise ValueError('Audio source changed during loudness measurement')
        _LOUDNESS[key] = measured
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
    return runtime_common.check_number(value, label, low, high, style='editor')


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


def cached_native_resources(user_data=None):
    """Probe which admitted local Jianying effect/font/glow caches exist with matching hashes."""
    results = {}
    for kind in read_json(SKILL_ROOT / 'assets/native-extensions-11.5.json'):
        try:
            native_advanced.cached_material(kind, user_data)
            results[kind] = True
        except (ValueError, OSError, configparser.Error):
            results[kind] = False
    for name, spec in read_json(SKILL_ROOT / 'assets/native-fonts-11.5.json').items():
        key = 'font:' + name
        try:
            root = Path(user_data) if user_data is not None else resource_user_data(spec['cache_relative'])
            results[key] = bool(root and file_hash.matches(root / spec['cache_relative'], spec['sha256']))
        except (ValueError, OSError, configparser.Error):
            results[key] = False
    for name in read_json(SKILL_ROOT / 'assets/native-text-glow-11.5.json'):
        key = 'text_glow:' + name
        try:
            native_text_glow.material({'name': name, 'color': '#4080FF', 'strength': 0.2, 'range': 0.3},
                                      user_data, allow_vip=True)
            results[key] = True
        except (ValueError, OSError, configparser.Error):
            results[key] = False
    return results


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
    """Directory of the installed Jianying build: JIANYING_INSTALL when set (a background update can move the
    registry to a build that is not running yet), else the uninstall registry entry, then a recorded preview manifest."""
    pinned = os.environ.get('JIANYING_INSTALL')
    if pinned:
        path = Path(os.path.expandvars(pinned)).expanduser()
        return path if (path / 'Resources' / 'Font' / 'SystemFont').is_dir() else None
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
    instead of left for the app to repair on save. Cached per install (the install can be pinned per call).
    """
    install = jianying_install()
    key = str(install) if install else ''
    if key not in DEFAULT_FONT_CACHE:
        font = (install / 'Resources' / 'Font' / 'SystemFont' / 'zh-hans.ttf') if install else None
        DEFAULT_FONT_CACHE[key] = {'path': font.resolve().as_posix()} if font and font.is_file() else None
    return DEFAULT_FONT_CACHE[key]


def load_backend():
    try:
        version = importlib.metadata.version('pyJianYingDraft')
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError(f'Install the backend in an external virtual environment using its Python -m pip install -r "{SKILL_ROOT / "scripts/requirements.txt"}" -c "{SKILL_ROOT / "scripts/constraints.txt"}"; current interpreter: {sys.executable}') from exc
    if version != '0.3.0':
        raise ValueError(f'Unsupported backend version {version}; expected 0.3.0')
    import pyJianYingDraft
    return pyJianYingDraft


QT_ESCAPES = {'0': '\0', 'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v'}

import editor_env
import editor_validate
import editor_build
import native_protocol

probe = editor_env.probe
frame = editor_env.frame
new_directory = editor_env.new_directory
draft_location = editor_env.draft_location
wait_for_registration = editor_env.wait_for_registration
jianying_running = editor_env.jianying_running
qt_ini_value = editor_env.qt_ini_value
register_media_library = editor_env.register_media_library
draft_tree_digest = editor_env.draft_tree_digest
refresh_registration = editor_env.refresh_registration
doctor = editor_env.doctor
inspect_media = editor_env.inspect_media
analyze_reference = editor_env.analyze_reference

validate_grading = editor_validate.validate_grading
validate_visual = editor_validate.validate_visual
native_enum = editor_validate.native_enum
validate_native = editor_validate.validate_native
effect_params = editor_validate.effect_params
apply_animations = editor_validate.apply_animations
planned_animation_id = editor_validate.planned_animation_id
cached_animation_id = editor_validate.cached_animation_id
animation_availability = editor_validate.animation_availability
animation_cache_status = editor_validate.animation_cache_status
animation_preflight = editor_validate.animation_preflight
drop_unavailable_animations = editor_validate.drop_unavailable_animations
apply_native = editor_validate.apply_native
native_catalog = editor_validate.native_catalog
validate_plan = editor_validate.validate_plan

text_segment = editor_build.text_segment
bind_text_font = editor_build.bind_text_font
srt_time = editor_build.srt_time
provenance = editor_build.provenance
source_delivery = editor_build.source_delivery
delivery = editor_build.delivery
native_delivery = editor_build.native_delivery
verify_native = editor_build.verify_native
verify_structure = editor_build.verify_structure
build_draft = editor_build.build_draft
publish_draft = editor_build.publish_draft


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
    p.add_argument('--detector', choices=('ffmpeg', 'pyscenedetect'), default='ffmpeg',
                   help='Candidate detector; default ffmpeg preserves existing behavior')
    p.add_argument('--content-threshold', type=float, default=27.0,
                   help='PySceneDetect ContentDetector threshold (0.01-255), not FFmpeg scene threshold')
    p.add_argument('--min-scene-seconds', type=float, default=0.3,
                   help='PySceneDetect minimum scene length in seconds')
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
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--cache', type=Path, required=True)
    p.add_argument('--custom-id', required=True)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
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
            result = native_matting_cache.admit(args.source, args.cache, args.custom_id, args.review, args.out, probe(args.source))
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
