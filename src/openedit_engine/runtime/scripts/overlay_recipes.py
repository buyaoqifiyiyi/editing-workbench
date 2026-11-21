"""Declarative, resolution-adaptive EffectCraft motion-graphics recipe registry and compiler."""
import re

VALID_TEXT_PRESETS = frozenset({
    'none',
    'typewriter',
    'fadeUpCharacters',
    'bounceInWords',
    'trackingIn',
    'scramble',
    'blurIn',
    'jitter',
    'dropInLines',
})
VALID_BLEND_MODES = frozenset({'Add', 'Screen', 'Overlay', 'Normal'})
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$')


def _check_hex(value, label, allow_alpha=False):
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise ValueError(f'{label} must be #RRGGBB' + (' or #RRGGBBAA' if allow_alpha else ''))
    return value[:7] if not allow_alpha else value


def _check_num(value, label, low, high):
    if type(value) not in (int, float) or float(value) != float(value) or float(value) in (float('inf'), float('-inf')):
        raise ValueError(f'{label} must be a finite number')
    val = float(value)
    if val < low or val > high:
        raise ValueError(f'{label} must be in [{low}, {high}]')
    return val


def _check_bool(value, label):
    if type(value) is not bool:
        raise ValueError(f'{label} must be boolean')
    return value


def _check_text(value, label, max_len=200, *, allow_empty=False):
    if not isinstance(value, str) or '\0' in value or len(value) > max_len:
        raise ValueError(f'{label} must be text (<= {max_len} chars)')
    if not allow_empty and not value.strip():
        raise ValueError(f'{label} must be non-empty text')
    return value


def _check_highlights(spans, text, label):
    if not isinstance(spans, list):
        raise ValueError(f'{label} must be an array')
    norm = []
    for idx, sp in enumerate(spans):
        if not isinstance(sp, dict):
            raise ValueError(f'{label}[{idx}] must be an object')
        unknown = set(sp) - {'start', 'end', 'color'}
        if unknown:
            raise ValueError(f'{label}[{idx}] unsupported keys: {", ".join(sorted(unknown))}')
        for req in ('start', 'end', 'color'):
            if req not in sp:
                raise ValueError(f'{label}[{idx}] missing required key: {req}')
        st, en = sp['start'], sp['end']
        if type(st) is not int or type(en) is not int or not (0 <= st < en <= len(text)):
            raise ValueError(f'{label}[{idx}] requires integer 0 <= start < end <= {len(text)}')
        col = _check_hex(sp['color'], f'{label}[{idx}].color')
        norm.append({'start': st, 'end': en, 'color': col})
    norm.sort(key=lambda item: item['start'])
    for a, b in zip(norm, norm[1:]):
        if a['end'] > b['start']:
            raise ValueError(f'{label} highlight spans must not overlap')
    return norm


def _text_visual_units(text):
    """Estimate normalized width units so Chinese fullwidth and Latin glyphs size cards cleanly."""
    total = 0.0
    for ch in text:
        total += 1.0 if ord(ch) > 127 else 0.58
    return max(2.0, total)


def _fade_out_keys(duration, end_fade, *, start_op=100.0):
    t_out_start = round(max(0.0, duration - end_fade), 4)
    return [{'time': t_out_start, 'value': start_op}, {'time': round(duration, 4), 'value': 0.0}]


