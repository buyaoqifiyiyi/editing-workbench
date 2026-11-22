"""Headless EffectCraft backend: .ecproj, RGBA previews, and ProRes 4444 alpha overlays."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time

from PIL import Image

import craft_rpc
import file_hash
import overlay_recipes
import runtime_core as core
import runtime_paths

DEFAULT_CJK_FONTS = ('Noto Sans SC', 'Microsoft YaHei', 'SimHei')


def find_cli():
    """Locate effectcraft-cli executable without starting the GUI."""
    from openedit_engine.software_detection import find_craft_cli

    return find_craft_cli('effectcraft')


def _pick_cjk_font(client, requested=None):
    if requested:
        res = client.call_tool('list_fonts', {'query': requested})
        if isinstance(res, dict) and res.get('families'):
            return res['families'][0]['family']
    for name in DEFAULT_CJK_FONTS:
        res = client.call_tool('list_fonts', {'query': name})
        if isinstance(res, dict) and res.get('families'):
            return res['families'][0]['family']
    return requested or 'Noto Sans SC'


def overlay_cache_dir(explicit=None):
    """Return validated external directory for content-addressed EffectCraft overlay caches."""
    raw = explicit or os.environ.get('OPENEDIT_OVERLAY_CACHE_DIR')
    path = Path(raw).expanduser() if raw else Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditEngine' / 'overlay-cache'
    return runtime_paths.external_directory(path, 'Overlay cache')


def _overlay_cache_key(
    cli_path,
    compiled_spec,
    *,
    width,
    height,
    fps,
    duration,
    render_video,
    preview_time,
    gpu,
    resolution='full',
    quality='best',
):
    cli_file = Path(cli_path)
    if not cli_file.is_file() or compiled_spec.get('script'):
        return None
    resolved_cli = cli_file.resolve()
    cli_stat = resolved_cli.stat()
    footage_hashes = {}
    layers = compiled_spec.get('layers', [])
    for idx, layer in enumerate(layers):
        if isinstance(layer, dict) and layer.get('type') == 'footage' and layer.get('path'):
            fpath = Path(layer['path']).resolve()
            if not fpath.is_file():
                return None
            footage_hashes[f'{idx}:{fpath}'] = file_hash.sha256(fpath)
    payload = {
        'schema': 1,
        'cli': {'path': str(resolved_cli), 'size': cli_stat.st_size, 'mtime_ns': cli_stat.st_mtime_ns},
        'width': int(width),
        'height': int(height),
        'fps': float(fps),
        'duration': float(duration),
        'preview_time': float(preview_time),
        'render_video': bool(render_video),
        'gpu': bool(gpu),
        'resolution': str(resolution),
        'quality': str(quality),
        'motion_blur': compiled_spec.get('motion_blur'),
        'layers': layers,
        'footage_hashes': footage_hashes,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _try_restore_overlay_cache(entry_dir, out, comp_name, *, render_video, width, height, fps, duration, spec, cache_key, cache_root, resolution='full', quality='best'):
    meta_path = entry_dir / 'cache-meta.json'
    if not meta_path.is_file():
        return None
    try:
        meta = core.read_json(meta_path)
    except (ValueError, OSError):
        return None
    if not isinstance(meta, dict) or meta.get('schema') != 1:
        return None
    if (meta.get('width') != int(width) or meta.get('height') != int(height)
            or float(meta.get('fps', -1)) != float(fps) or float(meta.get('duration', -1)) != float(duration)
            or bool(meta.get('render_video')) != bool(render_video)
            or meta.get('resolution', 'full') != resolution or meta.get('quality', 'best') != quality):
        return None
    cached_ecproj = entry_dir / 'overlay.ecproj'
    cached_png = entry_dir / 'preview_rgba.png'
    cached_mov = entry_dir / 'overlay.mov'
    if not cached_ecproj.is_file() or not cached_png.is_file():
        return None
    if file_hash.sha256(cached_ecproj) != meta.get('ecproj_sha256') or file_hash.sha256(cached_png) != meta.get('preview_png_sha256'):
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

    ecproj = out / f'{comp_name}.ecproj'
    preview_png = out / 'preview_rgba.png'
    shutil.copy2(cached_ecproj, ecproj)
    shutil.copy2(cached_png, preview_png)
    overlay_video = None
    if render_video:
        mov_path = out / f'{comp_name}.mov'
        shutil.copy2(cached_mov, mov_path)
        overlay_video = dict(meta['overlay_video'])
        overlay_video['path'] = str(mov_path)

    result = {
        'status': 'passed',
        'backend': 'effectcraft',
        'comp': comp_name,
        'project': str(ecproj),
        'preview_png': str(preview_png),
        'width': width,
        'height': height,
        'fps': fps,
        'duration': duration,
        'overlay_video': overlay_video,
        'cache': {'hit': True, 'key': cache_key, 'cache_dir': str(cache_root)},
    }
    if 'recipe' in spec:
        result['recipe'] = spec['recipe']
    return result


def _store_overlay_cache(entry_dir, ecproj, preview_png, result, *, render_video):
    entry_dir.parent.mkdir(parents=True, exist_ok=True)
    if entry_dir.is_dir():
        return
    stage = entry_dir.parent / f'.tmp-{entry_dir.name[:8]}-{secrets.token_hex(4)}'
    stage.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(ecproj, stage / 'overlay.ecproj')
        shutil.copy2(preview_png, stage / 'preview_rgba.png')
        ov_meta = None
        if render_video and isinstance(result.get('overlay_video'), dict):
            ov_info = dict(result['overlay_video'])
            shutil.copy2(ov_info['path'], stage / 'overlay.mov')
            ov_meta = {k: v for k, v in ov_info.items() if k != 'path'}
        meta = {
            'schema': 1,
            'width': result['width'],
            'height': result['height'],
            'fps': result['fps'],
            'duration': result['duration'],
            'resolution': result.get('resolution', 'full'),
            'quality': result.get('quality', 'best'),
            'render_video': bool(render_video),
            'ecproj_sha256': file_hash.sha256(ecproj),
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


def _hex_to_rgb01(value, default=(1.0, 1.0, 1.0)):
    if not isinstance(value, str) or len(value) not in (7, 9) or not value.startswith('#'):
        return list(default)
    return [round(int(value[i:i + 2], 16) / 255.0, 4) for i in (1, 3, 5)]


def _map_interp(value):
    return {
        'smoothstep': 'easyEase',
        'easeIn': 'easyEaseIn',
        'easeOut': 'easyEaseOut',
        'linear': 'linear',
        'bezier': 'bezier',
        'hold': 'hold',
        'easyEase': 'easyEase',
        'easyEaseIn': 'easyEaseIn',
        'easyEaseOut': 'easyEaseOut',
    }.get(value, 'easyEase')


def _add_declarative_layer(client, comp_name, layer, width, height, default_font):
    del width, height
    ltype = layer.get('type', 'text')
    name = layer.get('name')
    if ltype == 'solid':
        params = {'color': layer.get('color', '#111827')}
        if name:
            params['name'] = name
        res = client.call_tool('execute_command', {'command': 'layer.newSolid', 'params': params})
        layer_id = res['layer']
        if 'width_pct' in layer or 'height_pct' in layer:
            sx = float(layer.get('width_pct', 100.0))
            sy = float(layer.get('height_pct', 100.0))
            client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/scale', 'value': [sx, sy]})
        elif 'scale' in layer and isinstance(layer['scale'], list):
            client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/scale', 'value': layer['scale']})
    elif ltype == 'shape':
        shape_keys = (
            'kind', 'name', 'size', 'fill', 'fillType', 'fillBlend', 'fillOpacity',
            'stroke', 'strokeType', 'strokeBlend', 'strokeOpacity', 'strokeWidth', 'position',
        )
        shape_params = {k: layer[k] for k in shape_keys if k in layer}
        if 'size' in shape_params and 'kind' not in shape_params:
            shape_params['kind'] = 'rounded'
        res = client.call_tool('execute_command', {'command': 'layer.newShape', 'params': shape_params})
        layer_id = res['layer']
    elif ltype == 'camera':
        cam_params = {'name': name or 'Camera'}
        if 'camera_type' in layer:
            cam_params['type'] = str(layer['camera_type'])
        elif 'kind' in layer:
            cam_params['type'] = str(layer['kind'])
        for ck in (
            'preset', 'zoom', 'focalLength', 'angleOfView', 'dof',
            'focusDistance', 'aperture', 'fStop', 'blurLevel', 'lockToZoom',
            'position', 'poi',
        ):
            if ck in layer:
                cam_params[ck] = layer[ck]
        res = client.call_tool('execute_command', {'command': 'layer.newCamera', 'params': cam_params})
        layer_id = res['layer']
    elif ltype == 'footage':
        res = client.call_tool('execute_command', {
            'command': 'file.import',
            'params': {'paths': [str(Path(layer['path']).resolve())]},
        })
        if isinstance(res, dict) and res.get('errors'):
            raise RuntimeError(f'EffectCraft file.import failed: {res["errors"]}')
        items = res.get('items') if isinstance(res, dict) else None
        item_id = items[0] if isinstance(items, list) and items else (res.get('item') or res.get('id') if isinstance(res, dict) else None)
        if item_id is None:
            raise RuntimeError(f'EffectCraft file.import returned no item id: {res}')
        add_params = {'item': item_id}
        for fk in ('time', 'in', 'out', 'duration', 'anchor'):
            if fk in layer and layer[fk] is not None:
                add_params[fk] = layer[fk]
        if 'position' in layer and not layer.get('three_d'):
            add_params['position'] = layer['position']
        add_res = client.call_tool('execute_command', {'command': 'layer.addItem', 'params': add_params})
        layer_id = add_res['layer']
    elif ltype == 'text':
        text_val = layer.get('text', 'Title')
        font_family = _pick_cjk_font(client, layer.get('font')) if layer.get('font') else default_font
        size = float(layer.get('size', 68))
        fill = layer.get('color', '#FFFFFF')
        stroke_color = layer.get('stroke_color', '#0B1220')
        stroke_width = float(layer.get('stroke_width', 3 if layer.get('stroke', True) else 0))
        apply_stroke = bool(layer.get('stroke', stroke_width > 0) and stroke_width > 0)
        text_params = {
            'text': text_val,
            'font': font_family,
            'size': size,
            'fill': fill[:7] if isinstance(fill, str) else '#FFFFFF',
            'stroke': stroke_color[:7] if isinstance(stroke_color, str) else '#0B1220',
            'applyStroke': apply_stroke,
            'strokeWidth': stroke_width,
            'strokeOverFill': bool(layer.get('stroke_over_fill', False)),
        }
        if name:
            text_params['name'] = name
        if layer.get('font_style') or layer.get('style'):
            text_params['style'] = str(layer.get('font_style') or layer['style'])
        if 'justify' in layer or 'align' in layer:
            text_params['justify'] = str(layer.get('justify') or layer['align'])
        if 'position' in layer:
            text_params['position'] = layer['position']
        if 'box' in layer:
            text_params['box'] = layer['box']
        if 'tracking' in layer:
            text_params['tracking'] = float(layer['tracking'])
        res = client.call_tool('execute_command', {'command': 'layer.newText', 'params': text_params})
        layer_id = res['layer']

        for span in layer.get('highlights', []):
            set_params = {
                'layer': layer_id,
                'range': [int(span['start']), int(span['end'])],
                'fill': str(span['color'])[:7],
            }
            if 'size' in span:
                set_params['size'] = float(span['size'])
            if 'stroke' in span:
                set_params['stroke'] = str(span['stroke'])[:7]
            client.call_tool('execute_command', {'command': 'layer.setText', 'params': set_params})

        if layer.get('text_preset'):
            client.call_tool('execute_command', {
                'command': 'layer.applyTextPreset',
                'params': {'layer': layer_id, 'preset': str(layer['text_preset'])},
            })
    else:
        raise ValueError(f'Unsupported EffectCraft declarative layer type: {ltype}')

    # Enable 3D / motion-blur switches before 3D transforms and Time Remap before timeRemap keys.
    if ltype != 'camera':
        if 'three_d' in layer:
            client.call_tool('execute_command', {
                'command': 'layer.setSwitch',
                'params': {'layers': [layer_id], 'switch': 'threeD', 'value': bool(layer['three_d'])},
            })
        if 'motion_blur' in layer:
            client.call_tool('execute_command', {
                'command': 'layer.setSwitch',
                'params': {'layers': [layer_id], 'switch': 'motionBlur', 'value': bool(layer['motion_blur'])},
            })

    if 'anchor' in layer and ltype not in ('camera', 'footage'):
        client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/anchor', 'value': layer['anchor']})
    if 'position' in layer and ltype != 'camera':
        if ltype not in ('shape', 'text', 'footage') or layer.get('three_d'):
            client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/position', 'value': layer['position']})
    if 'scale' in layer and ltype not in ('solid', 'camera'):
        client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/scale', 'value': layer['scale']})
    if 'opacity' in layer and ltype != 'camera':
        client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/opacity', 'value': float(layer['opacity'])})
    if 'rotation' in layer and ltype != 'camera':
        client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': 'transform/rotation', 'value': float(layer['rotation'])})
    if layer.get('blend_mode') and ltype != 'camera':
        client.call_tool('execute_command', {'command': 'layer.setBlendMode', 'params': {'layers': [layer_id], 'mode': str(layer['blend_mode'])}})

    for st in layer.get('styles', []):
        client.call_tool('execute_command', {'command': f'layer.style.{st}', 'params': {'layers': [layer_id]}})

    for pid in layer.get('builtin_presets', []):
        client.call_tool('execute_command', {'command': 'anim.applyBuiltinPreset', 'params': {'preset': str(pid), 'layers': [layer_id]}})

    for m in layer.get('masks', []):
        mask_params = {'layer': layer_id}
        for mk in ('shape', 'rect', 'roundness', 'mode'):
            if mk in m:
                mask_params[mk] = m[mk]
        m_res = client.call_tool('execute_command', {'command': 'layer.addMask', 'params': mask_params})
        if 'inverted' in m:
            mask_id = m_res.get('mask', 1) if isinstance(m_res, dict) else 1
            client.call_tool('execute_command', {'command': 'layer.setMask', 'params': {
                'layer': layer_id, 'mask': mask_id, 'inverted': bool(m['inverted']),
            }})

    # Effects must be attached BEFORE keyframes or expressions referencing effects/#N/... are added.
    for eff in layer.get('effects', []):
        eff_name = eff if isinstance(eff, str) else eff['name']
        eff_vals = None if isinstance(eff, str) else eff.get('values')
        args = {'comp': comp_name, 'layer': layer_id, 'effect': eff_name}
        if eff_vals:
            args['values'] = eff_vals
        client.call_tool('add_effect', args)

    keyframes = layer.get('keyframes', [])
    wants_time_remap = bool(layer.get('time_remap')) or any(
        isinstance(k, dict) and k.get('path') in ('timeRemap', 'time_remap') for k in keyframes
    )
    if wants_time_remap:
        client.call_tool('execute_command', {
            'command': 'layer.enableTimeRemap',
            'params': {'layers': [layer_id], 'value': True},
        })

    for kf_group in keyframes:
        prop_path = kf_group['path']
        if prop_path == 'time_remap':
            prop_path = 'timeRemap'
        elif prop_path != 'timeRemap' and not prop_path.startswith(('transform/', 'effects/', 'masks/', 'text/', 'cameraOptions/', '@')):
            prop_path = {
                'position': 'transform/position',
                'scale': 'transform/scale',
                'opacity': 'transform/opacity',
                'rotation': 'transform/rotation',
                'anchor': 'transform/anchor',
                'zoom': 'cameraOptions/zoom',
            }.get(prop_path, prop_path)
        client.call_tool('add_keyframe', {
            'comp': comp_name,
            'layer': layer_id,
            'path': prop_path,
            'keys': kf_group['keys'],
            'interpolation': _map_interp(kf_group.get('interpolation', 'easyEase')),
        })

    for prop_path, expr in layer.get('expressions', {}).items():
        client.call_tool('set_property', {'comp': comp_name, 'layer': layer_id, 'path': prop_path, 'expression': expr})
    return layer_id


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
):
    """Compile an EffectCraft motion-graphics spec into .ecproj, RGBA PNG, and verified ProRes 4444 .mov."""
    cli = find_cli()
    w = int(spec.get('width', width or 1920))
    h = int(spec.get('height', height or 1080))
    rate = float(spec.get('fps', fps or 30))
    if draft:
        if resolution == 'full':
            resolution = 'half'
        if quality == 'best':
            quality = 'draft'
    if resolution not in ('full', 'half', 'third', 'quarter'):
        raise ValueError(f'Unsupported overlay resolution: {resolution}')
    if quality not in ('best', 'draft'):
        raise ValueError(f'Unsupported overlay quality: {quality}')
    compiled_spec = overlay_recipes.expand_recipe(spec, width=w, height=h, fps=rate) if 'recipe' in spec else spec
    comp_name = compiled_spec.get('name', 'Overlay')
    duration = float(compiled_spec['duration'])
    pt = round(duration * 0.5, 3) if preview_time is None else float(preview_time)
    gpu_flag = bool(spec.get('gpu', gpu))

    cache_key = None
    cache_root = None
    entry_dir = None
    if use_cache:
        cache_key = _overlay_cache_key(
            cli, compiled_spec, width=w, height=h, fps=rate, duration=duration,
            render_video=render_video, preview_time=pt, gpu=gpu_flag,
            resolution=resolution, quality=quality,
        )
        if cache_key is not None:
            cache_root = overlay_cache_dir(cache_dir)
            entry_dir = cache_root / cache_key[:2] / cache_key

    out = core.new_directory(out_dir)
    t0 = time.monotonic()
    if entry_dir is not None:
        cached = _try_restore_overlay_cache(
            entry_dir, out, comp_name, render_video=render_video,
            width=w, height=h, fps=rate, duration=duration, spec=spec,
            cache_key=cache_key, cache_root=cache_root,
            resolution=resolution, quality=quality,
        )
        if cached is not None:
            cached['resolution'] = resolution
            cached['quality'] = quality
            cached['elapsed_seconds'] = round(time.monotonic() - t0, 3)
            core.write_json(out / 'overlay-report.json', cached)
            return cached

    ecproj = out / f'{comp_name}.ecproj'
    preview_png = out / 'preview_rgba.png'

    with craft_rpc.CraftMcpClient([cli, 'mcp']) as client:
        default_font = _pick_cjk_font(client)
        if compiled_spec.get('script'):
            client.call_tool('run_script', {'code': compiled_spec['script'], 'name': comp_name})
            state = client.call_tool('get_state')
            active_comp = state.get('activeComp') or comp_name
        else:
            client.call_tool('execute_command', {
                'command': 'comp.new',
                'params': {'name': comp_name, 'width': w, 'height': h, 'frameRate': rate, 'duration': duration},
            })
            if 'motion_blur' in compiled_spec:
                client.call_tool('execute_command', {
                    'command': 'comp.setSwitch',
                    'params': {'switch': 'motionBlur', 'value': bool(compiled_spec['motion_blur'])},
                })
            active_comp = comp_name
            for layer in compiled_spec.get('layers', []):
                _add_declarative_layer(client, active_comp, layer, w, h, default_font)

        client.call_tool('save_project', {'path': str(ecproj)})
        client.call_tool('render_frame', {
            'comp': active_comp,
            'time': pt,
            'max_side': 0,
            'transparent': True,
            'inline': False,
            'path': str(preview_png),
        })

    if not ecproj.is_file() or not preview_png.is_file():
        raise RuntimeError('EffectCraft failed to produce .ecproj or transparent PNG')

    with Image.open(preview_png) as img:
        if img.mode not in ('RGBA', 'RGB') or img.size != (w, h):
            raise RuntimeError(f'EffectCraft PNG preview unexpected mode/size: {img.mode} {img.size}')
        if img.mode == 'RGB':
            # EffectCraft trims redundant all-255 alpha byte on full-frame opaque transitions; normalize to RGBA.
            img.convert('RGBA').save(preview_png)

    result = {
        'status': 'passed',
        'backend': 'effectcraft',
        'comp': active_comp,
        'project': str(ecproj),
        'preview_png': str(preview_png),
        'width': w,
        'height': h,
        'fps': rate,
        'duration': duration,
        'resolution': resolution,
        'quality': quality,
        'overlay_video': None,
    }
    if 'recipe' in spec:
        result['recipe'] = spec['recipe']

    if render_video:
        mov_path = out / f'{comp_name}.mov'
        cmd = [cli]
        if gpu_flag:
            cmd.append('--gpu')
        cmd.extend([
            '--project', str(ecproj),
            'render', '--comp', str(active_comp),
            '--out', str(mov_path),
            '--format', 'prores', '--prores', '4444',
            '--channels', 'rgba', '--audio', 'off',
            '--resolution', resolution, '--quality', quality,
        ])
        core.run(cmd, timeout=600)
        if not mov_path.is_file():
            raise RuntimeError(f'EffectCraft ProRes 4444 render produced no output: {mov_path}')
        info = core.probe(mov_path)
        if not info.get('has_alpha'):
            raise RuntimeError(f'Exported overlay lacks alpha channel (pix_fmt={info.get("pix_fmt")})')
        core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(mov_path), '-f', 'null', '-'], timeout=300)
        result['overlay_video'] = {
            'path': str(mov_path),
            'sha256': file_hash.sha256(mov_path),
            'bytes': mov_path.stat().st_size,
            'pix_fmt': info.get('pix_fmt'),
            'codec': info.get('video_codec'),
            'strict_decode': 'passed',
        }

    if entry_dir is not None:
        _store_overlay_cache(entry_dir, ecproj, preview_png, result, render_video=render_video)
        result['cache'] = {'hit': False, 'stored': True, 'key': cache_key, 'cache_dir': str(cache_root)}
    elif not use_cache:
        result['cache'] = {'hit': False, 'stored': False, 'reason': 'disabled'}
    else:
        result['cache'] = {'hit': False, 'stored': False, 'reason': 'bypassed'}

    result['elapsed_seconds'] = round(time.monotonic() - t0, 3)
    core.write_json(out / 'overlay-report.json', result)
    return result


def exec_commands(commands=(), *, project=None, save_as=None, script=None, inspect_comp=None):
    """Advanced escape hatch directly executing EffectCraft MCP commands or JSX scripts."""
    cli = find_cli()
    args = [cli]
    if project:
        proj = Path(project).resolve()
        if not proj.is_file():
            raise ValueError(f'Project file not found: {proj}')
        args.extend(['--project', str(proj)])
    args.append('mcp')

    results = []
    with craft_rpc.CraftMcpClient(args) as client:
        for step in commands:
            cid = step.get('command') or step.get('id')
            params = step.get('params', {})
            results.append({'command': cid, 'result': client.call_tool('execute_command', {'command': cid, 'params': params})})
        script_res = client.call_tool('run_script', {'code': script, 'name': 'effectcraft-exec'}) if script else None
        if save_as:
            client.call_tool('save_project', {'path': str(Path(save_as).resolve())})
        comp_state = client.call_tool('get_comp', {'comp': inspect_comp} if inspect_comp else {})
        return {
            'status': 'passed',
            'commands_executed': len(results),
            'results': results,
            'script_result': script_res,
            'comp': comp_state,
            'saved_project': str(Path(save_as).resolve()) if save_as else None,
        }
