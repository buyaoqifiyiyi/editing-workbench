"""Read-only local open-source tool discovery; never starts a GUI application or model."""
import os
from pathlib import Path
import re
import shutil
import subprocess

from .api import RUNTIME


def _path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else None


def _tool_path(name):
    for prefix in ('OPENEDIT_', 'JIANYING_'):
        override = os.environ.get(prefix + name.upper())
        if override:
            path = _path(override)
            return path if path and path.is_file() else None
    located = shutil.which(name)
    if located and Path(located).is_file():
        return Path(located)
    candidate = RUNTIME / 'tools/bin' / (name + ('.exe' if os.name == 'nt' else ''))
    return candidate if candidate.is_file() else None


def _exe_suffix():
    return '.exe' if os.name == 'nt' else ''


def _craft_candidates(app_id):
    cli_name = f'{app_id}-cli{_exe_suffix()}'
    env_key = f'OPENEDIT_{app_id.upper()}_CLI'
    explicit = os.environ.get(env_key)
    if explicit:
        target = _path(explicit)
        if target:
            yield target
        return

    root_env = os.environ.get(f'OPENEDIT_{app_id.upper()}_ROOT')
    if root_env:
        root = _path(root_env)
        if root:
            yield root / cli_name

    located = shutil.which(cli_name)
    if located:
        yield Path(located)

    local = Path(os.environ.get('LOCALAPPDATA', Path.home()))
    title = 'FilmCraft' if app_id == 'filmcraft' else 'EffectCraft'
    for base in (local / 'OpenEditTools',):
        if base.is_dir():
            for match in sorted(base.glob(f'{app_id}-*/**/{cli_name}'), reverse=True):
                yield match
    for program_dir in (
        local / 'Programs' / 'ArtCraft' / title / cli_name,
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'ArtCraft' / title / cli_name,
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'ArtCraft' / title / cli_name,
    ):
        yield program_dir


def _query_cli_version(cli_path):
    try:
        proc = subprocess.run(
            [str(cli_path), '--version'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        text = (proc.stdout + ' ' + proc.stderr).strip()
        match = re.search(r'(\d+\.\d+\.\d+)', text)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    match = re.search(r'(\d+\.\d+\.\d+)', str(cli_path))
    return match.group(1) if match else 'unknown'


def _detect_craft(app_id):
    title = 'FilmCraft' if app_id == 'filmcraft' else 'EffectCraft'
    gui_name = f'{app_id}{_exe_suffix()}'
    for candidate in _craft_candidates(app_id):
        if candidate and candidate.is_file():
            resolved = candidate.resolve()
            gui = resolved.with_name(gui_name)
            version = _query_cli_version(resolved)
            return {
                'id': app_id,
                'installed': True,
                'compatible': True,
                'selectable': True,
                'state': 'available',
                'label': '可用',
                'reason': f'已检测到开源 {title} ({version})，CLI/MCP 与明文工程就绪。',
                'version': version,
                'path': str(resolved),
                'gui_path': str(gui) if gui.is_file() else None,
            }
    return {
        'id': app_id,
        'installed': False,
        'compatible': False,
        'selectable': False,
        'state': 'missing',
        'label': '未安装',
        'reason': f'未检测到 {app_id}-cli；可从 GitHub storytold/{app_id} 下载便携版或设置 OPENEDIT_{app_id.upper()}_CLI。',
        'version': None,
        'path': None,
        'gui_path': None,
    }


def _detect_ffmpeg():
    ffmpeg = _tool_path('ffmpeg')
    ffprobe = _tool_path('ffprobe')
    ready = bool(ffmpeg and ffprobe)
    version = None
    if ffmpeg:
        try:
            proc = subprocess.run(
                [str(ffmpeg), '-version'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            first = proc.stdout.splitlines()[0] if proc.stdout else ''
            match = re.search(r'ffmpeg version (\S+)', first)
            version = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        'id': 'ffmpeg',
        'installed': ready,
        'compatible': ready,
        'selectable': ready,
        'state': 'available' if ready else 'missing',
        'label': '可用' if ready else '缺失',
        'reason': 'FFmpeg/FFprobe 就绪，可用于媒体探测、VFR 标准化与严格解码验收。' if ready else '缺少 ffmpeg 或 ffprobe 可执行文件。',
        'version': version,
        'path': str(ffmpeg) if ffmpeg else None,
        'ffprobe': str(ffprobe) if ffprobe else None,
    }


def detect_gpu():
    smi = shutil.which('nvidia-smi')
    if not smi:
        return {'nvidia': False, 'gpus': []}
    try:
        proc = subprocess.run(
            [smi, '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            gpus = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            return {'nvidia': bool(gpus), 'gpus': gpus}
    except (OSError, subprocess.SubprocessError):
        pass
    return {'nvidia': False, 'gpus': []}


def detect_backends():
    """Read-only availability inventory of open-source backends on this machine."""
    return {
        'filmcraft': _detect_craft('filmcraft'),
        'effectcraft': _detect_craft('effectcraft'),
        'ffmpeg': _detect_ffmpeg(),
    }