def _validate_params(recipe_name, raw_params, duration):
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise ValueError(f'overlay recipe {recipe_name} params must be an object')
    meta = RECIPES[recipe_name]
    allowed = set(meta['params'])
    unknown = set(raw_params) - allowed
    if unknown:
        raise ValueError(f'overlay recipe {recipe_name} unsupported params: {", ".join(sorted(unknown))}')

    p = dict(raw_params)
    if recipe_name == 'title_card':
        if 'title' not in p:
            raise ValueError('overlay recipe title_card requires params.title')
        p['title'] = _check_text(p['title'], 'params.title', 200)
        if 'subtitle' in p and p['subtitle'] is not None:
            p['subtitle'] = _check_text(p['subtitle'], 'params.subtitle', 240, allow_empty=True)
        else:
            p['subtitle'] = ''
        p['highlights'] = _check_highlights(p.get('highlights', []), p['title'], 'params.highlights') if 'highlights' in p else []
        p['accent_color'] = _check_hex(p.get('accent_color', '#38BDF8'), 'params.accent_color')
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        p['bg_color'] = _check_hex(p.get('bg_color', '#0F172A'), 'params.bg_color')
        p['bg_opacity'] = _check_num(p.get('bg_opacity', 82.0), 'params.bg_opacity', 0.0, 100.0)
        p['stroke_color'] = _check_hex(p.get('stroke_color', '#0B1220'), 'params.stroke_color')
        p['glow'] = _check_bool(p.get('glow', True), 'params.glow')
        tp = p.get('text_preset', 'fadeUpCharacters')
        if tp not in VALID_TEXT_PRESETS:
            raise ValueError(f'params.text_preset must be one of {sorted(VALID_TEXT_PRESETS)}')
        p['text_preset'] = tp
        p['position_y'] = _check_num(p.get('position_y', 0.5), 'params.position_y', 0.1, 0.9)
    elif recipe_name == 'lower_third':
        if 'title' not in p:
            raise ValueError('overlay recipe lower_third requires params.title')
        p['title'] = _check_text(p['title'], 'params.title', 120)
        if 'subtitle' in p and p['subtitle'] is not None:
            p['subtitle'] = _check_text(p['subtitle'], 'params.subtitle', 180, allow_empty=True)
        else:
            p['subtitle'] = ''
        p['accent_color'] = _check_hex(p.get('accent_color', '#38BDF8'), 'params.accent_color')
        p['bg_color'] = _check_hex(p.get('bg_color', '#0B1220'), 'params.bg_color')
        p['bg_opacity'] = _check_num(p.get('bg_opacity', 84.0), 'params.bg_opacity', 0.0, 100.0)
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        align = p.get('align', 'left')
        if align not in ('left', 'right'):
            raise ValueError('params.align must be left or right')
        p['align'] = align
        p['position_y'] = _check_num(p.get('position_y', 0.83), 'params.position_y', 0.15, 0.92)
    elif recipe_name == 'keyword_callout':
        if 'text' not in p:
            raise ValueError('overlay recipe keyword_callout requires params.text')
        p['text'] = _check_text(p['text'], 'params.text', 160)
        p['highlights'] = _check_highlights(p.get('highlights', []), p['text'], 'params.highlights') if 'highlights' in p else []
        p['color'] = _check_hex(p.get('color', '#FFFFFF'), 'params.color')
        p['stroke_color'] = _check_hex(p.get('stroke_color', '#0B1220'), 'params.stroke_color')
        p['stroke_width'] = _check_num(p.get('stroke_width', 5.0), 'params.stroke_width', 0.0, 24.0)
        if 'size' in p:
            p['size'] = _check_num(p['size'], 'params.size', 16.0, 300.0)
        p['glow'] = _check_bool(p.get('glow', True), 'params.glow')
        tp = p.get('text_preset', 'bounceInWords')
        if tp not in VALID_TEXT_PRESETS:
            raise ValueError(f'params.text_preset must be one of {sorted(VALID_TEXT_PRESETS)}')
        p['text_preset'] = tp
        p['position_y'] = _check_num(p.get('position_y', 0.72), 'params.position_y', 0.1, 0.9)
    elif recipe_name == 'beat_flash':
        p['color'] = _check_hex(p.get('color', '#FFFFFF'), 'params.color')
        default_peak = round(duration * 0.35, 3)
        p['peak_time'] = _check_num(p.get('peak_time', default_peak), 'params.peak_time', 0.0, duration)
        p['peak_opacity'] = _check_num(p.get('peak_opacity', 85.0), 'params.peak_opacity', 1.0, 100.0)
        bm = p.get('blend_mode', 'Add')
        if bm not in VALID_BLEND_MODES:
            raise ValueError(f'params.blend_mode must be one of {sorted(VALID_BLEND_MODES)}')
        p['blend_mode'] = bm
        p['glow'] = _check_bool(p.get('glow', True), 'params.glow')
    elif recipe_name == 'cinematic_bars':
        p['bar_ratio'] = _check_num(p.get('bar_ratio', 0.11), 'params.bar_ratio', 0.02, 0.30)
        p['color'] = _check_hex(p.get('color', '#000000'), 'params.color')
        default_in = min(0.4, round(duration * 0.25, 3))
        p['animate_in'] = _check_num(p.get('animate_in', default_in), 'params.animate_in', 0.0, duration)
        p['animate_out'] = _check_num(p.get('animate_out', 0.0), 'params.animate_out', 0.0, duration)
    elif recipe_name == 'progress_banner':
        if 'label' in p and p['label'] is not None:
            p['label'] = _check_text(p['label'], 'params.label', 120, allow_empty=True)
        else:
            p['label'] = ''
        p['progress_start'] = _check_num(p.get('progress_start', 0.0), 'params.progress_start', 0.0, 1.0)
        p['progress_end'] = _check_num(p.get('progress_end', 1.0), 'params.progress_end', 0.0, 1.0)
        p['bar_color'] = _check_hex(p.get('bar_color', '#38BDF8'), 'params.bar_color')
        p['track_color'] = _check_hex(p.get('track_color', '#1E293B'), 'params.track_color')
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        pos = p.get('position', 'bottom')
        if pos not in ('top', 'bottom'):
            raise ValueError('params.position must be top or bottom')
        p['position'] = pos
    elif recipe_name == 'chapter_opener':
        if 'title' not in p:
            raise ValueError('overlay recipe chapter_opener requires params.title')
        p['title'] = _check_text(p['title'], 'params.title', 120)
        p['badge'] = _check_text(p['badge'], 'params.badge', 24) if p.get('badge') is not None else '01'
        p['subtitle'] = _check_text(p['subtitle'], 'params.subtitle', 200, allow_empty=True) if p.get('subtitle') is not None else ''
        p['highlights'] = _check_highlights(p.get('highlights', []), p['title'], 'params.highlights') if 'highlights' in p else []
        p['accent_color'] = _check_hex(p.get('accent_color', '#38BDF8'), 'params.accent_color')
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        p['bg_color'] = _check_hex(p.get('bg_color', '#0B1220'), 'params.bg_color')
        p['bg_opacity'] = _check_num(p.get('bg_opacity', 78.0), 'params.bg_opacity', 0.0, 100.0)
        cs = p.get('card_style', 'card')
        if cs not in ('card', 'minimal'):
            raise ValueError('params.card_style must be card or minimal')
        p['card_style'] = cs
        p['glow'] = _check_bool(p.get('glow', True), 'params.glow')
        tp = p.get('text_preset', 'fadeUpCharacters')
        if tp not in VALID_TEXT_PRESETS:
            raise ValueError(f'params.text_preset must be one of {sorted(VALID_TEXT_PRESETS)}')
        p['text_preset'] = tp
        p['position_y'] = _check_num(p.get('position_y', 0.5), 'params.position_y', 0.15, 0.85)
    elif recipe_name == 'pip_card':
        p['title'] = _check_text(p['title'], 'params.title', 80, allow_empty=True) if p.get('title') is not None else ''
        corner = p.get('corner', 'bottom_right')
        if corner not in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
            raise ValueError('params.corner must be top_left, top_right, bottom_left, or bottom_right')
        p['corner'] = corner
        p['width_ratio'] = _check_num(p.get('width_ratio', 0.32), 'params.width_ratio', 0.18, 0.65)
        p['aspect'] = _check_num(p.get('aspect', 1.7778), 'params.aspect', 0.4, 2.5)
        p['border_color'] = _check_hex(p.get('border_color', '#38BDF8'), 'params.border_color')
        p['border_width'] = _check_num(p.get('border_width', 6.0), 'params.border_width', 1.0, 24.0)
        p['bg_color'] = _check_hex(p.get('bg_color', '#0F172A'), 'params.bg_color')
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        p['glow'] = _check_bool(p.get('glow', False), 'params.glow')
    elif recipe_name == 'split_screen_frame':
        layout = p.get('layout', 'vertical_2')
        if layout not in ('vertical_2', 'horizontal_2', 'vertical_3'):
            raise ValueError('params.layout must be vertical_2, horizontal_2, or vertical_3')
        p['layout'] = layout
        p['divider_color'] = _check_hex(p.get('divider_color', '#FFFFFF'), 'params.divider_color')
        p['divider_width'] = _check_num(p.get('divider_width', 6.0), 'params.divider_width', 2.0, 32.0)
        expected_panels = 3 if layout == 'vertical_3' else 2
        if 'labels' in p and p['labels'] is not None:
            raw_labels = p['labels']
            if not isinstance(raw_labels, list) or len(raw_labels) != expected_panels:
                raise ValueError(f'params.labels for {layout} must be an array of {expected_panels} strings')
            p['labels'] = [_check_text(item, f'params.labels[{i}]', 40) for i, item in enumerate(raw_labels)]
        else:
            p['labels'] = []
        p['badge_color'] = _check_hex(p.get('badge_color', '#0F172A'), 'params.badge_color')
        p['text_color'] = _check_hex(p.get('text_color', '#FFFFFF'), 'params.text_color')
        p['animate_in'] = _check_num(p.get('animate_in', min(0.35, round(duration * 0.25, 3))), 'params.animate_in', 0.0, duration)
    elif recipe_name == 'wipe_transition':
        direction = p.get('direction', 'left')
        if direction not in ('left', 'right', 'up', 'down'):
            raise ValueError('params.direction must be left, right, up, or down')
        p['direction'] = direction
        p['color'] = _check_hex(p.get('color', '#0F172A'), 'params.color')
        p['accent_color'] = _check_hex(p.get('accent_color', '#38BDF8'), 'params.accent_color')
        p['angle_offset'] = _check_num(p.get('angle_offset', 12.0), 'params.angle_offset', -35.0, 35.0)
        p['feather'] = _check_num(p.get('feather', 16.0), 'params.feather', 0.0, 100.0)
        p['peak_hold'] = _check_num(p.get('peak_hold', 0.12), 'params.peak_hold', 0.0, 0.5)
    return p


