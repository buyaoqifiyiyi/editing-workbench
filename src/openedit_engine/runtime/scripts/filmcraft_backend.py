"""Headless FilmCraft backend: .fcproj multi-track build, graphics/captions, Lumetri, and H.264/NVENC export."""
import json
import math
import os
from pathlib import Path
import shutil
import time

import craft_rpc
import effectcraft_backend
import file_hash
import runtime_core as core

TICKS_PER_SEC = core.TICKS_PER_SEC


def find_cli():
    """Locate filmcraft-cli executable without starting the GUI."""
    explicit = os.environ.get('OPENEDIT_FILMCRAFT_CLI')
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise ValueError(f'Configured OPENEDIT_FILMCRAFT_CLI not found: {explicit}')

    root_env = os.environ.get('OPENEDIT_FILMCRAFT_ROOT')
    exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
    if root_env:
        candidate = Path(os.path.expandvars(root_env)).expanduser() / exe
        if candidate.is_file():
            return str(candidate.resolve())

    located = shutil.which(exe)
    if located and Path(located).is_file():
        return str(Path(located).resolve())

    local = Path(os.environ.get('LOCALAPPDATA', Path.home()))
    for match in sorted((local / 'OpenEditTools').glob(f'filmcraft-*/**/{exe}'), reverse=True):
        if match.is_file():
            return str(match.resolve())
    for candidate in (
        local / 'Programs' / 'ArtCraft' / 'FilmCraft' / exe,
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'ArtCraft' / 'FilmCraft' / exe,
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    raise ValueError('filmcraft-cli not found; install ArtCraft FilmCraft or set OPENEDIT_FILMCRAFT_CLI')


def _ticks(seconds):
    return round(float(seconds) * TICKS_PER_SEC)


def _linear_to_db(gain):
    if gain <= 0:
        return -96.0
    if abs(gain - 1.0) < 1e-6:
        return 0.0
    return round(max(-96.0, min(24.0, 20.0 * math.log10(float(gain)))), 2)


def _fc_interp(name):
    return {
        'smoothstep': 'autoBezier',
        'easeIn': 'easeIn',
        'easeOut': 'easeOut',
        'bezier': 'bezier',
        'linear': 'linear',
        'hold': 'hold',
        'autoBezier': 'autoBezier',
        'continuousBezier': 'continuousBezier',
    }.get(name, 'linear')


def _collect_items_map(proj):
    out = {}
    all_ids = {}

    def walk(node):
        if isinstance(node, dict):
            if 'item' in node and 'name' in node:
                out.setdefault(node['name'], []).append(node['item'])
                all_ids[node['item']] = node['name']
            for ch in node.get('children', []):
                walk(ch)

    walk(proj.get('root', {}))
    return out, all_ids


def _find_sequence_clip(seq, clip_id, *, section='video'):
    for track in seq.get(section, []):
        for item in track.get('items', []):
            if item.get('clip') == clip_id:
                return item
    return None


def _clip_timing(placed, start_sec, source_in_sec=0.0, speed=1.0):
    """Resolve actual frame-snapped FilmCraft sequence ticks with unquantized fallbacks for stubs."""
    start_ticks = int(placed['start']) if isinstance(placed, dict) and 'start' in placed else _ticks(start_sec)
    source_in_ticks = int(placed['sourceIn']) if isinstance(placed, dict) and 'sourceIn' in placed else _ticks(source_in_sec)
    actual_speed = float(placed['speed']) if isinstance(placed, dict) and 'speed' in placed else float(speed)
    return start_ticks, source_in_ticks, actual_speed


def _plan_track_mapping(plan, overlays_info):
    """Map semantic plan track names deterministically to FilmCraft V1..Vn / A1..An tracks."""
    v_set = {'video'}
    v_default = ['video']
    for item in plan.get('clips', []):
        t = item.get('track', 'video')
        if t not in v_set:
            v_set.add(t)
            v_default.append(t)
    for ov in overlays_info:
        t = ov.get('track', 'overlay')
        if t not in v_set:
            v_set.add(t)
            v_default.append(t)
    for cap in plan.get('captions', []):
        if cap.get('kind', 'graphic') == 'graphic':
            t = cap.get('track', 'captions')
            if t not in v_set:
                v_set.add(t)
                v_default.append(t)

    a_set = set()
    a_default = []
    if any(item.get('volume', 0) > 0 for item in plan.get('clips', [])):
        a_set.add('A1')
        a_default.append('A1')
    for item in plan.get('audio', []):
        t = item.get('track', 'voice')
        if t not in a_set:
            a_set.add(t)
            a_default.append(t)

    order = plan.get('layer_order')
    if isinstance(order, list) and order:
        v_tracks = ['video'] + [t for t in order if t in v_set and t != 'video']
        for t in v_default:
            if t not in v_tracks:
                v_tracks.append(t)
        a_tracks = (['A1'] if 'A1' in a_set else []) + [t for t in order if t in a_set and t != 'A1']
        for t in a_default:
            if t not in a_tracks:
                a_tracks.append(t)
    else:
        v_tracks, a_tracks = v_default, a_default

    if not a_tracks:
        a_tracks = ['A1']
    v_map = {name: f'V{idx + 1}' for idx, name in enumerate(v_tracks)}
    a_map = {name: f'A{idx + 1}' for idx, name in enumerate(a_tracks)}
    return v_map, a_map


def _set_effect_param(client, clip_id, effect, param, value, **extra):
    params = {'clip': clip_id, 'effect': effect, 'param': param, 'value': value}
    params.update(extra)
    client.call_tool('command_run', {'id': 'effects.setParam', 'params': params})


def _resolve_fit_scale(clip, info, canvas):
    if not info:
        return 1.0, 1.0
    sw, sh = float(info.get('width') or 0), float(info.get('height') or 0)
    cw, ch = float(canvas['width']), float(canvas['height'])
    if sw <= 0 or sh <= 0 or (abs(sw - cw) < 1e-6 and abs(sh - ch) < 1e-6):
        return 1.0, 1.0
    rx, ry = cw / sw, ch / sh
    fit = clip.get('fit', 'contain')
    if fit == 'cover':
        s = max(rx, ry)
        return s, s
    if fit == 'fill':
        return rx, ry
    s = min(rx, ry)
    return s, s


def _apply_clip_fit(client, clip_id, clip, info, canvas):
    fit_sx, fit_sy = _resolve_fit_scale(clip, info, canvas)
    if not info:
        return fit_sx, fit_sy
    sw, sh = float(info.get('width') or 0), float(info.get('height') or 0)
    cw, ch = float(canvas['width']), float(canvas['height'])
    if sw <= 0 or sh <= 0 or (abs(sw - cw) < 1e-6 and abs(sh - ch) < 1e-6):
        return 1.0, 1.0
    fit = clip.get('fit', 'contain')
    if fit == 'cover':
        client.call_tool('command_run', {'id': 'clip.fillFrame', 'params': {'clips': [clip_id]}})
    elif fit == 'fill' and abs(fit_sx - fit_sy) > 1e-4:
        _set_effect_param(client, clip_id, 'motion', 'uniform_scale', False)
        _set_effect_param(client, clip_id, 'motion', 'scale_width', fit_sx * 100.0)
        _set_effect_param(client, clip_id, 'motion', 'scale', fit_sy * 100.0)
    else:
        client.call_tool('command_run', {'id': 'clip.fitToFrame', 'params': {'clips': [clip_id]}})
    return fit_sx, fit_sy


def _hue_luma_curve_from_hsl(hue_deg, hue_range, lightness):
    y_t = round(max(0.0, min(1.0, 0.5 + float(lightness) / 200.0)), 4)
    h = (float(hue_deg) % 360.0) / 360.0
    w = max(0.03, min(0.25, float(hue_range) / 720.0))
    if h < w or h > 1.0 - w:
        return [[0.0, y_t], [round(w, 4), 0.5], [round(1.0 - w, 4), 0.5], [1.0, y_t]]
    pts = [[0.0, 0.5]]
    if h - w > 1e-4:
        pts.append([round(h - w, 4), 0.5])
    pts.append([round(h, 4), y_t])
    if h + w < 1.0 - 1e-4:
        pts.append([round(h + w, 4), 0.5])
    pts.append([1.0, 0.5])
    return pts


def _norm_to_pixels(vx, vy, canvas):
    cx, cy = canvas['width'] / 2.0, canvas['height'] / 2.0
    return [cx + float(vx) * cx, cy - float(vy) * cy]


def _mask_rect_path(center, size):
    cx, cy = float(center[0]), float(center[1])
    hw, hh = float(size[0]) / 2.0, float(size[1]) / 2.0
    return [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]]


