"""Headless HyperFrames (Apache-2.0) backend: offline data-motion recipes, custom HTML, RGBA PNG, and ProRes 4444 overlays."""
import hashlib
import html as html_lib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time

from PIL import Image

import effectcraft_backend
import file_hash
import runtime_core as core
import runtime_paths

RECIPES = (
    'kpi_bar_chart',
    'kpi_counter_banner',
    'comparison_table',
    'milestone_progress',
    'viewfinder_hud',
    'polaroid_grid',
    'filmstrip_reel',
    'text_matte_dissolve',
    'ui_flow_stage',
    'kinetic_ui_title',
)

STYLE_THEMES = {
    'editorial_scrapbook': {
        'accent_color': '#A855F7',
        'secondary_color': '#F43F5E',
        'highlight_color': '#E9D5FF',
        'bg_color': '#0D0B16',
        'frame_color': '#F8FAFC',
        'ink_color': '#111827',
    },
    'neon_cyber': {
        'accent_color': '#22D3EE',
        'secondary_color': '#22C55E',
        'highlight_color': '#FDE047',
        'bg_color': '#050811',
        'frame_color': '#E2E8F0',
        'ink_color': '#090D16',
    },
    'cinema_clean': {
        'accent_color': '#38BDF8',
        'secondary_color': '#F59E0B',
        'highlight_color': '#F8FAFC',
        'bg_color': '#0F172A',
        'frame_color': '#FFFFFF',
        'ink_color': '#0F172A',
    },
}

_ALLOWED_IMAGE_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'})
_ALLOWED_VIDEO_EXTS = frozenset({'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'})

HYPERFRAMES_ONLY_KEYS = frozenset({'html', 'variables', 'check', 'allow_network', 'font_path'})
EFFECTCRAFT_ONLY_KEYS = frozenset({'layers', 'script', 'effects', 'motion_blur'})

_HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
_EXTERNAL_URL_RE = re.compile(
    r'(?:<(?:script|img|video|audio|source|iframe)\b[^>]*\bsrc\s*=\s*["\']\s*https?://'
    r'|<link\b[^>]*\bhref\s*=\s*["\']\s*https?://'
    r'|@import\s+(?:url\s*\(\s*)?["\']?\s*https?://'
    r'|\burl\s*\(\s*["\']?\s*https?://'
    r'|\bfetch\s*\(\s*["\']\s*https?://)',
    re.IGNORECASE,
)

_PRIVACY_ENV = {
    'HYPERFRAMES_NO_TELEMETRY': '1',
    'DO_NOT_TRACK': '1',
    'HYPERFRAMES_NO_UPDATE_CHECK': '1',
    'HYPERFRAMES_SKIP_SKILLS': '1',
    'NO_COLOR': '1',
    'FORCE_COLOR': '0',
}


def list_recipes():
    """Return supported HyperFrames data-motion recipe names."""
    return list(RECIPES)


def find_cli():
    """Locate HyperFrames CLI executable or .mjs entrypoint without starting any GUI."""
    return runtime_paths.discover_hyperframes_cli()


def _node_executable():
    node = shutil.which('node')
    if not node:
        raise ValueError('Node.js (>=22) is required to run HyperFrames; install Node.js and ensure node is on PATH')
    return node


def _cli_command(cli_path):
    """Build executable prefix, preferring direct `node hyperframes.mjs` over .cmd wrappers on Windows."""
    cli = Path(cli_path).resolve()
    if cli.suffix.lower() == '.mjs':
        return [_node_executable(), str(cli)]
    if cli.suffix.lower() in ('.cmd', '.bat'):
        # Avoid cmd.exe quote mangling on JSON --variables by invoking the sibling .mjs directly.
        sibling_mjs = cli.parent.parent / 'hyperframes' / 'bin' / 'hyperframes.mjs'
        if sibling_mjs.is_file():
            return [_node_executable(), str(sibling_mjs.resolve())]
    return [str(cli)]


def _has_cached_headless_shell():
    cache_chrome = Path.home() / '.cache' / 'hyperframes' / 'chrome'
    if not cache_chrome.is_dir():
        return False
    for pattern in ('chrome-headless-shell*', 'chrome.exe', 'chrome'):
        if any(cache_chrome.rglob(pattern)):
            return True
    return False


def _subprocess_env():
    env = dict(os.environ)
    env.update(_PRIVACY_ENV)
    if not env.get('HYPERFRAMES_BROWSER_PATH') and not _has_cached_headless_shell():
        browser = runtime_paths.discover_chromium_browser()
        if browser is not None:
            env['HYPERFRAMES_BROWSER_PATH'] = str(browser)
    return env


def _link_or_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _check_hex(value, label, default=None):
    if value is None:
        return default
    if not isinstance(value, str) or not _HEX_COLOR_RE.fullmatch(value.strip()):
        raise ValueError(f'{label} must be a #RRGGBB hex color')
    return value.strip().upper()


def _check_num(value, label, low, high, default=None):
    if value is None:
        return default
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    val = float(value)
    if val < low or val > high:
        raise ValueError(f'{label} must be within [{low}, {high}]')
    return val


def _check_int(value, label, low, high, default=None):
    if value is None:
        return default
    if type(value) is not int:
        raise ValueError(f'{label} must be an integer')
    if value < low or value > high:
        raise ValueError(f'{label} must be within [{low}, {high}]')
    return value


def _check_bool(value, label, default=False):
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError(f'{label} must be a boolean')
    return value


def _check_text(value, label, max_len=160, required=False, default=''):
    if value is None:
        if required:
            raise ValueError(f'{label} is required')
        return default
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string')
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f'{label} must be non-empty')
    if len(cleaned) > max_len:
        raise ValueError(f'{label} exceeds max length {max_len}')
    return cleaned


def _hex_to_rgba_css(hex_color, opacity):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'rgba({r}, {g}, {b}, {round(float(opacity), 3)})'


def _check_offline_html_text(text, label, allow_network):
    if allow_network:
        return
    m = _EXTERNAL_URL_RE.search(text)
    if m:
        raise ValueError(
            f'{label} contains external HTTP(S) asset reference ({m.group(0)[:64]}); '
            'use local relative assets or set allow_network=true explicitly'
        )


def _check_theme(p, label):
    theme_name = p.get('theme')
    if theme_name is None:
        return None, {}
    name = _check_text(theme_name, f'{label}.params.theme', 32, required=True).lower()
    if name not in STYLE_THEMES:
        raise ValueError(f'{label}.params.theme must be one of {tuple(STYLE_THEMES)}')
    return name, STYLE_THEMES[name]


def _check_local_image(raw_path, label, base_dir=None):
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip() or '://' in raw_path or '\0' in raw_path:
        raise ValueError(f'{label} must be a local image file path')
    cand = Path(os.path.expanduser(os.path.expandvars(raw_path.strip())))
    if not cand.is_absolute() and base_dir is not None:
        cand = (Path(base_dir) / cand).resolve()
    else:
        cand = cand.resolve()
    if not cand.is_file() or cand.suffix.lower() not in _ALLOWED_IMAGE_EXTS:
        raise ValueError(f'{label} image file not found or unsupported format: {cand}')
    return str(cand)


def _check_local_video(raw_path, label, base_dir=None):
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip() or '://' in raw_path or '\0' in raw_path:
        raise ValueError(f'{label} must be a local video file path')
    cand = Path(os.path.expanduser(os.path.expandvars(raw_path.strip())))
    if not cand.is_absolute() and base_dir is not None:
        cand = (Path(base_dir) / cand).resolve()
    else:
        cand = cand.resolve()
    if not cand.is_file() or cand.suffix.lower() not in _ALLOWED_VIDEO_EXTS:
        raise ValueError(f'{label} video file not found or unsupported format: {cand}')
    return str(cand)


def _collect_recipe_media(recipe, params):
    if not isinstance(params, dict):
        return []
    found = []
    if recipe in ('polaroid_grid', 'ui_flow_stage'):
        for idx, card in enumerate(params.get('cards') or []):
            if not isinstance(card, dict):
                continue
            vid = card.get('video')
            img = card.get('image')
            if isinstance(vid, str) and vid.strip():
                found.append({
                    'slot': f'card_{idx}', 'path': vid, 'kind': 'video',
                    'source_in': float(card.get('source_in') or 0.0),
                })
            elif isinstance(img, str) and img.strip():
                found.append({'slot': f'card_{idx}', 'path': img, 'kind': 'image', 'source_in': 0.0})
    elif recipe == 'filmstrip_reel':
        for idx, fr in enumerate(params.get('frames') or []):
            if not isinstance(fr, dict):
                continue
            vid = fr.get('video')
            img = fr.get('image')
            if isinstance(vid, str) and vid.strip():
                found.append({
                    'slot': f'frame_{idx}', 'path': vid, 'kind': 'video',
                    'source_in': float(fr.get('source_in') or 0.0),
                })
            elif isinstance(img, str) and img.strip():
                found.append({'slot': f'frame_{idx}', 'path': img, 'kind': 'image', 'source_in': 0.0})
    elif recipe == 'text_matte_dissolve':
        vid = params.get('fill_video')
        img = params.get('fill_image')
        if isinstance(vid, str) and vid.strip():
            found.append({
                'slot': 'fill', 'path': vid, 'kind': 'video',
                'source_in': float(params.get('source_in') or 0.0),
            })
        elif isinstance(img, str) and img.strip():
            found.append({'slot': 'fill', 'path': img, 'kind': 'image', 'source_in': 0.0})
    return found


def _collect_recipe_images(recipe, params):
    return [entry['path'] for entry in _collect_recipe_media(recipe, params)]


def iter_recipe_media_paths(spec):
    """Yield local image or video paths referenced by a HyperFrames recipe spec for provenance & cache hashing."""
    if not isinstance(spec, dict):
        return
    yield from _collect_recipe_images(spec.get('recipe'), spec.get('params'))


def _recipe_asset_map(recipe, params):
    entries = _collect_recipe_media(recipe, params)
    slot_urls = {}
    staged = []
    seen_imgs = {}
    counter = 0
    for entry in entries:
        src = entry['path']
        if entry['kind'] == 'image':
            fname = seen_imgs.get(src)
            if fname is None:
                ext = Path(src).suffix.lower() or '.png'
                fname = f'media_{counter}{ext}'
                counter += 1
                seen_imgs[src] = fname
                staged.append({**entry, 'dst_name': fname})
            slot_urls[entry['slot']] = f'../assets/{fname}'
        else:
            fname = f'media_{counter}.mp4'
            counter += 1
            staged.append({**entry, 'dst_name': fname})
            slot_urls[entry['slot']] = f'../assets/{fname}'
    return slot_urls, staged