def _expand_title_card(p, w, h, dur):
    unit = min(w, h)
    title_sz = round(max(26.0, min(96.0, unit * 0.068)))
    sub_sz = round(max(18.0, title_sz * 0.48))
    has_sub = bool(p['subtitle'])
    title_units = _text_visual_units(p['title'])
    sub_units = _text_visual_units(p['subtitle']) if has_sub else 0.0
    content_w = max(title_units * title_sz, sub_units * sub_sz) + title_sz * 2.2
    card_w = round(min(w * 0.88, max(w * 0.42, content_w)))
    card_h = round((title_sz * 1.9 + sub_sz * 1.65) if has_sub else title_sz * 2.15)
    cx = round(w * 0.5, 2)
    cy = round(h * p['position_y'], 2)
    wipe_in = round(min(0.45, dur * 0.25), 3)
    fade_out = round(min(0.35, dur * 0.2), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.4 else []

    card_layer = {
        'type': 'shape',
        'kind': 'rounded',
        'name': 'title_card_bg',
        'size': [card_w, card_h],
        'position': [cx, cy],
        'fill': p['bg_color'],
        'fillOpacity': p['bg_opacity'],
        'stroke': p['accent_color'],
        'strokeWidth': 3.0,
        'effects': [
            {'name': 'Linear Wipe', 'values': {'completion': 100.0, 'angle': -90.0, 'feather': 28.0}},
        ],
        'keyframes': [
            {'path': 'effects/#1/completion', 'keys': [{'time': 0.0, 'value': 100.0}, {'time': wipe_in, 'value': 0.0}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    }

    accent_w = round(min(card_w * 0.72, max(120.0, card_w * 0.45)))
    accent_layer = {
        'type': 'shape',
        'kind': 'rect',
        'name': 'title_accent_bar',
        'size': [accent_w, max(4, round(unit * 0.0055))],
        'position': [cx, round(cy - card_h * 0.5 + 8.0, 2)],
        'fill': p['accent_color'],
        'fillOpacity': 100.0,
        'strokeWidth': 0.0,
        'keyframes': [
            {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [0.0, 100.0]}, {'time': wipe_in, 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    }

    title_cy = cy - card_h * 0.14 if has_sub else cy
    title_y = round(title_cy + title_sz * 0.34, 2)
    title_effects = [{'name': 'Glow', 'values': {'threshold': 55.0, 'radius': 22.0, 'intensity': 1.15}}] if p['glow'] else []
    title_kfs = []
    if p['text_preset'] != 'none':
        title_kfs.append({
            'path': 'text/animators/#1/selectors/#1/offset',
            'keys': [{'time': 0.0, 'value': -100.0}, {'time': round(min(0.65, dur * 0.38), 3), 'value': 100.0}],
            'interpolation': 'easyEaseOut',
        })
    if out_keys:
        title_kfs.append({'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'})
    title_layer = {
        'type': 'text',
        'name': 'title_main',
        'text': p['title'],
        'size': title_sz,
        'color': p['text_color'],
        'stroke_color': p['stroke_color'],
        'stroke_width': max(2.0, round(title_sz * 0.055, 1)),
        'justify': 'center',
        'position': [cx, title_y],
        'styles': ['dropShadow'],
        'effects': title_effects,
        **({'highlights': p['highlights']} if p['highlights'] else {}),
        **({'text_preset': p['text_preset']} if p['text_preset'] != 'none' else {}),
        **({'keyframes': title_kfs} if title_kfs else {}),
    }

    layers = [card_layer, accent_layer, title_layer]
    if has_sub:
        sub_y = round(cy + card_h * 0.22 + sub_sz * 0.34, 2)
        sub_in_end = round(min(dur * 0.55, wipe_in + 0.2), 3)
        sub_kfs = [{'path': 'transform/opacity', 'keys': [{'time': 0.0, 'value': 0.0}, {'time': sub_in_end, 'value': 92.0}, *out_keys], 'interpolation': 'easyEase'}]
        layers.append({
            'type': 'text',
            'name': 'title_sub',
            'text': p['subtitle'],
            'size': sub_sz,
            'color': p['accent_color'],
            'stroke_color': p['stroke_color'],
            'stroke_width': max(1.0, round(sub_sz * 0.04, 1)),
            'justify': 'center',
            'position': [cx, sub_y],
            'styles': ['dropShadow'],
            'keyframes': sub_kfs,
        })
    return layers


def _expand_lower_third(p, w, h, dur):
    unit = min(w, h)
    title_sz = round(max(22.0, min(58.0, unit * 0.042)))
    sub_sz = round(max(16.0, title_sz * 0.58))
    has_sub = bool(p['subtitle'])
    max_units = max(_text_visual_units(p['title']), _text_visual_units(p['subtitle']) * 0.65 if has_sub else 0.0)
    card_w = round(min(w * 0.68, max(w * 0.28, max_units * title_sz + title_sz * 2.2)))
    card_h = round((title_sz * 1.55 + sub_sz * 1.5) if has_sub else title_sz * 1.95)
    margin_x = round(max(48.0, w * 0.055))
    is_left = p['align'] == 'left'
    cx = round((margin_x + card_w * 0.5) if is_left else (w - margin_x - card_w * 0.5), 2)
    cy = round(h * p['position_y'], 2)

    t_in = round(min(0.4, dur * 0.25), 3)
    fade_out = round(min(0.35, dur * 0.2), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.4 else []
    wipe_angle = -90.0 if is_left else 90.0

    panel = {
        'type': 'shape',
        'kind': 'rounded',
        'name': 'lt_panel',
        'size': [card_w, card_h],
        'position': [cx, cy],
        'fill': p['bg_color'],
        'fillOpacity': p['bg_opacity'],
        'stroke': p['accent_color'],
        'strokeWidth': 2.0,
        'effects': [{'name': 'Linear Wipe', 'values': {'completion': 100.0, 'angle': wipe_angle, 'feather': 18.0}}],
        'keyframes': [
            {'path': 'effects/#1/completion', 'keys': [{'time': 0.0, 'value': 100.0}, {'time': t_in, 'value': 0.0}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    }

    pill_w = max(8, round(unit * 0.008))
    pill_x = round((cx - card_w * 0.5 + pill_w * 1.2) if is_left else (cx + card_w * 0.5 - pill_w * 1.2), 2)
    pill = {
        'type': 'shape',
        'kind': 'rounded',
        'name': 'lt_accent',
        'size': [pill_w, round(card_h * 0.72)],
        'position': [pill_x, cy],
        'fill': p['accent_color'],
        'fillOpacity': 100.0,
        'strokeWidth': 0.0,
        'keyframes': [
            {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [100.0, 0.0]}, {'time': round(t_in * 0.75, 3), 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    }

    pad_in = pill_w * 2.8
    text_x = round((cx - card_w * 0.5 + pad_in) if is_left else (cx + card_w * 0.5 - pad_in), 2)
    slide_dx = -24.0 if is_left else 24.0
    title_cy = cy - card_h * 0.16 if has_sub else cy
    title_y = round(title_cy + title_sz * 0.34, 2)
    title_layer = {
        'type': 'text',
        'name': 'lt_title',
        'text': p['title'],
        'size': title_sz,
        'color': p['text_color'],
        'stroke_color': '#08101C',
        'stroke_width': 2.0,
        'justify': p['align'],
        'position': [text_x, title_y],
        'styles': ['dropShadow'],
        'keyframes': [
            {'path': 'transform/position', 'keys': [{'time': 0.0, 'value': [text_x + slide_dx, title_y]}, {'time': t_in, 'value': [text_x, title_y]}], 'interpolation': 'easyEaseOut'},
            {'path': 'transform/opacity', 'keys': [{'time': 0.0, 'value': 0.0}, {'time': t_in, 'value': 100.0}, *out_keys], 'interpolation': 'easyEase'},
        ],
    }

    layers = [panel, pill, title_layer]
    if has_sub:
        sub_y = round(cy + card_h * 0.22 + sub_sz * 0.34, 2)
        sub_in = round(min(dur * 0.6, t_in + 0.12), 3)
        layers.append({
            'type': 'text',
            'name': 'lt_sub',
            'text': p['subtitle'],
            'size': sub_sz,
            'color': p['accent_color'],
            'stroke_color': '#08101C',
            'stroke_width': 1.5,
            'justify': p['align'],
            'position': [text_x, sub_y],
            'styles': ['dropShadow'],
            'keyframes': [
                {'path': 'transform/opacity', 'keys': [{'time': 0.0, 'value': 0.0}, {'time': sub_in, 'value': 95.0}, *out_keys], 'interpolation': 'easyEase'},
            ],
        })
    return layers


def _expand_keyword_callout(p, w, h, dur):
    unit = min(w, h)
    sz = round(float(p.get('size', max(30.0, min(110.0, unit * 0.082)))))
    cx = round(w * 0.5, 2)
    cy = round(h * p['position_y'] + sz * 0.34, 2)
    t_in = round(min(0.24, dur * 0.25), 3)
    fade_out = round(min(0.25, dur * 0.2), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.3 else []
    kfs = [
        {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [84.0, 84.0]}, {'time': t_in, 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
    ]
    if p['text_preset'] != 'none':
        kfs.append({
            'path': 'text/animators/#1/selectors/#1/offset',
            'keys': [{'time': 0.0, 'value': -100.0}, {'time': round(min(0.45, dur * 0.35), 3), 'value': 100.0}],
            'interpolation': 'easyEaseOut',
        })
    if out_keys:
        kfs.append({'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'})
    return [{
        'type': 'text',
        'name': 'keyword_callout',
        'text': p['text'],
        'size': sz,
        'color': p['color'],
        'stroke_color': p['stroke_color'],
        'stroke_width': p['stroke_width'],
        'justify': 'center',
        'position': [cx, cy],
        'styles': ['dropShadow'],
        'effects': [{'name': 'Glow', 'values': {'threshold': 50.0, 'radius': 24.0, 'intensity': 1.25}}] if p['glow'] else [],
        'keyframes': kfs,
        **({'highlights': p['highlights']} if p['highlights'] else {}),
        **({'text_preset': p['text_preset']} if p['text_preset'] != 'none' else {}),
    }]


def _expand_beat_flash(p, w, h, dur):
    del w, h
    peak_t = float(p['peak_time'])
    keys = [{'time': 0.0, 'value': 0.0}]
    if 1e-4 < peak_t < dur - 1e-4:
        keys.append({'time': round(peak_t, 4), 'value': p['peak_opacity']})
    elif peak_t <= 1e-4:
        keys[0]['value'] = p['peak_opacity']
    keys.append({'time': round(dur, 4), 'value': 0.0})
    return [{
        'type': 'solid',
        'name': 'beat_flash',
        'color': p['color'],
        'blend_mode': p['blend_mode'],
        'effects': [{'name': 'Glow', 'values': {'threshold': 35.0, 'radius': 36.0, 'intensity': 1.5}}] if p['glow'] else [],
        'keyframes': [{'path': 'transform/opacity', 'keys': keys, 'interpolation': 'easyEaseOut'}],
    }]


def _expand_cinematic_bars(p, w, h, dur):
    bar_h = max(8, round(h * p['bar_ratio']))
    t_in = float(p['animate_in'])
    t_out = float(p['animate_out'])
    top_start_y = round(-bar_h * 0.5, 2)
    top_end_y = round(bar_h * 0.5, 2)
    bot_start_y = round(h + bar_h * 0.5, 2)
    bot_end_y = round(h - bar_h * 0.5, 2)
    cx = round(w * 0.5, 2)

    def _bar_keys(y_off, y_on):
        if t_in <= 1e-4 and t_out <= 1e-4:
            return []
        keys = []
        if t_in > 1e-4:
            keys.extend([{'time': 0.0, 'value': [cx, y_off]}, {'time': round(t_in, 4), 'value': [cx, y_on]}])
        else:
            keys.append({'time': 0.0, 'value': [cx, y_on]})
        if t_out > 1e-4 and dur > t_out + 1e-4:
            t_hold = round(max(t_in, dur - t_out), 4)
            if t_hold > keys[-1]['time'] + 1e-4:
                keys.append({'time': t_hold, 'value': [cx, y_on]})
            keys.append({'time': round(dur, 4), 'value': [cx, y_off]})
        return [{'path': 'transform/position', 'keys': keys, 'interpolation': 'easyEase'}]

    return [
        {
            'type': 'shape',
            'kind': 'rect',
            'name': 'cinema_bar_top',
            'size': [w + 4, bar_h],
            'position': [cx, top_end_y],
            'fill': p['color'],
            'fillOpacity': 100.0,
            'strokeWidth': 0.0,
            'keyframes': _bar_keys(top_start_y, top_end_y),
        },
        {
            'type': 'shape',
            'kind': 'rect',
            'name': 'cinema_bar_bottom',
            'size': [w + 4, bar_h],
            'position': [cx, bot_end_y],
            'fill': p['color'],
            'fillOpacity': 100.0,
            'strokeWidth': 0.0,
            'keyframes': _bar_keys(bot_start_y, bot_end_y),
        },
    ]


def _expand_progress_banner(p, w, h, dur):
    bar_w = round(w * 0.84)
    bar_h = max(12, round(h * 0.016))
    cx = round(w * 0.5, 2)
    cy = round(h * (0.065 if p['position'] == 'top' else 0.93), 2)
    comp_start = round((1.0 - p['progress_start']) * 100.0, 2)
    comp_end = round((1.0 - p['progress_end']) * 100.0, 2)

    track = {
        'type': 'shape',
        'kind': 'rounded',
        'name': 'progress_track',
        'size': [bar_w, bar_h],
        'position': [cx, cy],
        'fill': p['track_color'],
        'fillOpacity': 78.0,
        'stroke': '#475569',
        'strokeWidth': 1.5,
    }
    fill = {
        'type': 'shape',
        'kind': 'rounded',
        'name': 'progress_fill',
        'size': [bar_w, bar_h],
        'position': [cx, cy],
        'fill': p['bar_color'],
        'fillOpacity': 100.0,
        'strokeWidth': 0.0,
        'effects': [{'name': 'Linear Wipe', 'values': {'completion': comp_start, 'angle': -90.0, 'feather': 0.0}}],
        'keyframes': [{'path': 'effects/#1/completion', 'keys': [{'time': 0.0, 'value': comp_start}, {'time': round(dur, 4), 'value': comp_end}], 'interpolation': 'linear'}],
    }
    layers = [track, fill]
    if p['label']:
        lbl_sz = round(max(18.0, min(40.0, min(w, h) * 0.03)))
        offset = (bar_h * 0.5 + lbl_sz * 1.15) if p['position'] == 'top' else -(bar_h * 0.5 + lbl_sz * 0.9)
        lbl_x = round(cx - bar_w * 0.5, 2)
        lbl_y = round(cy + offset + lbl_sz * 0.34, 2)
        layers.append({
            'type': 'text',
            'name': 'progress_label',
            'text': p['label'],
            'size': lbl_sz,
            'color': p['text_color'],
            'stroke_color': '#0B1220',
            'stroke_width': 2.0,
            'justify': 'left',
            'position': [lbl_x, lbl_y],
            'styles': ['dropShadow'],
        })
    return layers


def _expand_chapter_opener(p, w, h, dur):
    unit = min(w, h)
    title_sz = round(max(28.0, min(92.0, unit * 0.066)))
    sub_sz = round(max(18.0, title_sz * 0.45))
    badge_sz = round(max(16.0, title_sz * 0.34))
    has_sub = bool(p['subtitle'])
    cx = round(w * 0.5, 2)
    cy = round(h * p['position_y'], 2)
    wipe_in = round(min(0.45, dur * 0.25), 3)
    fade_out = round(min(0.35, dur * 0.2), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.4 else []

    title_units = _text_visual_units(p['title'])
    sub_units = _text_visual_units(p['subtitle']) if has_sub else 0.0
    content_w = max(title_units * title_sz, sub_units * sub_sz, badge_sz * 4.0) + title_sz * 2.2
    card_w = round(min(w * 0.90, max(w * 0.46, content_w)))
    card_h = round(badge_sz * 2.3 + title_sz * 1.85 + (sub_sz * 1.65 if has_sub else 0.0))

    layers = []
    if p['card_style'] == 'card':
        layers.append({
            'type': 'shape',
            'kind': 'rounded',
            'name': 'chapter_card_bg',
            'size': [card_w, card_h],
            'position': [cx, cy],
            'fill': p['bg_color'],
            'fillOpacity': p['bg_opacity'],
            'stroke': p['accent_color'],
            'strokeWidth': 2.5,
            'effects': [{'name': 'Linear Wipe', 'values': {'completion': 100.0, 'angle': -90.0, 'feather': 24.0}}],
            'keyframes': [
                {'path': 'effects/#1/completion', 'keys': [{'time': 0.0, 'value': 100.0}, {'time': wipe_in, 'value': 0.0}], 'interpolation': 'easyEaseOut'},
                *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
            ],
        })

    badge_w = round(max(badge_sz * (_text_visual_units(p['badge']) + 1.8), badge_sz * 3.2))
    badge_h = round(badge_sz * 1.75)
    badge_cy = round(cy - card_h * 0.31, 2)
    layers.append({
        'type': 'shape',
        'kind': 'rounded',
        'name': 'chapter_badge_bg',
        'size': [badge_w, badge_h],
        'position': [cx, badge_cy],
        'fill': p['accent_color'],
        'fillOpacity': 96.0,
        'strokeWidth': 0.0,
        'keyframes': [
            {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [0.0, 0.0]}, {'time': round(min(0.28, dur * 0.2), 3), 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    })
    layers.append({
        'type': 'text',
        'name': 'chapter_badge_text',
        'text': p['badge'],
        'size': badge_sz,
        'color': '#0B1220',
        'stroke': False,
        'stroke_width': 0.0,
        'justify': 'center',
        'position': [cx, round(badge_cy + badge_sz * 0.34, 2)],
        **({'keyframes': [{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}]} if out_keys else {}),
    })

    line_w = round(min(card_w * 0.56, max(120.0, card_w * 0.42)))
    line_y = round(badge_cy + badge_h * 0.72, 2)
    layers.append({
        'type': 'shape',
        'kind': 'rect',
        'name': 'chapter_divider',
        'size': [line_w, max(3, round(unit * 0.0045))],
        'position': [cx, line_y],
        'fill': p['accent_color'],
        'fillOpacity': 95.0,
        'strokeWidth': 0.0,
        'keyframes': [
            {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [0.0, 100.0]}, {'time': wipe_in, 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    })

    title_cy = cy + title_sz * (0.05 if has_sub else 0.16)
    title_kfs = []
    if p['text_preset'] != 'none':
        title_kfs.append({
            'path': 'text/animators/#1/selectors/#1/offset',
            'keys': [{'time': 0.0, 'value': -100.0}, {'time': round(min(0.65, dur * 0.38), 3), 'value': 100.0}],
            'interpolation': 'easyEaseOut',
        })
    if out_keys:
        title_kfs.append({'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'})
    layers.append({
        'type': 'text',
        'name': 'chapter_title',
        'text': p['title'],
        'size': title_sz,
        'color': p['text_color'],
        'stroke_color': '#0B1220',
        'stroke_width': max(2.0, round(title_sz * 0.055, 1)),
        'justify': 'center',
        'position': [cx, round(title_cy + title_sz * 0.34, 2)],
        'styles': ['dropShadow'],
        'effects': [{'name': 'Glow', 'values': {'threshold': 55.0, 'radius': 20.0, 'intensity': 1.1}}] if p['glow'] else [],
        **({'highlights': p['highlights']} if p['highlights'] else {}),
        **({'text_preset': p['text_preset']} if p['text_preset'] != 'none' else {}),
        **({'keyframes': title_kfs} if title_kfs else {}),
    })

    if has_sub:
        sub_cy = title_cy + title_sz * 0.92
        layers.append({
            'type': 'text',
            'name': 'chapter_subtitle',
            'text': p['subtitle'],
            'size': sub_sz,
            'color': p['accent_color'],
            'stroke_color': '#0B1220',
            'stroke_width': max(1.5, round(sub_sz * 0.05, 1)),
            'justify': 'center',
            'position': [cx, round(sub_cy + sub_sz * 0.34, 2)],
            'styles': ['dropShadow'],
            'keyframes': [
                {'path': 'transform/opacity', 'keys': [
                    {'time': 0.0, 'value': 0.0},
                    {'time': round(min(0.32, dur * 0.25), 3), 'value': 100.0},
                    *([k for k in out_keys[1:]] if out_keys else []),
                ], 'interpolation': 'easeOut'},
            ],
        })
    return layers


def _expand_pip_card(p, w, h, dur):
    unit = min(w, h)
    margin_x = round(w * 0.055, 2)
    margin_y = round(h * 0.065, 2)
    has_title = bool(p['title'])
    pill_h = round(max(28.0, min(52.0, unit * 0.048))) if has_title else 0
    box_w = round(w * p['width_ratio'])
    box_h = round(min(h * 0.70, max(80.0, box_w / p['aspect'])))

    is_left = p['corner'].endswith('left')
    is_top = p['corner'].startswith('top')
    cx = round(margin_x + box_w * 0.5, 2) if is_left else round(w - margin_x - box_w * 0.5, 2)
    top_guard = margin_y + (pill_h + 6 if is_top and has_title else 0)
    cy = round(top_guard + box_h * 0.5, 2) if is_top else round(h - margin_y - box_h * 0.5, 2)

    fade_out = round(min(0.3, dur * 0.18), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.4 else []
    pop_in = round(min(0.28, dur * 0.22), 3)

    layers = [{
        'type': 'shape',
        'kind': 'rounded',
        'name': 'pip_frame',
        'size': [box_w, box_h],
        'position': [cx, cy],
        'fillType': 'none',
        'fillOpacity': 0.0,
        'stroke': p['border_color'],
        'strokeWidth': p['border_width'],
        'styles': ['dropShadow'],
        'effects': [{'name': 'Glow', 'values': {'threshold': 55.0, 'radius': 18.0, 'intensity': 1.0}}] if p['glow'] else [],
        'keyframes': [
            {'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': [92.0, 92.0]}, {'time': pop_in, 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'},
            *([{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}] if out_keys else []),
        ],
    }]

    if has_title:
        lbl_sz = round(max(16.0, pill_h * 0.52))
        pill_w = round(min(box_w, max(pill_h * 2.5, (_text_visual_units(p['title']) + 1.8) * lbl_sz)))
        pill_cx = round(cx - box_w * 0.5 + pill_w * 0.5, 2) if is_left else round(cx + box_w * 0.5 - pill_w * 0.5, 2)
        pill_cy = round(cy - box_h * 0.5 - pill_h * 0.35, 2)
        layers.append({
            'type': 'shape',
            'kind': 'rounded',
            'name': 'pip_header_bg',
            'size': [pill_w, pill_h],
            'position': [pill_cx, pill_cy],
            'fill': p['bg_color'],
            'fillOpacity': 92.0,
            'stroke': p['border_color'],
            'strokeWidth': max(1.5, round(p['border_width'] * 0.4, 1)),
            **({'keyframes': [{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}]} if out_keys else {}),
        })
        layers.append({
            'type': 'text',
            'name': 'pip_header_text',
            'text': p['title'],
            'size': lbl_sz,
            'color': p['text_color'],
            'stroke': False,
            'stroke_width': 0.0,
            'justify': 'center',
            'position': [pill_cx, round(pill_cy + lbl_sz * 0.34, 2)],
            **({'keyframes': [{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}]} if out_keys else {}),
        })
    return layers


def _expand_split_screen_frame(p, w, h, dur):
    dw = p['divider_width']
    anim = p['animate_in']
    fade_out = round(min(0.28, dur * 0.18), 3)
    out_keys = _fade_out_keys(dur, fade_out) if dur >= 0.4 else []
    layers = []

    if p['layout'] == 'vertical_2':
        bar_specs = [('split_divider_1', [dw, h + 4], [round(w * 0.5, 2), round(h * 0.5, 2)], [100.0, 0.0])]
        anchors = [(round(w * 0.08, 2), round(h * 0.08, 2)), (round(w * 0.58, 2), round(h * 0.08, 2))]
    elif p['layout'] == 'horizontal_2':
        bar_specs = [('split_divider_1', [w + 4, dw], [round(w * 0.5, 2), round(h * 0.5, 2)], [0.0, 100.0])]
        anchors = [(round(w * 0.08, 2), round(h * 0.08, 2)), (round(w * 0.08, 2), round(h * 0.56, 2))]
    else:
        bar_specs = [
            ('split_divider_1', [dw, h + 4], [round(w / 3.0, 2), round(h * 0.5, 2)], [100.0, 0.0]),
            ('split_divider_2', [dw, h + 4], [round(w * 2.0 / 3.0, 2), round(h * 0.5, 2)], [100.0, 0.0]),
        ]
        anchors = [(round(w * 0.06, 2), round(h * 0.08, 2)), (round(w * 0.39, 2), round(h * 0.08, 2)), (round(w * 0.72, 2), round(h * 0.08, 2))]

    for name, size, pos, start_scale in bar_specs:
        kfs = []
        if anim > 0:
            kfs.append({'path': 'transform/scale', 'keys': [{'time': 0.0, 'value': start_scale}, {'time': anim, 'value': [100.0, 100.0]}], 'interpolation': 'easyEaseOut'})
        if out_keys:
            kfs.append({'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'})
        layers.append({
            'type': 'shape',
            'kind': 'rect',
            'name': name,
            'size': size,
            'position': pos,
            'fill': p['divider_color'],
            'fillOpacity': 100.0,
            'strokeWidth': 0.0,
            'styles': ['dropShadow'],
            **({'keyframes': kfs} if kfs else {}),
        })

    if p['labels']:
        lbl_sz = round(max(16.0, min(38.0, min(w, h) * 0.032)))
        pill_h = round(lbl_sz * 1.65)
        for idx, (lbl, (ax, ay)) in enumerate(zip(p['labels'], anchors)):
            pill_w = round(max(pill_h * 2.2, (_text_visual_units(lbl) + 1.8) * lbl_sz))
            cx = round(ax + pill_w * 0.5, 2)
            cy = round(ay + pill_h * 0.5, 2)
            layers.append({
                'type': 'shape',
                'kind': 'rounded',
                'name': f'split_label_bg_{idx}',
                'size': [pill_w, pill_h],
                'position': [cx, cy],
                'fill': p['badge_color'],
                'fillOpacity': 84.0,
                'stroke': p['divider_color'],
                'strokeWidth': 1.5,
                **({'keyframes': [{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}]} if out_keys else {}),
            })
            layers.append({
                'type': 'text',
                'name': f'split_label_text_{idx}',
                'text': lbl,
                'size': lbl_sz,
                'color': p['text_color'],
                'stroke': False,
                'stroke_width': 0.0,
                'justify': 'center',
                'position': [cx, round(cy + lbl_sz * 0.34, 2)],
                **({'keyframes': [{'path': 'transform/opacity', 'keys': out_keys, 'interpolation': 'easeOut'}]} if out_keys else {}),
            })
    return layers


def _expand_wipe_transition(p, w, h, dur):
    del w, h
    base_in = {'left': -90.0, 'right': 90.0, 'up': 180.0, 'down': 0.0}[p['direction']]
    angle_in = round(base_in + p['angle_offset'], 2)
    angle_out = round(angle_in + 180.0, 2)
    hold_half = p['peak_hold'] * 0.5
    t_mid = dur * 0.5
    t_in_end = round(max(0.02, t_mid - dur * hold_half), 4)
    t_out_start = round(min(dur - 0.02, t_mid + dur * hold_half), 4)
    lead_shift = round(min(dur * 0.08, t_in_end * 0.25), 4)

    def _wipe_layer(name, color, in_end, out_start, out_end):
        return {
            'type': 'solid',
            'name': name,
            'color': color,
            'effects': [
                {'name': 'Linear Wipe', 'values': {'completion': 100.0, 'angle': angle_in, 'feather': p['feather']}},
                {'name': 'Linear Wipe', 'values': {'completion': 0.0, 'angle': angle_out, 'feather': p['feather']}},
            ],
            'keyframes': [
                {'path': 'effects/#1/completion', 'keys': [{'time': 0.0, 'value': 100.0}, {'time': in_end, 'value': 0.0}], 'interpolation': 'easeOut'},
                {'path': 'effects/#2/completion', 'keys': [{'time': out_start, 'value': 0.0}, {'time': out_end, 'value': 100.0}], 'interpolation': 'easeIn'},
            ],
        }

    return [
        _wipe_layer('wipe_accent', p['accent_color'], max(0.02, round(t_in_end - lead_shift, 4)), t_out_start, round(dur, 4)),
        _wipe_layer('wipe_main', p['color'], t_in_end, max(t_in_end, round(t_out_start - lead_shift, 4)), max(t_out_start, round(dur - lead_shift, 4))),
    ]


RECIPES = {
    'title_card': {
        'description': 'Centered rounded backdrop + accent line + main/subtitle text with wipe reveal, highlights, and drop shadow.',
        'params': {
            'title': 'required str (<=200)',
            'subtitle': 'optional str (<=240)',
            'highlights': 'optional [{start, end, color}] within title',
            'accent_color': 'hex color (default #38BDF8)',
            'text_color': 'hex color (default #FFFFFF)',
            'bg_color': 'hex color (default #0F172A)',
            'bg_opacity': '0..100 (default 82)',
            'stroke_color': 'hex color (default #0B1220)',
            'glow': 'bool (default true)',
            'text_preset': 'text preset or none (default fadeUpCharacters)',
            'position_y': '0.1..0.9 normalized vertical center (default 0.5)',
        },
        'builder': _expand_title_card,
    },
    'lower_third': {
        'description': 'Broadcast speaker/topic lower-third bar with accent pill, directional wipe reveal, and text slide.',
        'params': {
            'title': 'required str (<=120)',
            'subtitle': 'optional str (<=180)',
            'accent_color': 'hex color (default #38BDF8)',
            'bg_color': 'hex color (default #0B1220)',
            'bg_opacity': '0..100 (default 84)',
            'text_color': 'hex color (default #FFFFFF)',
            'align': 'left | right (default left)',
            'position_y': '0.15..0.92 normalized vertical center (default 0.83)',
        },
        'builder': _expand_lower_third,
    },
    'keyword_callout': {
        'description': 'High-contrast punchy kinetic word callout with multi-color highlights, text preset, shadow, and glow.',
        'params': {
            'text': 'required str (<=160)',
            'highlights': 'optional [{start, end, color}] within text',
            'color': 'hex color (default #FFFFFF)',
            'stroke_color': 'hex color (default #0B1220)',
            'stroke_width': '0..24 px (default 5)',
            'size': 'optional 16..300 px (default resolution-scaled)',
            'glow': 'bool (default true)',
            'text_preset': 'text preset or none (default bounceInWords)',
            'position_y': '0.1..0.9 normalized vertical center (default 0.72)',
        },
        'builder': _expand_keyword_callout,
    },
    'beat_flash': {
        'description': 'Beat-synced full-frame additive flash envelope peaking cleanly at cut/beat boundaries.',
        'params': {
            'color': 'hex color (default #FFFFFF)',
            'peak_time': '0..duration local seconds (default duration * 0.35)',
            'peak_opacity': '1..100 percent (default 85)',
            'blend_mode': 'Add | Screen | Overlay | Normal (default Add)',
            'glow': 'bool (default true)',
        },
        'builder': _expand_beat_flash,
    },
    'cinematic_bars': {
        'description': 'Smooth animated top and bottom cinema letterbox bars inside transparent overlay comp.',
        'params': {
            'bar_ratio': '0.02..0.30 height fraction per bar (default 0.11)',
            'color': 'hex color (default #000000)',
            'animate_in': '0..duration seconds (default min(0.4, duration * 0.25))',
            'animate_out': '0..duration seconds (default 0.0)',
        },
        'builder': _expand_cinematic_bars,
    },
    'progress_banner': {
        'description': 'Chapter/step progress bar track with linear-wiped fill indicator and optional label.',
        'params': {
            'label': 'optional str (<=120)',
            'progress_start': '0..1 fraction (default 0.0)',
            'progress_end': '0..1 fraction (default 1.0)',
            'bar_color': 'hex color (default #38BDF8)',
            'track_color': 'hex color (default #1E293B)',
            'text_color': 'hex color (default #FFFFFF)',
            'position': 'top | bottom (default bottom)',
        },
        'builder': _expand_progress_banner,
    },
    'chapter_opener': {
        'description': 'Full-featured chapter opener with top number badge, animated divider, bold title, optional subtitle, and drop shadow.',
        'params': {
            'title': 'required str (<=120)',
            'badge': 'optional str (<=24, default 01)',
            'subtitle': 'optional str (<=200)',
            'highlights': 'optional [{start, end, color}] within title',
            'accent_color': 'hex color (default #38BDF8)',
            'text_color': 'hex color (default #FFFFFF)',
            'bg_color': 'hex color (default #0B1220)',
            'bg_opacity': '0..100 (default 78)',
            'card_style': 'card | minimal (default card)',
            'glow': 'bool (default true)',
            'text_preset': 'text preset or none (default fadeUpCharacters)',
            'position_y': '0.15..0.85 normalized vertical center (default 0.5)',
        },
        'builder': _expand_chapter_opener,
    },
    'pip_card': {
        'description': 'Transparent-interior rounded picture-in-picture border frame with optional attached corner header pill.',
        'params': {
            'title': 'optional str (<=80)',
            'corner': 'top_left | top_right | bottom_left | bottom_right (default bottom_right)',
            'width_ratio': '0.18..0.65 frame width fraction (default 0.32)',
            'aspect': '0.4..2.5 width/height aspect ratio (default 1.7778)',
            'border_color': 'hex color (default #38BDF8)',
            'border_width': '1..24 px (default 6)',
            'bg_color': 'hex color (default #0F172A)',
            'text_color': 'hex color (default #FFFFFF)',
            'glow': 'bool (default false)',
        },
        'builder': _expand_pip_card,
    },
    'split_screen_frame': {
        'description': 'Animated 2- or 3-panel split-screen divider overlay with optional panel corner labels and transparent interiors.',
        'params': {
            'layout': 'vertical_2 | horizontal_2 | vertical_3 (default vertical_2)',
            'divider_color': 'hex color (default #FFFFFF)',
            'divider_width': '2..32 px (default 6)',
            'labels': 'optional list of 2 (or 3 for vertical_3) panel strings',
            'badge_color': 'hex color (default #0F172A)',
            'text_color': 'hex color (default #FFFFFF)',
            'animate_in': '0..duration seconds (default min(0.35, duration*0.25))',
        },
        'builder': _expand_split_screen_frame,
    },
    'wipe_transition': {
        'description': 'Two-tone angled directional wipe transition sweeping cleanly from transparent -> full coverage -> transparent.',
        'params': {
            'direction': 'left | right | up | down (default left)',
            'color': 'hex color (default #0F172A)',
            'accent_color': 'hex color (default #38BDF8)',
            'angle_offset': '-35..35 degrees (default 12)',
            'feather': '0..100 px (default 16)',
            'peak_hold': '0..0.5 midpoint hold fraction (default 0.12)',
        },
        'builder': _expand_wipe_transition,
    },
}


def list_recipes():
    """Return discoverable metadata summary for all bundled EffectCraft recipes."""
    return {
        name: {'description': meta['description'], 'params': dict(meta['params'])}
        for name, meta in RECIPES.items()
    }


def validate_recipe_spec(spec):
    """Validate an EffectCraft overlay recipe name and parameter payload before execution."""
    recipe = spec.get('recipe')
    if not isinstance(recipe, str) or recipe not in RECIPES:
        raise ValueError(f'Unknown overlay recipe: {recipe!r}; choose from {", ".join(sorted(RECIPES))}')
    duration = _check_num(spec.get('duration'), 'overlay.duration', 0.04, 3600.0)
    return _validate_params(recipe, spec.get('params'), duration)


def expand_recipe(spec, *, width=1920, height=1080, fps=30.0):
    """Expand an overlay recipe spec into concrete declarative EffectCraft layers."""
    params = validate_recipe_spec(spec)
    w = int(spec.get('width', width))
    h = int(spec.get('height', height))
    rate = float(spec.get('fps', fps))
    dur = float(spec['duration'])
    layers = RECIPES[spec['recipe']]['builder'](params, w, h, dur)
    expanded = dict(spec)
    expanded['width'] = w
    expanded['height'] = h
    expanded['fps'] = rate
    expanded['layers'] = layers
    return expanded
