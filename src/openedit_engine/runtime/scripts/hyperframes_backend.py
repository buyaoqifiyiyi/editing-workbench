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
)

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


def _validate_recipe_params(recipe, raw_params, label):
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

    # milestone_progress
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
        ov['params'] = _validate_recipe_params(ov['recipe'], ov.get('params'), label)
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


def _build_recipe_files(recipe, params, *, width, height, fps, duration):
    scale, lw, lh, portrait = _logical_viewport(width, height)
    index_html = _shell_html(width, height, fps, duration, scale=scale, lw=lw, lh=lh, card_id=recipe)
    if recipe == 'kpi_bar_chart':
        card_html = _render_kpi_bar_chart_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'kpi_counter_banner':
        card_html = _render_kpi_counter_banner_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'comparison_table':
        card_html = _render_comparison_table_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
    elif recipe == 'milestone_progress':
        card_html = _render_milestone_progress_card(params, lw=lw, lh=lh, portrait=portrait, duration=duration)
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


def _stage_project(proj_dir, spec, *, width, height, fps, duration, gsap_file, font_file):
    proj_dir.mkdir(parents=True, exist_ok=True)
    if 'recipe' in spec:
        files = _build_recipe_files(spec['recipe'], spec['params'], width=width, height=height, fps=fps, duration=duration)
        for rel, content in files.items():
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
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