def _mask_ellipse_path(center, size):
    cx, cy = float(center[0]), float(center[1])
    rx, ry = float(size[0]) / 2.0, float(size[1]) / 2.0
    k = 0.5522847498307934
    kx, ky = rx * k, ry * k
    return {
        'closed': True,
        'vertices': [
            {'p': {'x': cx, 'y': cy - ry}, 't_in': {'x': -kx, 'y': 0.0}, 't_out': {'x': kx, 'y': 0.0}},
            {'p': {'x': cx + rx, 'y': cy}, 't_in': {'x': 0.0, 'y': -ky}, 't_out': {'x': 0.0, 'y': ky}},
            {'p': {'x': cx, 'y': cy + ry}, 't_in': {'x': kx, 'y': 0.0}, 't_out': {'x': -kx, 'y': 0.0}},
            {'p': {'x': cx - rx, 'y': cy}, 't_in': {'x': 0.0, 'y': ky}, 't_out': {'x': 0.0, 'y': -ky}},
        ],
    }


def _apply_clip_mask(client, clip_id, mask_spec, canvas, *, item_start=0.0, start_ticks=None, default_effect='opacity'):
    raw_shape = mask_spec.get('shape') or mask_spec.get('type')
    if raw_shape is None:
        raw_shape = 'polygon' if ('path' in mask_spec or 'points' in mask_spec) else 'rectangle'
    shape = {'rect': 'rectangle', 'circle': 'ellipse'}.get(raw_shape, raw_shape)
    target_effect = mask_spec.get('effect') or default_effect
    add_params = {'clip': clip_id, 'effect': target_effect, 'shape': shape}
    cur_center, cur_size = None, None
    if 'path' in mask_spec or 'points' in mask_spec:
        add_params['path'] = mask_spec['path'] if 'path' in mask_spec else mask_spec['points']
    else:
        cw, ch = float(canvas['width']), float(canvas['height'])
        if 'center' in mask_spec:
            cur_center = [float(mask_spec['center'][0]), float(mask_spec['center'][1])]
        else:
            cur_center = _norm_to_pixels(mask_spec.get('center_x', 0.0), mask_spec.get('center_y', 0.0), canvas)
        if 'size' in mask_spec:
            cur_size = [float(mask_spec['size'][0]), float(mask_spec['size'][1])]
        elif 'radius' in mask_spec:
            r = float(mask_spec['radius'])
            cur_size = [r * 2.0, r * 2.0]
        else:
            cur_size = [float(mask_spec.get('width', 0.5)) * cw, float(mask_spec.get('height', 0.5)) * ch]
        add_params['center'] = cur_center
        add_params['size'] = cur_size
        if 'corner_radius' in mask_spec:
            add_params['cornerRadius'] = float(mask_spec['corner_radius'])

    added = client.call_tool('command_run', {'id': 'masks.add', 'params': add_params})
    eff_ref = added.get('effect', target_effect) if isinstance(added, dict) else target_effect
    mask_idx = added.get('mask', 0) if isinstance(added, dict) else 0

    set_params = {'clip': clip_id, 'effect': eff_ref, 'mask': mask_idx}
    if 'feather' in mask_spec:
        set_params['feather'] = float(mask_spec['feather'])
    if 'opacity' in mask_spec:
        set_params['opacity'] = float(mask_spec['opacity']) * 100.0
    if 'expansion' in mask_spec:
        set_params['expansion'] = float(mask_spec['expansion'])
    if 'inverted' in mask_spec or 'invert' in mask_spec:
        set_params['inverted'] = bool(mask_spec['inverted'] if 'inverted' in mask_spec else mask_spec['invert'])
    if 'mode' in mask_spec:
        set_params['mode'] = str(mask_spec['mode'])
    if len(set_params) > 3:
        client.call_tool('command_run', {'id': 'masks.set', 'params': set_params})

    base_ticks = _ticks(item_start) if start_ticks is None else int(start_ticks)
    for kf in mask_spec.get('keyframes', []):
        t_abs = base_ticks + _ticks(kf['time'])
        if 'feather' in kf:
            _set_effect_param(client, clip_id, eff_ref, 'feather', float(kf['feather']), mask=mask_idx, time=t_abs, keyframe=True)
        if 'opacity' in kf:
            _set_effect_param(client, clip_id, eff_ref, 'opacity', float(kf['opacity']) * 100.0, mask=mask_idx, time=t_abs, keyframe=True)
        if 'expansion' in kf:
            _set_effect_param(client, clip_id, eff_ref, 'expansion', float(kf['expansion']), mask=mask_idx, time=t_abs, keyframe=True)
        if 'path' in kf:
            _set_effect_param(client, clip_id, eff_ref, 'path', kf['path'], mask=mask_idx, time=t_abs, keyframe=True)
        elif 'center' in kf or 'size' in kf:
            if cur_center is None:
                cur_center = [float(canvas['width']) / 2.0, float(canvas['height']) / 2.0]
            if cur_size is None:
                cur_size = [float(canvas['width']) * 0.5, float(canvas['height']) * 0.5]
            if 'center' in kf:
                cur_center = [float(kf['center'][0]), float(kf['center'][1])]
            if 'size' in kf:
                cur_size = [float(kf['size'][0]), float(kf['size'][1])]
            computed_path = (
                _mask_ellipse_path(cur_center, cur_size)
                if shape == 'ellipse'
                else _mask_rect_path(cur_center, cur_size)
            )
            _set_effect_param(client, clip_id, eff_ref, 'path', computed_path, mask=mask_idx, time=t_abs, keyframe=True)


