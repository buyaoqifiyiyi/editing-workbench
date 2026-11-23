"""Read-only local open-source tool discovery; never starts a GUI application or model."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .api import RUNTIME

VERIFIED_BASELINES = {
    'filmcraft': {'baseline': '0.20.1', 'min_version': '0.20.0', 'max_minor_exclusive': '0.21.0'},
    'effectcraft': {'baseline': '0.24.1', 'min_version': '0.24.0', 'max_minor_exclusive': '0.25.0'},
}
_SEMVER_RE = re.compile(r'^\s*v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?\s*$')
_PATH_SEMVER_RE = re.compile(r'(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)')


def parse_semver(value):
    """Parse SemVer string into a comparable tuple or None."""
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value)
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    pre = match.group(4)
    return (major, minor, patch, 1 if pre is None else 0, pre or '')


def _path_semver_key(path):
    matches = _PATH_SEMVER_RE.findall(str(path))
    parsed = parse_semver(matches[-1]) if matches else None
    if parsed is not None:
        return (1, parsed, str(path))
    return (0, (0, 0, 0, 0, ''), str(path))


def _runtime_verified_version(app_id):
    state_path = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OpenEditTools' / 'upstream-state.json'
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = (data.get('verified_versions') or {}).get(app_id)
    ver = entry.get('version') if isinstance(entry, dict) else None
    return ver if isinstance(ver, str) and ver.strip() else None


def check_version_baseline(app_id, version, *, check_runtime_state=True):
    """Compare detected or upstream Craft version against verified engine baselines."""
    spec = VERIFIED_BASELINES.get(app_id)
    if spec is None:
        return {'verified_baseline': None, 'verified_range': None, 'version_status': 'unknown', 'warning': None}
    baseline = spec['baseline']
    v_range = f">={spec['min_version']},<{spec['max_minor_exclusive']}"
    parsed = parse_semver(version)
    min_v = parse_semver(spec['min_version'])
    max_excl = parse_semver(spec['max_minor_exclusive'])
    title = 'FilmCraft' if app_id == 'filmcraft' else 'EffectCraft'
    if parsed is None:
        return {
            'verified_baseline': baseline,
            'verified_range': v_range,
            'version_status': 'unknown',
            'warning': f'未能解析 {title} 版本号 ({version!r})，已验证基线范围为 {v_range}（基线 {baseline}）；建议运行 openedit contract-check 复验契约。',
        }
    core_ver = parsed[:3]
    if core_ver < min_v[:3]:
        return {
            'verified_baseline': baseline,
            'verified_range': v_range,
            'version_status': 'older_than_baseline',
            'warning': f'{title} 当前版本 {version} 低于已验证基线 {baseline}（要求 {v_range}），部分命令或参数可能缺失；建议升级到基线版本并运行 openedit contract-check。',
        }
    if core_ver >= max_excl[:3]:
        if check_runtime_state:
            runtime_ver = _runtime_verified_version(app_id)
            runtime_parsed = parse_semver(runtime_ver)
            if runtime_parsed is not None and runtime_parsed == parsed:
                return {
                    'verified_baseline': baseline,
                    'verified_range': v_range,
                    'version_status': 'verified_runtime',
                    'warning': None,
                    'runtime_verified_version': runtime_ver,
                }
        return {
            'verified_baseline': baseline,
            'verified_range': v_range,
            'version_status': 'unverified_newer',
            'warning': f'{title} 当前版本 {version} 高于已验证基线范围 {v_range}（基线 {baseline}），上游 MCP/CLI 契约可能存在破坏性变更；建议先用 openedit contract-check 进行实机契约验收。',
        }
    return {
        'verified_baseline': baseline,
        'verified_range': v_range,
        'version_status': 'verified',
        'warning': None,
    }


def _path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else None


def _tool_path(name):
    override = os.environ.get('OPENEDIT_' + name.upper())
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
            for match in sorted(base.glob(f'{app_id}-*/**/{cli_name}'), key=_path_semver_key, reverse=True):
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


def find_craft_cli(app_id):
    """Locate filmcraft-cli or effectcraft-cli executable without starting the GUI."""
    if app_id not in ('filmcraft', 'effectcraft'):
        raise ValueError(f'Unsupported Craft backend: {app_id}')
    title = 'FilmCraft' if app_id == 'filmcraft' else 'EffectCraft'
    env_key = f'OPENEDIT_{app_id.upper()}_CLI'
    explicit = os.environ.get(env_key)
    if explicit:
        target = _path(explicit)
        if target and target.is_file():
            return str(target.resolve())
        raise ValueError(f'Configured {env_key} not found: {explicit}')
    for candidate in _craft_candidates(app_id):
        if candidate and candidate.is_file():
            return str(candidate.resolve())
    raise ValueError(f'{app_id}-cli not found; install ArtCraft {title} or set {env_key}')


def _detect_craft(app_id):
    title = 'FilmCraft' if app_id == 'filmcraft' else 'EffectCraft'
    gui_name = f'{app_id}{_exe_suffix()}'
    spec = VERIFIED_BASELINES[app_id]
    verified_range = f">={spec['min_version']},<{spec['max_minor_exclusive']}"
    for candidate in _craft_candidates(app_id):
        if candidate and candidate.is_file():
            resolved = candidate.resolve()
            gui = resolved.with_name(gui_name)
            version = _query_cli_version(resolved)
            baseline_info = check_version_baseline(app_id, version)
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
                **baseline_info,
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
        'verified_baseline': spec['baseline'],
        'verified_range': verified_range,
        'version_status': 'missing',
        'warning': None,
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