def _validate_recipe_params(recipe, raw_params, label, base_dir=None):
    if recipe not in RECIPES:
        raise ValueError(f'{label}.recipe must be one of {RECIPES}')
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise ValueError(f'{label}.params must be an object')
    p = dict(raw_params)

    if recipe == 'kpi_bar_chart':
        allowed = {
            'title', 'subtitle', 'badge', 'period', 'kpi_label', 'kpi_target',
            'kpi_prefix', 'kpi_suffix', 'kpi_decimals', 'pill_text', 'note',
            'bars', 'show_trend', 'position', 'accent_color', 'highlight_color',
            'bg_color', 'bg_opacity',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for kpi_bar_chart: {sorted(unknown)}')
        title = _check_text(p.get('title'), f'{label}.params.title', 80, required=True)
        subtitle = _check_text(p.get('subtitle'), f'{label}.params.subtitle', 120, default='')
        badge = _check_text(p.get('badge'), f'{label}.params.badge', 40, default='数据实测')
        period = _check_text(p.get('period'), f'{label}.params.period', 40, default='')
        kpi_label = _check_text(p.get('kpi_label'), f'{label}.params.kpi_label', 48, default='核心指标提升')
        kpi_target = _check_num(p.get('kpi_target'), f'{label}.params.kpi_target', -1e9, 1e9)
        if kpi_target is None:
            raise ValueError(f'{label}.params.kpi_target is required')
        kpi_prefix = _check_text(p.get('kpi_prefix'), f'{label}.params.kpi_prefix', 8, default='+')
        kpi_suffix = _check_text(p.get('kpi_suffix'), f'{label}.params.kpi_suffix', 12, default='%')
        kpi_decimals = _check_int(p.get('kpi_decimals'), f'{label}.params.kpi_decimals', 0, 3, default=0)
        pill_text = _check_text(p.get('pill_text'), f'{label}.params.pill_text', 48, default='')
        note = _check_text(p.get('note'), f'{label}.params.note', 64, default='')
        bars_raw = p.get('bars')
        if not isinstance(bars_raw, list) or not (2 <= len(bars_raw) <= 6):
            raise ValueError(f'{label}.params.bars must contain 2..6 bar objects')
        bars = []
        for idx, b in enumerate(bars_raw):
            if not isinstance(b, dict):
                raise ValueError(f'{label}.params.bars[{idx}] must be an object')
            b_unknown = set(b) - {'label', 'value', 'display', 'highlight'}
            if b_unknown:
                raise ValueError(f'{label}.params.bars[{idx}] unsupported fields: {sorted(b_unknown)}')
            blabel = _check_text(b.get('label'), f'{label}.params.bars[{idx}].label', 24, required=True)
            bval = _check_num(b.get('value'), f'{label}.params.bars[{idx}].value', 0.0001, 1e9)
            if bval is None:
                raise ValueError(f'{label}.params.bars[{idx}].value is required')
            bdisp = _check_text(
                b.get('display'),
                f'{label}.params.bars[{idx}].display',
                24,
                default=f'{int(round(bval))}' if abs(bval - round(bval)) < 1e-6 else f'{bval:.1f}',
            )
            bhl = _check_bool(b.get('highlight'), f'{label}.params.bars[{idx}].highlight', default=False)
            bars.append({'label': blabel, 'value': bval, 'display': bdisp, 'highlight': bhl})
        if not any(item['highlight'] for item in bars):
            bars[-1]['highlight'] = True
        pos = _check_text(p.get('position'), f'{label}.params.position', 16, default='right').lower()
        if pos not in ('right', 'left', 'center'):
            raise ValueError(f'{label}.params.position must be right, left, or center')
        return {
            'title': title,
            'subtitle': subtitle,
            'badge': badge,
            'period': period,
            'kpi_label': kpi_label,
            'kpi_target': kpi_target,
            'kpi_prefix': kpi_prefix,
            'kpi_suffix': kpi_suffix,
            'kpi_decimals': kpi_decimals,
            'pill_text': pill_text,
            'note': note,
            'bars': bars,
            'show_trend': _check_bool(p.get('show_trend'), f'{label}.params.show_trend', default=True),
            'position': pos,
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', '#38BDF8'),
            'highlight_color': _check_hex(p.get('highlight_color'), f'{label}.params.highlight_color', '#FACC15'),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', '#0B1222'),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.4, 1.0, default=0.94),
        }

    if recipe == 'kpi_counter_banner':
        allowed = {
            'title', 'badge', 'metrics', 'position', 'accent_color',
            'highlight_color', 'bg_color', 'bg_opacity',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for kpi_counter_banner: {sorted(unknown)}')
        title = _check_text(p.get('title'), f'{label}.params.title', 80, default='核心业务指标')
        badge = _check_text(p.get('badge'), f'{label}.params.badge', 40, default='实时战报')
        m_raw = p.get('metrics')
        if not isinstance(m_raw, list) or not (1 <= len(m_raw) <= 4):
            raise ValueError(f'{label}.params.metrics must contain 1..4 metric objects')
        metrics = []
        for idx, m in enumerate(m_raw):
            if not isinstance(m, dict):
                raise ValueError(f'{label}.params.metrics[{idx}] must be an object')
            m_unknown = set(m) - {'label', 'target', 'prefix', 'suffix', 'decimals', 'accent'}
            if m_unknown:
                raise ValueError(f'{label}.params.metrics[{idx}] unsupported fields: {sorted(m_unknown)}')
            mlabel = _check_text(m.get('label'), f'{label}.params.metrics[{idx}].label', 32, required=True)
            mtarget = _check_num(m.get('target'), f'{label}.params.metrics[{idx}].target', -1e9, 1e9)
            if mtarget is None:
                raise ValueError(f'{label}.params.metrics[{idx}].target is required')
            metrics.append({
                'label': mlabel,
                'target': mtarget,
                'prefix': _check_text(m.get('prefix'), f'{label}.params.metrics[{idx}].prefix', 8, default=''),
                'suffix': _check_text(m.get('suffix'), f'{label}.params.metrics[{idx}].suffix', 12, default=''),
                'decimals': _check_int(m.get('decimals'), f'{label}.params.metrics[{idx}].decimals', 0, 3, default=0),
                'accent': _check_bool(m.get('accent'), f'{label}.params.metrics[{idx}].accent', default=(idx == 0)),
            })
        pos = _check_text(p.get('position'), f'{label}.params.position', 16, default='bottom').lower()
        if pos not in ('bottom', 'center', 'top'):
            raise ValueError(f'{label}.params.position must be bottom, center, or top')
        return {
            'title': title,
            'badge': badge,
            'metrics': metrics,
            'position': pos,
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', '#38BDF8'),
            'highlight_color': _check_hex(p.get('highlight_color'), f'{label}.params.highlight_color', '#FACC15'),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', '#0B1222'),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.4, 1.0, default=0.94),
        }

    if recipe == 'comparison_table':
        allowed = {
            'title', 'subtitle', 'badge', 'left_header', 'right_header', 'rows',
            'position', 'accent_color', 'highlight_color', 'bg_color', 'bg_opacity',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for comparison_table: {sorted(unknown)}')
        title = _check_text(p.get('title'), f'{label}.params.title', 80, required=True)
        subtitle = _check_text(p.get('subtitle'), f'{label}.params.subtitle', 120, default='')
        badge = _check_text(p.get('badge'), f'{label}.params.badge', 40, default='方案对比')
        left_header = _check_text(p.get('left_header'), f'{label}.params.left_header', 32, default='传统方案')
        right_header = _check_text(p.get('right_header'), f'{label}.params.right_header', 32, default='升级方案')
        rows_raw = p.get('rows')
        if not isinstance(rows_raw, list) or not (2 <= len(rows_raw) <= 5):
            raise ValueError(f'{label}.params.rows must contain 2..5 row objects')
        rows = []
        for idx, r in enumerate(rows_raw):
            if not isinstance(r, dict):
                raise ValueError(f'{label}.params.rows[{idx}] must be an object')
            r_unknown = set(r) - {'label', 'left', 'right'}
            if r_unknown:
                raise ValueError(f'{label}.params.rows[{idx}] unsupported fields: {sorted(r_unknown)}')
            rows.append({
                'label': _check_text(r.get('label'), f'{label}.params.rows[{idx}].label', 32, required=True),
                'left': _check_text(r.get('left'), f'{label}.params.rows[{idx}].left', 40, required=True),
                'right': _check_text(r.get('right'), f'{label}.params.rows[{idx}].right', 40, required=True),
            })
        pos = _check_text(p.get('position'), f'{label}.params.position', 16, default='center').lower()
        if pos not in ('center', 'right', 'left'):
            raise ValueError(f'{label}.params.position must be center, right, or left')
        return {
            'title': title,
            'subtitle': subtitle,
            'badge': badge,
            'left_header': left_header,
            'right_header': right_header,
            'rows': rows,
            'position': pos,
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', '#38BDF8'),
            'highlight_color': _check_hex(p.get('highlight_color'), f'{label}.params.highlight_color', '#4ADE80'),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', '#0B1222'),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.4, 1.0, default=0.94),
        }

    if recipe == 'milestone_progress':
        allowed = {
            'title', 'badge', 'steps', 'position', 'accent_color',
            'highlight_color', 'bg_color', 'bg_opacity',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for milestone_progress: {sorted(unknown)}')
        title = _check_text(p.get('title'), f'{label}.params.title', 80, required=True)
        badge = _check_text(p.get('badge'), f'{label}.params.badge', 40, default='阶段达成')
        steps_raw = p.get('steps')
        if not isinstance(steps_raw, list) or not (2 <= len(steps_raw) <= 5):
            raise ValueError(f'{label}.params.steps must contain 2..5 step objects')
        steps = []
        for idx, st in enumerate(steps_raw):
            if not isinstance(st, dict):
                raise ValueError(f'{label}.params.steps[{idx}] must be an object')
            st_unknown = set(st) - {'label', 'value', 'percent'}
            if st_unknown:
                raise ValueError(f'{label}.params.steps[{idx}] unsupported fields: {sorted(st_unknown)}')
            pct = _check_num(st.get('percent'), f'{label}.params.steps[{idx}].percent', 1, 100)
            if pct is None:
                raise ValueError(f'{label}.params.steps[{idx}].percent is required')
            steps.append({
                'label': _check_text(st.get('label'), f'{label}.params.steps[{idx}].label', 32, required=True),
                'value': _check_text(st.get('value'), f'{label}.params.steps[{idx}].value', 24, required=True),
                'percent': pct,
            })
        pos = _check_text(p.get('position'), f'{label}.params.position', 16, default='right').lower()
        if pos not in ('right', 'left', 'center'):
            raise ValueError(f'{label}.params.position must be right, left, or center')
        return {
            'title': title,
            'badge': badge,
            'steps': steps,
            'position': pos,
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', '#38BDF8'),
            'highlight_color': _check_hex(p.get('highlight_color'), f'{label}.params.highlight_color', '#FACC15'),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', '#0B1222'),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.4, 1.0, default=0.94),
        }

    theme_name, tkn = _check_theme(p, label)

    if recipe == 'viewfinder_hud':
        allowed = {
            'theme', 'rec_label', 'show_rec', 'blink_hz', 'timecode_fps', 'start_seconds',
            'top_right', 'bottom_left', 'corner_color', 'rec_color', 'accent_color',
            'pill_opacity', 'show_crosshair', 'margin_px',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for viewfinder_hud: {sorted(unknown)}')
        return {
            'theme': theme_name,
            'rec_label': _check_text(p.get('rec_label'), f'{label}.params.rec_label', 24, default='REC'),
            'show_rec': _check_bool(p.get('show_rec'), f'{label}.params.show_rec', default=True),
            'blink_hz': _check_num(p.get('blink_hz'), f'{label}.params.blink_hz', 0.2, 8.0, default=1.0),
            'timecode_fps': _check_int(p.get('timecode_fps'), f'{label}.params.timecode_fps', 1, 120, default=30),
            'start_seconds': _check_num(p.get('start_seconds'), f'{label}.params.start_seconds', 0.0, 86400.0, default=0.0),
            'top_right': _check_text(p.get('top_right'), f'{label}.params.top_right', 40, default='4:3 · ISO 800'),
            'bottom_left': _check_text(p.get('bottom_left'), f'{label}.params.bottom_left', 40, default='CAM A'),
            'corner_color': _check_hex(p.get('corner_color'), f'{label}.params.corner_color', tkn.get('frame_color', '#FFFFFF')),
            'rec_color': _check_hex(p.get('rec_color'), f'{label}.params.rec_color', '#EF4444'),
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'pill_opacity': _check_num(p.get('pill_opacity'), f'{label}.params.pill_opacity', 0.0, 1.0, default=0.68),
            'show_crosshair': _check_bool(p.get('show_crosshair'), f'{label}.params.show_crosshair', default=True),
            'margin_px': _check_int(p.get('margin_px'), f'{label}.params.margin_px', 16, 140, default=42),
        }

    if recipe == 'polaroid_grid':
        allowed = {
            'theme', 'cards', 'highlight_index', 'circle_color', 'frame_color',
            'ink_color', 'accent_color', 'layout', 'stagger',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for polaroid_grid: {sorted(unknown)}')
        cards_raw = p.get('cards')
        if not isinstance(cards_raw, list) or not (2 <= len(cards_raw) <= 4):
            raise ValueError(f'{label}.params.cards must contain 2..4 card objects')
        cards = []
        default_tilts = (-4.5, 3.0, -2.5, 4.0)
        for idx, card in enumerate(cards_raw):
            if not isinstance(card, dict):
                raise ValueError(f'{label}.params.cards[{idx}] must be an object')
            c_unknown = set(card) - {'label', 'sublabel', 'image', 'video', 'source_in', 'tilt_deg'}
            if c_unknown:
                raise ValueError(f'{label}.params.cards[{idx}] unsupported fields: {sorted(c_unknown)}')
            if card.get('image') is not None and card.get('video') is not None:
                raise ValueError(f'{label}.params.cards[{idx}] cannot specify both image and video')
            cards.append({
                'label': _check_text(card.get('label'), f'{label}.params.cards[{idx}].label', 24, required=True),
                'sublabel': _check_text(card.get('sublabel'), f'{label}.params.cards[{idx}].sublabel', 28, default=''),
                'image': _check_local_image(card.get('image'), f'{label}.params.cards[{idx}].image', base_dir=base_dir),
                'video': _check_local_video(card.get('video'), f'{label}.params.cards[{idx}].video', base_dir=base_dir),
                'source_in': _check_num(card.get('source_in'), f'{label}.params.cards[{idx}].source_in', 0.0, 86400.0, default=0.0),
                'tilt_deg': _check_num(card.get('tilt_deg'), f'{label}.params.cards[{idx}].tilt_deg', -18.0, 18.0, default=default_tilts[idx]),
            })
        hl_idx = _check_int(p.get('highlight_index'), f'{label}.params.highlight_index', 0, len(cards) - 1, default=None)
        layout = _check_text(p.get('layout'), f'{label}.params.layout', 16, default='auto').lower()
        if layout not in ('auto', 'row', 'grid_2x2'):
            raise ValueError(f'{label}.params.layout must be auto, row, or grid_2x2')
        return {
            'theme': theme_name,
            'cards': cards,
            'highlight_index': hl_idx,
            'circle_color': _check_hex(p.get('circle_color'), f'{label}.params.circle_color', tkn.get('secondary_color', '#EF4444')),
            'frame_color': _check_hex(p.get('frame_color'), f'{label}.params.frame_color', tkn.get('frame_color', '#F8FAFC')),
            'ink_color': _check_hex(p.get('ink_color'), f'{label}.params.ink_color', tkn.get('ink_color', '#111827')),
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'layout': layout,
            'stagger': _check_num(p.get('stagger'), f'{label}.params.stagger', 0.0, 0.6, default=0.14),
        }

    if recipe == 'filmstrip_reel':
        allowed = {
            'theme', 'frames', 'top_code', 'bottom_code', 'accent_color',
            'strip_color', 'height_ratio', 'position_y', 'pulse_beats',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for filmstrip_reel: {sorted(unknown)}')
        frames_raw = p.get('frames')
        if not isinstance(frames_raw, list) or not (2 <= len(frames_raw) <= 8):
            raise ValueError(f'{label}.params.frames must contain 2..8 frame objects')
        frames = []
        for idx, fr in enumerate(frames_raw):
            if not isinstance(fr, dict):
                raise ValueError(f'{label}.params.frames[{idx}] must be an object')
            f_unknown = set(fr) - {'label', 'image', 'video', 'source_in'}
            if f_unknown:
                raise ValueError(f'{label}.params.frames[{idx}] unsupported fields: {sorted(f_unknown)}')
            if fr.get('image') is not None and fr.get('video') is not None:
                raise ValueError(f'{label}.params.frames[{idx}] cannot specify both image and video')
            frames.append({
                'label': _check_text(fr.get('label'), f'{label}.params.frames[{idx}].label', 24, default=f'{idx + 1}A'),
                'image': _check_local_image(fr.get('image'), f'{label}.params.frames[{idx}].image', base_dir=base_dir),
                'video': _check_local_video(fr.get('video'), f'{label}.params.frames[{idx}].video', base_dir=base_dir),
                'source_in': _check_num(fr.get('source_in'), f'{label}.params.frames[{idx}].source_in', 0.0, 86400.0, default=0.0),
            })
        return {
            'theme': theme_name,
            'frames': frames,
            'top_code': _check_text(p.get('top_code'), f'{label}.params.top_code', 48, default='RE:FRAME 400 · 35MM'),
            'bottom_code': _check_text(p.get('bottom_code'), f'{label}.params.bottom_code', 48, default='ARCHIVE REEL'),
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'strip_color': _check_hex(p.get('strip_color'), f'{label}.params.strip_color', '#0B0D14'),
            'height_ratio': _check_num(p.get('height_ratio'), f'{label}.params.height_ratio', 0.30, 0.85, default=0.56),
            'position_y': _check_num(p.get('position_y'), f'{label}.params.position_y', 0.2, 0.8, default=0.5),
            'pulse_beats': _check_int(p.get('pulse_beats'), f'{label}.params.pulse_beats', 1, 16, default=4),
        }

    if recipe == 'text_matte_dissolve':
        allowed = {
            'theme', 'text', 'subtitle', 'fill_image', 'fill_video', 'source_in', 'accent_color', 'secondary_color',
            'text_color', 'glow', 'dissolve_start', 'dissolve_direction',
            'particle_count', 'font_size',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for text_matte_dissolve: {sorted(unknown)}')
        if p.get('fill_image') is not None and p.get('fill_video') is not None:
            raise ValueError(f'{label}.params cannot specify both fill_image and fill_video')
        direction = _check_text(p.get('dissolve_direction'), f'{label}.params.dissolve_direction', 16, default='right').lower()
        if direction not in ('right', 'left', 'up'):
            raise ValueError(f'{label}.params.dissolve_direction must be right, left, or up')
        return {
            'theme': theme_name,
            'text': _check_text(p.get('text'), f'{label}.params.text', 64, required=True),
            'subtitle': _check_text(p.get('subtitle'), f'{label}.params.subtitle', 120, default=''),
            'fill_image': _check_local_image(p.get('fill_image'), f'{label}.params.fill_image', base_dir=base_dir),
            'fill_video': _check_local_video(p.get('fill_video'), f'{label}.params.fill_video', base_dir=base_dir),
            'source_in': _check_num(p.get('source_in'), f'{label}.params.source_in', 0.0, 86400.0, default=0.0),
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'secondary_color': _check_hex(p.get('secondary_color'), f'{label}.params.secondary_color', tkn.get('secondary_color', '#F43F5E')),
            'text_color': _check_hex(p.get('text_color'), f'{label}.params.text_color', tkn.get('highlight_color', '#F8FAFC')),
            'glow': _check_bool(p.get('glow'), f'{label}.params.glow', default=True),
            'dissolve_start': _check_num(p.get('dissolve_start'), f'{label}.params.dissolve_start', 0.1, 0.85, default=0.45),
            'dissolve_direction': direction,
            'particle_count': _check_int(p.get('particle_count'), f'{label}.params.particle_count', 40, 500, default=180),
            'font_size': _check_int(p.get('font_size'), f'{label}.params.font_size', 64, 280, default=168),
        }

    if recipe == 'ui_flow_stage':
        allowed = {
            'theme', 'cards', 'focus_index', 'states', 'spring_stiffness', 'spring_damping',
            'spring_mass', 'camera_pan_x', 'camera_zoom', 'camera_tilt_deg',
            'motion_blur_strength', 'header_label', 'accent_color', 'secondary_color',
            'frame_color', 'ink_color', 'bg_color', 'bg_opacity',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for ui_flow_stage: {sorted(unknown)}')
        cards_raw = p.get('cards')
        if not isinstance(cards_raw, list) or not (2 <= len(cards_raw) <= 4):
            raise ValueError(f'{label}.params.cards must contain 2..4 card objects')
        cards = []
        for idx, card in enumerate(cards_raw):
            if not isinstance(card, dict):
                raise ValueError(f'{label}.params.cards[{idx}] must be an object')
            c_unknown = set(card) - {'title', 'subtitle', 'tag', 'image', 'video', 'source_in', 'accent_color'}
            if c_unknown:
                raise ValueError(f'{label}.params.cards[{idx}] unsupported fields: {sorted(c_unknown)}')
            if card.get('image') is not None and card.get('video') is not None:
                raise ValueError(f'{label}.params.cards[{idx}] cannot specify both image and video')
            cards.append({
                'title': _check_text(card.get('title'), f'{label}.params.cards[{idx}].title', 48, required=True),
                'subtitle': _check_text(card.get('subtitle'), f'{label}.params.cards[{idx}].subtitle', 72, default=''),
                'tag': _check_text(card.get('tag'), f'{label}.params.cards[{idx}].tag', 24, default=f'0{idx + 1}'),
                'image': _check_local_image(card.get('image'), f'{label}.params.cards[{idx}].image', base_dir=base_dir),
                'video': _check_local_video(card.get('video'), f'{label}.params.cards[{idx}].video', base_dir=base_dir),
                'source_in': _check_num(card.get('source_in'), f'{label}.params.cards[{idx}].source_in', 0.0, 86400.0, default=0.0),
                'accent_color': _check_hex(card.get('accent_color'), f'{label}.params.cards[{idx}].accent_color', tkn.get('accent_color', '#A855F7')),
            })
        raw_states = p.get('states')
        if raw_states is None:
            states = ['overview', 'focus', 'hero_zoom']
        else:
            if not isinstance(raw_states, list) or not (2 <= len(raw_states) <= 4):
                raise ValueError(f'{label}.params.states must contain 2..4 state names')
            states = []
            for s_idx, st_val in enumerate(raw_states):
                st_name = _check_text(st_val, f'{label}.params.states[{s_idx}]', 24, required=True).lower()
                if st_name not in ('overview', 'focus', 'split', 'hero_zoom'):
                    raise ValueError(f'{label}.params.states[{s_idx}] must be overview, focus, split, or hero_zoom')
                states.append(st_name)
        return {
            'theme': theme_name,
            'cards': cards,
            'focus_index': _check_int(p.get('focus_index'), f'{label}.params.focus_index', 0, len(cards) - 1, default=0),
            'states': states,
            'spring_stiffness': _check_num(p.get('spring_stiffness'), f'{label}.params.spring_stiffness', 40.0, 600.0, default=190.0),
            'spring_damping': _check_num(p.get('spring_damping'), f'{label}.params.spring_damping', 6.0, 60.0, default=20.0),
            'spring_mass': _check_num(p.get('spring_mass'), f'{label}.params.spring_mass', 0.3, 4.0, default=1.0),
            'camera_pan_x': _check_num(p.get('camera_pan_x'), f'{label}.params.camera_pan_x', -300.0, 300.0, default=44.0),
            'camera_zoom': _check_num(p.get('camera_zoom'), f'{label}.params.camera_zoom', 0.8, 1.6, default=1.12),
            'camera_tilt_deg': _check_num(p.get('camera_tilt_deg'), f'{label}.params.camera_tilt_deg', -8.0, 8.0, default=-2.0),
            'motion_blur_strength': _check_num(p.get('motion_blur_strength'), f'{label}.params.motion_blur_strength', 0.0, 2.0, default=0.75),
            'header_label': _check_text(p.get('header_label'), f'{label}.params.header_label', 64, default=''),
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'secondary_color': _check_hex(p.get('secondary_color'), f'{label}.params.secondary_color', tkn.get('secondary_color', '#F43F5E')),
            'frame_color': _check_hex(p.get('frame_color'), f'{label}.params.frame_color', tkn.get('frame_color', '#F8FAFC')),
            'ink_color': _check_hex(p.get('ink_color'), f'{label}.params.ink_color', tkn.get('ink_color', '#111827')),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', tkn.get('bg_color', '#0D0B16')),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.0, 100.0, default=0.0),
        }

    if recipe == 'kinetic_ui_title':
        allowed = {
            'theme', 'title', 'subtitle', 'badge', 'stagger_ms', 'reveal_style',
            'spring_stiffness', 'spring_damping', 'glass_card', 'position_y', 'align',
            'accent_color', 'text_color', 'bg_color', 'bg_opacity', 'font_size',
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f'{label}.params unsupported fields for kinetic_ui_title: {sorted(unknown)}')
        rev = _check_text(p.get('reveal_style'), f'{label}.params.reveal_style', 24, default='blur_scale_y').lower()
        if rev not in ('blur_scale_y', 'clip_wipe', 'spring_card'):
            raise ValueError(f'{label}.params.reveal_style must be blur_scale_y, clip_wipe, or spring_card')
        al = _check_text(p.get('align'), f'{label}.params.align', 16, default='center').lower()
        if al not in ('center', 'left'):
            raise ValueError(f'{label}.params.align must be center or left')
        return {
            'theme': theme_name,
            'title': _check_text(p.get('title'), f'{label}.params.title', 80, required=True),
            'subtitle': _check_text(p.get('subtitle'), f'{label}.params.subtitle', 120, default=''),
            'badge': _check_text(p.get('badge'), f'{label}.params.badge', 32, default=''),
            'stagger_ms': _check_num(p.get('stagger_ms'), f'{label}.params.stagger_ms', 10.0, 120.0, default=32.0),
            'reveal_style': rev,
            'spring_stiffness': _check_num(p.get('spring_stiffness'), f'{label}.params.spring_stiffness', 60.0, 500.0, default=220.0),
            'spring_damping': _check_num(p.get('spring_damping'), f'{label}.params.spring_damping', 8.0, 50.0, default=22.0),
            'glass_card': _check_bool(p.get('glass_card'), f'{label}.params.glass_card', default=True),
            'position_y': _check_num(p.get('position_y'), f'{label}.params.position_y', 0.15, 0.85, default=0.52),
            'align': al,
            'accent_color': _check_hex(p.get('accent_color'), f'{label}.params.accent_color', tkn.get('accent_color', '#A855F7')),
            'text_color': _check_hex(p.get('text_color'), f'{label}.params.text_color', tkn.get('highlight_color', '#F8FAFC')),
            'bg_color': _check_hex(p.get('bg_color'), f'{label}.params.bg_color', tkn.get('bg_color', '#0D0B16')),
            'bg_opacity': _check_num(p.get('bg_opacity'), f'{label}.params.bg_opacity', 0.0, 100.0, default=74.0),
            'font_size': _check_int(p.get('font_size'), f'{label}.params.font_size', 42, 160, default=96),
        }

    raise ValueError(f'Unsupported HyperFrames recipe: {recipe}')


def validate_overlay_spec(ov, base_dir=None, label='overlay'):
    """Validate and normalize a HyperFrames overlay entry in-place / returning normalized copy."""
    if not isinstance(ov, dict):
        raise ValueError(f'{label} must be an object')
    for bad_key in sorted(EFFECTCRAFT_ONLY_KEYS & set(ov)):
        raise ValueError(f'{label}.{bad_key} is only supported with backend="effectcraft"')
    if 'check' in ov and type(ov['check']) is not bool:
        raise ValueError(f'{label}.check must be boolean')
    if 'allow_network' in ov and type(ov['allow_network']) is not bool:
        raise ValueError(f'{label}.allow_network must be boolean')
    if 'variables' in ov and not isinstance(ov['variables'], dict):
        raise ValueError(f'{label}.variables must be an object')

    has_recipe = 'recipe' in ov
    has_html = 'html' in ov
    if has_recipe == has_html:
        raise ValueError(f'{label} with backend="hyperframes" must specify exactly one of recipe or html')

    allow_network = bool(ov.get('allow_network', False))
    if 'font_path' in ov:
        raw_font = ov['font_path']
        if not isinstance(raw_font, str) or not raw_font.strip() or '\0' in raw_font:
            raise ValueError(f'{label}.font_path must be a non-empty file path')
        font_cand = Path(raw_font)
        if not font_cand.is_absolute() and base_dir is not None:
            font_cand = (Path(base_dir) / font_cand).resolve()
        else:
            font_cand = font_cand.resolve()
        if not font_cand.is_file():
            raise ValueError(f'{label}.font_path does not exist: {font_cand}')
        ov['font_path'] = str(font_cand)

    if has_recipe:
        if 'variables' in ov:
            raise ValueError(f'{label}.variables cannot be combined with recipe; use params instead')
        ov['params'] = _validate_recipe_params(ov['recipe'], ov.get('params'), label, base_dir=base_dir)
        return ov

    raw_html = ov['html']
    if not isinstance(raw_html, str) or not raw_html.strip() or '\0' in raw_html:
        raise ValueError(f'{label}.html must be a non-empty file path or HTML string')
    stripped = raw_html.lstrip()
    if stripped.startswith('<'):
        _check_offline_html_text(stripped, f'{label}.html', allow_network)
    else:
        cand = Path(raw_html)
        if not cand.is_absolute() and base_dir is not None:
            cand = (Path(base_dir) / cand).resolve()
        else:
            cand = cand.resolve()
        if not cand.is_file():
            raise ValueError(f'{label}.html file not found: {cand}')
        ov['html'] = str(cand)
        _check_offline_html_text(cand.read_text(encoding='utf-8'), f'{label}.html', allow_network)
        comp_dir = cand.parent / 'compositions'
        if comp_dir.is_dir():
            for sub_html in sorted(comp_dir.rglob('*.html')):
                _check_offline_html_text(sub_html.read_text(encoding='utf-8'), f'{label}.{sub_html.name}', allow_network)
    return ov


def _logical_viewport(width, height):
    """Scale any canvas so the short side is always 1080 logical px, keeping fonts >= 28px and zero clipping."""
    scale = min(float(width), float(height)) / 1080.0
    lw = round(float(width) / scale, 3)
    lh = round(float(height) / scale, 3)
    return scale, lw, lh, (width < height)


def _shell_html(width, height, fps, duration, *, scale, lw, lh, card_id):
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <script src="vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        margin: 0;
        width: {width}px;
        height: {height}px;
        overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      #stage-scale {{
        position: relative;
        width: {lw}px;
        height: {lh}px;
        transform: scale({scale:.6f});
        transform-origin: top left;
        background: transparent;
      }}
      .clip {{
        position: absolute;
        inset: 0;
        width: {lw}px;
        height: {lh}px;
      }}
    </style>
  </head>
  <body>
    <div
      id="root"
      data-composition-id="main"
      data-start="0"
      data-duration="{duration}"
      data-width="{width}"
      data-height="{height}"
      data-fps="{fps}"
    >
      <div id="stage-scale">
        <div
          id="{card_id}-slot"
          class="clip"
          data-composition-id="{card_id}"
          data-composition-src="compositions/card.html"
          data-start="0"
          data-duration="{duration}"
          data-track-index="0"
        ></div>
      </div>
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["main"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_kpi_bar_chart_card(p, *, lw, lh, portrait, duration):
    bg_rgba = _hex_to_rgba_css(p['bg_color'], p['bg_opacity'])
    accent = p['accent_color']
    highlight = p['highlight_color']
    card_w = min(920, int(lw - 112)) if portrait else 880
    card_h = 900 if portrait else 860
    if portrait or p['position'] == 'center':
        pos_css = f'left: 50%; margin-left: {-card_w // 2}px; top: {max(60, int((lh - card_h) // 2))}px;'
    elif p['position'] == 'left':
        pos_css = f'left: 80px; top: {max(60, int((lh - card_h) // 2))}px;'
    else:
        pos_css = f'right: 80px; top: {max(60, int((lh - card_h) // 2))}px;'

    bars = p['bars']
    max_val = max(b['value'] for b in bars)
    bar_max_h = 168
    svg_w = card_w - 88
    trend_h = 84
    col_step = svg_w / len(bars)
    pts = []
    bars_html = []
    labels_html = []
    for i, b in enumerate(bars):
        bh = max(24, round(bar_max_h * (b['value'] / max_val)))
        hl_cls = ' peak' if b['highlight'] else ''
        bars_html.append(
            f'<div class="bar-col"><div class="bar-inner">'
            f'<span class="bar-val{hl_cls}" id="val-{i + 1}">{html_lib.escape(b["display"])}</span>'
            f'<div class="bar-fill{hl_cls}" id="bar-{i + 1}" style="height:{bh}px;"></div>'
            f'</div></div>'
        )
        labels_html.append(f'<span class="x-label">{html_lib.escape(b["label"])}</span>')
        cx = round(col_step * (i + 0.5), 1)
        cy = round(trend_h - 14 - ((b['value'] / max_val) * (trend_h - 30)), 1)
        pts.append((cx, cy, b['highlight']))

    trend_svg = ''
    if p['show_trend'] and len(pts) >= 2:
        d_cmds = [f'M {pts[0][0]} {pts[0][1]}']
        for (x0, y0, _), (x1, y1, _) in zip(pts, pts[1:]):
            mx = round((x0 + x1) * 0.5, 1)
            d_cmds.append(f'C {mx} {y0}, {mx} {y1}, {x1} {y1}')
        circles = ''.join(
            f'<circle cx="{cx}" cy="{cy}" r="{8 if hl else 6}" '
            f'fill="{highlight if hl else accent}" stroke="#0B1222" stroke-width="3" />'
            for cx, cy, hl in pts
        )
        # Trend line sits inside its own dedicated header band above all bar labels: zero label overlap.
        trend_svg = (
            f'<div class="trend-band">'
            f'<svg id="trend-svg" viewBox="0 0 {svg_w} {trend_h}" fill="none">'
            f'<defs><clipPath id="trend-clip"><rect id="trend-clip-rect" x="0" y="0" width="0" height="{trend_h}" /></clipPath>'
            f'<linearGradient id="trend-grad" x1="0%" y1="0%" x2="100%" y2="0%">'
            f'<stop offset="0%" stop-color="{accent}" /><stop offset="100%" stop-color="{highlight}" />'
            f'</linearGradient></defs>'
            f'<g clip-path="url(#trend-clip)">'
            f'<path d="{" ".join(d_cmds)}" stroke="url(#trend-grad)" stroke-width="5" stroke-linecap="round" />'
            f'{circles}</g></svg></div>'
        )

    period_html = f'<span class="period-tag">{html_lib.escape(p["period"])}</span>' if p['period'] else ''
    sub_html = f'<p id="subhead">{html_lib.escape(p["subtitle"])}</p>' if p['subtitle'] else ''
    pill_html = f'<span class="kpi-pill">{html_lib.escape(p["pill_text"])}</span>' if p['pill_text'] else ''
    note_html = f'<span class="kpi-note">{html_lib.escape(p["note"])}</span>' if p['note'] else ''

    settle_t = round(min(duration * 0.58, 2.0), 3)
    counter_dur = round(max(0.25, settle_t - 0.12), 3)
    bar_span = max(0.2, settle_t - 0.55)
    bar_delays = [round(0.15 + (bar_span * i / max(1, len(bars) - 1)), 3) for i in range(len(bars))]
    bar_dur = round(min(0.45, max(0.2, settle_t * 0.28)), 3)
    fade_start = round(max(duration - 0.28, settle_t + 0.1), 3)

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={int(lw)}, height={int(lh)}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        margin: 0; width: {int(lw)}px; height: {int(lh)}px;
        overflow: hidden; background: transparent !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      #card-root {{ position: relative; width: {int(lw)}px; height: {int(lh)}px; background: transparent; }}
      #card {{
        position: absolute; {pos_css}
        width: {card_w}px; height: {card_h}px; padding: 38px 44px;
        border-radius: 28px; background: {bg_rgba};
        border: 2px solid {accent}; box-shadow: 0 20px 44px rgba(2, 6, 23, 0.52);
        display: flex; flex-direction: column; justify-content: space-between;
      }}
      .badge-row {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
      .badge {{
        display: inline-flex; align-items: center; gap: 10px; padding: 8px 18px;
        border-radius: 999px; background: rgba(56, 189, 248, 0.16);
        border: 1.5px solid {accent}; color: #E0F2FE; font-size: 28px; font-weight: 700;
      }}
      .badge-dot {{ width: 12px; height: 12px; border-radius: 50%; background: {accent}; }}
      .period-tag {{ color: #CBD5E1; font-size: 28px; font-weight: 600; }}
      #headline {{ color: #FFFFFF; font-size: 48px; font-weight: 700; line-height: 1.2; }}
      #subhead {{ color: #CBD5E1; font-size: 28px; margin-top: 8px; line-height: 1.35; }}
      .kpi-panel {{
        display: flex; align-items: center; justify-content: space-between;
        padding: 18px 26px; border-radius: 18px;
        background: rgba(15, 23, 42, 0.98); border: 1px solid rgba(148, 163, 184, 0.3);
      }}
      .kpi-label {{ color: #CBD5E1; font-size: 28px; font-weight: 600; }}
      #kpi-value {{
        color: {highlight}; font-size: 70px; font-weight: 800; line-height: 1.05;
        font-variant-numeric: tabular-nums; margin-top: 4px;
      }}
      .kpi-side {{ display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }}
      .kpi-pill {{
        padding: 6px 16px; border-radius: 10px; background: rgba(34, 197, 94, 0.2);
        border: 1px solid rgba(74, 222, 128, 0.55); color: #86EFAC; font-size: 28px; font-weight: 700;
      }}
      .kpi-note {{ color: #CBD5E1; font-size: 28px; }}
      .chart-area {{ display: flex; flex-direction: column; justify-content: flex-end; }}
      .trend-band {{ width: 100%; height: {trend_h}px; margin-bottom: 6px; }}
      #trend-svg {{ width: 100%; height: {trend_h}px; display: block; }}
      .bars-wrap {{ position: relative; width: 100%; height: 224px; }}
      .grid-lines {{
        position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: space-between; pointer-events: none;
      }}
      .grid-line {{ width: 100%; height: 1px; background: rgba(148, 163, 184, 0.22); }}
      .bars-row {{
        position: relative; z-index: 2; display: flex; align-items: flex-end; justify-content: space-around; height: 224px;
      }}
      .bar-col {{ flex: 1; height: 100%; display: flex; justify-content: center; }}
      .bar-inner {{
        width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 8px;
      }}
      .bar-val {{
        color: #F8FAFC; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; opacity: 0;
      }}
      .bar-val.peak {{ color: {highlight}; font-size: 32px; }}
      .bar-fill {{
        width: 78px; border-radius: 12px 12px 4px 4px;
        background: linear-gradient(180deg, {accent} 0%, #1D4ED8 100%);
        transform-origin: bottom center; transform: scaleY(0.08);
      }}
      .bar-fill.peak {{
        background: linear-gradient(180deg, {highlight} 0%, #EA580C 100%);
        box-shadow: 0 0 22px rgba(250, 204, 21, 0.35);
      }}
      .x-labels {{ display: flex; justify-content: space-around; padding-top: 12px; }}
      .x-label {{ flex: 1; text-align: center; color: #E2E8F0; font-size: 28px; font-weight: 600; }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="kpi_bar_chart" data-start="0" data-duration="{duration}" data-width="{int(lw)}" data-height="{int(lh)}">
      <div id="card">
        <div class="card-header">
          <div class="badge-row">
            <span class="badge"><span class="badge-dot"></span>{html_lib.escape(p['badge'])}</span>
            {period_html}
          </div>
          <h1 id="headline">{html_lib.escape(p['title'])}</h1>
          {sub_html}
        </div>
        <div class="kpi-panel">
          <div>
            <div class="kpi-label">{html_lib.escape(p['kpi_label'])}</div>
            <div id="kpi-value">{html_lib.escape(p['kpi_prefix'])}0{html_lib.escape(p['kpi_suffix'])}</div>
          </div>
          <div class="kpi-side">{pill_html}{note_html}</div>
        </div>
        <div class="chart-area">
          {trend_svg}
          <div class="bars-wrap">
            <div class="grid-lines"><div class="grid-line"></div><div class="grid-line"></div><div class="grid-line"></div><div class="grid-line"></div></div>
            <div class="bars-row">{''.join(bars_html)}</div>
          </div>
          <div class="x-labels">{''.join(labels_html)}</div>
        </div>
      </div>
    </div>
    <script>
      const kpiEl = document.getElementById("kpi-value");
      const kpiCfg = {json.dumps({'target': p['kpi_target'], 'prefix': p['kpi_prefix'], 'suffix': p['kpi_suffix'], 'decimals': p['kpi_decimals']}, ensure_ascii=False)};
      const state = {{ val: 0 }};
      const tl = gsap.timeline({{ paused: true }});
      tl.fromTo("#card", {{ x: 20, opacity: 0.96 }}, {{ x: 0, opacity: 1, duration: 0.3, ease: "power3.out" }}, 0.0);
      tl.to(state, {{
        val: kpiCfg.target, duration: {counter_dur}, ease: "power2.out",
        onUpdate: () => {{
          const num = kpiCfg.decimals > 0 ? state.val.toFixed(kpiCfg.decimals) : String(Math.round(state.val));
          kpiEl.textContent = kpiCfg.prefix + num + kpiCfg.suffix;
        }},
      }}, 0.12);
      const barDelays = {json.dumps(bar_delays)};
      barDelays.forEach((d, i) => {{
        const idx = i + 1;
        tl.to("#bar-" + idx, {{ scaleY: 1, duration: {bar_dur}, ease: "power3.out" }}, d);
        tl.fromTo("#val-" + idx, {{ y: 8, opacity: 0 }}, {{ y: 0, opacity: 1, duration: 0.25, ease: "power2.out" }}, d + {round(bar_dur * 0.55, 3)});
      }});
      if (document.getElementById("trend-clip-rect")) {{
        tl.to("#trend-clip-rect", {{ attr: {{ width: {svg_w} }}, duration: {round(max(0.3, settle_t - 0.35), 3)}, ease: "power2.inOut" }}, 0.35);
      }}
      tl.to("#card", {{ opacity: 0.88, duration: 0.22, ease: "power2.in" }}, {fade_start});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["kpi_bar_chart"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_kpi_counter_banner_card(p, *, lw, lh, portrait, duration):
    bg_rgba = _hex_to_rgba_css(p['bg_color'], p['bg_opacity'])
    accent = p['accent_color']
    highlight = p['highlight_color']
    card_w = min(960, int(lw - 96)) if portrait else int(lw - 180)
    if p['position'] == 'top':
        pos_css = f'left: {int((lw - card_w) // 2)}px; top: 72px;'
    elif p['position'] == 'center':
        pos_css = f'left: {int((lw - card_w) // 2)}px; top: 50%; transform: translateY(-50%);'
    else:
        pos_css = f'left: {int((lw - card_w) // 2)}px; bottom: 72px;'

    cards = []
    js_cfgs = []
    for idx, m in enumerate(p['metrics']):
        color = highlight if m['accent'] else '#FFFFFF'
        init_txt = f"{m['prefix']}0{m['suffix']}"
        cards.append(
            f'<div class="metric-box">'
            f'<div class="metric-label">{html_lib.escape(m["label"])}</div>'
            f'<div class="metric-num" id="m-val-{idx}" style="color:{color};">{html_lib.escape(init_txt)}</div>'
            f'</div>'
        )
        js_cfgs.append(m)

    settle_t = round(min(duration * 0.58, 2.0), 3)
    cols_css = 'grid-template-columns: repeat(2, 1fr);' if (portrait and len(p['metrics']) > 2) else f'grid-template-columns: repeat({len(p["metrics"])}, 1fr);'

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={int(lw)}, height={int(lh)}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{ font-family: "OpenEditCJK"; src: url("../vendor/cjk.ttf") format("truetype"); font-weight: 100 900; font-style: normal; }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {int(lw)}px; height: {int(lh)}px; overflow: hidden; background: transparent !important; font-family: "OpenEditCJK", sans-serif; }}
      #card-root {{ position: relative; width: {int(lw)}px; height: {int(lh)}px; background: transparent; }}
      #banner {{
        position: absolute; {pos_css} width: {card_w}px; padding: 34px 44px;
        border-radius: 26px; background: {bg_rgba}; border: 2px solid {accent};
        box-shadow: 0 20px 44px rgba(2, 6, 23, 0.52); display: flex; flex-direction: column; gap: 24px;
      }}
      .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
      .badge {{ padding: 6px 18px; border-radius: 999px; background: rgba(56,189,248,0.18); border: 1.5px solid {accent}; color: #E0F2FE; font-size: 28px; font-weight: 700; }}
      .title {{ color: #FFFFFF; font-size: 38px; font-weight: 700; }}
      .grid {{ display: grid; {cols_css} gap: 24px; }}
      .metric-box {{ padding: 20px 24px; border-radius: 18px; background: rgba(15, 23, 42, 0.96); border: 1px solid rgba(148,163,184,0.28); }}
      .metric-label {{ color: #CBD5E1; font-size: 28px; font-weight: 600; }}
      .metric-num {{ font-size: 62px; font-weight: 800; line-height: 1.1; margin-top: 8px; font-variant-numeric: tabular-nums; }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="kpi_counter_banner" data-start="0" data-duration="{duration}" data-width="{int(lw)}" data-height="{int(lh)}">
      <div id="banner">
        <div class="top-bar"><h1 class="title">{html_lib.escape(p['title'])}</h1><span class="badge">{html_lib.escape(p['badge'])}</span></div>
        <div class="grid">{''.join(cards)}</div>
      </div>
    </div>
    <script>
      const cfgs = {json.dumps(js_cfgs, ensure_ascii=False)};
      const tl = gsap.timeline({{ paused: true }});
      tl.fromTo("#banner", {{ y: 18, opacity: 0.95 }}, {{ y: 0, opacity: 1, duration: 0.3, ease: "power3.out" }}, 0.0);
      cfgs.forEach((cfg, i) => {{
        const el = document.getElementById("m-val-" + i);
        const st = {{ v: 0 }};
        tl.to(st, {{
          v: cfg.target, duration: {round(max(0.25, settle_t - 0.1), 3)}, ease: "power2.out",
          onUpdate: () => {{
            const s = cfg.decimals > 0 ? st.v.toFixed(cfg.decimals) : String(Math.round(st.v));
            el.textContent = cfg.prefix + s + cfg.suffix;
          }},
        }}, 0.1);
      }});
      tl.to("#banner", {{ opacity: 0.88, duration: 0.22 }}, {round(max(duration - 0.26, settle_t + 0.1), 3)});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["kpi_counter_banner"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_comparison_table_card(p, *, lw, lh, portrait, duration):
    bg_rgba = _hex_to_rgba_css(p['bg_color'], p['bg_opacity'])
    accent = p['accent_color']
    highlight = p['highlight_color']
    card_w = min(940, int(lw - 96)) if portrait else 1120
    if portrait or p['position'] == 'center':
        pos_css = f'left: {int((lw - card_w) // 2)}px; top: 50%; transform: translateY(-50%);'
    elif p['position'] == 'left':
        pos_css = 'left: 80px; top: 50%; transform: translateY(-50%);'
    else:
        pos_css = 'right: 80px; top: 50%; transform: translateY(-50%);'

    rows_html = []
    for idx, r in enumerate(p['rows']):
        rows_html.append(
            f'<div class="row" id="cmp-row-{idx}">'
            f'<div class="cell label">{html_lib.escape(r["label"])}</div>'
            f'<div class="cell left">{html_lib.escape(r["left"])}</div>'
            f'<div class="cell right">{html_lib.escape(r["right"])}</div>'
            f'</div>'
        )
    sub_html = f'<p class="sub">{html_lib.escape(p["subtitle"])}</p>' if p['subtitle'] else ''
    settle_t = round(min(duration * 0.58, 2.0), 3)
    row_delays = [round(0.15 + (max(0.2, settle_t - 0.35) * i / max(1, len(p['rows']))), 3) for i in range(len(p['rows']))]

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={int(lw)}, height={int(lh)}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{ font-family: "OpenEditCJK"; src: url("../vendor/cjk.ttf") format("truetype"); font-weight: 100 900; font-style: normal; }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {int(lw)}px; height: {int(lh)}px; overflow: hidden; background: transparent !important; font-family: "OpenEditCJK", sans-serif; }}
      #card-root {{ position: relative; width: {int(lw)}px; height: {int(lh)}px; background: transparent; }}
      #card {{
        position: absolute; {pos_css} width: {card_w}px; padding: 40px 44px;
        border-radius: 28px; background: {bg_rgba}; border: 2px solid {accent};
        box-shadow: 0 20px 44px rgba(2, 6, 23, 0.52); display: flex; flex-direction: column; gap: 22px;
      }}
      .hdr {{ display: flex; align-items: center; justify-content: space-between; }}
      .badge {{ padding: 6px 18px; border-radius: 999px; background: rgba(56,189,248,0.18); border: 1.5px solid {accent}; color: #E0F2FE; font-size: 28px; font-weight: 700; }}
      .title {{ color: #FFFFFF; font-size: 46px; font-weight: 700; }}
      .sub {{ color: #CBD5E1; font-size: 28px; }}
      .tbl-head, .row {{ display: grid; grid-template-columns: 1.1fr 1fr 1.15fr; gap: 16px; align-items: center; }}
      .tbl-head {{ padding: 14px 20px; border-radius: 14px; background: rgba(15, 23, 42, 0.98); color: #CBD5E1; font-size: 28px; font-weight: 700; }}
      .tbl-head .rh {{ color: {highlight}; }}
      .row {{ padding: 18px 20px; border-radius: 14px; background: rgba(30, 41, 59, 0.72); border: 1px solid rgba(148,163,184,0.22); }}
      .cell {{ font-size: 30px; font-weight: 600; }}
      .cell.label {{ color: #E2E8F0; }}
      .cell.left {{ color: #CBD5E1; }}
      .cell.right {{ color: {highlight}; font-weight: 800; }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="comparison_table" data-start="0" data-duration="{duration}" data-width="{int(lw)}" data-height="{int(lh)}">
      <div id="card">
        <div class="hdr"><h1 class="title">{html_lib.escape(p['title'])}</h1><span class="badge">{html_lib.escape(p['badge'])}</span></div>
        {sub_html}
        <div class="tbl-head"><span>对比维度</span><span>{html_lib.escape(p['left_header'])}</span><span class="rh">{html_lib.escape(p['right_header'])}</span></div>
        {''.join(rows_html)}
      </div>
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      tl.fromTo("#card", {{ y: 16, opacity: 0.95 }}, {{ y: 0, opacity: 1, duration: 0.28, ease: "power3.out" }}, 0.0);
      const delays = {json.dumps(row_delays)};
      delays.forEach((d, i) => {{
        tl.fromTo("#cmp-row-" + i, {{ x: 24, opacity: 0.2 }}, {{ x: 0, opacity: 1, duration: 0.3, ease: "power2.out" }}, d);
      }});
      tl.to("#card", {{ opacity: 0.88, duration: 0.22 }}, {round(max(duration - 0.26, settle_t + 0.1), 3)});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["comparison_table"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_milestone_progress_card(p, *, lw, lh, portrait, duration):
    bg_rgba = _hex_to_rgba_css(p['bg_color'], p['bg_opacity'])
    accent = p['accent_color']
    highlight = p['highlight_color']
    card_w = min(940, int(lw - 96)) if portrait else 880
    if portrait or p['position'] == 'center':
        pos_css = f'left: {int((lw - card_w) // 2)}px; top: 50%; transform: translateY(-50%);'
    elif p['position'] == 'left':
        pos_css = 'left: 80px; top: 50%; transform: translateY(-50%);'
    else:
        pos_css = 'right: 80px; top: 50%; transform: translateY(-50%);'

    steps_html = []
    for idx, st in enumerate(p['steps']):
        is_last = idx == len(p['steps']) - 1
        fill_bg = f'linear-gradient(90deg, {accent}, {highlight})' if is_last else accent
        steps_html.append(
            f'<div class="step">'
            f'<div class="step-top"><span class="step-lbl">{html_lib.escape(st["label"])}</span>'
            f'<span class="step-val" style="color:{highlight if is_last else "#FFFFFF"};">{html_lib.escape(st["value"])}</span></div>'
            f'<div class="track"><div class="fill" id="step-fill-{idx}" style="width:{st["percent"]}%;background:{fill_bg};"></div></div>'
            f'</div>'
        )
    settle_t = round(min(duration * 0.58, 2.0), 3)
    delays = [round(0.15 + (max(0.2, settle_t - 0.45) * i / max(1, len(p['steps']))), 3) for i in range(len(p['steps']))]

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={int(lw)}, height={int(lh)}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{ font-family: "OpenEditCJK"; src: url("../vendor/cjk.ttf") format("truetype"); font-weight: 100 900; font-style: normal; }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {int(lw)}px; height: {int(lh)}px; overflow: hidden; background: transparent !important; font-family: "OpenEditCJK", sans-serif; }}
      #card-root {{ position: relative; width: {int(lw)}px; height: {int(lh)}px; background: transparent; }}
      #card {{
        position: absolute; {pos_css} width: {card_w}px; padding: 40px 44px;
        border-radius: 28px; background: {bg_rgba}; border: 2px solid {accent};
        box-shadow: 0 20px 44px rgba(2, 6, 23, 0.52); display: flex; flex-direction: column; gap: 26px;
      }}
      .hdr {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
      .badge {{ padding: 6px 18px; border-radius: 999px; background: rgba(56,189,248,0.18); border: 1.5px solid {accent}; color: #E0F2FE; font-size: 28px; font-weight: 700; }}
      .title {{ color: #FFFFFF; font-size: 46px; font-weight: 700; }}
      .step {{ display: flex; flex-direction: column; gap: 10px; }}
      .step-top {{ display: flex; justify-content: space-between; align-items: baseline; }}
      .step-lbl {{ color: #E2E8F0; font-size: 30px; font-weight: 600; }}
      .step-val {{ font-size: 32px; font-weight: 800; font-variant-numeric: tabular-nums; }}
      .track {{ width: 100%; height: 22px; border-radius: 999px; background: rgba(30, 41, 59, 0.95); overflow: hidden; }}
      .fill {{ height: 100%; border-radius: 999px; transform-origin: left center; transform: scaleX(0.08); }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="milestone_progress" data-start="0" data-duration="{duration}" data-width="{int(lw)}" data-height="{int(lh)}">
      <div id="card">
        <div class="hdr"><h1 class="title">{html_lib.escape(p['title'])}</h1><span class="badge">{html_lib.escape(p['badge'])}</span></div>
        {''.join(steps_html)}
      </div>
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      tl.fromTo("#card", {{ x: 18, opacity: 0.95 }}, {{ x: 0, opacity: 1, duration: 0.28, ease: "power3.out" }}, 0.0);
      const delays = {json.dumps(delays)};
      delays.forEach((d, i) => {{
        tl.to("#step-fill-" + i, {{ scaleX: 1, duration: 0.45, ease: "power3.out" }}, d);
      }});
      tl.to("#card", {{ opacity: 0.88, duration: 0.22 }}, {round(max(duration - 0.26, settle_t + 0.1), 3)});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["milestone_progress"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_viewfinder_hud_card(p, *, lw, lh, portrait, duration):
    del portrait
    m = p['margin_px']
    w, h = int(lw), int(lh)
    arm = max(32, min(64, round(min(w, h) * 0.045)))
    cc = p['corner_color']
    rc = p['rec_color']
    ac = p['accent_color']
    pill_bg = f'rgba(11, 15, 25, {round(p["pill_opacity"], 3)})'
    cx, cy = w // 2, h // 2
    crosshair_svg = ''
    if p['show_crosshair']:
        g, ln = 16, 26
        crosshair_svg = (
            f'<g stroke="{cc}" stroke-opacity="0.52" stroke-width="2" stroke-linecap="round">'
            f'<line x1="{cx - g - ln}" y1="{cy}" x2="{cx - g}" y2="{cy}" />'
            f'<line x1="{cx + g}" y1="{cy}" x2="{cx + g + ln}" y2="{cy}" />'
            f'<line x1="{cx}" y1="{cy - g - ln}" x2="{cx}" y2="{cy - g}" />'
            f'<line x1="{cx}" y1="{cy + g}" x2="{cx}" y2="{cy + g + ln}" />'
            f'</g>'
        )
    corners_svg = (
        f'<path d="M {m} {m + arm} L {m} {m} L {m + arm} {m}" />'
        f'<path d="M {w - m - arm} {m} L {w - m} {m} L {w - m} {m + arm}" />'
        f'<path d="M {m} {h - m - arm} L {m} {h - m} L {m + arm} {h - m}" />'
        f'<path d="M {w - m - arm} {h - m} L {w - m} {h - m} L {w - m} {h - m - arm}" />'
    )
    rec_pill = ''
    if p['show_rec']:
        rec_pill = (
            f'<div class="hud-pill top-left">'
            f'<span id="rec-dot"></span><span>{html_lib.escape(p["rec_label"])}</span>'
            f'</div>'
        )
    top_right_pill = (
        f'<div class="hud-pill top-right"><span class="accent-bar"></span>'
        f'<span>{html_lib.escape(p["top_right"])}</span></div>'
    ) if p['top_right'] else ''
    bot_left_pill = (
        f'<div class="hud-pill bot-left"><span>{html_lib.escape(p["bottom_left"])}</span></div>'
    ) if p['bottom_left'] else ''
    pad_in = m + 18

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", "Consolas", monospace;
      }}
      #hud-root {{ position: relative; width: {w}px; height: {h}px; background: transparent; }}
      #hud-svg {{
        position: absolute; inset: 0; width: {w}px; height: {h}px; pointer-events: none;
        filter: drop-shadow(0 2px 5px rgba(0, 0, 0, 0.72));
      }}
      .hud-pill {{
        position: absolute; display: inline-flex; align-items: center; gap: 10px;
        padding: 7px 16px; border-radius: 999px; background: {pill_bg};
        border: 1.5px solid rgba(255, 255, 255, 0.24);
        color: #F8FAFC; font-size: 26px; font-weight: 700; letter-spacing: 0.08em;
        font-variant-numeric: tabular-nums;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.42);
      }}
      .top-left {{ top: {pad_in}px; left: {pad_in}px; }}
      .top-right {{ top: {pad_in}px; right: {pad_in}px; }}
      .bot-left {{ bottom: {pad_in}px; left: {pad_in}px; }}
      .bot-right {{ bottom: {pad_in}px; right: {pad_in}px; border-color: {ac}; }}
      #rec-dot {{
        width: 14px; height: 14px; border-radius: 50%; background: {rc};
        box-shadow: 0 0 10px {rc};
      }}
      .accent-bar {{ width: 5px; height: 18px; border-radius: 3px; background: {ac}; }}
    </style>
  </head>
  <body>
    <div id="hud-root" data-composition-id="viewfinder_hud" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}">
      <svg id="hud-svg" viewBox="0 0 {w} {h}" fill="none">
        <g stroke="{cc}" stroke-width="4" stroke-linecap="square" stroke-linejoin="miter">{corners_svg}</g>
        {crosshair_svg}
      </svg>
      {rec_pill}
      {top_right_pill}
      {bot_left_pill}
      <div class="hud-pill bot-right"><span id="tc-readout">00:00:00:00</span></div>
    </div>
    <script>
      const tcEl = document.getElementById("tc-readout");
      const dotEl = document.getElementById("rec-dot");
      const cfg = {json.dumps({'start': p['start_seconds'], 'fps': p['timecode_fps'], 'blinkHz': p['blink_hz']})};
      const st = {{ t: 0 }};
      function syncHud() {{
        const abs = cfg.start + st.t;
        const framesTotal = Math.max(0, Math.floor(abs * cfg.fps + 1e-4));
        const ff = framesTotal % cfg.fps;
        const secsTotal = Math.floor(framesTotal / cfg.fps);
        const ss = secsTotal % 60;
        const mm = Math.floor(secsTotal / 60) % 60;
        const hh = Math.floor(secsTotal / 3600);
        const p2 = n => String(n).padStart(2, "0");
        tcEl.textContent = `${{p2(hh)}}:${{p2(mm)}}:${{p2(ss)}}:${{p2(ff)}}`;
        if (dotEl) {{
          const ph = (st.t * cfg.blinkHz) % 1.0;
          dotEl.style.opacity = ph < 0.55 ? "1" : "0.18";
        }}
      }}
      const tl = gsap.timeline({{ paused: true }});
      tl.to(st, {{ t: {duration}, duration: {duration}, ease: "none", onUpdate: syncHud }}, 0);
      window.__timelines = window.__timelines || {{}};
      window.__timelines["viewfinder_hud"] = tl;
      syncHud();
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_polaroid_grid_card(p, *, lw, lh, portrait, duration, asset_urls):
    w, h = int(lw), int(lh)
    cards = p['cards']
    count = len(cards)
    use_grid = (p['layout'] == 'grid_2x2') or (p['layout'] == 'auto' and (portrait or count == 4))
    if use_grid:
        cols, rows = 2, (count + 1) // 2
        gap_x, gap_y = (64, 48) if portrait else (112, 62)
        card_h = min(400, int((h - 150 - gap_y * (rows - 1)) / rows))
        card_w = min(400 if portrait else 330, round(card_h / 1.22))
        total_w = cols * card_w + (cols - 1) * gap_x
        total_h = rows * card_h + (rows - 1) * gap_y
        x0 = (w - total_w) * 0.5
        y0 = (h - total_h) * 0.5
        centers = []
        for idx in range(count):
            r, c = divmod(idx, cols)
            centers.append((round(x0 + c * (card_w + gap_x) + card_w * 0.5, 1),
                            round(y0 + r * (card_h + gap_y) + card_h * 0.5, 1)))
    else:
        gap_x = 128 if count <= 3 else 78
        card_w = min(330, int((w - 160 - gap_x * (count - 1)) / count))
        card_h = min(int(h * 0.72), round(card_w * 1.24))
        total_w = count * card_w + (count - 1) * gap_x
        x0 = (w - total_w) * 0.5
        cy = round(h * 0.5, 1)
        centers = [(round(x0 + idx * (card_w + gap_x) + card_w * 0.5, 1), cy) for idx in range(count)]

    cards_html = []
    for idx, (card, (cx, cy)) in enumerate(zip(cards, centers)):
        left_px = round(cx - card_w * 0.5, 1)
        top_px = round(cy - card_h * 0.5, 1)
        rot = card['tilt_deg']
        if card.get('video'):
            src = html_lib.escape(asset_urls[f'card_{idx}'])
            media_html = f'<video id="hf-vid-{idx}" src="{src}" data-start="0" data-duration="{duration}" muted playsinline preload="auto"></video>'
        elif card.get('image'):
            src = html_lib.escape(asset_urls[f'card_{idx}'])
            media_html = f'<img src="{src}" alt="" />'
        else:
            media_html = f'<div class="ph-bg" style="--ac:{p["accent_color"]};"><span>#{idx + 1:02d}</span></div>'
        sub_html = f'<div class="card-sub">{html_lib.escape(card["sublabel"])}</div>' if card['sublabel'] else ''
        cards_html.append(
            f'<div class="polaroid" id="pol-{idx}" '
            f'style="left:{left_px}px;top:{top_px}px;width:{card_w}px;height:{card_h}px;--rot:{rot}deg;">'
            f'<div class="photo-box">{media_html}</div>'
            f'<div class="caption-box"><div class="card-title">{html_lib.escape(card["label"])}</div>{sub_html}</div>'
            f'</div>'
        )

    circle_svg = ''
    hl = p['highlight_index']
    if hl is not None:
        hx, hy = centers[hl]
        rx = round(card_w * 0.72 + 18, 1)
        ry = round(card_h * 0.72 + 18, 1)
        perim = round(2.0 * math.pi * math.sqrt(0.5 * (rx * rx + ry * ry)) + 24.0, 1)
        rot_deg = cards[hl]['tilt_deg']
        circle_svg = (
            f'<svg id="hl-svg" viewBox="0 0 {w} {h}" fill="none">'
            f'<ellipse id="hl-ring-1" cx="{hx}" cy="{hy}" rx="{rx}" ry="{ry}" '
            f'transform="rotate({rot_deg:.1f} {hx} {hy})" '
            f'stroke="{p["circle_color"]}" stroke-width="6.5" stroke-linecap="round" '
            f'stroke-dasharray="{perim}" stroke-dashoffset="{perim}" />'
            f'<ellipse id="hl-ring-2" cx="{round(hx + 3, 1)}" cy="{round(hy - 2, 1)}" rx="{round(rx + 6, 1)}" ry="{round(ry + 6, 1)}" '
            f'transform="rotate({rot_deg - 1.5:.1f} {hx} {hy})" '
            f'stroke="{p["circle_color"]}" stroke-opacity="0.72" stroke-width="3.5" stroke-linecap="round" '
            f'stroke-dasharray="{perim}" stroke-dashoffset="{perim}" />'
            f'</svg>'
        )

    stagger = p['stagger']
    settle_t = round(min(duration * 0.45, 0.32 + stagger * max(0, count - 1) + 0.15), 3)
    fade_t = round(max(settle_t + 0.25, duration - 0.25), 3)

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      #stage {{ position: relative; width: {w}px; height: {h}px; background: transparent; }}
      .polaroid {{
        position: absolute; background: {p['frame_color']}; border-radius: 8px;
        padding: 16px 16px 12px 16px;
        box-shadow: 0 18px 42px rgba(2, 6, 23, 0.56);
        display: flex; flex-direction: column;
        transform: rotate(var(--rot));
      }}
      .photo-box {{
        flex: 1; width: 100%; overflow: hidden; border-radius: 4px;
        background: #141224; position: relative;
      }}
      .photo-box img, .photo-box video {{ width: 100%; height: 100%; object-fit: cover; object-position: center 22%; display: block; }}
      .ph-bg {{
        width: 100%; height: 100%;
        background: radial-gradient(circle at 30% 30%, color-mix(in srgb, var(--ac) 34%, #111827), #0B0D14);
        display: flex; align-items: center; justify-content: center;
        color: rgba(255, 255, 255, 0.72); font-size: 40px; font-weight: 800; letter-spacing: 0.1em;
      }}
      .caption-box {{
        height: 74px; padding-top: 10px;
        display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center;
      }}
      .card-title {{ color: {p['ink_color']}; font-size: 28px; font-weight: 800; line-height: 1.15; }}
      .card-sub {{ color: {p['ink_color']}; opacity: 0.72; font-size: 20px; font-weight: 600; margin-top: 2px; }}
      #hl-svg {{ position: absolute; inset: 0; width: {w}px; height: {h}px; pointer-events: none; filter: drop-shadow(0 0 8px rgba(239, 68, 68, 0.55)); }}
    </style>
  </head>
  <body>
    <div id="stage" data-composition-id="polaroid_grid" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}">
      {''.join(cards_html)}
      {circle_svg}
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      const count = {count};
      for (let i = 0; i < count; i++) {{
        tl.fromTo("#pol-" + i,
          {{ y: 36, scale: 0.86, opacity: 0 }},
          {{ y: 0, scale: 1, opacity: 1, duration: 0.34, ease: "back.out(1.4)" }},
          i * {stagger}
        );
      }}
      if (document.getElementById("hl-ring-1")) {{
        const drawStart = Math.min({round(settle_t * 0.78, 3)}, {round(count * stagger, 3)});
        tl.to("#hl-ring-1", {{ strokeDashoffset: 0, duration: 0.42, ease: "power2.out" }}, drawStart);
        tl.to("#hl-ring-2", {{ strokeDashoffset: 0, duration: 0.36, ease: "power2.out" }}, drawStart + 0.08);
      }}
      tl.to("#stage", {{ opacity: 0, duration: 0.22, ease: "power2.in" }}, {fade_t});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["polaroid_grid"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_filmstrip_reel_card(p, *, lw, lh, portrait, duration, asset_urls):
    del portrait
    w, h = int(lw), int(lh)
    strip_h = max(320, int(round(h * p['height_ratio'])))
    strip_top = max(24, min(h - strip_h - 24, int(round(h * p['position_y'] - strip_h * 0.5))))
    band_h = 34
    sprocket_h = 24
    inner_h = max(140, strip_h - 2 * (band_h + sprocket_h + 12))
    frame_w = max(220, int(round(inner_h * 1.42)))
    gap = 18
    frames = p['frames']
    n_frames = len(frames)
    track_w = n_frames * frame_w + (n_frames + 1) * gap
    shift_x = max(0, track_w - w + 40)

    sp_step = 44
    sp_count = (w // sp_step) + 3
    sprockets = ''.join(
        f'<rect x="{i * sp_step + 14}" y="4" width="22" height="15" rx="3.5" fill="#040508" stroke="rgba(255,255,255,0.22)" stroke-width="1" />'
        for i in range(sp_count)
    )
    sprocket_svg = f'<svg class="sprocket-svg" viewBox="0 0 {w} {sprocket_h}" preserveAspectRatio="none">{sprockets}</svg>'

    frames_html = []
    for idx, fr in enumerate(frames):
        if fr.get('video'):
            src = html_lib.escape(asset_urls[f'frame_{idx}'])
            media = f'<video id="hf-vid-{idx}" src="{src}" data-start="0" data-duration="{duration}" muted playsinline preload="auto"></video>'
        elif fr.get('image'):
            media = f'<img src="{html_lib.escape(asset_urls[f"frame_{idx}"])}" alt="" />'
        else:
            media = f'<div class="cell-ph"><span>FRAME {idx + 1:02d}</span></div>'
        frames_html.append(
            f'<div class="film-cell" style="width:{frame_w}px;height:{inner_h}px;">'
            f'{media}'
            f'<span class="cell-num">{html_lib.escape(fr["label"])}</span>'
            f'</div>'
        )

    play_svg = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><polygon points="2,1 15,8 2,15" /></svg>'
    scroll_dur = round(max(0.25, duration - 0.35), 3)
    beats = min(max(1, p['pulse_beats']), max(1, n_frames - 1)) if n_frames > 1 else 1

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", "Consolas", monospace;
      }}
      #stage {{ position: relative; width: {w}px; height: {h}px; background: transparent; }}
      #strip {{
        position: absolute; left: 0; top: {strip_top}px; width: {w}px; height: {strip_h}px;
        background: {p['strip_color']};
        border-top: 2px solid rgba(255, 255, 255, 0.20);
        border-bottom: 2px solid rgba(255, 255, 255, 0.20);
        box-shadow: 0 24px 56px rgba(0, 0, 0, 0.72);
        display: flex; flex-direction: column; justify-content: space-between;
        padding: 4px 0;
      }}
      .code-band {{
        height: {band_h}px; padding: 0 36px;
        display: flex; align-items: center; justify-content: space-between;
        color: {p['accent_color']}; font-size: 22px; font-weight: 700; letter-spacing: 0.12em;
      }}
      .reel-badge {{
        display: inline-flex; align-items: center; gap: 10px;
        color: #F8FAFC; background: rgba(255, 255, 255, 0.08);
        border: 1px solid rgba(255, 255, 255, 0.22);
        padding: 3px 14px; border-radius: 6px; font-variant-numeric: tabular-nums;
      }}
      .sprocket-rail {{ height: {sprocket_h}px; width: 100%; overflow: hidden; background: rgba(255,255,255,0.03); }}
      .sprocket-svg {{ width: {w}px; height: {sprocket_h}px; display: block; }}
      .reel-window {{ width: {w}px; height: {inner_h}px; overflow: hidden; position: relative; }}
      #reel-track {{
        position: absolute; left: 0; top: 0; height: {inner_h}px;
        display: flex; align-items: center; gap: {gap}px; padding: 0 {gap}px;
      }}
      .film-cell {{
        flex: 0 0 auto; border-radius: 4px; overflow: hidden; position: relative;
        border: 2px solid rgba(255, 255, 255, 0.24); background: #141824;
      }}
      .film-cell img, .film-cell video {{ width: 100%; height: 100%; object-fit: cover; object-position: center 24%; display: block; }}
      .cell-ph {{
        width: 100%; height: 100%;
        background: linear-gradient(135deg, #161829, #24193D);
        display: flex; align-items: center; justify-content: center;
        color: rgba(255, 255, 255, 0.72); font-size: 28px; font-weight: 800; letter-spacing: 0.08em;
      }}
      .cell-num {{
        position: absolute; right: 10px; bottom: 8px;
        padding: 3px 10px; border-radius: 4px;
        background: rgba(5, 8, 17, 0.78); color: #F8FAFC; font-size: 18px; font-weight: 700;
      }}
    </style>
  </head>
  <body>
    <div id="stage" data-composition-id="filmstrip_reel" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}">
      <div id="strip">
        <div class="code-band">
          <span>{html_lib.escape(p['top_code'])}</span>
          <span class="reel-badge">{play_svg}<span id="frame-idx">1 / {n_frames}</span></span>
        </div>
        <div class="sprocket-rail">{sprocket_svg}</div>
        <div class="reel-window"><div id="reel-track">{''.join(frames_html)}</div></div>
        <div class="sprocket-rail">{sprocket_svg}</div>
        <div class="code-band">
          <span>{html_lib.escape(p['bottom_code'])}</span>
          <span style="color:#CBD5E1;">35MM ARCHIVE</span>
        </div>
      </div>
    </div>
    <script>
      const track = document.getElementById("reel-track");
      const idxEl = document.getElementById("frame-idx");
      const nFrames = {n_frames};
      const st = {{ p: 0 }};
      const tl = gsap.timeline({{ paused: true }});
      tl.fromTo("#strip", {{ y: 24, opacity: 0 }}, {{ y: 0, opacity: 1, duration: 0.22, ease: "power3.out" }}, 0);
      tl.to("#reel-track", {{
        x: -{shift_x}, duration: {scroll_dur},
        ease: {json.dumps(f'steps({beats})') if n_frames > 2 else '"power2.inOut"'},
      }}, 0.12);
      tl.to(st, {{
        p: 1, duration: {scroll_dur}, ease: "none",
        onUpdate: () => {{
          const cur = Math.min(nFrames, Math.max(1, Math.floor(st.p * n_frames) + 1));
          idxEl.textContent = `${{cur}} / ${{nFrames}}`;
        }},
      }}, 0.12);
      window.__timelines = window.__timelines || {{}};
      window.__timelines["filmstrip_reel"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_text_matte_dissolve_card(p, *, lw, lh, portrait, duration, asset_urls):
    w, h = int(lw), int(lh)
    fill_video = bool(p.get('fill_video'))
    fill_url = html_lib.escape(asset_urls['fill']) if (fill_video or p.get('fill_image')) else ''
    if fill_video and fill_url:
        fill_media_tag = (
            f'<video id="fill-vid" src="{fill_url}" data-start="0" data-duration="{duration}" '
            f'muted playsinline preload="auto" '
            f'style="position:absolute;left:0;top:0;width:2px;height:2px;opacity:0.001;pointer-events:none;"></video>'
        )
    elif fill_url:
        fill_media_tag = (
            f'<img id="fill-img" src="{fill_url}" alt="" '
            f'style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;" />'
        )
    else:
        fill_media_tag = ''
    font_sz = min(p['font_size'], int(w * (0.20 if portrait else 0.16)))
    cfg = {
        'w': w, 'h': h, 'text': p['text'], 'subtitle': p['subtitle'],
        'accent': p['accent_color'], 'secondary': p['secondary_color'], 'color': p['text_color'],
        'glow': p['glow'], 'dissolveStart': p['dissolve_start'], 'direction': p['dissolve_direction'],
        'particleCount': p['particle_count'], 'fontSize': font_sz,
        'hasFillImage': bool(fill_url and not fill_video), 'hasFillVideo': bool(fill_url and fill_video),
    }

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      #cv {{ position: absolute; inset: 0; width: {w}px; height: {h}px; }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="text_matte_dissolve" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}" style="position:relative;width:{w}px;height:{h}px;background:transparent;">
      {fill_media_tag}
      <canvas id="cv" width="{w}" height="{h}"></canvas>
    </div>
    <script>
      const cfg = {json.dumps(cfg, ensure_ascii=False)};
      const cv = document.getElementById("cv");
      const ctx = cv.getContext("2d");
      const fillImg = document.getElementById("fill-img");
      const fillVid = document.getElementById("fill-vid");

      function mulberry32(seed) {{
        let a = seed >>> 0;
        return () => {{
          a |= 0; a = (a + 0x6D2B79F5) | 0;
          let t = Math.imul(a ^ (a >>> 15), 1 | a);
          t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
          return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        }};
      }}
      const rng = mulberry32(0xE4171A);
      const cx = cfg.w * 0.5, cy = cfg.h * 0.48;
      const boxW = Math.min(cfg.w * 0.86, Math.max(cfg.fontSize * 3.0, cfg.text.length * cfg.fontSize * 0.85));
      const boxH = cfg.fontSize * 1.15;
      const particles = Array.from({{ length: cfg.particleCount }}, () => {{
        const rx = rng(), ry = rng();
        return {{
          x: cx - boxW * 0.5 + rx * boxW,
          y: cy - boxH * 0.5 + ry * boxH,
          rx, ry,
          sz: 2.2 + rng() * 4.8,
          vx: 50 + rng() * 140,
          vy: (rng() - 0.62) * 110,
          col: rng() > 0.45 ? cfg.accent : (rng() > 0.5 ? cfg.secondary : cfg.color),
        }};
      }});

      const textBuf = document.createElement("canvas");
      textBuf.width = cfg.w; textBuf.height = cfg.h;
      const tctx = textBuf.getContext("2d");

      function paintTextBuffer() {{
        tctx.clearRect(0, 0, cfg.w, cfg.h);
        tctx.save();
        tctx.font = `900 ${{cfg.fontSize}}px "OpenEditCJK", sans-serif`;
        tctx.textAlign = "center";
        tctx.textBaseline = "middle";
        if (cfg.hasFillVideo && fillVid && fillVid.readyState >= 2 && fillVid.videoWidth > 0) {{
          tctx.fillStyle = "#FFFFFF";
          tctx.fillText(cfg.text, cx, cy);
          tctx.globalCompositeOperation = "source-in";
          const scale = Math.max(cfg.w / fillVid.videoWidth, cfg.h / fillVid.videoHeight);
          const dw = fillVid.videoWidth * scale, dh = fillVid.videoHeight * scale;
          tctx.drawImage(fillVid, (cfg.w - dw) * 0.5, (cfg.h - dh) * 0.5, dw, dh);
          tctx.globalCompositeOperation = "source-over";
        }} else if (cfg.hasFillImage && fillImg && fillImg.complete && fillImg.naturalWidth > 0) {{
          tctx.fillStyle = "#FFFFFF";
          tctx.fillText(cfg.text, cx, cy);
          tctx.globalCompositeOperation = "source-in";
          const scale = Math.max(cfg.w / fillImg.naturalWidth, cfg.h / fillImg.naturalHeight);
          const dw = fillImg.naturalWidth * scale, dh = fillImg.naturalHeight * scale;
          tctx.drawImage(fillImg, (cfg.w - dw) * 0.5, (cfg.h - dh) * 0.5, dw, dh);
          tctx.globalCompositeOperation = "source-over";
        }} else {{
          const grad = tctx.createLinearGradient(cx - boxW * 0.5, cy - boxH * 0.4, cx + boxW * 0.5, cy + boxH * 0.4);
          grad.addColorStop(0, cfg.color);
          grad.addColorStop(0.55, cfg.accent);
          grad.addColorStop(1, cfg.secondary);
          tctx.fillStyle = grad;
          tctx.fillText(cfg.text, cx, cy);
        }}
        if (cfg.glow) {{
          tctx.shadowColor = cfg.accent;
          tctx.shadowBlur = 22;
        }}
        tctx.lineWidth = 3.5;
        tctx.strokeStyle = cfg.color;
        tctx.strokeText(cfg.text, cx, cy);
        tctx.restore();
      }}

      const st = {{ p: 0 }};
      function draw() {{
        paintTextBuffer();
        ctx.clearRect(0, 0, cfg.w, cfg.h);
        const p = Math.max(0, Math.min(1, st.p));
        const inScale = p < 0.14 ? 0.92 + 0.08 * (p / 0.14) : 1.0;
        const wipe = p <= cfg.dissolveStart ? 0 : Math.min(1, (p - cfg.dissolveStart) / Math.max(0.08, 0.96 - cfg.dissolveStart));

        const x0 = cx - boxW * 0.56, x1 = cx + boxW * 0.56;
        const y0 = cy - boxH * 0.65, y1 = cy + boxH * 0.65;

        ctx.save();
        ctx.translate(cx, cy); ctx.scale(inScale, inScale); ctx.translate(-cx, -cy);
        ctx.drawImage(textBuf, 0, 0);
        if (wipe > 0) {{
          ctx.globalCompositeOperation = "destination-out";
          const band = 90;
          let g;
          if (cfg.direction === "left") {{
            const fx = x1 - wipe * (x1 - x0 + band);
            g = ctx.createLinearGradient(fx, 0, fx + band, 0);
            g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,1)");
          }} else if (cfg.direction === "up") {{
            const fy = y1 - wipe * (y1 - y0 + band);
            g = ctx.createLinearGradient(0, fy, 0, fy + band);
            g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,1)");
          }} else {{
            const fx = x0 + wipe * (x1 - x0 + band);
            g = ctx.createLinearGradient(fx - band, 0, fx, 0);
            g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
          }}
          ctx.fillStyle = g;
          ctx.fillRect(0, 0, cfg.w, cfg.h);
        }}
        ctx.restore();

        if (wipe > 0) {{
          for (const pt of particles) {{
            const posFrac = cfg.direction === "left" ? (1.0 - pt.rx) : (cfg.direction === "up" ? (1.0 - pt.ry) : pt.rx);
            const age = (wipe - posFrac * 0.85) / 0.35;
            if (age <= 0 || age >= 1) continue;
            const alpha = Math.sin(age * Math.PI);
            const dirMul = cfg.direction === "left" ? -1 : 1;
            const px = pt.x + dirMul * pt.vx * age;
            const py = pt.y + (cfg.direction === "up" ? -Math.abs(pt.vy) * 1.4 : pt.vy) * age;
            ctx.save();
            ctx.globalAlpha = Math.max(0, Math.min(1, alpha));
            ctx.fillStyle = pt.col;
            ctx.shadowColor = pt.col; ctx.shadowBlur = 10;
            ctx.beginPath();
            ctx.arc(px, py, pt.sz * (1.0 - 0.5 * age), 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
          }}
        }}

        if (cfg.subtitle && wipe < 0.96) {{
          ctx.save();
          ctx.globalAlpha = wipe <= 0.58 ? 1.0 : Math.max(0, 1.0 - (wipe - 0.58) / 0.38);
          ctx.font = `700 ${{Math.max(28, Math.round(cfg.fontSize * 0.24))}}px "OpenEditCJK", sans-serif`;
          ctx.textAlign = "center";
          ctx.lineJoin = "round";
          ctx.lineWidth = 4;
          ctx.strokeStyle = "rgba(2, 6, 23, 0.92)";
          ctx.strokeText(cfg.subtitle, cx, cy + boxH * 0.72);
          ctx.fillStyle = "#F8FAFC";
          ctx.fillText(cfg.subtitle, cx, cy + boxH * 0.72);
          ctx.restore();
        }}
      }}

      const tl = gsap.timeline({{ paused: true }});
      tl.to(st, {{ p: 1, duration: {duration}, ease: "none", onUpdate: draw }}, 0);
      if (fillImg) fillImg.addEventListener("load", draw);
      if (fillVid) {{
        fillVid.addEventListener("loadeddata", draw);
        fillVid.addEventListener("seeked", draw);
      }}
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(draw);
      window.__timelines = window.__timelines || {{}};
      window.__timelines["text_matte_dissolve"] = tl;
      draw();
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _render_ui_flow_stage_card(p, *, lw, lh, portrait, duration, asset_urls):
    w, h = int(lw), int(lh)
    cards = p['cards']
    count = len(cards)
    focus_idx = int(p['focus_index'])
    states = p['states']

    bg_rgba = _hex_to_rgba_css(p['bg_color'], round(p['bg_opacity'] / 100.0, 3)) if p['bg_opacity'] > 0.0 else 'transparent'

    # Pre-compute shared-element FLIP target boxes [x, y, cw, ch, radius, opacity, titleScale] per state.
    margin_x = 112 if not portrait else 64
    margin_y = 128 if not portrait else 160
    usable_w = w - 2 * margin_x
    usable_h = h - 2 * margin_y

    def _layout_state(st_name):
        boxes = []
        if st_name == 'overview':
            if portrait and count >= 3:
                cols = 2
                rows = (count + 1) // 2
                gap_x, gap_y = 26, 26
                cw = (usable_w - gap_x * (cols - 1)) / cols
                ch = min(cw * 1.15, (usable_h - gap_y * (rows - 1)) / rows)
                y_start = margin_y + (usable_h - (rows * ch + (rows - 1) * gap_y)) * 0.5
                for i in range(count):
                    r, c = divmod(i, cols)
                    bx = margin_x + c * (cw + gap_x)
                    by = y_start + r * (ch + gap_y)
                    boxes.append({'x': round(bx, 1), 'y': round(by, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 22.0, 'op': 1.0})
            else:
                gap = 28
                cw = (usable_w - gap * (count - 1)) / count
                ch = min(usable_h * 0.84, cw * (1.28 if portrait else 1.22))
                by = (h - ch) * 0.5
                for i in range(count):
                    bx = margin_x + i * (cw + gap)
                    boxes.append({'x': round(bx, 1), 'y': round(by, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 22.0, 'op': 1.0})
        elif st_name == 'split':
            gap = 26
            other = [i for i in range(count) if i != focus_idx]
            pair_b = other[0] if other else focus_idx
            if portrait:
                cw = usable_w
                ch = (usable_h - gap) * 0.48
                y0 = margin_y + (usable_h - (2 * ch + gap)) * 0.5
                for i in range(count):
                    if i == focus_idx:
                        boxes.append({'x': round(margin_x, 1), 'y': round(y0, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 24.0, 'op': 1.0})
                    elif i == pair_b:
                        boxes.append({'x': round(margin_x, 1), 'y': round(y0 + ch + gap, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 24.0, 'op': 1.0})
                    else:
                        boxes.append({'x': round(margin_x + cw * 0.15, 1), 'y': round(h + 90, 1), 'w': round(cw * 0.7, 1), 'h': round(ch * 0.5, 1), 'r': 20.0, 'op': 0.0})
            else:
                cw = (usable_w - gap) * 0.5
                ch = min(usable_h * 0.86, cw * 0.85)
                by = (h - ch) * 0.5
                for i in range(count):
                    if i == focus_idx:
                        boxes.append({'x': round(margin_x, 1), 'y': round(by, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 24.0, 'op': 1.0})
                    elif i == pair_b:
                        boxes.append({'x': round(margin_x + cw + gap, 1), 'y': round(by, 1), 'w': round(cw, 1), 'h': round(ch, 1), 'r': 24.0, 'op': 1.0})
                    else:
                        boxes.append({'x': round(w * 0.5 - cw * 0.25, 1), 'y': round(h + 100, 1), 'w': round(cw * 0.5, 1), 'h': round(ch * 0.5, 1), 'r': 18.0, 'op': 0.0})
        elif st_name == 'focus':
            others = [i for i in range(count) if i != focus_idx]
            if portrait:
                main_h = usable_h * 0.64
                rail_h = usable_h - main_h - 24
                rail_n = max(1, len(others))
                rail_w = (usable_w - 18 * (rail_n - 1)) / rail_n
                for i in range(count):
                    if i == focus_idx:
                        boxes.append({'x': round(margin_x, 1), 'y': round(margin_y, 1), 'w': round(usable_w, 1), 'h': round(main_h, 1), 'r': 26.0, 'op': 1.0})
                    else:
                        k = others.index(i)
                        boxes.append({'x': round(margin_x + k * (rail_w + 18), 1), 'y': round(margin_y + main_h + 24, 1), 'w': round(rail_w, 1), 'h': round(rail_h, 1), 'r': 18.0, 'op': 0.78})
            else:
                main_w = usable_w * 0.68
                rail_w = usable_w - main_w - 28
                rail_n = max(1, len(others))
                rail_h = (usable_h - 20 * (rail_n - 1)) / rail_n
                for i in range(count):
                    if i == focus_idx:
                        boxes.append({'x': round(margin_x, 1), 'y': round(margin_y, 1), 'w': round(main_w, 1), 'h': round(usable_h, 1), 'r': 28.0, 'op': 1.0})
                    else:
                        k = others.index(i)
                        boxes.append({'x': round(margin_x + main_w + 28, 1), 'y': round(margin_y + k * (rail_h + 20), 1), 'w': round(rail_w, 1), 'h': round(rail_h, 1), 'r': 18.0, 'op': 0.78})
        else:  # hero_zoom
            hw = usable_w * (0.94 if portrait else 0.82)
            hh = usable_h * (0.78 if portrait else 0.90)
            hx = (w - hw) * 0.5
            hy = (h - hh) * 0.5
            for i in range(count):
                if i == focus_idx:
                    boxes.append({'x': round(hx, 1), 'y': round(hy, 1), 'w': round(hw, 1), 'h': round(hh, 1), 'r': 30.0, 'op': 1.0})
                else:
                    side = -1.0 if i < focus_idx else 1.0
                    boxes.append({'x': round(hx + side * (hw * 0.92), 1), 'y': round(hy + 60, 1), 'w': round(hw * 0.42, 1), 'h': round(hh * 0.75, 1), 'r': 22.0, 'op': 0.0})
        return boxes

    def _camera_for_state(st_name):
        if st_name == 'overview':
            return {'tx': 0.0, 'ty': 0.0, 'scale': 1.0, 'rot': 0.0}
        if st_name == 'split':
            return {'tx': round(-p['camera_pan_x'] * 0.35, 2), 'ty': -4.0, 'scale': round(1.0 + (p['camera_zoom'] - 1.0) * 0.45, 3), 'rot': round(p['camera_tilt_deg'] * 0.45, 2)}
        if st_name == 'focus':
            return {'tx': 0.0, 'ty': -4.0, 'scale': round(1.0 + (p['camera_zoom'] - 1.0) * 0.35, 3), 'rot': round(p['camera_tilt_deg'] * 0.55, 2)}
        return {'tx': round(p['camera_pan_x'], 2), 'ty': -10.0, 'scale': round(p['camera_zoom'], 3), 'rot': round(p['camera_tilt_deg'], 2)}

    waypoints = [{'state': s, 'boxes': _layout_state(s), 'cam': _camera_for_state(s)} for s in states]

    cards_html = []
    for idx, c in enumerate(cards):
        if c.get('video'):
            src = html_lib.escape(asset_urls[f'card_{idx}'])
            bg_layer = f'<video id="hf-vid-{idx}" class="card-vid" src="{src}" data-start="0" data-duration="{duration}" muted playsinline preload="auto"></video>'
        elif c.get('image'):
            src = html_lib.escape(asset_urls[f'card_{idx}'])
            bg_layer = f'<img class="card-img" src="{src}" alt="" />'
        else:
            bg_layer = (
                f'<div class="card-grad" style="--c1:{c["accent_color"]};--c2:{p["secondary_color"]};">'
                f'<div class="grid-sheen"></div></div>'
            )
        sub_html = f'<div class="card-sub">{html_lib.escape(c["subtitle"])}</div>' if c['subtitle'] else ''
        cards_html.append(
            f'<div class="ui-card" id="ui-card-{idx}" style="--card-accent:{c["accent_color"]};">'
            f'{bg_layer}'
            f'<div class="card-scrim"></div>'
            f'<div class="card-tag">{html_lib.escape(c["tag"])}</div>'
            f'<div class="card-meta"><div class="card-title">{html_lib.escape(c["title"])}</div>{sub_html}</div>'
            f'</div>'
        )

    header_html = (
        f'<div class="stage-header"><span class="dot"></span><span>{html_lib.escape(p["header_label"])}</span></div>'
        if p['header_label'] else ''
    )

    cfg = {
        'w': w, 'h': h, 'duration': float(duration),
        'stiffness': float(p['spring_stiffness']),
        'damping': float(p['spring_damping']),
        'mass': float(p['spring_mass']),
        'blurStrength': float(p['motion_blur_strength']),
        'waypoints': waypoints,
        'count': count,
    }

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: {bg_rgba} !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      #camera-world {{
        position: relative;
        width: {w}px; height: {h}px;
        transform-origin: 50% 50%;
        will-change: transform, filter, opacity;
      }}
      .stage-header {{
        position: absolute; top: 44px; left: {margin_x}px;
        display: inline-flex; align-items: center; gap: 10px;
        padding: 8px 16px; border-radius: 999px;
        background: rgba(9, 13, 22, 0.80);
        border: 1px solid rgba(248, 250, 252, 0.18);
        color: #F8FAFC; font-size: 19px; font-weight: 600; letter-spacing: 0.08em;
      }}
      .stage-header .dot {{
        width: 10px; height: 10px; border-radius: 50%;
        background: {p['accent_color']}; box-shadow: 0 0 10px {p['accent_color']};
      }}
      .ui-card {{
        position: absolute; left: 0; top: 0; overflow: hidden;
        border: 1.5px solid rgba(248, 250, 252, 0.28);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.42), 0 0 0 1px rgba(15, 23, 42, 0.55);
        background: #0B101D;
        will-change: transform, width, height, border-radius, opacity;
      }}
      .card-img, .card-vid {{
        width: 100%; height: 100%; object-fit: cover; object-position: 50% 28%; display: block;
      }}
      .card-grad {{
        position: absolute; inset: 0;
        background: radial-gradient(circle at 22% 20%, var(--c1), transparent 58%),
                    linear-gradient(135deg, #111827 0%, #1E1B4B 65%, var(--c2) 140%);
      }}
      .grid-sheen {{
        position: absolute; inset: 0;
        background-image: radial-gradient(rgba(255,255,255,0.14) 1px, transparent 1px);
        background-size: 24px 24px; opacity: 0.45;
      }}
      .card-scrim {{
        position: absolute; inset: 0;
        background: linear-gradient(180deg, rgba(9, 13, 22, 0.16) 0%, rgba(9, 13, 22, 0.12) 48%, rgba(9, 13, 22, 0.88) 100%);
      }}
      .card-tag {{
        position: absolute; top: 18px; left: 18px;
        padding: 5px 12px; border-radius: 999px;
        background: rgba(9, 13, 22, 0.82);
        border: 1px solid var(--card-accent);
        color: #F8FAFC; font-size: 15px; font-weight: 700; letter-spacing: 0.08em;
      }}
      .card-meta {{
        position: absolute; left: 22px; right: 22px; bottom: 20px;
      }}
      .card-title {{
        color: #F8FAFC; font-size: 28px; font-weight: 800; line-height: 1.22;
        text-shadow: 0 2px 10px rgba(0, 0, 0, 0.72);
      }}
      .card-sub {{
        margin-top: 6px; color: #CBD5E1; font-size: 17px; font-weight: 500; line-height: 1.3;
        text-shadow: 0 2px 8px rgba(0, 0, 0, 0.65);
      }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="ui_flow_stage" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}" style="position:relative;width:{w}px;height:{h}px;background:{bg_rgba};">
      <svg width="0" height="0" style="position:absolute;pointer-events:none;">
        <defs>
          <filter id="motion-blur-filter" x="-15%" y="-15%" width="130%" height="130%">
            <feGaussianBlur id="cam-blur" in="SourceGraphic" stdDeviation="0,0" />
          </filter>
        </defs>
      </svg>
      <div id="camera-world">
        {header_html}
        <div id="cards-layer" style="position:absolute;inset:0;will-change:filter;">
          {''.join(cards_html)}
        </div>
      </div>
    </div>
    <script>
      const cfg = {json.dumps(cfg, ensure_ascii=False)};
      const worldEl = document.getElementById("camera-world");
      const cardsLayerEl = document.getElementById("cards-layer");
      const blurNode = document.getElementById("cam-blur");
      const cardEls = Array.from({{length: cfg.count}}, (_, i) => document.getElementById("ui-card-" + i));

      // Deterministic RK4 damped spring step-response table (inspired by motiondivision/motion)
      function buildSpringTable(stiffness, damping, mass, steps = 240, horizon = 1.15) {{
        const dt = horizon / steps;
        const tbl = new Float64Array(steps + 1);
        const vel = new Float64Array(steps + 1);
        let x = 0.0, v = 0.0;
        const accel = (px, pv) => (-stiffness * (px - 1.0) - damping * pv) / mass;
        for (let i = 1; i <= steps; i++) {{
          const k1x = v, k1v = accel(x, v);
          const k2x = v + 0.5 * dt * k1v, k2v = accel(x + 0.5 * dt * k1x, v + 0.5 * dt * k1v);
          const k3x = v + 0.5 * dt * k2v, k3v = accel(x + 0.5 * dt * k2x, v + 0.5 * dt * k2v);
          const k4x = v + dt * k3v, k4v = accel(x + dt * k3x, v + dt * k3v);
          x += (dt / 6.0) * (k1x + 2 * k2x + 2 * k3x + k4x);
          v += (dt / 6.0) * (k1v + 2 * k2v + 2 * k3v + k4v);
          tbl[i] = x;
          vel[i] = v;
        }}
        const endVal = Math.abs(tbl[steps]) > 1e-6 ? tbl[steps] : 1.0;
        for (let i = 0; i <= steps; i++) tbl[i] /= endVal;
        return {{ tbl, vel, steps }};
      }}

      const sp = buildSpringTable(cfg.stiffness, cfg.damping, cfg.mass);
      function evalSpring(u) {{
        const c = Math.max(0.0, Math.min(1.0, u));
        const pos = c * sp.steps;
        const i0 = Math.min(sp.steps - 1, Math.floor(pos));
        const frac = pos - i0;
        return {{
          v: sp.tbl[i0] + (sp.tbl[i0 + 1] - sp.tbl[i0]) * frac,
          dv: sp.vel[i0] + (sp.vel[i0 + 1] - sp.vel[i0]) * frac,
        }};
      }}

      function renderProgress(p) {{
        const nSeg = Math.max(1, cfg.waypoints.length - 1);
        const holdEnd = 0.90;
        const phase = Math.max(0.0, Math.min(1.0, p / holdEnd)) * nSeg;
        const seg = Math.min(nSeg - 1, Math.floor(phase));
        const localT = phase - seg;
        // Dwell ~28% before FLIP and ~26% settled after FLIP so each state reads cleanly
        const moveRatio = Math.max(0.0, Math.min(1.0, (localT - 0.28) / 0.46));
        const s = evalSpring(moveRatio);

        const wa = cfg.waypoints[seg];
        const wb = cfg.waypoints[seg + 1] || wa;

        const enterU = evalSpring(Math.min(1.0, p / 0.14));
        const fadeOut = p > 0.90 ? Math.max(0.0, 1.0 - (p - 0.90) / 0.10) : 1.0;

        const camTx = (wa.cam.tx + (wb.cam.tx - wa.cam.tx) * s.v) * 0.55;
        const camTy = (wa.cam.ty + (wb.cam.ty - wa.cam.ty) * s.v) * 0.55;
        const camSc = (wa.cam.scale + (wb.cam.scale - wa.cam.scale) * s.v) * (0.94 + 0.06 * enterU.v);
        const camRot = wa.cam.rot + (wb.cam.rot - wa.cam.rot) * s.v;
        worldEl.style.transform = `translate3d(${{camTx.toFixed(2)}}px, ${{camTy.toFixed(2)}}px, 0) scale(${{camSc.toFixed(4)}}) rotate(${{camRot.toFixed(2)}}deg)`;
        worldEl.style.opacity = String((Math.min(1.0, p / 0.06) * fadeOut).toFixed(4));

        let maxDx = 0.0, maxDy = 0.0;
        for (let i = 0; i < cfg.count; i++) {{
          const a = wa.boxes[i];
          const b = wb.boxes[i];
          const x = a.x + (b.x - a.x) * s.v;
          const y = a.y + (b.y - a.y) * s.v;
          const w = Math.max(20, a.w + (b.w - a.w) * s.v);
          const h = Math.max(20, a.h + (b.h - a.h) * s.v);
          const r = Math.max(0, a.r + (b.r - a.r) * s.v);
          const op = Math.max(0.0, Math.min(1.0, a.op + (b.op - a.op) * s.v));
          const el = cardEls[i];
          el.style.transform = `translate3d(${{x.toFixed(2)}}px, ${{y.toFixed(2)}}px, 0)`;
          el.style.width = `${{w.toFixed(1)}}px`;
          el.style.height = `${{h.toFixed(1)}}px`;
          el.style.borderRadius = `${{r.toFixed(1)}}px`;
          el.style.opacity = String(op.toFixed(3));
          const vx = Math.abs(b.x - a.x) * Math.abs(s.dv);
          const vy = Math.abs(b.y - a.y) * Math.abs(s.dv);
          if (vx > maxDx) maxDx = vx;
          if (vy > maxDy) maxDy = vy;
        }}

        const blurPx = (val) => Math.min(4.0, val * 0.0040 * cfg.blurStrength);
        const bx = blurPx(maxDx);
        const by = blurPx(maxDy);
        if (bx < 0.15 && by < 0.15) {{
          blurNode.setAttribute("stdDeviation", "0,0");
          cardsLayerEl.style.filter = "none";
        }} else {{
          blurNode.setAttribute("stdDeviation", `${{bx.toFixed(2)}},${{by.toFixed(2)}}`);
          cardsLayerEl.style.filter = "url(#motion-blur-filter)";
        }}
      }}

      const state = {{ progress: 0 }};
      const tl = gsap.timeline({{ paused: true }});
      tl.to(state, {{
        progress: 1,
        duration: cfg.duration,
        ease: "none",
        onUpdate: () => renderProgress(state.progress),
      }}, 0);
      renderProgress(0);
      window.__timelines = window.__timelines || {{}};
      window.__timelines["ui_flow_stage"] = tl;
    </script>
  </body>
</html>
"""


def _render_kinetic_ui_title_card(p, *, lw, lh, portrait, duration):
    w, h = int(lw), int(lh)
    title_str = p['title']
    tokens = []
    buf = []
    for ch in title_str:
        if ord(ch) > 127:
            if buf:
                tokens.append(''.join(buf))
                buf.clear()
            tokens.append(ch)
        elif ch.isspace():
            if buf:
                tokens.append(''.join(buf))
                buf.clear()
            tokens.append(' ')
        else:
            buf.append(ch)
    if buf:
        tokens.append(''.join(buf))

    token_spans = []
    anim_idx = 0
    for tk in tokens:
        if tk == ' ':
            token_spans.append('<span class="tok-space">&nbsp;</span>')
        else:
            token_spans.append(f'<span class="tok" data-tidx="{anim_idx}">{html_lib.escape(tk)}</span>')
            anim_idx += 1

    font_sz = min(p['font_size'], int(w * (0.115 if portrait else 0.088)))
    bg_rgba = _hex_to_rgba_css(p['bg_color'], round(p['bg_opacity'] / 100.0, 3))
    align_css = 'flex-start' if p['align'] == 'left' else 'center'
    text_align = 'left' if p['align'] == 'left' else 'center'
    top_px = int(round(h * p['position_y']))

    badge_html = (
        f'<div class="ui-badge" id="ui-badge">{html_lib.escape(p["badge"])}</div>'
        if p['badge'] else ''
    )
    sub_html = (
        f'<div class="ui-sub" id="ui-sub">{html_lib.escape(p["subtitle"])}</div>'
        if p['subtitle'] else ''
    )

    cfg = {
        'duration': float(duration),
        'staggerS': float(p['stagger_ms']) / 1000.0,
        'tokenCount': max(1, anim_idx),
        'revealStyle': p['reveal_style'],
        'stiffness': float(p['spring_stiffness']),
        'damping': float(p['spring_damping']),
        'hasGlass': bool(p['glass_card']),
        'hasBadge': bool(p['badge']),
        'hasSub': bool(p['subtitle']),
    }

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="../vendor/gsap.min.js"></script>
    <style>
      @font-face {{
        font-family: "OpenEditCJK";
        src: url("../vendor/cjk.ttf") format("truetype");
        font-weight: 100 900;
        font-style: normal;
      }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {w}px; height: {h}px; overflow: hidden;
        background: transparent !important;
        font-family: "OpenEditCJK", sans-serif;
      }}
      .wrap {{
        position: absolute; left: {80 if p['align'] == 'left' else 0}px;
        right: {80 if p['align'] == 'left' else 0}px;
        top: {top_px}px; transform: translateY(-50%);
        display: flex; justify-content: {align_css};
      }}
      .glass-panel {{
        display: inline-flex; flex-direction: column; align-items: {align_css};
        text-align: {text_align}; max-width: {int(w * 0.86)}px;
        padding: {('34px 48px' if p['glass_card'] else '12px 18px')};
        border-radius: 26px;
        background: {bg_rgba if p['glass_card'] else 'transparent'};
        border: {('1.5px solid rgba(248, 250, 252, 0.22)' if p['glass_card'] else 'none')};
        box-shadow: {('0 24px 60px rgba(0, 0, 0, 0.45)' if p['glass_card'] else 'none')};
        will-change: transform, clip-path, opacity;
      }}
      .ui-badge {{
        display: inline-flex; align-items: center; gap: 8px;
        padding: 6px 14px; border-radius: 999px; margin-bottom: 14px;
        background: rgba(9, 13, 22, 0.82);
        border: 1px solid {p['accent_color']};
        color: {p['accent_color']}; font-size: 18px; font-weight: 700; letter-spacing: 0.12em;
      }}
      .ui-title {{
        font-size: {font_sz}px; font-weight: 800; line-height: 1.15;
        color: {p['text_color']}; letter-spacing: 0.02em;
        text-shadow: 0 3px 16px rgba(0, 0, 0, 0.55);
      }}
      .tok {{
        display: inline-block;
        will-change: transform, filter, opacity;
      }}
      .tok-space {{ display: inline-block; width: 0.26em; }}
      .accent-rule {{
        width: 84px; height: 4px; border-radius: 99px; margin-top: 16px;
        background: {p['accent_color']};
        transform-origin: {('left center' if p['align'] == 'left' else 'center center')};
      }}
      .ui-sub {{
        margin-top: 12px; font-size: {max(20, int(font_sz * 0.30))}px;
        font-weight: 500; color: #E2E8F0; line-height: 1.35;
        text-shadow: 0 2px 10px rgba(0, 0, 0, 0.65);
      }}
    </style>
  </head>
  <body>
    <div id="card-root" data-composition-id="kinetic_ui_title" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}" style="position:relative;width:{w}px;height:{h}px;background:transparent;">
      <div class="wrap">
        <div class="glass-panel" id="glass-panel">
          {badge_html}
          <div class="ui-title">{''.join(token_spans)}</div>
          <div class="accent-rule" id="accent-rule"></div>
          {sub_html}
        </div>
      </div>
    </div>
    <script>
      const cfg = {json.dumps(cfg, ensure_ascii=False)};
      const panelEl = document.getElementById("glass-panel");
      const ruleEl = document.getElementById("accent-rule");
      const badgeEl = document.getElementById("ui-badge");
      const subEl = document.getElementById("ui-sub");
      const tokEls = Array.from(document.querySelectorAll(".tok"));

      function buildSpring(stiffness, damping, steps = 200, horizon = 0.95) {{
        const dt = horizon / steps;
        const tbl = new Float64Array(steps + 1);
        let x = 0.0, v = 0.0;
        const acc = (px, pv) => -stiffness * (px - 1.0) - damping * pv;
        for (let i = 1; i <= steps; i++) {{
          const k1x = v, k1v = acc(x, v);
          const k2x = v + 0.5 * dt * k1v, k2v = acc(x + 0.5 * dt * k1x, v + 0.5 * dt * k1v);
          const k3x = v + 0.5 * dt * k2v, k3v = acc(x + 0.5 * dt * k2x, v + 0.5 * dt * k2v);
          const k4x = v + dt * k3v, k4v = acc(x + dt * k3x, v + dt * k3v);
          x += (dt / 6.0) * (k1x + 2 * k2x + 2 * k3x + k4x);
          v += (dt / 6.0) * (k1v + 2 * k2v + 2 * k3v + k4v);
          tbl[i] = x;
        }}
        const endVal = Math.abs(tbl[steps]) > 1e-6 ? tbl[steps] : 1.0;
        for (let i = 0; i <= steps; i++) tbl[i] /= endVal;
        return {{ tbl, steps }};
      }}

      const sp = buildSpring(cfg.stiffness, cfg.damping);
      function springVal(t) {{
        const c = Math.max(0.0, Math.min(1.0, t));
        const pos = c * sp.steps;
        const i0 = Math.min(sp.steps - 1, Math.floor(pos));
        return sp.tbl[i0] + (sp.tbl[i0 + 1] - sp.tbl[i0]) * (pos - i0);
      }}

      function renderProgress(p) {{
        const t = p * cfg.duration;
        const fadeOut = p > 0.88 ? Math.max(0.0, 1.0 - (p - 0.88) / 0.12) : 1.0;
        const cardS = springVal(Math.min(1.0, t / 0.42));

        if (cfg.revealStyle === "clip_wipe") {{
          const rightInset = Math.max(0.0, (1.0 - Math.min(1.0, cardS)) * 100.0);
          panelEl.style.clipPath = `inset(0 ${{rightInset.toFixed(1)}}% 0 0 round 26px)`;
          panelEl.style.transform = `translate3d(0, ${{((1.0 - cardS) * 14).toFixed(2)}}px, 0)`;
        }} else if (cfg.revealStyle === "spring_card") {{
          panelEl.style.clipPath = "none";
          panelEl.style.transform = `translate3d(0, ${{((1.0 - cardS) * 28).toFixed(2)}}px, 0) scale(${{(0.86 + 0.14 * cardS).toFixed(4)}})`;
        }} else {{
          panelEl.style.clipPath = "none";
          panelEl.style.transform = `translate3d(0, ${{((1.0 - cardS) * 16).toFixed(2)}}px, 0) scale(${{(0.96 + 0.04 * cardS).toFixed(4)}})`;
        }}
        panelEl.style.opacity = String((Math.min(1.0, t / 0.14) * fadeOut).toFixed(3));

        for (let i = 0; i < tokEls.length; i++) {{
          const delay = 0.04 + i * cfg.staggerS;
          const u = Math.max(0.0, Math.min(1.0, (t - delay) / 0.46));
          const sv = springVal(u);
          const op = Math.min(1.0, u / 0.45);
          const blurPx = Math.max(0.0, (1.0 - Math.min(1.0, u / 0.72)) * 10.0);
          const ty = (1.0 - sv) * 22.0;
          const sc = 0.90 + 0.10 * sv;
          const el = tokEls[i];
          el.style.opacity = String(op.toFixed(3));
          el.style.transform = `translate3d(0, ${{ty.toFixed(2)}}px, 0) scale(${{sc.toFixed(4)}})`;
          el.style.filter = blurPx > 0.25 ? `blur(${{blurPx.toFixed(2)}}px)` : "none";
        }}

        const ruleU = springVal(Math.max(0.0, Math.min(1.0, (t - 0.14) / 0.45)));
        ruleEl.style.transform = `scaleX(${{Math.max(0.0, ruleU).toFixed(4)}})`;

        if (badgeEl) {{
          const bu = springVal(Math.min(1.0, t / 0.34));
          badgeEl.style.opacity = String(Math.min(1.0, t / 0.18).toFixed(3));
          badgeEl.style.transform = `translate3d(0, ${{((1.0 - bu) * -10).toFixed(2)}}px, 0)`;
        }}
        if (subEl) {{
          const su = springVal(Math.max(0.0, Math.min(1.0, (t - 0.18) / 0.48)));
          subEl.style.opacity = String(Math.max(0.0, Math.min(1.0, (t - 0.18) / 0.24)).toFixed(3));
          subEl.style.transform = `translate3d(0, ${{((1.0 - su) * 14).toFixed(2)}}px, 0)`;
        }}
      }}

      const state = {{ progress: 0 }};
      const tl = gsap.timeline({{ paused: true }});
      tl.to(state, {{
        progress: 1,
        duration: cfg.duration,
        ease: "none",
        onUpdate: () => renderProgress(state.progress),
      }}, 0);
      renderProgress(0);
      window.__timelines = window.__timelines || {{}};
      window.__timelines["kinetic_ui_title"] = tl;
    </script>
  </body>
</html>
"""


def _build_recipe_files(recipe, params, *, width, height, fps, duration):
    scale, lw, lh, portrait = _logical_viewport(width, height)
    index_html = _shell_html(width, height, fps, duration, scale=scale, lw=lw, lh=lh, card_id=recipe)
    asset_urls, _ = _recipe_asset_map(recipe, params)
    if recipe == 'kpi_bar_chart':
        card_html = _render_kpi_bar_chart_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'kpi_counter_banner':
        card_html = _render_kpi_counter_banner_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'comparison_table':
        card_html = _render_comparison_table_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'milestone_progress':
        card_html = _render_milestone_progress_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'viewfinder_hud':
        card_html = _render_viewfinder_hud_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'polaroid_grid':
        card_html = _render_polaroid_grid_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration, asset_urls=asset_urls)
    elif recipe == 'filmstrip_reel':
        card_html = _render_filmstrip_reel_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration, asset_urls=asset_urls)
    elif recipe == 'text_matte_dissolve':
        card_html = _render_text_matte_dissolve_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration, asset_urls=asset_urls)
    elif recipe == 'ui_flow_stage':
        card_html = _render_ui_flow_stage_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration, asset_urls=asset_urls)
    elif recipe == 'kinetic_ui_title':
        card_html = _render_kinetic_ui_title_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    else:
        raise ValueError(f'Unsupported HyperFrames recipe: {recipe}')
    return {'index.html': index_html, 'compositions/card.html': card_html}


def _overlay_cache_key(
    cli_path,
    spec,
    *,
    width,
    height,
    fps,
    duration,
    render_video,
    preview_time,
    gpu,
    quality,
    generated_files,
    font_file,
    gsap_file,
):
    cli_file = Path(cli_path)
    if not cli_file.is_file():
        return None
    resolved_cli = cli_file.resolve()
    cli_stat = resolved_cli.stat()
    asset_hashes = {}
    if font_file is not None and Path(font_file).is_file():
        asset_hashes['font'] = file_hash.sha256(font_file)
    if gsap_file is not None and Path(gsap_file).is_file():
        asset_hashes['gsap'] = file_hash.sha256(gsap_file)
    if 'recipe' in spec:
        for idx, img_path in enumerate(_collect_recipe_images(spec['recipe'], spec.get('params'))):
            p_obj = Path(img_path).resolve()
            if not p_obj.is_file():
                return None
            asset_hashes[f'recipe_media:{idx}:{p_obj}'] = file_hash.sha256(p_obj)
    if 'html' in spec and not spec['html'].lstrip().startswith('<'):
        html_path = Path(spec['html']).resolve()
        if not html_path.is_file():
            return None
        asset_hashes['html_entry'] = file_hash.sha256(html_path)
        for sub_name in ('compositions', 'assets', 'vendor'):
            sub_dir = html_path.parent / sub_name
            if sub_dir.is_dir():
                for child in sorted(sub_dir.rglob('*')):
                    if child.is_file():
                        asset_hashes[child.relative_to(html_path.parent).as_posix()] = file_hash.sha256(child)

    payload = {
        'schema': 1,
        'backend': 'hyperframes',
        'cli': {'path': str(resolved_cli), 'size': cli_stat.st_size, 'mtime_ns': cli_stat.st_mtime_ns},
        'width': int(width),
        'height': int(height),
        'fps': float(fps),
        'duration': float(duration),
        'preview_time': float(preview_time),
        'render_video': bool(render_video),
        'gpu': bool(gpu),
        'quality': str(quality),
        'recipe': spec.get('recipe'),
        'params': spec.get('params'),
        'variables': spec.get('variables'),
        'check': bool(spec.get('check', False)),
        'generated_files': generated_files,
        'asset_hashes': asset_hashes,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _try_restore_cache(entry_dir, out, comp_name, *, render_video, width, height, fps, duration, spec, cache_key, cache_root, quality):
    meta_path = entry_dir / 'cache-meta.json'
    if not meta_path.is_file():
        return None
    try:
        meta = core.read_json(meta_path)
    except (ValueError, OSError):
        return None
    if not isinstance(meta, dict) or meta.get('schema') != 1 or meta.get('backend') != 'hyperframes':
        return None
    if (meta.get('width') != int(width) or meta.get('height') != int(height)
            or float(meta.get('fps', -1)) != float(fps) or float(meta.get('duration', -1)) != float(duration)
            or bool(meta.get('render_video')) != bool(render_video) or meta.get('quality') != quality):
        return None
    cached_html = entry_dir / 'index.html'
    cached_png = entry_dir / 'preview_rgba.png'
    cached_mov = entry_dir / 'overlay.mov'
    if not cached_html.is_file() or not cached_png.is_file():
        return None
    if file_hash.sha256(cached_html) != meta.get('index_html_sha256') or file_hash.sha256(cached_png) != meta.get('preview_png_sha256'):
        return None
    try:
        with Image.open(cached_png) as img:
            if img.mode != 'RGBA' or img.size != (width, height):
                return None
    except OSError:
        return None
    if render_video:
        if not cached_mov.is_file() or not isinstance(meta.get('overlay_video'), dict):
            return None
        if file_hash.sha256(cached_mov) != meta['overlay_video'].get('sha256'):
            return None

    proj_dir = out / 'project'
    proj_dir.mkdir(parents=True, exist_ok=True)
    entry_out = proj_dir / 'index.html'
    shutil.copy2(cached_html, entry_out)
    preview_png = out / 'preview_rgba.png'
    shutil.copy2(cached_png, preview_png)
    overlay_video = None
    if render_video:
        mov_path = out / f'{comp_name}.mov'
        shutil.copy2(cached_mov, mov_path)
        overlay_video = dict(meta['overlay_video'])
        overlay_video['path'] = str(mov_path)

    result = {
        'status': 'passed',
        'backend': 'hyperframes',
        'comp': comp_name,
        'project': str(entry_out),
        'preview_png': str(preview_png),
        'width': width,
        'height': height,
        'fps': fps,
        'duration': duration,
        'quality': quality,
        'overlay_video': overlay_video,
        'cache': {'hit': True, 'key': cache_key, 'cache_dir': str(cache_root)},
    }
    if 'recipe' in spec:
        result['recipe'] = spec['recipe']
    return result


def _store_cache(entry_dir, entry_html, preview_png, result, *, render_video):
    entry_dir.parent.mkdir(parents=True, exist_ok=True)
    if entry_dir.is_dir():
        return
    stage = entry_dir.parent / f'.tmp-{entry_dir.name[:8]}-{secrets.token_hex(4)}'
    stage.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(entry_html, stage / 'index.html')
        shutil.copy2(preview_png, stage / 'preview_rgba.png')
        ov_meta = None
        if render_video and isinstance(result.get('overlay_video'), dict):
            ov_info = dict(result['overlay_video'])
            shutil.copy2(ov_info['path'], stage / 'overlay.mov')
            ov_meta = {k: v for k, v in ov_info.items() if k != 'path'}
        meta = {
            'schema': 1,
            'backend': 'hyperframes',
            'width': result['width'],
            'height': result['height'],
            'fps': result['fps'],
            'duration': result['duration'],
            'quality': result['quality'],
            'render_video': bool(render_video),
            'index_html_sha256': file_hash.sha256(entry_html),
            'preview_png_sha256': file_hash.sha256(preview_png),
            'overlay_video': ov_meta,
        }
        core.write_json(stage / 'cache-meta.json', meta)
        try:
            stage.replace(entry_dir)
        except OSError:
            pass
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _stage_video_asset(src_path, dst_path, *, source_in, duration, fps, max_w, max_h):
    """Trim/loop a local video to fast-seek muted 8-bit CFR H.264 MP4 for HyperFrames live containers."""
    info = core.probe(src_path)
    if not info.get('has_video'):
        raise ValueError(f'Video file has no video stream: {src_path}')
    src_dur = float(info.get('duration') or 0.0)
    eff_in = max(0.0, float(source_in or 0.0))
    if src_dur > 0.05 and eff_in >= src_dur - 0.05:
        raise ValueError(f'Video source_in {eff_in:.2f}s exceeds video duration {src_dur:.2f}s: {src_path}')
    target_dur = round(max(0.25, float(duration) + 0.15), 4)
    fps_val = float(fps)
    fps_str = str(int(fps_val)) if abs(fps_val - round(fps_val)) < 1e-6 else f'{fps_val:.6g}'
    vf = (
        f"scale=w='min({int(max_w)},iw)':h='min({int(max_h)},ih)':"
        f"force_original_aspect_ratio=decrease:force_divisible_by=2,fps={fps_str}"
    )
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
    if src_dur > 0.05 and (src_dur - eff_in) < target_dur:
        cmd.extend(['-stream_loop', '-1', '-i', str(src_path)])
        if eff_in > 1e-4:
            cmd.extend(['-ss', f'{eff_in:.4f}'])
    else:
        if eff_in > 1e-4:
            cmd.extend(['-ss', f'{eff_in:.4f}'])
        cmd.extend(['-i', str(src_path)])
    cmd.extend([
        '-t', f'{target_dur:.4f}',
        '-vf', vf,
        '-an',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-g', '15',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        str(dst_path),
    ])
    core.run(cmd, timeout=180)
    if not dst_path.is_file():
        raise RuntimeError(f'Failed to stage HyperFrames video asset: {dst_path}')


def _stage_project(proj_dir, spec, *, width, height, fps, duration, gsap_file, font_file):
    proj_dir.mkdir(parents=True, exist_ok=True)
    if 'recipe' in spec:
        recipe = spec['recipe']
        files = _build_recipe_files(recipe, spec['params'], width=width, height=height, fps=fps, duration=duration)
        for rel, content in files.items():
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        _, staged_media = _recipe_asset_map(recipe, spec['params'])
        max_w = min(int(width), 1280 if recipe in ('ui_flow_stage', 'text_matte_dissolve') else 960)
        max_h = min(int(height), 720 if recipe in ('ui_flow_stage', 'text_matte_dissolve') else 540)
        for item in staged_media:
            dst = proj_dir / 'assets' / item['dst_name']
            if item['kind'] == 'video':
                _stage_video_asset(
                    item['path'], dst,
                    source_in=item['source_in'], duration=duration, fps=fps, max_w=max_w, max_h=max_h,
                )
            else:
                _link_or_copy(item['path'], dst)
        _link_or_copy(gsap_file, proj_dir / 'vendor' / 'gsap.min.js')
        _link_or_copy(font_file, proj_dir / 'vendor' / 'cjk.ttf')
        return proj_dir / 'index.html', files

    raw_html = spec['html']
    if raw_html.lstrip().startswith('<'):
        entry = proj_dir / 'index.html'
        entry.write_text(raw_html, encoding='utf-8')
        if gsap_file is not None:
            _link_or_copy(gsap_file, proj_dir / 'vendor' / 'gsap.min.js')
        if font_file is not None:
            _link_or_copy(font_file, proj_dir / 'vendor' / 'cjk.ttf')
        return entry, {'index.html': raw_html}

    src_html = Path(raw_html).resolve()
    src_root = src_html.parent
    for sub_name in ('compositions', 'assets', 'vendor', 'fonts', 'styles', 'images'):
        sub_dir = src_root / sub_name
        if sub_dir.is_dir():
            shutil.copytree(sub_dir, proj_dir / sub_name, dirs_exist_ok=True)
    for child in src_root.iterdir():
        if (child.is_file()
                and child.resolve() != src_html
                and child.suffix.lower() in ('.css', '.js', '.mjs', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff2', '.ttf', '.otf', '.json', '.html')
                and child.stat().st_size <= 32 * 1024 * 1024):
            shutil.copy2(child, proj_dir / child.name)
    entry = proj_dir / 'index.html'
    shutil.copy2(src_html, entry)
    return entry, None


def _parse_json_output(stdout_text, label):
    text = (stdout_text or '').strip()
    if not text:
        return None
    for idx, ch in enumerate(text):
        if ch in '{[':
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f'{label} produced non-JSON output: {text[-300:]}')


def _run_hf(args, *, env, timeout=180, check=True):
    result = subprocess.run(
        [str(x) for x in args],
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if check and result.returncode:
        tail = (result.stdout + '\n' + result.stderr).strip()[-2000:]
        raise RuntimeError(f'{Path(str(args[0])).name} failed: {tail}')
    return result


def build_overlay(
    spec,
    out_dir,
    *,
    width=None,
    height=None,
    fps=None,
    render_video=True,
    preview_time=None,
    gpu=False,
    resolution='full',
    quality='best',
    draft=False,
    use_cache=True,
    cache_dir=None,
    base_dir=None,
):
    """Compile a HyperFrames data-motion recipe or offline HTML spec into RGBA PNG and ProRes 4444 .mov."""
    cli = find_cli()
    if cli is None:
        raise ValueError(
            'HyperFrames CLI not found; install into %LOCALAPPDATA%\\OpenEditTools\\hyperframes '
            'or set OPENEDIT_HYPERFRAMES_CLI'
        )
    spec = validate_overlay_spec(dict(spec), base_dir=base_dir, label='overlay')
    if 'duration' not in spec:
        raise ValueError('overlay.duration is required')
    w = int(core.number(spec.get('width', width or 1920), 'overlay.width', 64, 7680))
    h = int(core.number(spec.get('height', height or 1080), 'overlay.height', 64, 4320))
    rate = float(core.number(spec.get('fps', fps or 30), 'overlay.fps', 1, 120))
    duration = float(core.number(spec['duration'], 'overlay.duration', 0.04, 3600))
    pt = round(min(max(0.0, duration * 0.5), max(0.0, duration - 0.05)), 3) if preview_time is None else float(preview_time)
    if resolution not in ('full', 'half', 'third', 'quarter'):
        raise ValueError(f'Unsupported overlay resolution: {resolution}')
    if quality not in ('best', 'draft'):
        raise ValueError(f'Unsupported overlay quality: {quality}')
    hf_quality = 'draft' if (draft or quality == 'draft') else 'looks'
    gpu_flag = bool(spec.get('gpu', gpu))
    comp_name = spec.get('name', 'Overlay')

    gsap_file = runtime_paths.discover_hyperframes_gsap(cli)
    font_file = runtime_paths.discover_cjk_font_file(spec.get('font_path'))
    if 'recipe' in spec:
        if gsap_file is None:
            raise ValueError('Local gsap.min.js not found alongside HyperFrames; install gsap in the HyperFrames tool folder')
        if font_file is None:
            raise ValueError('No local static CJK font file found; pass font_path or set OPENEDIT_CJK_FONT_PATH')
        generated_files = _build_recipe_files(spec['recipe'], spec['params'], width=w, height=h, fps=rate, duration=duration)
    else:
        generated_files = {'index.html': spec['html']} if spec['html'].lstrip().startswith('<') else None

    cache_key = None
    cache_root = None
    entry_dir = None
    if use_cache:
        cache_key = _overlay_cache_key(
            cli, spec, width=w, height=h, fps=rate, duration=duration,
            render_video=render_video, preview_time=pt, gpu=gpu_flag, quality=hf_quality,
            generated_files=generated_files, font_file=font_file, gsap_file=gsap_file,
        )
        if cache_key is not None:
            cache_root = effectcraft_backend.overlay_cache_dir(cache_dir)
            entry_dir = cache_root / cache_key[:2] / cache_key

    out = core.new_directory(out_dir)
    t0 = time.monotonic()
    if entry_dir is not None:
        cached = _try_restore_cache(
            entry_dir, out, comp_name, render_video=render_video,
            width=w, height=h, fps=rate, duration=duration, spec=spec,
            cache_key=cache_key, cache_root=cache_root, quality=hf_quality,
        )
        if cached is not None:
            cached['elapsed_seconds'] = round(time.monotonic() - t0, 3)
            core.write_json(out / 'overlay-report.json', cached)
            return cached

    proj_dir = out / 'project'
    entry_html, _ = _stage_project(
        proj_dir, spec, width=w, height=h, fps=rate, duration=duration,
        gsap_file=gsap_file, font_file=font_file,
    )
    cli_prefix = _cli_command(cli)
    env = _subprocess_env()

    # Always run static lint first (~0.5s).
    lint_done = _run_hf([*cli_prefix, 'lint', '--json', str(proj_dir)], env=env, timeout=60, check=False)
    lint_payload = _parse_json_output(lint_done.stdout, 'hyperframes lint')
    if lint_done.returncode != 0:
        raise RuntimeError(f'HyperFrames lint failed: {(lint_done.stdout + lint_done.stderr)[-1200:]}')

    check_summary = None
    if spec.get('check') and not draft:
        chk_done = _run_hf([*cli_prefix, 'check', '--json', str(proj_dir)], env=env, timeout=180, check=False)
        check_summary = _parse_json_output(chk_done.stdout, 'hyperframes check')
        if chk_done.returncode != 0:
            raise RuntimeError(f'HyperFrames check failed: {(chk_done.stdout + chk_done.stderr)[-1200:]}')

    variables_arg = []
    if spec.get('variables'):
        vars_path = out / 'variables.json'
        core.write_json(vars_path, spec['variables'])
        variables_arg = ['--variables-file', str(vars_path)]
    preview_png = out / 'preview_rgba.png'
    overlay_video = None

    if render_video:
        mov_path = out / f'{comp_name}.mov'
        fps_str = str(int(rate)) if abs(rate - round(rate)) < 1e-6 else f'{rate:.6g}'
        cmd = [
            *cli_prefix, 'render', str(proj_dir),
            '--format', 'mov',
            '--fps', fps_str,
            '--quality', hf_quality,
            '--output', str(mov_path),
            *variables_arg,
        ]
        if gpu_flag:
            cmd.append('--browser-gpu')
        _run_hf(cmd, env=env, timeout=600)
        if not mov_path.is_file():
            raise RuntimeError(f'HyperFrames ProRes 4444 render produced no output: {mov_path}')
        info = core.probe(mov_path)
        if not info.get('has_alpha'):
            raise RuntimeError(f'HyperFrames exported MOV lacks alpha channel (pix_fmt={info.get("pix_fmt")})')
        core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(mov_path), '-f', 'null', '-'], timeout=300)
        core.run([
            core.tool('ffmpeg'), '-y', '-v', 'error',
            '-ss', f'{pt:.3f}', '-i', str(mov_path),
            '-frames:v', '1', '-pix_fmt', 'rgba', str(preview_png),
        ], timeout=120)
        overlay_video = {
            'path': str(mov_path),
            'sha256': file_hash.sha256(mov_path),
            'bytes': mov_path.stat().st_size,
            'pix_fmt': info.get('pix_fmt'),
            'codec': info.get('video_codec'),
            'strict_decode': 'passed',
        }
    else:
        snap_dir = out / 'snapshots'
        cmd = [
            *cli_prefix, 'snapshot', str(proj_dir),
            '--at', f'{pt:.3f}', '--no-end',
            '--output', str(snap_dir), '--json',
        ]
        if gpu_flag:
            cmd.append('--browser-gpu')
        snap_done = _run_hf(cmd, env=env, timeout=180)
        snap_payload = _parse_json_output(snap_done.stdout, 'hyperframes snapshot')
        snap_files = []
        if isinstance(snap_payload, dict):
            for key in ('files', 'snapshots', 'frames'):
                val = snap_payload.get(key)
                if isinstance(val, list) and val:
                    first = val[0]
                    p_str = first.get('path') if isinstance(first, dict) else first
                    if isinstance(p_str, str) and Path(p_str).is_file():
                        snap_files.append(Path(p_str))
                        break
        if not snap_files:
            snap_files = sorted(snap_dir.glob('frame-*.png'))
        if not snap_files:
            raise RuntimeError(f'HyperFrames snapshot produced no PNG in {snap_dir}')
        shutil.copy2(snap_files[0], preview_png)

    with Image.open(preview_png) as img:
        if img.mode != 'RGBA' or img.size != (w, h):
            raise RuntimeError(f'HyperFrames PNG preview unexpected mode/size: {img.mode} {img.size}')

    result = {
        'status': 'passed',
        'backend': 'hyperframes',
        'comp': comp_name,
        'project': str(entry_html),
        'preview_png': str(preview_png),
        'width': w,
        'height': h,
        'fps': rate,
        'duration': duration,
        'quality': hf_quality,
        'lint': lint_payload,
        'check': check_summary,
        'overlay_video': overlay_video,
    }
    if 'recipe' in spec:
        result['recipe'] = spec['recipe']

    if entry_dir is not None:
        _store_cache(entry_dir, entry_html, preview_png, result, render_video=render_video)
        result['cache'] = {'hit': False, 'stored': True, 'key': cache_key, 'cache_dir': str(cache_root)}
    elif not use_cache:
        result['cache'] = {'hit': False, 'stored': False, 'reason': 'disabled'}
    else:
        result['cache'] = {'hit': False, 'stored': False, 'reason': 'bypassed'}

    result['elapsed_seconds'] = round(time.monotonic() - t0, 3)
    core.write_json(out / 'overlay-report.json', result)
    return result
