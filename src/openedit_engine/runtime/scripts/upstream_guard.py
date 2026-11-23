"""Upstream release tracking, real-binary MCP/CLI contract checks, and gated auto-upgrades."""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import craft_rpc
import effectcraft_backend
import file_hash
import filmcraft_backend
import runtime_common
import runtime_core as core
import runtime_paths
from openedit_engine import software_detection

TRACKED_REPOS = (
    ('filmcraft', 'storytold/filmcraft'),
    ('effectcraft', 'storytold/effectcraft'),
)

FILMCRAFT_REQUIRED_TOOLS = (
    'command_list',
    'command_run',
    'media_import',
    'project_inspect',
    'sequence_inspect',
    'doc_inspect',
    'render_frame',
)

FILMCRAFT_REQUIRED_COMMANDS = (
    'file.newProject',
    'file.newSequence',
    'file.saveAs',
    'file.exportMedia',
    'file.exportOtio',
    'file.exportFcpxml',
    'timeline.place',
    'timeline.select',
    'timeline.trim',
    'clip.speedDuration',
    'clip.fillFrame',
    'clip.fitToFrame',
    'clip.nest',
    'effects.apply',
    'effects.setParam',
    'effects.setInterpolation',
    'masks.add',
    'masks.set',
    'lumetri.setInputLut',
    'lumetri.setLook',
    'essentialSound.setType',
    'essentialSound.autoMatch',
    'sequence.applyVideoTransition',
    'sequence.applyAudioTransition',
    'markers.add',
    'captions.newTrack',
    'captions.setStyle',
    'captions.add',
    'graphics.newText',
    'graphics.set',
    'graphics.setCharStyle',
)

EFFECTCRAFT_REQUIRED_TOOLS = (
    'list_commands',
    'execute_command',
    'set_property',
    'add_effect',
    'add_keyframe',
    'run_script',
    'save_project',
    'render_frame',
    'list_fonts',
    'list_effects',
    'get_state',
    'get_comp',
)

EFFECTCRAFT_REQUIRED_COMMANDS = (
    'comp.new',
    'comp.setSwitch',
    'file.import',
    'layer.newSolid',
    'layer.newShape',
    'layer.newCamera',
    'layer.addItem',
    'layer.newText',
    'layer.setText',
    'layer.applyTextPreset',
    'layer.setBlendMode',
    'layer.setSwitch',
    'layer.enableTimeRemap',
    'layer.addMask',
    'layer.setMask',
    'anim.applyBuiltinPreset',
)