def _apply_clip_chroma(client, clip_id, chroma):
    method = chroma.get('method', 'ultra_key')
    client.call_tool('command_run', {'id': 'effects.apply', 'params': {'clips': [clip_id], 'effect': method}})
    raw_color = chroma.get('key_color') or chroma.get('color')
    if method == 'ultra_key':
        if isinstance(raw_color, str):
            _set_effect_param(client, clip_id, 'ultra_key', 'key_color', raw_color[:7])
        for key in ('setting', 'output'):
            if key in chroma:
                _set_effect_param(client, clip_id, 'ultra_key', key, chroma[key])
        num_keys = (
            'transparency', 'highlight', 'shadow', 'tolerance', 'pedestal',
            'choke', 'soften', 'spill', 'contrast', 'mid_point', 'desaturate',
            'range', 'spill_luma',
        )
        for key in num_keys:
            if key in chroma:
                _set_effect_param(client, clip_id, 'ultra_key', key, float(chroma[key]))
        if 'strength' in chroma and 'tolerance' not in chroma:
            _set_effect_param(client, clip_id, 'ultra_key', 'tolerance', float(chroma['strength']))
        if 'feather' in chroma and 'soften' not in chroma:
            _set_effect_param(client, clip_id, 'ultra_key', 'soften', float(chroma['feather']))
    else:
        if isinstance(raw_color, str):
            _set_effect_param(client, clip_id, 'color_key', 'color', raw_color[:7])
        if 'tolerance' in chroma:
            _set_effect_param(client, clip_id, 'color_key', 'tolerance', float(chroma['tolerance']))
        elif 'strength' in chroma:
            _set_effect_param(client, clip_id, 'color_key', 'tolerance', round(float(chroma['strength']) * 2.55, 2))
        if 'thin' in chroma:
            _set_effect_param(client, clip_id, 'color_key', 'thin', float(chroma['thin']))
        if 'feather' in chroma:
            _set_effect_param(client, clip_id, 'color_key', 'feather', float(chroma['feather']))
        elif 'soften' in chroma:
            _set_effect_param(client, clip_id, 'color_key', 'feather', min(50.0, float(chroma['soften'])))


def _apply_effect_list(client, clip_id, entries):
    for eff in entries:
        eff_name = eff if isinstance(eff, str) else eff['name']
        applied = client.call_tool('command_run', {'id': 'effects.apply', 'params': {'clips': [clip_id], 'effect': eff_name}})
        eff_idx = applied['applied'][0]['index'] if isinstance(applied, dict) and applied.get('applied') else eff_name
        if isinstance(eff, dict):
            for pk, pv in eff.get('params', {}).items():
                if pv is not None:
                    _set_effect_param(client, clip_id, eff_idx, pk, pv)


def _apply_audio_loudness(client, aclip_id, loudness_spec, *, default_type='dialogue'):
    if isinstance(loudness_spec, (int, float)):
        audio_type = default_type
        target = float(loudness_spec)
    else:
        if not bool(loudness_spec.get('enabled', True)):
            return
        audio_type = loudness_spec.get('type', default_type)
        raw_target = loudness_spec.get('target_lufs') if 'target_lufs' in loudness_spec else loudness_spec.get('target')
        target = float(raw_target) if raw_target is not None else None
    client.call_tool('command_run', {'id': 'essentialSound.setType', 'params': {'clips': [aclip_id], 'type': audio_type}})
    match_params = {'clips': [aclip_id]}
    if target is not None:
        match_params['target'] = target
    client.call_tool('command_run', {'id': 'essentialSound.autoMatch', 'params': match_params})


def _apply_clip_grading(client, clip_id, grading, canvas):
    client.call_tool('command_run', {'id': 'effects.apply', 'params': {'clips': [clip_id], 'effect': 'lumetri'}})
    param_map = {
        'exposure': 'exposure',
        'contrast': 'contrast',
        'saturation': 'saturation',
        'creative_sat': 'creative_sat',
        'highlights': 'highlights',
        'shadows': 'shadows',
        'whites': 'whites',
        'blacks': 'blacks',
        'temperature': 'temperature',
        'tint': 'tint',
        'vibrance': 'vibrance',
        'sharpen': 'sharpen',
        'sharpness': 'sharpen',
        'faded_film': 'faded_film',
        'look_intensity': 'look_intensity',
        'vignette': 'vignette_amount',
        'vignette_midpoint': 'vignette_midpoint',
        'vignette_roundness': 'vignette_roundness',
        'vignette_feather': 'vignette_feather',
        'luma_curve': 'curve_luma',
        'hue_vs_sat': 'hue_vs_sat',
        'hue_vs_hue': 'hue_vs_hue',
        'hue_vs_luma': 'hue_vs_luma',
        'luma_vs_sat': 'luma_vs_sat',
        'sat_vs_sat': 'sat_vs_sat',
    }
    for key, fc_param in param_map.items():
        if key in grading:
            _set_effect_param(client, clip_id, 'lumetri', fc_param, grading[key])
    if 'rgb_curves' in grading:
        for ch, fc_param in (('red', 'curve_red'), ('green', 'curve_green'), ('blue', 'curve_blue')):
            if ch in grading['rgb_curves']:
                _set_effect_param(client, clip_id, 'lumetri', fc_param, grading['rgb_curves'][ch])
    wheel_spec = grading.get('wheels') if 'wheels' in grading else grading.get('primary_wheel')
    if isinstance(wheel_spec, dict):
        for wkey in ('shadows', 'shadows_l', 'midtones', 'midtones_l', 'highlights', 'highlights_l'):
            if wkey in wheel_spec:
                _set_effect_param(client, clip_id, 'lumetri', f'wheel_{wkey}', wheel_spec[wkey])
    if 'hsl' in grading:
        _set_effect_param(client, clip_id, 'lumetri', 'hsl_on', True)
        hsl = grading['hsl']
        for hk in ('hue', 'hue_range', 'sat_min', 'luma_min', 'luma_max', 'soft',
                   'denoise', 'blur', 'temp', 'tint', 'sat', 'hue_shift'):
            if hk in hsl:
                _set_effect_param(client, clip_id, 'lumetri', f'hsl_{hk}', float(hsl[hk]))
    for sk, default_hue in (('red_hsl', 0.0), ('green_hsl', 120.0), ('blue_hsl', 240.0)):
        if sk in grading:
            sh = grading[sk]
            _set_effect_param(client, clip_id, 'lumetri', 'hsl_on', True)
            hue_deg = float(sh.get('hue', default_hue)) % 360.0
            hue_range = float(sh.get('hue_range', 30.0))
            _set_effect_param(client, clip_id, 'lumetri', 'hsl_hue', hue_deg)
            if 'hue_range' in sh:
                _set_effect_param(client, clip_id, 'lumetri', 'hsl_hue_range', hue_range)
            if 'soft' in sh:
                _set_effect_param(client, clip_id, 'lumetri', 'hsl_soft', float(sh['soft']))
            if 'hue_shift' in sh:
                _set_effect_param(client, clip_id, 'lumetri', 'hsl_hue_shift', float(sh['hue_shift']))
            if 'saturation' in sh:
                _set_effect_param(client, clip_id, 'lumetri', 'hsl_sat', 100.0 + float(sh['saturation']))
            if 'lightness' in sh and 'hue_vs_luma' not in grading:
                _set_effect_param(
                    client,
                    clip_id,
                    'lumetri',
                    'hue_vs_luma',
                    _hue_luma_curve_from_hsl(hue_deg, hue_range, sh['lightness']),
                )
    input_lut = grading.get('input_lut')
    if isinstance(input_lut, str) and input_lut:
        params = {'clip': clip_id, 'path': input_lut} if Path(input_lut).is_file() else {'clip': clip_id, 'lut': input_lut}
        client.call_tool('command_run', {'id': 'lumetri.setInputLut', 'params': params})
    lut_spec = grading.get('lut') or grading.get('look')
    if isinstance(lut_spec, str) and lut_spec:
        params = {'clip': clip_id, 'path': lut_spec} if Path(lut_spec).is_file() else {'clip': clip_id, 'lut': lut_spec}
        client.call_tool('command_run', {'id': 'lumetri.setLook', 'params': params})
    if 'local_circle' in grading:
        _apply_clip_mask(
            client,
            clip_id,
            dict(grading['local_circle'], shape='ellipse'),
            canvas,
            default_effect='lumetri',
        )


