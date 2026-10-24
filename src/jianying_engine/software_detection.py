"""Read-only local application discovery; never starts an application or model."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct

from .api import RUNTIME


_EXECUTABLES = {
    'resolve': ('Resolve.exe',), 'jianying': ('JianyingPro.exe',),
    'after-effects': ('AfterFX.exe',), 'premiere': ('Adobe Premiere Pro.exe',),
}


def _file_version(path):
    if os.name != 'nt':
        return None
    try:
        from ctypes import wintypes
        library = ctypes.WinDLL('version', use_last_error=True)
        size = library.GetFileVersionInfoSizeW
        size.argtypes, size.restype = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD
        count = size(str(path), None)
        if not count:
            return None
        buffer = ctypes.create_string_buffer(count)
        load = library.GetFileVersionInfoW
        load.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        query = library.VerQueryValueW
        query.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not load(str(path), 0, count, buffer) or not query(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
            return None
        if length.value < 52:
            return None
        fields = struct.unpack('<13I', ctypes.string_at(pointer, 52))
        if fields[0] != 0xFEEF04BD:
            return None
        return '.'.join(str(value) for value in (fields[2] >> 16, fields[2] & 65535,
                                                fields[3] >> 16, fields[3] & 65535))
    except (OSError, ValueError):
        return None


def _path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else None


def _command_path(value):
    # Registry command text is a location hint, never an executable command.
    match = re.match(r'^\s*"([^"]+\.exe)"|^\s*(.+?\.exe)(?:\s|,|$)', value or '', re.I)
    return _path(next((item for item in match.groups() if item), '')) if match else None


def _application_id(name):
    name = name.casefold()
    for backend, terms in (
        ('resolve', ('davinci resolve',)), ('after-effects', ('after effects',)),
        ('premiere', ('premiere pro',)), ('jianying', ('剪映', 'jianyingpro', 'jianying pro')),
    ):
        if any(term in name for term in terms):
            return backend
    return None


def _registered_apps():
    if os.name != 'nt':
        return []
    import winreg
    found = []
    key_name = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            name = winreg.EnumKey(parent, index)
                            with winreg.OpenKey(parent, name) as key:
                                def value(field):
                                    try:
                                        result = winreg.QueryValueEx(key, field)[0]
                                        return result if isinstance(result, str) else ''
                                    except OSError:
                                        return ''
                                backend = _application_id(value('DisplayName') or name)
                                if backend is None:
                                    continue
                                version = value('DisplayVersion')
                                location = _path(value('InstallLocation'))
                                executable = _command_path(value('DisplayIcon'))
                                uninstall = _command_path(value('UninstallString')) if backend == 'jianying' else None
                                roots = [location, executable.parent if executable else None,
                                         uninstall.parent if uninstall else None]
                                for root in filter(None, roots):
                                    if backend == 'jianying' and version:
                                        found.append((backend, root / version))
                                    found.append((backend, root))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _configuration(name):
    path = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JianyingEngine' / name
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {'_error': name + '配置格式无效。'}
    except (OSError, ValueError):
        return {'_error': name + '配置无法读取，请修复软件路径配置。'}


def _known_roots(backend):
    program_files = [Path(os.environ.get(key, default)) for key, default in
                     (('ProgramFiles', 'C:/Program Files'), ('ProgramFiles(x86)', 'C:/Program Files (x86)'))]
    local = Path(os.environ.get('LOCALAPPDATA', Path.home()))
    patterns = {
        'resolve': ('Blackmagic Design/DaVinci Resolve',),
        'after-effects': ('Adobe/Adobe After Effects */Support Files',),
        'premiere': ('Adobe/Adobe Premiere Pro *',),
        'jianying': ('JianyingPro/Apps/*', 'JianyingPro/Apps'),
    }
    for base in program_files + [local, local / 'Programs']:
        for pattern in patterns[backend]:
            try:
                yield from sorted(base.glob(pattern), reverse=True)
            except OSError:
                continue


def _candidate_roots(backend, registered):
    explicit = None
    if backend == 'resolve':
        explicit = os.environ.get('JIANYING_RESOLVE_ROOT') or _configuration('resolve.json').get('resolve_root')
    elif backend == 'after-effects':
        explicit = _configuration('adobe.json').get('ae_root')
    elif backend == 'premiere':
        value = _path(_configuration('adobe.json').get('pr_exe'))
        explicit = str(value.parent) if value else None
    if explicit:
        yield _path(explicit)
        return
    for key, root in registered:
        if key == backend:
            yield root
            if backend == 'after-effects':
                yield root / 'Support Files'
    if backend == 'jianying':
        # Match the execution engine's recorded-install fallback without importing its CLI.
        for manifest in sorted((RUNTIME / 'assets').glob('native-preview-*.json'), reverse=True):
            try:
                root = _path(json.loads(manifest.read_text(encoding='utf-8')).get('install'))
                if root:
                    yield root
            except (OSError, ValueError):
                continue
    for name in _EXECUTABLES[backend]:
        executable = shutil.which(name)
        if executable:
            yield Path(executable).parent
    yield from _known_roots(backend)


def _tool_path(name):
    override = os.environ.get('JIANYING_' + name.upper())
    if override:
        path = _path(override)
        return path if path and path.is_file() else None
    located = shutil.which(name)
    if located and Path(located).is_file():
        return Path(located)
    candidate = RUNTIME / 'tools/bin' / (name + ('.exe' if os.name == 'nt' else ''))
    return candidate if candidate.is_file() else None


def _result(installed, compatible, reason, path=None, version=None, unsupported=False):
    state = 'unsupported' if unsupported else 'available' if compatible else 'incompatible' if installed else 'missing'
    return {'installed': installed, 'compatible': compatible, 'selectable': compatible and not unsupported,
            'state': state, 'label': {'available': '本机可用', 'missing': '未安装',
                                    'incompatible': '接入条件不满足', 'unsupported': '暂不兼容'}[state],
            'reason': reason, 'path': str(path) if path else None, 'version': version}


def _inspect_application(backend, executable, tools):
    executable = executable.resolve()
    root = executable.parent
    version = _file_version(executable)
    reason = None
    if backend == 'resolve':
        missing = [name for name in ('ResolvePython/ResolvePython.exe', 'fusionscript.dll')
                   if not (root / name).is_file()]
        if missing:
            reason = '缺少达芬奇脚本运行文件：' + '、'.join(missing)
        elif version is None or version.split('.')[:3] != ['21', '1', '1']:
            reason = '当前仅接入达芬奇21.1.1；' + (f'检测版本为{version}。' if version else '未能读取安装版本。')
    elif backend == 'after-effects':
        missing = [name for name in ('AfterFX.com', 'aerender.exe') if not (root / name).is_file()]
        if missing:
            reason = '缺少AE执行文件：' + '、'.join(missing)
    elif backend == 'jianying':
        if not (root / 'Resources/Font/SystemFont').is_dir():
            reason = '剪映安装不完整：缺少系统字体资源目录。'
        elif importlib.util.find_spec('pyJianYingDraft') is None:
            reason = '引擎缺少pyJianYingDraft依赖。'
    if backend != 'premiere' and not all(tools.values()):
        reason = reason or '缺少FFmpeg或FFprobe，请检查本机工具配置。'
    if backend in ('resolve', 'after-effects'):
        reason = _configuration('resolve.json' if backend == 'resolve' else 'adobe.json').get('_error') or reason
    if backend == 'premiere':
        return _result(True, False, '当前未开放PR适配，安装后仍不能选择。', executable, version, True)
    if os.name != 'nt':
        reason = '当前软件执行适配仅在Windows准入。'
    return _result(True, reason is None, reason or '已检测到安装及基础接入文件；授权、连接和具体能力在执行时核对。',
                   executable, version)


def detect_backends():
    """Fresh filesystem/registry discovery; the result grants no native capability."""
    registered = _registered_apps()
    tools = {name: _tool_path(name) for name in ('ffmpeg', 'ffprobe')}
    result = {}
    for backend, filenames in _EXECUTABLES.items():
        detected = None
        for root in dict.fromkeys(_candidate_roots(backend, registered)):
            if root is None or not root.is_absolute():
                continue
            for name in filenames:
                executable = root / name
                if executable.is_file():
                    candidate = _inspect_application(backend, executable, tools)
                    if detected is None or candidate['selectable']:
                        detected = candidate
                    if candidate['selectable']:
                        break
            if detected and detected['selectable']:
                break
        result[backend] = detected or _result(False, False,
            '当前未开放PR适配。' if backend == 'premiere' else '未发现应用可执行文件，可能未安装或已卸载。',
            unsupported=backend == 'premiere')
    return result