def _extract_command_ids(payload):
    """Extract command ID strings from list_commands/command_list responses."""
    items = payload.get('commands', []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return set()
    ids = set()
    for item in items:
        if isinstance(item, str) and item:
            ids.add(item)
        elif isinstance(item, dict):
            cid = item.get('id') or item.get('command')
            if isinstance(cid, str) and cid:
                ids.add(cid)
    return ids


def _probe_craft_contract(app_id, cli_finder, backend_info, required_tools, list_cmd_tool, required_commands):
    """Run live MCP handshake and verify required tools and command IDs."""
    base = {
        'version': backend_info.get('version'),
        'path': backend_info.get('path'),
        'verified_baseline': backend_info.get('verified_baseline'),
        'verified_range': backend_info.get('verified_range'),
        'version_status': backend_info.get('version_status'),
        'warning': backend_info.get('warning'),
        'contract_ok': False,
        'tool_count': 0,
        'command_count': 0,
        'missing_tools': list(required_tools),
        'missing_commands': list(required_commands),
        'error': None,
    }
    try:
        cli_path = cli_finder()
        base['path'] = cli_path
        with craft_rpc.CraftMcpClient([cli_path, 'mcp']) as client:
            tool_names = {t.get('name') for t in client.list_tools() if isinstance(t, dict) and t.get('name')}
            missing_tools = [name for name in required_tools if name not in tool_names]
            command_ids = set()
            if list_cmd_tool in tool_names:
                command_ids = _extract_command_ids(client.call_tool(list_cmd_tool, {}))
            missing_commands = [cid for cid in required_commands if cid not in command_ids]
        base['tool_count'] = len(tool_names)
        base['command_count'] = len(command_ids)
        base['missing_tools'] = missing_tools
        base['missing_commands'] = missing_commands
        base['contract_ok'] = not missing_tools and not missing_commands
        return base
    except (ValueError, RuntimeError, OSError) as exc:
        base['error'] = f'{app_id} MCP 契约检查失败: {exc}'
        return base


def _generate_fixture_clip(ffmpeg_bin, out_path, *, video_filter, freq_hz):
    core.run([
        ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-f', 'lavfi', '-i', video_filter,
        '-f', 'lavfi', '-i', f'sine=frequency={freq_hz}:sample_rate=48000:duration=1.5',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
        str(out_path),
    ], timeout=60)


def _run_micro_e2e(work_dir, *, output_kept):
    """Build and strictly verify a miniature multi-feature project against real binaries."""
    import open_engine

    t0 = time.monotonic()
    ffmpeg_bin = core.tool('ffmpeg')
    media_dir = work_dir / 'media'
    media_dir.mkdir(parents=True, exist_ok=True)
    clip_a = media_dir / 'clip_a.mp4'
    clip_b = media_dir / 'clip_b.mp4'
    _generate_fixture_clip(ffmpeg_bin, clip_a, video_filter='testsrc2=size=640x360:rate=30:duration=1.5', freq_hz=440)
    _generate_fixture_clip(ffmpeg_bin, clip_b, video_filter='color=c=#1E293B:s=640x360:r=30:d=1.5', freq_hz=660)

    plan_path = work_dir / 'edit-plan.json'
    plan = {
        'schema_version': 2,
        'name': 'contract-regression',
        'canvas': {'width': 640, 'height': 360, 'fps': 30},
        'clips': [
            {
                'path': str(clip_a),
                'start': 0.0,
                'duration': 1.5,
                'volume': 0.5,
                'grading': {'exposure': 0.1, 'contrast': 5, 'saturation': 105},
                'mask': {
                    'shape': 'ellipse',
                    'center_x': 0.5,
                    'center_y': 0.5,
                    'width': 0.6,
                    'height': 0.6,
                    'feather': 0,
                },
            },
            {
                'path': str(clip_b),
                'start': 1.5,
                'duration': 1.5,
                'volume': 0.5,
                'transition': {'name': 'Cross Dissolve', 'duration': 0.3, 'edge': 'in'},
            },
        ],
        'captions': [
            {
                'text': '契约回归测试',
                'start': 0.2,
                'duration': 1.2,
                'highlights': [{'start': 0, 'end': 2, 'color': '#FACC15'}],
            },
        ],
        'overlays': [
            {
                'name': 'contract_lt',
                'track': 'overlay',
                'start': 0.4,
                'duration': 1.8,
                'recipe': 'lower_third',
                'params': {'title': '契约回归', 'subtitle': 'OpenEdit 实机验收'},
            },
        ],
    }
    core.write_json(plan_path, plan)

    proj_out = work_dir / 'build'
    mp4_path = work_dir / 'final.mp4'
    otio_path = work_dir / 'timeline.otio'
    fcpxml_path = work_dir / 'timeline.fcpxml'
    report = open_engine.build(
        plan_path,
        proj_out,
        preview_times=[0.8],
        export_path=mp4_path,
        export_otio=otio_path,
        export_fcpxml=fcpxml_path,
    )

    previews = report.get('previews') or []
    overlays = report.get('overlays') or []
    export_info = report.get('export') or {}
    overlay_pix_fmt = (overlays[0].get('overlay_video') or {}).get('pix_fmt') if overlays else None
    preview_ok = bool(previews) and all(Path(p['path']).is_file() and Path(p['path']).stat().st_size > 0 for p in previews)
    interchange_ok = otio_path.is_file() and otio_path.stat().st_size > 0 and fcpxml_path.is_file() and fcpxml_path.stat().st_size > 0
    project_ok = Path(report.get('project', '')).is_file()
    decode_ok = export_info.get('strict_decode') == 'passed' and mp4_path.is_file()
    alpha_ok = bool(overlays) and bool((overlays[0].get('overlay_video') or {}).get('strict_decode') == 'passed')
    passed = bool(report.get('status') == 'passed' and project_ok and preview_ok and interchange_ok and decode_ok and alpha_ok)

    return {
        'status': 'passed' if passed else 'failed',
        'elapsed_seconds': round(time.monotonic() - t0, 3),
        'strict_decode': export_info.get('strict_decode'),
        'overlay_pix_fmt': overlay_pix_fmt,
        'preview_count': len(previews),
        'interchange_formats': sorted(report.get('interchange', {}).keys()),
        'output_kept': output_kept,
        'output_dir': str(work_dir) if output_kept else None,
    }


@contextlib.contextmanager
def _temp_cli_env(filmcraft_cli=None, effectcraft_cli=None):
    overrides = {}
    if filmcraft_cli is not None:
        overrides['OPENEDIT_FILMCRAFT_CLI'] = str(filmcraft_cli)
    if effectcraft_cli is not None:
        overrides['OPENEDIT_EFFECTCRAFT_CLI'] = str(effectcraft_cli)
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, orig in previous.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig


def contract_check(*, out_dir=None, skip_e2e=False, filmcraft_cli=None, effectcraft_cli=None):
    """Verify real FilmCraft/EffectCraft MCP tools, required commands, and optional micro E2E build."""
    with _temp_cli_env(filmcraft_cli=filmcraft_cli, effectcraft_cli=effectcraft_cli):
        backends = software_detection.detect_backends()
        fc_res = _probe_craft_contract(
            'filmcraft',
            filmcraft_backend.find_cli,
            backends['filmcraft'],
            FILMCRAFT_REQUIRED_TOOLS,
            'command_list',
            FILMCRAFT_REQUIRED_COMMANDS,
        )
        ec_res = _probe_craft_contract(
            'effectcraft',
            effectcraft_backend.find_cli,
            backends['effectcraft'],
            EFFECTCRAFT_REQUIRED_TOOLS,
            'list_commands',
            EFFECTCRAFT_REQUIRED_COMMANDS,
        )
        ff_info = backends['ffmpeg']
        contracts_ok = fc_res['contract_ok'] and ec_res['contract_ok']

        if skip_e2e:
            e2e_res = {'status': 'skipped', 'reason': '用户指定 --skip-e2e，跳过实机渲染回归。'}
        elif not contracts_ok:
            e2e_res = {'status': 'skipped', 'reason': 'MCP 契约检查未通过，已跳过实机渲染回归。'}
        elif not ff_info.get('installed'):
            e2e_res = {'status': 'failed', 'error': '缺少 ffmpeg/ffprobe，无法执行实机渲染回归。'}
        else:
            try:
                if out_dir is not None:
                    work_dir = core.new_directory(out_dir)
                    e2e_res = _run_micro_e2e(work_dir, output_kept=True)
                else:
                    with tempfile.TemporaryDirectory(prefix='openedit-contract-') as tmp:
                        e2e_res = _run_micro_e2e(Path(tmp), output_kept=False)
            except (ValueError, RuntimeError, OSError) as exc:
                e2e_res = {'status': 'failed', 'error': str(exc), 'output_kept': bool(out_dir)}

        e2e_ok = True if skip_e2e else (e2e_res.get('status') == 'passed')
        warnings = [item['warning'] for item in (fc_res, ec_res) if item.get('warning')]
        return {
            'status': 'passed' if (contracts_ok and e2e_ok) else 'failed',
            'e2e_executed': not skip_e2e,
            'filmcraft': fc_res,
            'effectcraft': ec_res,
            'ffmpeg': {
                'installed': ff_info.get('installed'),
                'version': ff_info.get('version'),
                'path': ff_info.get('path'),
            },
            'e2e': e2e_res,
            'warnings': warnings,
        }


def _resolve_github_token():
    """Resolve a GitHub API token from env vars, config.env, or `gh auth token`."""
    for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
        token = (os.environ.get(name) or '').strip()
        if token:
            return token
    try:
        config_path = runtime_paths.stock_config()
        if config_path.is_file():
            values = {}
            for line in config_path.read_text(encoding='utf-8-sig').splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                key, sep, val = stripped.partition('=')
                key = key.strip()
                if sep and key in ('GITHUB_TOKEN', 'GH_TOKEN'):
                    values[key] = val.strip().strip('"\'').strip()
            for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
                if values.get(name):
                    return values[name]
    except (OSError, ValueError):
        pass
    gh_bin = shutil.which('gh')
    if not gh_bin:
        return None
    try:
        proc = subprocess.run(
            [gh_bin, 'auth', 'token'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or '').strip()
    if token and not any(ch.isspace() for ch in token):
        return token
    return None


def _fetch_github_latest_release(repo, *, timeout=15):
    url = f'https://api.github.com/repos/{repo}/releases/latest'
    headers = {
        'User-Agent': 'openedit-engine-upstream-check/1.0',
        'Accept': 'application/vnd.github+json',
    }
    token = _resolve_github_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    raw_body = (data.get('body') or '').strip()
    if len(raw_body) > 1500:
        raw_body = raw_body[:1500].rstrip() + '...'
    assets = []
    for asset in data.get('assets') or []:
        if isinstance(asset, dict) and asset.get('name'):
            assets.append({
                'name': asset['name'],
                'size': asset.get('size'),
                'digest': asset.get('digest'),
                'download_url': asset.get('browser_download_url'),
            })
    return {
        'tag_name': data.get('tag_name'),
        'name': data.get('name'),
        'published_at': data.get('published_at'),
        'html_url': data.get('html_url'),
        'prerelease': bool(data.get('prerelease', False)),
        'body_excerpt': raw_body,
        'assets': assets,
    }


def _classify_update(local_version, latest_version):
    if not local_version:
        return False, 'not_installed'
    loc = software_detection.parse_semver(local_version)
    lat = software_detection.parse_semver(latest_version)
    if loc is None or lat is None:
        return False, 'unknown'
    if lat <= loc:
        return False, 'none'
    if lat[0] > loc[0]:
        return True, 'major'
    if lat[1] > loc[1]:
        return True, 'minor'
    if lat[2] > loc[2]:
        return True, 'patch'
    return True, 'prerelease'


def _read_state_file(path):
    if not path.is_file():
        return {}, None
    try:
        data = core.read_json(path)
        if not isinstance(data, dict):
            return {}, f'状态文件格式无效（非 JSON 对象）: {path}'
        return data, None
    except (OSError, ValueError) as exc:
        return {}, f'读取状态文件失败 ({path}): {exc}'


def _write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def upstream_check(*, state_path=None, save_state=False, timeout=15):
    """Read-only check of upstream GitHub Releases for FilmCraft and EffectCraft."""
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    backends = software_detection.detect_backends()

    resolved_state = None
    prev_state = {}
    state_warning = None
    if state_path is not None:
        resolved_state = Path(os.path.expandvars(str(state_path))).expanduser().resolve()
        prev_state, state_warning = _read_state_file(resolved_state)

    prev_tags = prev_state.get('last_seen_tags') if isinstance(prev_state.get('last_seen_tags'), dict) else {}
    prev_repos = prev_state.get('repos') if isinstance(prev_state.get('repos'), dict) else {}
    next_tags = dict(prev_tags)
    next_repos = dict(prev_repos)

    apps = {}
    succeeded = 0
    for app_id, repo in TRACKED_REPOS:
        local_info = backends[app_id]
        local_version = local_info.get('version')
        prev_tag = prev_tags.get(app_id)
        entry = {
            'repo': repo,
            'local_version': local_version,
            'local_installed': bool(local_info.get('installed')),
            'local_baseline': {
                'verified_baseline': local_info.get('verified_baseline'),
                'verified_range': local_info.get('verified_range'),
                'version_status': local_info.get('version_status'),
            },
            'previous_seen_tag': prev_tag,
            'fetch_ok': False,
        }
        try:
            rel = _fetch_github_latest_release(repo, timeout=timeout)
            tag_name = rel.get('tag_name') or ''
            latest_version = tag_name.lstrip('vV') if tag_name else None
            update_available, update_kind = _classify_update(local_version, latest_version)
            entry.update({
                'fetch_ok': True,
                'latest_tag': tag_name or None,
                'latest_version': latest_version,
                'release_name': rel.get('name'),
                'published_at': rel.get('published_at'),
                'html_url': rel.get('html_url'),
                'prerelease': rel.get('prerelease'),
                'body_excerpt': rel.get('body_excerpt'),
                'update_available': update_available,
                'update_kind': update_kind,
                'changed_since_last_check': bool(prev_tag is not None and tag_name and tag_name != prev_tag),
                'first_recorded': prev_tag is None,
                'latest_vs_baseline': software_detection.check_version_baseline(
                    app_id, latest_version, check_runtime_state=False,
                ),
                'error': None,
            })
            if tag_name:
                next_tags[app_id] = tag_name
            next_repos[app_id] = {
                'repo': repo,
                'latest_tag': entry['latest_tag'],
                'published_at': entry['published_at'],
                'html_url': entry['html_url'],
                'local_version': local_version,
                'update_available': update_available,
            }
            succeeded += 1
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            entry.update({
                'latest_tag': None,
                'latest_version': None,
                'update_available': False,
                'update_kind': 'error',
                'changed_since_last_check': False,
                'first_recorded': prev_tag is None,
                'error': str(exc),
            })
        apps[app_id] = entry

    state_saved = False
    if resolved_state is not None and save_state and succeeded > 0:
        _write_json_atomic(resolved_state, {
            'schema_version': 1,
            'checked_at': checked_at,
            'last_seen_tags': next_tags,
            'verified_versions': prev_state.get('verified_versions', {}),
            'failed_versions': prev_state.get('failed_versions', {}),
            'repos': next_repos,
        })
        state_saved = True

    any_update = any(bool(item.get('update_available')) for item in apps.values())
    any_changed = any(bool(item.get('changed_since_last_check')) for item in apps.values())
    if succeeded == len(TRACKED_REPOS):
        status = 'ok'
    elif succeeded > 0:
        status = 'partial'
    else:
        status = 'failed'

    next_steps = []
    if any_update:
        next_steps = [
            '请勿直接覆盖 %LOCALAPPDATA%\\OpenEditTools 中的现有稳定版本目录。',
            '先将上游新版便携包解压到临时目录，并在新终端设置 $env:OPENEDIT_FILMCRAFT_CLI / $env:OPENEDIT_EFFECTCRAFT_CLI 指向新版 cli.exe。',
            '执行 openedit contract-check 运行实机 MCP 契约 + 微型全特性 E2E 严格解码回归验收；全部通过后再移入 OpenEditTools 并保留旧版以便回滚。',
        ]

    ff_info = backends['ffmpeg']
    return {
        'status': status,
        'checked_at': checked_at,
        'read_only_binaries': True,
        'any_update_available': any_update,
        'any_changed_since_last_check': any_changed,
        'apps': apps,
        'local_only': {
            'ffmpeg': {
                'installed': ff_info.get('installed'),
                'version': ff_info.get('version'),
                'path': ff_info.get('path'),
                'note': '仅报告本机版本；FFmpeg 不经 GitHub Releases 自动追踪。',
            },
        },
        'state': {
            'path': str(resolved_state) if resolved_state is not None else None,
            'saved': state_saved,
            'previous_checked_at': prev_state.get('checked_at'),
            'warning': state_warning,
        },
        'recommended_next_steps': next_steps,
    }


def _adapter_fingerprint():
    scripts_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('craft_rpc.py', 'effectcraft_backend.py', 'filmcraft_backend.py'):
        path = scripts_dir / name
        digest.update(f'{name}:{file_hash.sha256(path)}\n'.encode('utf-8'))
    return digest.hexdigest()


def _windows_arch():
    return 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'


def _select_portable_asset(app_id, version, assets):
    arch = _windows_arch()
    exact = f'{app_id}-{version}-windows-{arch}-portable.zip'
    suffix = f'-windows-{arch}-portable.zip'
    portable = None
    fallback = None
    sums_asset = None
    for item in assets or ():
        name = item.get('name', '')
        if name == exact:
            portable = item
        elif name.startswith(f'{app_id}-') and name.endswith(suffix) and fallback is None:
            fallback = item
        elif name == 'SHA256SUMS.txt':
            sums_asset = item
    return portable or fallback, sums_asset


def _expected_sha256(asset, sums_asset, repo, *, timeout=15):
    digest = asset.get('digest')
    if isinstance(digest, str):
        m = re.fullmatch(r'sha256:([0-9a-fA-F]{64})', digest.strip())
        if m:
            return m.group(1).lower()
    if sums_asset and isinstance(sums_asset.get('download_url'), str):
        url = sums_asset['download_url']
        prefix = f'https://github.com/{repo}/releases/download/'
        if not url.startswith(prefix):
            raise RuntimeError(f'拒绝非官方校验和下载地址: {url}')
        req = urllib.request.Request(url, headers={'User-Agent': 'openedit-engine-upstream-upgrade/1.0'})
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            text = resp.read().decode('utf-8', errors='replace')
        target_name = asset.get('name', '')
        for line in text.splitlines():
            m = re.match(r'^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$', line.strip())
            if m and Path(m.group(2)).name == target_name:
                return m.group(1).lower()
    return None


def _download_verified_zip(url, dest_path, expected_sha256, repo, *, timeout=120):
    prefix = f'https://github.com/{repo}/releases/download/'
    if not isinstance(url, str) or not url.startswith(prefix):
        raise RuntimeError(f'拒绝非官方 Release 下载地址: {url}')
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise RuntimeError('上游发布缺少有效 SHA-256 摘要，拒绝安装未校验包')
    expected_lower = expected_sha256.lower()
    req = urllib.request.Request(url, headers={'User-Agent': 'openedit-engine-upstream-upgrade/1.0'})
    hasher = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=int(timeout)) as resp, Path(dest_path).open('wb') as out:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            hasher.update(chunk)
            out.write(chunk)
    actual = hasher.hexdigest().lower()
    if actual != expected_lower:
        try:
            Path(dest_path).unlink()
        except OSError:
            pass
        raise RuntimeError(f'SHA-256 校验失败: 期望 {expected_lower}，实际 {actual}')
    return actual


def _safe_extract_zip(zip_path, dest_dir):
    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f'压缩包包含非法符号链接: {info.filename}')
            if not info.filename or info.filename.startswith(('/', '\\')) or Path(info.filename).drive:
                raise RuntimeError(f'压缩包包含非法绝对路径: {info.filename}')
            target = (dest_root / info.filename).resolve()
            if target != dest_root and not target.is_relative_to(dest_root):
                raise RuntimeError(f'压缩包存在路径穿越风险: {info.filename}')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, 'r') as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)