def _apply_clip_transforms_and_keys(client, clip_id, item, canvas, fit_scale=(1.0, 1.0), timing=None):
    fit_sx, fit_sy = fit_scale
    tx_spec = item.get('transform', {})
    if tx_spec:
        if 'position' in tx_spec:
            px, py = float(tx_spec['position'][0]), float(tx_spec['position'][1])
            _set_effect_param(client, clip_id, 'motion', 'position', [px, py])
        elif 'transform_x' in tx_spec or 'transform_y' in tx_spec:
            pos = _norm_to_pixels(tx_spec.get('transform_x', 0.0), tx_spec.get('transform_y', 0.0), canvas)
            _set_effect_param(client, clip_id, 'motion', 'position', pos)
        if 'scale_x' in tx_spec or 'scale_y' in tx_spec:
            sx = float(tx_spec.get('scale_x', 1.0)) * fit_sx * 100.0
            sy = float(tx_spec.get('scale_y', 1.0)) * fit_sy * 100.0
            if abs(sx - sy) > 1e-4:
                _set_effect_param(client, clip_id, 'motion', 'uniform_scale', False)
                _set_effect_param(client, clip_id, 'motion', 'scale_width', sx)
            elif abs(fit_sx - fit_sy) > 1e-4:
                _set_effect_param(client, clip_id, 'motion', 'uniform_scale', True)
            _set_effect_param(client, clip_id, 'motion', 'scale', sy)
        if 'rotation' in tx_spec:
            _set_effect_param(client, clip_id, 'motion', 'rotation', float(tx_spec['rotation']))
        if 'alpha' in tx_spec:
            _set_effect_param(client, clip_id, 'opacity', 'opacity', float(tx_spec['alpha']) * 100.0)
        if tx_spec.get('flip_horizontal'):
            client.call_tool('command_run', {'id': 'effects.apply', 'params': {'clips': [clip_id], 'effect': 'horizontal_flip'}})
        if tx_spec.get('flip_vertical'):
            client.call_tool('command_run', {'id': 'effects.apply', 'params': {'clips': [clip_id], 'effect': 'vertical_flip'}})

    blend = item.get('blend_mode') or (item.get('mix_mode', {}).get('name') if isinstance(item.get('mix_mode'), dict) else item.get('mix_mode'))
    if blend:
        _set_effect_param(client, clip_id, 'opacity', 'blend', str(blend))
    if isinstance(item.get('mix_mode'), dict) and 'opacity' in item['mix_mode']:
        _set_effect_param(client, clip_id, 'opacity', 'opacity', float(item['mix_mode']['opacity']) * 100.0)

    clip_start = float(item.get('start', 0.0))
    source_in = float(item.get('source_in', 0.0))
    speed = float(item.get('speed', 1.0))
    easing = item.get('motion_easing')
    keyframes = item.get('keyframes', [])
    non_uniform_fit_or_keys = abs(fit_sx - fit_sy) > 1e-4 or any(k['property'] == 'scale_x' for k in keyframes)

    # Merge position_x/position_y keyframes at the same time into Vec2 motion/position keys.
    pos_by_t = {}
    for kf in keyframes:
        if kf['property'] in ('position_x', 'position_y'):
            pos_by_t.setdefault(float(kf['time']), {})[kf['property']] = float(kf['value'])

    applied_pos_times = set()
    base_nx = float(tx_spec.get('transform_x', 0.0))
    base_ny = float(tx_spec.get('transform_y', 0.0))
    if timing is not None:
        start_ticks, source_in_ticks, actual_speed = timing
    else:
        start_ticks = _ticks(clip_start)
        source_in_ticks = _ticks(source_in)
        actual_speed = speed
    for kf in keyframes:
        prop = kf['property']
        t_local = float(kf['time'])
        local_ticks = _ticks(t_local)
        timeline_ticks = start_ticks + local_ticks
        media_ticks = round(source_in_ticks + local_ticks * actual_speed)
        interp = _fc_interp(kf.get('interpolation') or easing or 'linear')
        if prop in ('position_x', 'position_y'):
            if t_local in applied_pos_times:
                continue
            applied_pos_times.add(t_local)
            pair = pos_by_t[t_local]
            pos = _norm_to_pixels(pair.get('position_x', base_nx), pair.get('position_y', base_ny), canvas)
            _set_effect_param(client, clip_id, 'motion', 'position', pos, time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'position', 'motion'
        elif prop == 'uniform_scale':
            sy_val = float(kf['value']) * fit_sy * 100.0
            if abs(fit_sx - fit_sy) > 1e-4:
                _set_effect_param(client, clip_id, 'motion', 'uniform_scale', False)
                _set_effect_param(
                    client, clip_id, 'motion', 'scale_width',
                    float(kf['value']) * fit_sx * 100.0, time=timeline_ticks, keyframe=True,
                )
                if interp != 'linear':
                    client.call_tool('command_run', {'id': 'effects.setInterpolation', 'params': {
                        'clip': clip_id, 'effect': 'motion', 'param': 'scale_width', 'mediaTime': media_ticks, 'interpolation': interp,
                    }})
            _set_effect_param(client, clip_id, 'motion', 'scale', sy_val, time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'scale', 'motion'
        elif prop == 'scale_y':
            if non_uniform_fit_or_keys:
                _set_effect_param(client, clip_id, 'motion', 'uniform_scale', False)
            _set_effect_param(client, clip_id, 'motion', 'scale', float(kf['value']) * fit_sy * 100.0, time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'scale', 'motion'
        elif prop == 'scale_x':
            _set_effect_param(client, clip_id, 'motion', 'uniform_scale', False)
            _set_effect_param(client, clip_id, 'motion', 'scale_width', float(kf['value']) * fit_sx * 100.0, time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'scale_width', 'motion'
        elif prop == 'rotation':
            _set_effect_param(client, clip_id, 'motion', 'rotation', float(kf['value']), time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'rotation', 'motion'
        elif prop == 'alpha':
            _set_effect_param(client, clip_id, 'opacity', 'opacity', float(kf['value']) * 100.0, time=timeline_ticks, keyframe=True)
            fc_param, fc_effect = 'opacity', 'opacity'
        else:
            continue
        if interp != 'linear':
            client.call_tool('command_run', {'id': 'effects.setInterpolation', 'params': {
                'clip': clip_id, 'effect': fc_effect, 'param': fc_param, 'mediaTime': media_ticks, 'interpolation': interp
            }})


def _verify_exported_mp4(path, expect_audio):
    info = core.probe(path)
    if not info.get('has_video'):
        raise RuntimeError(f'Exported media lacks video stream: {path}')
    if expect_audio and not info.get('has_audio'):
        raise RuntimeError(f'Exported media lacks expected audio stream: {path}')
    core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], timeout=300)
    return info


def _import_media_paths(client, media_paths):
    """Import media paths unambiguously even when distinct folders share file basenames."""
    if not media_paths:
        return {}
    names = [Path(p).name for p in media_paths]
    if len(set(names)) == len(names):
        imp = client.call_tool('media_import', {'paths': media_paths})
        if imp.get('errors'):
            raise RuntimeError(f'FilmCraft media_import errors: {imp["errors"]}')
        proj = client.call_tool('project_inspect')
        items_map, _ = _collect_items_map(proj)
        return {p: items_map[Path(p).name][0] for p in media_paths}

    path_to_item = {}
    proj = client.call_tool('project_inspect')
    _, prev_ids = _collect_items_map(proj)
    for p in media_paths:
        imp = client.call_tool('media_import', {'paths': [p]})
        if imp.get('errors'):
            raise RuntimeError(f'FilmCraft media_import errors: {imp["errors"]}')
        proj = client.call_tool('project_inspect')
        _, curr_ids = _collect_items_map(proj)
        added = [item_id for item_id in curr_ids if item_id not in prev_ids]
        if not added:
            raise RuntimeError(f'FilmCraft failed to register imported media item: {p}')
        path_to_item[p] = added[-1]
        prev_ids = curr_ids
    return path_to_item


def _apply_graphic_caption_keyframes(client, gfx_clip, cap, canvas):
    keyframes = cap.get('keyframes', [])
    if not keyframes:
        return
    by_time = {}
    for kf in keyframes:
        by_time.setdefault(float(kf['time']), {})[kf['property']] = float(kf['value'])
    base_x = float(cap.get('x', 0.0))
    base_y = float(cap.get('y', -0.7))
    for t_local in sorted(by_time):
        items = by_time[t_local]
        props = {}
        if 'position_x' in items or 'position_y' in items:
            nx = items.get('position_x', base_x)
            ny = items.get('position_y', base_y)
            props['position'] = _norm_to_pixels(nx, ny, canvas)
        if 'alpha' in items:
            props['opacity'] = items['alpha'] * 100.0
        if 'uniform_scale' in items or 'scale_y' in items:
            props['scale'] = items.get('uniform_scale', items.get('scale_y', 1.0)) * 100.0
        if 'rotation' in items:
            props['rotation'] = items['rotation']
        if props:
            t_abs = _ticks(float(cap['start']) + t_local)
            client.call_tool('command_run', {'id': 'graphics.set', 'params': {
                'clip': gfx_clip, 'props': props, 'time': t_abs, 'keyframe': True
            }})


def _write_delivery_docs(out, plan, records):
    core.write_json(out / '素材来源.json', {'schema_version': 1, 'materials': records})
    (out / '素材来源.md').write_text(core.source_delivery(records), encoding='utf-8')
    lines = [
        f'# {plan["name"]} 剪辑交付',
        '',
        '本工程由纯开源引擎（FilmCraft + EffectCraft + FFmpeg）构建。',
        '',
        '- 工程文件：`' + plan['name'] + '.fcproj`（明文 JSON，可用 FilmCraft GUI 打开微调）',
        '- 剪辑计划：`edit-plan.json`',
        '- 素材来源与署名：`素材来源.md`、`素材来源.json`',
        '- 构建报告：`build-report.json`',
        '',
        '## 时间轴概览',
        '',
        '| 时间范围（秒） | 轨道 | 素材 | 源入点 | 变速 | 备注 |',
        '|---|---|---|---|---|---|',
    ]
    for c in sorted(plan['clips'], key=lambda x: (x['start'], x['track'])):
        reason = str(c.get('reason', '')).replace('|', '\\|').replace('\n', ' ')
        name = Path(c['path']).name.replace('|', '\\|')
        lines.append(f'| {c["start"]:.3f}–{c["start"] + c["duration"]:.3f} | {c["track"]} | {name} | {c.get("source_in", 0.0):.3f} | {c.get("speed", 1.0)} | {reason} |')
    (out / '剪辑交付.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def build_project(
    plan,
    out_dir,
    *,
    probes=None,
    render_overlays=True,
    preview_times=None,
    export_path=None,
    hardware_encoding='off',
    export_otio=None,
    export_fcpxml=None,
):
    """Build a plaintext .fcproj project (plus optional overlays, previews, and verified MP4)."""
    probe_map = dict(probes or {})
    # Verify stock provenance checksums and codec compatibility BEFORE creating output directory.
    records = core.provenance(plan)
    for clip in plan['clips']:
        clip_path = str(Path(clip['path']).resolve())
        info = probe_map.get(clip_path)
        if info is None:
            info = core.probe(clip_path)
            probe_map[clip_path] = info
        if core.is_filmcraft_hi10p(info):
            raise ValueError(
                f'Clip {clip_path} uses 10-bit H.264 ({info.get("pix_fmt")}), which FilmCraft renders as MEDIA UNREADABLE; '
                'run normalize-media to convert to 8-bit yuv420p first'
            )
    cli = find_cli()
    out = core.new_directory(out_dir)
    core.write_json(out / 'edit-plan.json', plan)
    _write_delivery_docs(out, plan, records)
    canvas = plan['canvas']

    # Step 1: Compile any EffectCraft transparent overlays first.
    compiled_overlays = []
    if plan.get('overlays') and render_overlays:
        ov_root = out / 'overlays'
        ov_root.mkdir()
        for idx, ov in enumerate(plan['overlays']):
            ov_res = effectcraft_backend.build_overlay(
                ov,
                ov_root / f'{idx:02d}_{ov["name"]}',
                width=canvas['width'],
                height=canvas['height'],
                fps=canvas['fps'],
                render_video=True,
            )
            compiled_overlays.append({
                'spec': ov,
                'report': ov_res,
                'path': ov_res['overlay_video']['path'],
                'track': ov.get('track', 'overlay'),
                'start': float(ov['start']),
                'duration': float(ov['duration']),
            })

    v_map, a_map = _plan_track_mapping(plan, compiled_overlays)
    fcproj_path = out / f'{plan["name"]}.fcproj'
    t0 = time.monotonic()

    previews_written = []
    export_receipt = None
    interchange_files = {}

    with craft_rpc.CraftMcpClient([cli, 'mcp']) as client:
        client.call_tool('command_run', {'id': 'file.newProject', 'params': {'name': plan['name']}})
        client.call_tool('command_run', {'id': 'file.newSequence', 'params': {
            'name': 'Main',
            'width': int(canvas['width']),
            'height': int(canvas['height']),
            'fps': float(canvas['fps']),
            'sampleRate': 48000,
            'video': max(3, len(v_map)),
            'audio': max(2, len(a_map)),
        }})

        # Import all unique media + overlay assets.
        media_paths = []
        seen_paths = set()
        for entry in [*plan['clips'], *plan.get('audio', []), *compiled_overlays]:
            p = str(Path(entry['path']).resolve())
            if p not in seen_paths:
                seen_paths.add(p)
                media_paths.append(p)
        path_to_item = _import_media_paths(client, media_paths)

        # Place video & image clips onto mapped V tracks.
        placed_entries_by_track = {}
        pending_transitions = []
        track_clip_counts = {}
        linked_audio_active = False
        for idx, clip in enumerate(plan['clips']):
            clip_path = str(Path(clip['path']).resolve())
            item_id = path_to_item[clip_path]
            info = probe_map.get(clip_path)
            if info is None:
                info = core.probe(clip_path)
                probe_map[clip_path] = info
            track_name = clip.get('track', 'video')
            v_track = v_map[track_name]
            place_audio = bool(info.get('has_audio') and float(clip.get('volume', 0)) > 0)
            place_params = {
                'item': item_id,
                'track': v_track,
                'seconds': float(clip['start']),
                'sourceIn': _ticks(clip.get('source_in', 0.0)),
                'duration': _ticks(clip['duration']),
                'insert': False,
                'audio': place_audio,
            }
            if place_audio:
                place_params['audioTrack'] = 'A1'
                linked_audio_active = True
            client.call_tool('command_run', {'id': 'timeline.place', 'params': place_params})
            seq = client.call_tool('sequence_inspect')
            t_idx = int(v_track[1:]) - 1
            placed = min(seq['video'][t_idx]['items'], key=lambda x: abs(x['start'] - _ticks(clip['start'])))
            clip_id = placed['clip']
            placed_entries_by_track.setdefault(track_name, []).append({
                'clip': clip_id, 'start': float(clip['start']), 'duration': float(clip['duration']),
            })

            if place_audio and seq.get('audio'):
                a1_items = seq['audio'][0].get('items', [])
                if a1_items:
                    placed_linked_a = min(a1_items, key=lambda x: abs(x['start'] - _ticks(clip['start'])))
                    if abs(float(clip.get('volume', 1.0)) - 1.0) > 1e-6:
                        _set_effect_param(client, placed_linked_a['clip'], 'volume', 'level', _linear_to_db(clip['volume']))
                    if 'loudness' in clip:
                        _apply_audio_loudness(client, placed_linked_a['clip'], clip['loudness'], default_type='dialogue')

            speed = float(clip.get('speed', 1.0))
            rev = bool(clip.get('reverse', False))
            if abs(speed - 1.0) > 1e-6 or rev:
                client.call_tool('command_run', {'id': 'clip.speedDuration', 'params': {
                    'clips': [clip_id], 'speed': speed * 100.0, 'reverse': rev, 'ripple': False
                }})
                placed = _find_sequence_clip(client.call_tool('sequence_inspect'), clip_id) or placed
            clip_timing = _clip_timing(placed, clip['start'], clip.get('source_in', 0.0), speed)

            fit_scale = _apply_clip_fit(client, clip_id, clip, info, canvas)
            _apply_clip_transforms_and_keys(client, clip_id, clip, canvas, fit_scale=fit_scale, timing=clip_timing)

            if 'chroma' in clip:
                _apply_clip_chroma(client, clip_id, clip['chroma'])
            if 'mask' in clip:
                _apply_clip_mask(client, clip_id, clip['mask'], canvas, item_start=float(clip['start']), start_ticks=clip_timing[0])
            if 'grading' in clip:
                _apply_clip_grading(client, clip_id, clip['grading'], canvas)
            _apply_effect_list(client, clip_id, [*clip.get('effects', []), *clip.get('filters', [])])

            prior_on_track = track_clip_counts.get(track_name, 0)
            track_clip_counts[track_name] = prior_on_track + 1
            if 'transition' in clip:
                pending_transitions.append((clip_id, clip, prior_on_track))

        # Apply video transitions after all clips are placed so adjacent boundaries exist.
        for clip_id, clip, prior_on_track in pending_transitions:
            tr = clip['transition']
            edge = tr.get('edge') if isinstance(tr, dict) else None
            if edge is None:
                edge = 'in' if prior_on_track > 0 else 'out'
            tr_params = {'clip': clip_id, 'effect': tr['name'] if isinstance(tr, dict) else str(tr), 'edge': edge}
            if isinstance(tr, dict):
                if 'duration' in tr:
                    tr_params['frames'] = max(1, round(float(tr['duration']) * float(canvas['fps'])))
                elif 'frames' in tr:
                    tr_params['frames'] = int(tr['frames'])
            client.call_tool('command_run', {'id': 'sequence.applyVideoTransition', 'params': tr_params})

        # Place EffectCraft ProRes 4444 alpha overlays.
        for ov in compiled_overlays:
            item_id = path_to_item[str(Path(ov['path']).resolve())]
            v_track = v_map[ov['track']]
            client.call_tool('command_run', {'id': 'timeline.place', 'params': {
                'item': item_id,
                'track': v_track,
                'seconds': ov['start'],
                'duration': _ticks(ov['duration']),
                'insert': False,
                'audio': False,
            }})
            seq = client.call_tool('sequence_inspect')
            t_idx = int(v_track[1:]) - 1
            ov_items = seq['video'][t_idx].get('items', []) if t_idx < len(seq.get('video', [])) else []
            if ov_items:
                placed_ov = min(ov_items, key=lambda x: abs(x['start'] - _ticks(ov['start'])))
                placed_entries_by_track.setdefault(ov['track'], []).append({
                    'clip': placed_ov['clip'], 'start': float(ov['start']), 'duration': float(ov['duration']),
                })

        # Place standalone audio clips and apply volume / keyframes / fades / loudness / beat markers.
        for aud in plan.get('audio', []):
            item_id = path_to_item[str(Path(aud['path']).resolve())]
            aud_track_name = aud.get('track', 'voice')
            a_track = a_map[aud_track_name]
            client.call_tool('command_run', {'id': 'timeline.place', 'params': {
                'item': item_id,
                'track': a_track,
                'seconds': float(aud['start']),
                'sourceIn': _ticks(aud.get('source_in', 0.0)),
                'duration': _ticks(aud['duration']),
                'insert': False,
                'video': False,
                'audio': True,
            }})
            seq = client.call_tool('sequence_inspect')
            a_idx = int(a_track[1:]) - 1
            placed_a = min(seq['audio'][a_idx]['items'], key=lambda x: abs(x['start'] - _ticks(aud['start'])))
            aclip_id = placed_a['clip']
            placed_entries_by_track.setdefault(aud_track_name, []).append({
                'clip': aclip_id, 'start': float(aud['start']), 'duration': float(aud['duration']),
            })
            if abs(float(aud.get('volume', 1.0)) - 1.0) > 1e-6:
                _set_effect_param(client, aclip_id, 'volume', 'level', _linear_to_db(aud['volume']))
            a_start_ticks, _, _ = _clip_timing(placed_a, aud['start'], aud.get('source_in', 0.0), aud.get('speed', 1.0))
            for kf in aud.get('keyframes', []):
                timeline_ticks = a_start_ticks + _ticks(kf['time'])
                _set_effect_param(
                    client, aclip_id, 'volume', 'level',
                    _linear_to_db(kf['value']), time=timeline_ticks, keyframe=True,
                )
            if float(aud.get('fade_in', 0.0)) > 0.0:
                client.call_tool('command_run', {'id': 'sequence.applyAudioTransition', 'params': {
                    'clip': aclip_id,
                    'effect': 'constant_power',
                    'edge': 'in',
                    'frames': max(1, round(float(aud['fade_in']) * float(canvas['fps']))),
                }})
            if float(aud.get('fade_out', 0.0)) > 0.0:
                client.call_tool('command_run', {'id': 'sequence.applyAudioTransition', 'params': {
                    'clip': aclip_id,
                    'effect': 'constant_power',
                    'edge': 'out',
                    'frames': max(1, round(float(aud['fade_out']) * float(canvas['fps']))),
                }})
            if 'loudness' in aud:
                default_type = 'music' if any(k in str(aud_track_name).lower() for k in ('bgm', 'music')) else 'dialogue'
                _apply_audio_loudness(client, aclip_id, aud['loudness'], default_type=default_type)
            aud_source_in = float(aud.get('source_in', 0.0))
            aud_speed = float(aud.get('speed', 1.0))
            aud_start = float(aud['start'])
            aud_end = aud_start + float(aud['duration'])
            for bi, mark in enumerate(aud.get('beat_marks', [])):
                seq_sec = aud_start + (float(mark) - aud_source_in) / aud_speed
                seq_sec = max(aud_start, min(aud_end, seq_sec))
                client.call_tool('command_run', {'id': 'markers.add', 'params': {
                    'target': 'program',
                    'time': _ticks(seq_sec),
                    'name': f'Beat {bi + 1}',
                    'color': '#FFD54F',
                }})

        # Place captions & native graphic titles (including multi-color highlights & keyframes).
        subtitle_tracks_created = set()
        for cap in plan.get('captions', []):
            kind = cap.get('kind', 'graphic')
            if kind == 'subtitle':
                t_name = cap.get('track', 'C1')
                if t_name not in subtitle_tracks_created:
                    client.call_tool('command_run', {'id': 'captions.newTrack', 'params': {'format': 'Subtitle', 'name': t_name, 'language': 'zh-CN'}})
                    style_params = {'track': t_name, 'font': cap.get('font') or 'Noto Sans SC',
                                    'size': float(cap.get('size', 48)), 'color': cap.get('color', '#FFFFFF'),
                                    'background': bool(cap.get('background', True)), 'outline': float(cap.get('stroke_width', 3.0))}
                    client.call_tool('command_run', {'id': 'captions.setStyle', 'params': style_params})
                    subtitle_tracks_created.add(t_name)
                client.call_tool('command_run', {'id': 'captions.add', 'params': {
                    'track': t_name, 'text': cap['text'], 'seconds': float(cap['start']), 'durationSeconds': float(cap['duration'])
                }})
            else:
                cap_track_name = cap.get('track', 'captions')
                v_track = v_map[cap_track_name]
                track_num = int(v_track[1:]) - 1
                pos = cap.get('position')
                if pos is None:
                    pos = _norm_to_pixels(cap.get('x', 0.0), cap.get('y', -0.7), canvas)
                gfx = client.call_tool('command_run', {'id': 'graphics.newText', 'params': {
                    'text': cap['text'],
                    'position': pos,
                    'size': float(cap.get('size', 56)),
                    'font': cap.get('font') or 'Noto Sans SC',
                    'fontStyle': cap.get('font_style') or ('Bold' if cap.get('bold') else 'Regular'),
                    'seconds': float(cap['duration']),
                    'track': track_num,
                    'time': _ticks(cap['start']),
                }})
                gfx_clip = gfx['clip']
                placed_entries_by_track.setdefault(cap_track_name, []).append({
                    'clip': gfx_clip, 'start': float(cap['start']), 'duration': float(cap['duration']),
                })
                props = {
                    'fill_color': cap.get('color', '#FFFFFF')[:7],
                    'align': cap.get('align', 'center'),
                    'stroke': bool(cap.get('border', cap.get('stroke', True))),
                    'stroke_color': cap.get('stroke_color', '#0B1220')[:7],
                    'stroke_width': float(cap.get('stroke_width', 4)),
                    'background': bool(cap.get('background', False)),
                    'shadow': bool(cap.get('shadow', True)),
                }
                if cap.get('background_color'):
                    props['background_color'] = cap['background_color']
                if 'style' in cap and isinstance(cap['style'], dict) and 'alpha' in cap['style']:
                    props['opacity'] = float(cap['style']['alpha']) * 100.0
                if 'alpha' in cap.get('transform', {}):
                    props['opacity'] = float(cap['transform']['alpha']) * 100.0
                client.call_tool('command_run', {'id': 'graphics.set', 'params': {'clip': gfx_clip, 'props': props}})
                for span in cap.get('highlights', []):
                    client.call_tool('command_run', {'id': 'graphics.setCharStyle', 'params': {
                        'clip': gfx_clip, 'start': span['start'], 'end': span['end'],
                        'style': {'color': span['color'][:7]}
                    }})
                _apply_graphic_caption_keyframes(client, gfx_clip, cap, canvas)

        # Build composite nested sequences and apply outer transforms / mask / grading / effects.
        for comp_idx, spec in enumerate(plan.get('composites', [])):
            candidates = [e for t in spec['tracks'] for e in placed_entries_by_track.get(t, [])]
            if 'start' in spec or 'duration' in spec:
                cs = float(spec.get('start', 0.0))
                cd = float(spec.get('duration', 86400.0))
                ce = cs + cd
                selected = [e for e in candidates if e['start'] < ce - 1e-6 and (e['start'] + e['duration']) > cs + 1e-6]
            else:
                selected = candidates
            if not selected:
                raise ValueError(f'composites[{comp_idx}] matched no placed timeline clips on tracks {spec["tracks"]}')
            clip_ids = [e['clip'] for e in selected]
            min_start = min(e['start'] for e in selected)
            max_end = max(e['start'] + e['duration'] for e in selected)
            nest_name = spec.get('name') or f'Composite {comp_idx + 1}'
            client.call_tool('command_run', {'id': 'timeline.select', 'params': {'clips': clip_ids, 'add': False, 'toggle': False}})
            nested = client.call_tool('command_run', {'id': 'clip.nest', 'params': {'name': nest_name}})
            nested_clips = nested.get('clips') if isinstance(nested, dict) else None
            if not nested_clips:
                raise RuntimeError(f'composites[{comp_idx}] clip.nest returned no nested clip id: {nested}')
            nested_id = nested_clips[0]
            nested_item = _find_sequence_clip(client.call_tool('sequence_inspect'), nested_id)
            outer_timing = _clip_timing(nested_item, min_start, 0.0, 1.0)
            outer_item = dict(spec, start=min_start, duration=max(1e-6, max_end - min_start), source_in=0.0, speed=1.0)
            _apply_clip_transforms_and_keys(client, nested_id, outer_item, canvas, timing=outer_timing)
            if 'mask' in spec:
                _apply_clip_mask(client, nested_id, spec['mask'], canvas, item_start=min_start, start_ticks=outer_timing[0])
            if 'grading' in spec:
                _apply_clip_grading(client, nested_id, spec['grading'], canvas)
            _apply_effect_list(client, nested_id, [*spec.get('effects', []), *spec.get('filters', [])])

        client.call_tool('command_run', {'id': 'file.saveAs', 'params': {'path': str(fcproj_path)}})
        final_seq = client.call_tool('sequence_inspect')

        if export_otio:
            otio_path = Path(export_otio).resolve()
            client.call_tool('command_run', {'id': 'file.exportOtio', 'params': {'path': str(otio_path)}})
            interchange_files['otio'] = str(otio_path)
        if export_fcpxml:
            xml_path = Path(export_fcpxml).resolve()
            client.call_tool('command_run', {'id': 'file.exportFcpxml', 'params': {'path': str(xml_path)}})
            interchange_files['fcpxml'] = str(xml_path)

        if preview_times:
            p_dir = out / 'previews'
            p_dir.mkdir()
            for idx, ts in enumerate(preview_times):
                res = client.call_tool('render_frame', {'seconds': float(ts), 'max_side': 0})
                raw = craft_rpc.extract_png_bytes(res)
                if not raw:
                    raise RuntimeError(f'No PNG bytes returned for preview t={ts}')
                png = p_dir / f'frame-{idx}.png'
                png.write_bytes(raw)
                previews_written.append({'time': float(ts), 'path': str(png), 'sha256': file_hash.sha256(png)})

        if export_path:
            mp4 = Path(export_path).resolve()
            mp4.parent.mkdir(parents=True, exist_ok=True)
            exp_cfg = plan.get('export', {})
            exp_params = {
                'path': str(mp4),
                'format': exp_cfg.get('format', 'h264'),
                'width': int(canvas['width']),
                'height': int(canvas['height']),
                'fps': float(canvas['fps']),
                'hardwareEncoding': exp_cfg.get('hardware_encoding', hardware_encoding),
                'audio': bool(exp_cfg.get('audio', True)),
                'burnCaptions': bool(exp_cfg.get('burn_captions', True)),
                'wait': True,
            }
            if exp_params['hardwareEncoding'] == 'off' and exp_params['format'] == 'h264':
                exp_params['bitrateMode'] = 'crf'
                exp_params['crf'] = int(exp_cfg.get('crf', 20))
            exp_res = client.call_tool('command_run', {'id': 'file.exportMedia', 'params': exp_params})
            expect_audio = bool(exp_params['audio'] and (plan.get('audio') or linked_audio_active))
            exp_info = _verify_exported_mp4(mp4, expect_audio)
            export_receipt = {
                'path': str(mp4),
                'sha256': file_hash.sha256(mp4),
                'bytes': mp4.stat().st_size,
                'render_result': exp_res.get('result') if isinstance(exp_res, dict) else exp_res,
                'probe': exp_info,
                'strict_decode': 'passed',
            }

    return {
        'status': 'passed',
        'backend': 'filmcraft',
        'project': str(fcproj_path),
        'project_sha256': file_hash.sha256(fcproj_path),
        'duration_frames': final_seq.get('durationFrames'),
        'video_tracks': len(final_seq.get('video', [])),
        'audio_tracks': len(final_seq.get('audio', [])),
        'overlays': [o['report'] for o in compiled_overlays],
        'interchange': interchange_files,
        'previews': previews_written,
        'export': export_receipt,
        'elapsed_seconds': round(time.monotonic() - t0, 3),
        'evidence_boundaries': {
            'project_plaintext_json': True,
            'human_audiovisual_review': 'unverified',
            'ui_manual_polish': 'available_by_opening_fcproj_in_filmcraft',
        },
    }


def preview_project(project_path, out_dir, times, max_side=0):
    """Render bounded full-resolution or scaled PNG frames from an existing .fcproj file."""
    cli = find_cli()
    proj = Path(project_path).resolve()
    if not proj.is_file():
        raise ValueError(f'Project not found: {proj}')
    if not times or len(times) > 64:
        raise ValueError('Provide 1..64 preview timestamps in seconds')
    out = core.new_directory(out_dir)
    frames = []
    with craft_rpc.CraftMcpClient([cli, '--project', str(proj), 'mcp']) as client:
        for idx, ts in enumerate(times):
            t = core.number(ts, f'times[{idx}]', 0, 86400)
            res = client.call_tool('render_frame', {'seconds': t, 'max_side': int(max_side)})
            raw = craft_rpc.extract_png_bytes(res)
            if not raw:
                raise RuntimeError(f'No PNG bytes returned for t={t}')
            png = out / f'frame-{idx}.png'
            png.write_bytes(raw)
            frames.append({'time': t, 'path': str(png), 'sha256': file_hash.sha256(png)})
    report = {'status': 'passed', 'project': str(proj), 'frames': frames}
    core.write_json(out / 'preview.json', report)
    return report


def export_project(project_path, out_path, *, fmt='h264', crf=20, hardware_encoding='off', burn_captions=True, audio=True, gpu_rendering='auto'):
    """Export an existing .fcproj to final media and run ffprobe + strict ffmpeg decode verification."""
    cli = find_cli()
    proj = Path(project_path).resolve()
    if not proj.is_file():
        raise ValueError(f'Project not found: {proj}')
    dst = Path(out_path).resolve()
    if dst.exists() or dst.is_symlink():
        raise ValueError(f'Export target already exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    if fmt not in ('h264', 'hevc', 'prores', 'wav', 'aac'):
        raise ValueError(f'Unsupported export format: {fmt}')

    t0 = time.monotonic()
    cmd = [cli, '--project', str(proj), 'export', str(dst), '--format', fmt, '--gpu-rendering', gpu_rendering]
    if not audio:
        cmd.append('--no-audio')
    settings = {}
    if fmt == 'h264':
        if hardware_encoding in ('auto', 'on'):
            settings['hardwareEncoding'] = 'auto'
        else:
            settings['bitrateMode'] = 'crf'
            settings['crf'] = int(core.number(crf, 'crf', 0, 51))
    settings['burnCaptions'] = bool(burn_captions)
    cmd.extend(['--settings', json.dumps(settings)])
    core.run(cmd, timeout=900)
    if not dst.is_file():
        raise RuntimeError(f'FilmCraft export created no file: {dst}')

    expect_audio = bool(audio and fmt in ('h264', 'hevc', 'prores'))
    info = core.probe(dst)
    core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(dst), '-f', 'null', '-'], timeout=300)
    return {
        'status': 'passed',
        'project': str(proj),
        'output': str(dst),
        'sha256': file_hash.sha256(dst),
        'bytes': dst.stat().st_size,
        'elapsed_seconds': round(time.monotonic() - t0, 3),
        'probe': info,
        'expect_audio_requested': expect_audio,
        'strict_decode': 'passed',
    }


def exec_commands(commands=(), *, project=None, save_as=None, inspect_target='sequence'):
    """Advanced escape hatch directly executing any of FilmCraft's 718 engine commands."""
    cli = find_cli()
    args = [cli]
    if project:
        proj = Path(project).resolve()
        if not proj.is_file():
            raise ValueError(f'Project not found: {proj}')
        args.extend(['--project', str(proj)])
    args.append('mcp')

    results = []
    with craft_rpc.CraftMcpClient(args) as client:
        for step in commands:
            cid = step['id']
            params = step.get('params', {})
            results.append({'id': cid, 'result': client.call_tool('command_run', {'id': cid, 'params': params})})
        if save_as:
            client.call_tool('command_run', {'id': 'file.saveAs', 'params': {'path': str(Path(save_as).resolve())}})
        state = None
        if inspect_target == 'project':
            state = client.call_tool('project_inspect')
        elif inspect_target == 'sequence':
            state = client.call_tool('sequence_inspect')
        elif inspect_target == 'both':
            state = client.call_tool('doc_inspect')
        return {
            'status': 'passed',
            'commands_executed': len(results),
            'results': results,
            'state': state,
            'saved_project': str(Path(save_as).resolve()) if save_as else None,
        }