def _find_staged_cli(stage_dir, app_id):
    cli_name = f'{app_id}-cli.exe' if os.name == 'nt' else f'{app_id}-cli'
    matches = sorted(Path(stage_dir).rglob(cli_name))
    for item in matches:
        if item.is_file():
            return str(item.resolve())
    raise RuntimeError(f'解压目录中未找到 {cli_name}: {stage_dir}')


def _release_age_hours(published_at, now_utc):
    if not isinstance(published_at, str) or not published_at.strip():
        return None
    try:
        dt = datetime.fromisoformat(published_at.strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except ValueError:
        return None


def _list_installed_dirs(tools_root, app_id):
    if not tools_root.is_dir():
        return []
    dirs = [p for p in tools_root.glob(f'{app_id}-*') if p.is_dir()]
    return sorted(dirs, key=software_detection._path_semver_key, reverse=True)


def _detect_local_in_tools_root(tools_root):
    out = {}
    for app_id, _ in TRACKED_REPOS:
        cli_name = f'{app_id}-cli.exe' if os.name == 'nt' else f'{app_id}-cli'
        chosen = None
        for folder in _list_installed_dirs(tools_root, app_id):
            matches = sorted(folder.rglob(cli_name))
            for item in matches:
                if item.is_file():
                    chosen = item.resolve()
                    break
            if chosen is not None:
                break
        if chosen is not None:
            ver = software_detection._query_cli_version(chosen)
            out[app_id] = {'installed': True, 'version': ver, 'path': str(chosen)}
        else:
            out[app_id] = {'installed': False, 'version': None, 'path': None}
    return out


def _promote_staged_dir(stage_dir, target_dir):
    tools_root = target_dir.parent
    tools_root.mkdir(parents=True, exist_ok=True)
    promote_tmp = tools_root / f'.promote-{target_dir.name}-{os.getpid()}'
    backup_tmp = tools_root / f'.backup-{target_dir.name}-{os.getpid()}'
    if promote_tmp.exists():
        shutil.rmtree(promote_tmp)
    if backup_tmp.exists():
        shutil.rmtree(backup_tmp)
    shutil.move(str(stage_dir), str(promote_tmp))
    try:
        if target_dir.exists():
            target_dir.rename(backup_tmp)
        promote_tmp.rename(target_dir)
    except OSError:
        if backup_tmp.exists() and not target_dir.exists():
            try:
                backup_tmp.rename(target_dir)
            except OSError:
                pass
        raise
    if backup_tmp.exists():
        shutil.rmtree(backup_tmp, ignore_errors=True)


def _prune_old_versions(tools_root, app_id, keep_versions, protected_dirs):
    dirs = _list_installed_dirs(tools_root, app_id)
    keep_limit = max(1, int(keep_versions))
    protected = {Path(p).resolve() for p in protected_dirs if p}
    kept = []
    removed = []
    warnings = []
    for idx, folder in enumerate(dirs):
        resolved = folder.resolve()
        if idx < keep_limit or resolved in protected:
            kept.append(folder.name)
            continue
        try:
            shutil.rmtree(folder)
            removed.append(folder.name)
        except OSError as exc:
            kept.append(folder.name)
            warnings.append(f'清理旧版本目录 {folder.name} 失败: {exc}')
    return kept, removed, warnings


def _summarize_contract_failure(app_id, report):
    app_rep = report.get(app_id) or {}
    e2e_rep = report.get('e2e') or {}
    return {
        'missing_tools': app_rep.get('missing_tools', []),
        'missing_commands': app_rep.get('missing_commands', []),
        'app_error': app_rep.get('error'),
        'e2e_status': e2e_rep.get('status'),
        'e2e_error': e2e_rep.get('error'),
    }


def _find_owning_dir(tools_root, path_str):
    if not path_str:
        return None
    try:
        resolved = Path(path_str).resolve()
        root = tools_root.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    for parent in resolved.parents:
        if parent.parent == root:
            return parent
    return None


def upstream_upgrade(*, state_path=None, tools_dir=None, min_age_hours=0, keep_versions=2,
                     dry_run=False, retry_failed=False, timeout=30):
    """Check upstream releases, verify SHA-256 + real sandbox contract-check, and promote passing builds."""
    if int(min_age_hours) < 0:
        raise ValueError('min_age_hours must be >= 0')
    if int(keep_versions) < 1:
        raise ValueError('keep_versions must be >= 1')
    if int(timeout) < 5:
        raise ValueError('timeout must be >= 5')

    custom_tools = tools_dir is not None
    tools_root = (
        Path(os.path.expandvars(str(tools_dir))).expanduser().resolve()
        if custom_tools
        else (Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OpenEditTools').resolve()
    )
    resolved_state = (
        Path(os.path.expandvars(str(state_path))).expanduser().resolve()
        if state_path is not None
        else (tools_root / 'upstream-state.json')
    )

    if dry_run:
        return _run_upstream_upgrade(
            tools_root=tools_root,
            custom_tools=custom_tools,
            resolved_state=resolved_state,
            min_age_hours=int(min_age_hours),
            keep_versions=int(keep_versions),
            dry_run=True,
            retry_failed=bool(retry_failed),
            timeout=int(timeout),
        )

    lock_path = tools_root / '.upstream-upgrade.lock'
    with runtime_common.file_lock(
        lock_path, blocking=False, pad_empty=False, busy_message='已有另一场 OpenEdit 上游升级正在执行'
    ):
        return _run_upstream_upgrade(
            tools_root=tools_root,
            custom_tools=custom_tools,
            resolved_state=resolved_state,
            min_age_hours=int(min_age_hours),
            keep_versions=int(keep_versions),
            dry_run=False,
            retry_failed=bool(retry_failed),
            timeout=int(timeout),
        )


def _run_upstream_upgrade(*, tools_root, custom_tools, resolved_state, min_age_hours,
                          keep_versions, dry_run, retry_failed, timeout):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    checked_at = now_utc.isoformat().replace('+00:00', 'Z')
    local_backends = _detect_local_in_tools_root(tools_root) if custom_tools else software_detection.detect_backends()
    prev_state, state_warning = _read_state_file(resolved_state)

    prev_tags = dict(prev_state.get('last_seen_tags')) if isinstance(prev_state.get('last_seen_tags'), dict) else {}
    verified_versions = dict(prev_state.get('verified_versions')) if isinstance(prev_state.get('verified_versions'), dict) else {}
    failed_versions = dict(prev_state.get('failed_versions')) if isinstance(prev_state.get('failed_versions'), dict) else {}
    prev_repos = dict(prev_state.get('repos')) if isinstance(prev_state.get('repos'), dict) else {}
    fingerprint = _adapter_fingerprint()

    apps = {}
    candidates = {}
    fetch_ok_count = 0

    for app_id, repo in TRACKED_REPOS:
        local_info = local_backends[app_id]
        local_version = local_info.get('version')
        entry = {
            'repo': repo,
            'local_version': local_version,
            'local_path': local_info.get('path'),
            'fetch_ok': False,
            'action': 'pending',
        }
        try:
            rel = _fetch_github_latest_release(repo, timeout=min(timeout, 30))
            fetch_ok_count += 1
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            entry['action'] = 'fetch_error'
            entry['error'] = str(exc)
            apps[app_id] = entry
            continue

        tag_name = rel.get('tag_name') or ''
        latest_version = tag_name.lstrip('vV') if tag_name else None
        update_available, update_kind = _classify_update(local_version, latest_version)
        age_hours = _release_age_hours(rel.get('published_at'), now_utc)
        portable_asset, sums_asset = _select_portable_asset(app_id, latest_version, rel.get('assets'))

        entry.update({
            'fetch_ok': True,
            'latest_tag': tag_name or None,
            'latest_version': latest_version,
            'published_at': rel.get('published_at'),
            'release_age_hours': round(age_hours, 2) if age_hours is not None else None,
            'html_url': rel.get('html_url'),
            'body_excerpt': rel.get('body_excerpt'),
            'update_available': update_available,
            'update_kind': update_kind,
            'error': None,
        })
        if tag_name:
            prev_tags[app_id] = tag_name
        prev_repos[app_id] = {
            'repo': repo,
            'latest_tag': entry['latest_tag'],
            'published_at': entry['published_at'],
            'html_url': entry['html_url'],
            'local_version': local_version,
            'update_available': update_available,
        }

        if not update_available:
            entry['action'] = 'up_to_date'
        elif min_age_hours > 0 and (age_hours is None or age_hours < min_age_hours):
            entry['action'] = 'deferred_cooldown'
            entry['reason'] = f'发布不足 {min_age_hours} 小时冷静期（当前约 {entry["release_age_hours"]}h），暂缓升级以避开零日热修。'
        else:
            known_fail = failed_versions.get(app_id)
            if (not retry_failed and isinstance(known_fail, dict)
                    and known_fail.get('tag') == tag_name
                    and known_fail.get('adapter_fingerprint') == fingerprint):
                entry['action'] = 'skipped_known_failure'
                entry['failure_summary'] = known_fail.get('summary')
                entry['reason'] = '该版本此前已在本机沙箱契约检查中失败，且本地适配未变更；使用 --retry-failed 可强制重试。'
            elif not portable_asset or not portable_asset.get('download_url'):
                entry['action'] = 'error_missing_asset'
                entry['error'] = f'Release {tag_name} 中未找到适用 Windows ({_windows_arch()}) 便携版压缩包'
            elif dry_run:
                entry['action'] = 'would_upgrade'
                entry['asset'] = portable_asset['name']
            else:
                candidates[app_id] = {
                    'repo': repo,
                    'tag': tag_name,
                    'version': latest_version,
                    'asset': portable_asset,
                    'sums_asset': sums_asset,
                }
        apps[app_id] = entry

    upgraded = []
    blocked = []
    prune_warnings = []

    if candidates and not dry_run:
        with tempfile.TemporaryDirectory(prefix='openedit-upgrade-stage-') as stage_root_str:
            stage_root = Path(stage_root_str)
            staged = {}
            for app_id, cand in candidates.items():
                try:
                    expected_hash = _expected_sha256(cand['asset'], cand['sums_asset'], cand['repo'], timeout=min(timeout, 30))
                    if not expected_hash:
                        raise RuntimeError(f'{cand["asset"]["name"]} 缺少 GitHub digest 与 SHA256SUMS.txt 校验和')
                    zip_path = stage_root / cand['asset']['name']
                    actual_hash = _download_verified_zip(
                        cand['asset']['download_url'], zip_path, expected_hash, cand['repo'],
                        timeout=max(timeout, 120),
                    )
                    extract_dir = stage_root / f'extract-{app_id}'
                    _safe_extract_zip(zip_path, extract_dir)
                    cli_path = _find_staged_cli(extract_dir, app_id)
                    staged[app_id] = {
                        **cand,
                        'stage_dir': extract_dir,
                        'cli': cli_path,
                        'sha256': actual_hash,
                    }
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                        ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                    is_hash_err = 'SHA-256' in str(exc) or '校验' in str(exc)
                    apps[app_id]['action'] = 'error_checksum' if is_hash_err else 'error_download'
                    apps[app_id]['error'] = str(exc)

            approved = {}
            if len(staged) == 2:
                combined = contract_check(
                    filmcraft_cli=staged['filmcraft']['cli'],
                    effectcraft_cli=staged['effectcraft']['cli'],
                    skip_e2e=False,
                )
                if combined.get('status') == 'passed':
                    for app_id, st in staged.items():
                        approved[app_id] = (st, combined)
                else:
                    solo_reports = {}
                    for app_id, st in staged.items():
                        kwargs = {'skip_e2e': False, f'{app_id}_cli': st['cli']}
                        solo_reports[app_id] = contract_check(**kwargs)
                    both_solo_passed = all(rep.get('status') == 'passed' for rep in solo_reports.values())
                    for app_id, st in staged.items():
                        rep = solo_reports[app_id]
                        if rep.get('status') == 'passed' and not both_solo_passed:
                            approved[app_id] = (st, rep)
                        else:
                            failure = _summarize_contract_failure(app_id, combined if both_solo_passed else rep)
                            if both_solo_passed:
                                failure['pair_incompatibility'] = '单独检查均通过但联合 E2E 失败，已拦截以避免破坏双后端协同。'
                            apps[app_id]['action'] = 'blocked_contract_failed'
                            apps[app_id]['failure_summary'] = failure
                            failed_versions[app_id] = {
                                'tag': st['tag'],
                                'version': st['version'],
                                'adapter_fingerprint': fingerprint,
                                'failed_at': checked_at,
                                'summary': failure,
                            }
                            blocked.append(app_id)
            elif len(staged) == 1:
                app_id, st = next(iter(staged.items()))
                kwargs = {'skip_e2e': False, f'{app_id}_cli': st['cli']}
                solo = contract_check(**kwargs)
                if solo.get('status') == 'passed':
                    approved[app_id] = (st, solo)
                else:
                    failure = _summarize_contract_failure(app_id, solo)
                    apps[app_id]['action'] = 'blocked_contract_failed'
                    apps[app_id]['failure_summary'] = failure
                    failed_versions[app_id] = {
                        'tag': st['tag'],
                        'version': st['version'],
                        'adapter_fingerprint': fingerprint,
                        'failed_at': checked_at,
                        'summary': failure,
                    }
                    blocked.append(app_id)

            for app_id, (st, report) in approved.items():
                target_dir = tools_root / f'{app_id}-{st["version"]}'
                prev_owner = _find_owning_dir(tools_root, apps[app_id].get('local_path'))
                try:
                    _promote_staged_dir(st['stage_dir'], target_dir)
                    new_cli = _find_staged_cli(target_dir, app_id)
                    installed_ver = software_detection._query_cli_version(new_cli)
                    if not custom_tools and not os.environ.get(f'OPENEDIT_{app_id.upper()}_CLI'):
                        resolved_cli = Path(software_detection.find_craft_cli(app_id)).resolve()
                        if not resolved_cli.is_relative_to(target_dir.resolve()):
                            raise RuntimeError(f'新安装目录未成为默认解析入口: {resolved_cli}')
                    protected = {target_dir}
                    if keep_versions >= 2 and prev_owner is not None:
                        protected.add(prev_owner)
                    kept, removed, p_warns = _prune_old_versions(tools_root, app_id, keep_versions, protected)
                    prune_warnings.extend(p_warns)
                    verified_versions[app_id] = {
                        'version': st['version'],
                        'tag': st['tag'],
                        'verified_at': checked_at,
                        'adapter_fingerprint': fingerprint,
                        'zip_sha256': st['sha256'],
                        'cli_version_output': installed_ver,
                    }
                    failed_versions.pop(app_id, None)
                    apps[app_id].update({
                        'action': 'upgraded',
                        'installed_path': new_cli,
                        'zip_sha256': st['sha256'],
                        'contract_e2e_seconds': (report.get('e2e') or {}).get('elapsed_seconds'),
                        'kept_versions': kept,
                        'removed_versions': removed,
                    })
                    upgraded.append(app_id)
                except (OSError, ValueError, RuntimeError) as exc:
                    apps[app_id]['action'] = 'error_install'
                    apps[app_id]['error'] = str(exc)

    for app_id, item in apps.items():
        if item.get('action') in ('skipped_known_failure', 'error_missing_asset', 'error_checksum', 'error_download', 'error_install'):
            if app_id not in blocked:
                blocked.append(app_id)

    any_fetch_error = any(item.get('action') == 'fetch_error' for item in apps.values())
    any_deferred = any(item.get('action') == 'deferred_cooldown' for item in apps.values())
    any_would = any(item.get('action') == 'would_upgrade' for item in apps.values())

    if fetch_ok_count == 0:
        status = 'failed'
    elif dry_run:
        if any_would:
            status = 'dry_run_updates_available'
        elif blocked:
            status = 'blocked'
        elif any_deferred:
            status = 'deferred'
        else:
            status = 'up_to_date'
    elif upgraded and (blocked or any_fetch_error):
        status = 'partial'
    elif upgraded:
        status = 'upgraded'
    elif blocked:
        status = 'blocked'
    elif any_deferred:
        status = 'deferred'
    elif any_fetch_error:
        status = 'partial'
    else:
        status = 'up_to_date'

    state_saved = False
    if not dry_run and fetch_ok_count > 0:
        _write_json_atomic(resolved_state, {
            'schema_version': 1,
            'checked_at': checked_at,
            'last_seen_tags': prev_tags,
            'verified_versions': verified_versions,
            'failed_versions': failed_versions,
            'repos': prev_repos,
            'last_upgrade_summary': {
                'status': status,
                'upgraded': upgraded,
                'blocked': blocked,
            },
        })
        state_saved = True

    rollback_versions = {
        app_id: [p.name for p in _list_installed_dirs(tools_root, app_id)]
        for app_id, _ in TRACKED_REPOS
    }
    return {
        'status': status,
        'dry_run': dry_run,
        'checked_at': checked_at,
        'tools_dir': str(tools_root),
        'upgraded': upgraded,
        'blocked': blocked,
        'apps': apps,
        'rollback_versions': rollback_versions,
        'prune_warnings': prune_warnings,
        'state': {
            'path': str(resolved_state),
            'saved': state_saved,
            'warning': state_warning,
        },
    }
