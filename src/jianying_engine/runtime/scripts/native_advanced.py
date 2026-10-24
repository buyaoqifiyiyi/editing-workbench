"""Native structures measured in Jianying 11.5; no media rendering or UI input."""
import copy
import json
import math
from pathlib import Path
import uuid
import file_hash


MASK_PROPERTIES = {'center_x': 'KFTypeCommonMaskPositionX',
                   'center_y': 'KFTypeCommonMaskPositionY',
                   'rotation': 'KFTypeCommonMaskRotation',
                   'width': 'KFTypeCommonMaskSizeWidth',
                   'height': 'KFTypeCommonMaskSizeHeight',
                   'feather': 'KFTypeCommonMaskFeather'}
CORNER_PIN_PROPERTIES = {'up_left': 'KFTypeCornerPinUpLeft', 'up_right': 'KFTypeCornerPinUpRight',
                         'down_left': 'KFTypeCornerPinDownLeft', 'down_right': 'KFTypeCornerPinDownRight'}
OTHER_CORNERS = {'up_right': [1, -1], 'down_left': [-1, 1], 'down_right': [1, 1]}

CORNER_PIN_INFO = {'lower_left_x': -1.0, 'lower_left_y': 1.0,
                   'lower_right_x': 1.0, 'lower_right_y': 1.0,
                   'upper_left_x': -1.0, 'upper_left_y': -1.0,
                   'upper_right_x': 1.0, 'upper_right_y': -1.0}


def corner_runtime(install=None, runtime=None):
    import editor
    install = Path(install) if install is not None else editor.jianying_install()
    assets = Path(__file__).resolve().parents[1] / 'assets'
    spec = json.loads((assets / 'native-preview-11.6.0.14552.json').read_text(encoding='utf-8'))
    font = json.loads((assets / 'text-alpha-v1.json').read_text(encoding='utf-8'))
    if not install or install.name != spec['version']:
        raise ValueError('Additional corner motion requires measured Jianying ' + spec['version'])
    files = dict(spec['files']); files[spec['runtime']['file']] = spec['runtime']['sha256']
    files[font['font_relative']] = font['font_sha256']
    for relative, digest in files.items():
        if not file_hash.matches(install / relative, digest):
            raise ValueError('Corner motion runtime/font identity changed: ' + relative)
    actual = Path(runtime) if runtime is not None else install / spec['runtime']['file']
    if not file_hash.matches(actual, spec['runtime']['sha256']):
        raise ValueError('Corner motion Bun identity changed: ' + str(actual))


def other_corner_requested(spec):
    return any(k['property'] in OTHER_CORNERS for k in spec.get('perspective', {}).get('keyframes', []))


def validate_other_corner(plan, spec, duration, source):
    keys = spec['perspective']['keyframes']; prop = keys[0]['property']
    if (len(keys) != 2 or prop not in OTHER_CORNERS or any(k['property'] != prop for k in keys) or
            any(k['values'] not in (OTHER_CORNERS[prop], [v * .5 for v in OTHER_CORNERS[prop]]) for k in keys)):
        raise ValueError('Additional corner motion requires two keys on one corner between original and half-inset positions')
    if (plan['canvas'] != {'width': 640, 'height': 360, 'fps': 30} or len(plan['clips']) != 1 or len(plan['captions']) != 1 or
            duration != 2000000 or spec.get('start', 0) != 0 or spec.get('keyframes') or spec.get('motion_easing') or
            plan['audio'] or plan['effects'] or plan['filters'] or source['kind'] != 'image' or
            (source['width'], source['height']) != (640, 360) or len(plan['composites']) != 1 or len(spec['tracks']) != 2 or
            Path(plan['clips'][0]['path']).suffix.lower() != '.png'):
        raise ValueError('Additional corner motion requires a standalone 640x360/30fps PNG/text two-second group')
    for item in plan['clips'] + plan['captions']:
        if item['start'] != 0 or item['duration'] != 2 or any(item.get(k) for k in ('transform', 'keyframes', 'animations', 'effects',
                'filters', 'grading', 'mask', 'chroma', 'background', 'mix_mode', 'motion_easing', 'text_curve', 'glow', 'font', 'style')):
            raise ValueError('Additional corner motion requires static full-range PNG and default-font text without effects')
    if plan['clips'][0]['fit'] != 'contain' or any(ord(c) > 0xffff or 0xd800 <= ord(c) <= 0xdfff or c in '\r\n' for c in plan['captions'][0]['text']):
        raise ValueError('Additional corner motion requires contain PNG and single-line BMP text')
    corner_runtime()


def corner_preview_check(content, root, install, runtime=None):
    import editor
    types = {CORNER_PIN_PROPERTIES[k] for k in OTHER_CORNERS}
    def contains(v):
        if isinstance(v, dict):return v.get('property_type') in types or any(contains(x) for x in v.values())
        return isinstance(v, list) and any(contains(x) for x in v)
    path = Path(root) / 'edit-plan.json'
    declared = path.is_file() and any(other_corner_requested(s) for s in editor.read_json(path).get('composites', []))
    if contains(content) or declared:
        corner_runtime(install, runtime)
        if not path.is_file():raise ValueError('Additional corner preview requires its formal edit-plan.json')
        plan, _, _ = editor.validate_plan(path)
        editor.verify_structure(content, plan)


def uid():
    return str(uuid.uuid4()).upper()


def green_lightness_runtime(install=None, runtime=None):
    import editor
    install = Path(install) if install is not None else editor.jianying_install()
    spec = json.loads((Path(__file__).resolve().parents[1] / 'assets/native-preview-11.6.0.14552.json').read_text(encoding='utf-8'))
    if not install or install.name != spec['version']:
        raise ValueError('Green lightness requires measured Jianying ' + spec['version'])
    for relative, digest in spec['files'].items():
        if not file_hash.matches(install / relative, digest):
            raise ValueError('Green lightness runtime identity changed: ' + relative)
    actual = Path(runtime) if runtime is not None else install / spec['runtime']['file']
    if not file_hash.matches(actual, spec['runtime']['sha256']):
        raise ValueError('Green lightness Bun identity changed: ' + str(actual))


def green_lightness_preview_check(content, root, install, runtime=None):
    import editor
    path = Path(root) / 'edit-plan.json'
    declared = path.is_file() and any('lightness' in c.get('grading', {}).get('green_hsl', {}) for c in editor.read_json(path).get('clips', []))
    observed = any(m.get('hsl_color_type') == 4 and m.get('lightness') != 0 for m in content.get('materials', {}).get('hsl', []))
    if declared or observed:
        green_lightness_runtime(install, runtime)
        if not path.is_file():raise ValueError('Green lightness preview requires formal edit-plan.json')
        plan, _, _ = editor.validate_plan(path)
        editor.verify_structure(content, plan)


def denoise_material(spec):
    model = Path(spec['model_path']).resolve()
    expected = 'a20cbd66d4ae8d483fe0f1759fa76803f4355d96aece3b65902b6cf34ff14c1c'
    if (not model.is_file() or model.name != 'unet_denoise_44k_music_model_v1.0.model' or
            file_hash.sha256(model) != expected):
        raise ValueError(f'Measured Jianying 11.5 realtime denoise model unavailable or changed: {model}')
    return {'id': uid(), 'type': 'realtime_denoise', 'is_denoise': True,
            'denoise_rate': 0.85, 'path': model.as_posix(), 'sami_type': 2}


def loudness_material(item, measurement):
    return {'id': uid(), 'enable': True,
            'time_range': {'start': round(item['source_in'] * 1_000_000), 'duration': round(item['duration'] * 1_000_000)},
            'file_id': '',  # Installed schema default; editor derives identity when saved.
            'target_loudness': item['loudness']['target_lufs'], 'loudness_param': measurement}


def templates():
    return json.loads((Path(__file__).resolve().parent.parent / 'assets' / 'native-11.5.json').read_text(encoding='utf-8'))


def composite_range(spec, total):
    return round(spec.get('start', 0) * 1_000_000), round(spec.get('duration', total / 1_000_000) * 1_000_000)


def linear_group(property_type, points):
    return {'property_type': property_type, 'keyframe_list': [
        {'curveType': 'Line', 'graphID': '', 'left_control': {'x': 0.0, 'y': 0.0},
         'right_control': {'x': 0.0, 'y': 0.0}, 'string_value': '',
         'time_offset': time, 'values': [value]} for time, value in points]}


def composite_motion(spec):
    points = [(round(k['time'] * 1_000_000), k['value']) for k in spec.get('keyframes', [])]
    if not points:
        return []
    prop = spec['keyframes'][0]['property']
    types = (['KFTypeScaleX', 'KFTypeScaleY'] if prop == 'uniform_scale' else
             [{'position_x': 'KFTypePositionX', 'position_y': 'KFTypePositionY', 'rotation': 'KFTypeRotation'}[prop]])
    groups = [linear_group(kind, points) for kind in types]
    if 'motion_easing' in spec:
        for group in groups:
            for point, values in zip(group['keyframe_list'], motion_curve_fields(spec)):
                point.update(values)
    return groups


def motion_curve_fields(item):
    keys = sorted(item['keyframes'], key=lambda k: k['time'])
    source_in, speed = item.get('source_in', 0), item.get('speed', 1)
    delta = (round((source_in + keys[-1]['time'] * speed) * 1_000_000) -
             round((source_in + keys[0]['time'] * speed) * 1_000_000))
    return [dict(curveType='CurveOut', graphID='', left_control={'x': 0.0, 'y': 0.0},
                 right_control={'x': delta / 3, 'y': 0.0}),
            dict(curveType='CurveIn', graphID='', left_control={'x': -delta / 3, 'y': 0.0},
                 right_control={'x': 0.0, 'y': 0.0})]


def verify_motion_curves(segment, item):
    # Verify actual handles, including unrequested non-linear mutations of ordinary keys.
    for group in segment['common_keyframes']:
        prop = group['property_type']
        if prop not in {'KFTypePositionX', 'KFTypePositionY', 'KFTypeRotation', 'KFTypeScaleX', 'KFTypeScaleY', 'KFTypeAlpha', 'KFTypeGlobalAlpha'}:
            continue
        if not group['keyframe_list']:
            raise ValueError('Motion keyframe group must not be empty')
        if any(not isinstance(p.get(hand), dict) or any(type(p[hand].get(axis)) not in (int, float) or not math.isfinite(p[hand][axis])
               for axis in ('x', 'y')) for p in group['keyframe_list'] for hand in ('left_control', 'right_control')):
            raise ValueError('Motion control values must be finite numbers, not booleans')
        expected = (motion_curve_fields(item) if 'motion_easing' in item else
                    [dict(curveType='Line', graphID='', left_control={'x': 0.0, 'y': 0.0},
                          right_control={'x': 0.0, 'y': 0.0}) for _ in group['keyframe_list']])
        actual = [{k: p.get(k) for k in expected[0]} for p in group['keyframe_list']]
        if actual != expected or any(p.get('graph') is not None for p in group['keyframe_list']):
            raise ValueError('Motion curve type, handle or graph readback mismatch')


def apply_motion_curve(segment, item):
    if 'motion_easing' in item:
        prop = {'position_x': 'KFTypePositionX', 'position_y': 'KFTypePositionY'}[item['keyframes'][0]['property']]
        group = next(g for g in segment['common_keyframes'] if g['property_type'] == prop)
        for point, values in zip(group['keyframe_list'], motion_curve_fields(item)):
            point.update(values)


def curve_points(anchors):
    result = []
    for i, (x, y) in enumerate(anchors):
        before, after = anchors[max(i - 1, 0)], anchors[min(i + 1, len(anchors) - 1)]
        result.append({'anchor': {'x': x, 'y': y},
                       'left_control': {'x': x + (before[0] - x) / 3, 'y': y + (before[1] - y) / 3},
                       'right_control': {'x': x + (after[0] - x) / 3, 'y': y + (after[1] - y) / 3},
                       'sync_direction_control': False})
    return result


def cache_root(relative, user_data):
    """The root holding this family's cache: an explicit root is exact, None resolves it on the machine.

    The resolver (candidate order, overrides, fallbacks) lives in editor; importing it lazily keeps this
    module importable on its own and avoids an import cycle. An explicit root means the caller already
    decided where the cache has to be, so the lookup stays exact there instead of reaching another
    installation behind the caller's back.
    """
    if user_data is not None:
        return Path(user_data)
    import editor
    return Path(editor.resource_user_data(relative))


def cached_material(kind, user_data):
    measured = json.loads((Path(__file__).resolve().parent.parent / 'assets' / 'native-extensions-11.5.json').read_text(encoding='utf-8'))
    material = copy.deepcopy(measured[kind])
    relative = material.pop('cache_relative')
    cache = cache_root(relative, user_data) / relative
    identities = material.pop('file_sha256', {})
    if not cache.is_dir():
        raise ValueError(f'Native resource {material["resource_id"]} unavailable: {cache}; load in Jianying or keep the effect pending')
    for relative, expected in identities.items():
        file = cache / relative
        if not file_hash.matches(file, expected):
            raise ValueError(f'Native resource {material["resource_id"]} identity mismatch: {file}')
    material.update(id=uid(), path=str(cache))
    if 'lumi_hub_path' in material:
        material['lumi_hub_path'] = str(cache / 'lumi_hub_path')
    if 'constant_material_id' in material:
        material['constant_material_id'] = uid()
    return material


def blur_material(item, user_data):
    material = cached_material('blur', user_data)
    value = item.get('params', {}).get('effects_adjust_blur')
    material['adjust_params'][0]['value'] = 0.5 if value is None else value / 100
    return material


def color_materials(spec, source, user_data):
    import native_hsl_channels
    if native_hsl_channels.requested(spec):
        return native_hsl_channels.material(spec, source, user_data)
    if {'blue_hsl', 'green_hsl'} <= spec.keys():
        return {'hsl': [color_materials({field: spec[field]}, source, user_data)['hsl']
                        for field in ('blue_hsl', 'green_hsl')]}
    result = {}
    for kind in ('hsl', 'color_curves', 'common_mask'):
        if (kind == 'hsl' and not ({'red_hsl', 'blue_hsl', 'green_hsl'} & spec.keys()) or
                kind == 'color_curves' and not ({'luma_curve', 'rgb_curves'} & spec.keys()) or
                kind == 'common_mask' and 'local_circle' not in spec):
            continue
        material = cached_material(kind, user_data)
        if kind == 'hsl':
            if 'green_hsl' in spec:
                material.update(hsl_color_type=4, hue=spec['green_hsl'].get('hue', 0), lightness=spec['green_hsl'].get('lightness', 0),
                                saturation=spec['green_hsl'].get('saturation', 0))
            elif 'blue_hsl' in spec:
                material.update(hsl_color_type=6, hue=spec['blue_hsl'].get('hue', 0), lightness=spec['blue_hsl'].get('lightness', 0),
                                saturation=spec['blue_hsl'].get('saturation', 0))
            else:
                material.update(spec['red_hsl'])
        elif kind == 'color_curves':
            material['luma'] = curve_points(spec.get('luma_curve', [(0, 0), (1, 1)]))
            for channel in ('red', 'green', 'blue'):
                material[channel] = curve_points(spec.get('rgb_curves', {}).get(channel, [(0, 0), (1, 1)]))
            material['select_channel'] = 0
        else:
            mask = spec['local_circle']
            material['config'].update(width=mask['width'] / source['width'], height=mask['height'] / source['height'],
                                       feather=mask['feather'] / 100, centerX=0, centerY=0, rotation=0)
        result[kind] = material
    return result


def average_curve_speed(points):
    return 1 / sum((b['x'] - a['x']) / ((a['y'] + b['y']) / 2)
                   for a, b in zip(points, points[1:]))


def source_keyframe_time(item, local_time):
    if 'speed_curve' not in item:
        return round((item['source_in'] + local_time * item['speed']) * 1_000_000)
    # Installed 11.5 module 15123: three linear-speed regions per anchor interval.
    points = item['speed_curve']
    weights = [(b['x'] - a['x']) / ((a['y'] + b['y']) / 2) for a, b in zip(points, points[1:])]
    total, start, offset = sum(weights), 0.0, 0.0
    target = round(local_time * 1_000_000)
    duration = round(item['duration'] * 1_000_000)
    for a, b, weight in zip(points, points[1:], weights):
        length = duration * weight / total
        delta = b['y'] - a['y']
        boundaries = [start, start + .2049 * length, start + .7951 * length, start + length]
        speeds = [a['y'], a['y'] + .1 * delta, a['y'] + .9 * delta, b['y']]
        for left, right, first, last in zip(boundaries, boundaries[1:], speeds, speeds[1:]):
            elapsed = max(0.0, min(target, right) - left)
            offset += first * elapsed + (last - first) * elapsed * elapsed / (2 * (right - left))
        start += length
    return round(item['source_in'] * 1_000_000) + math.floor(offset + .5)


def speed_curve_material(points):
    return dict(id='', name='', source_platform=0, speed_points=copy.deepcopy(points))


def verify_speed(segment, item, content):
    actual = [m for m in content['materials'].get('speeds', [])
              if m['id'] in segment['extra_material_refs']]
    if len(actual) != 1:
        raise ValueError('Speed material count/reference mismatch')
    expected_curve = speed_curve_material(item['speed_curve']) if 'speed_curve' in item else None
    if expected_curve is not None and any(g.get('property_type') not in
            {MASK_PROPERTIES[k['property']] for k in item.get('mask', {}).get('keyframes', [])}
            for g in segment.get('common_keyframes', [])):
        raise ValueError('Speed curve does not admit injected source-time keyframes')
    expected_speed = item['speed']
    if expected_curve is None:
        # Normal video/audio speed is derived by pyJianYingDraft from rounded microsecond ranges.
        expected_speed = round(item['duration'] * item['speed'] * 1_000_000) / round(item['duration'] * 1_000_000)
    if (actual[0].get('type') != 'speed' or actual[0].get('mode') != int(expected_curve is not None) or
            actual[0].get('curve_speed') != expected_curve or
            abs(actual[0].get('speed', -1) - expected_speed) > 1e-6 or
            abs(segment.get('speed', -1) - expected_speed) > 1e-6):
        raise ValueError('Speed mode, scalar or curve points readback mismatch')


def apply_extensions(content, plan, user_data, measure_loudness=None):
    segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    for item in plan['clips']:
        if 'speed_curve' in item:
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            speed = next(materials[ref] for ref in segment['extra_material_refs']
                         if materials[ref]['type'] == 'speed')
            speed.update(mode=1, speed=item['speed'], curve_speed=speed_curve_material(item['speed_curve']))
            segment['speed'] = item['speed']
        apply_motion_curve(segments[item['track'], round(item['start'] * 1_000_000)], item)
    for item in plan['captions']:
        apply_motion_curve(segments[item['track'], round(item['start'] * 1_000_000)], item)
    for item in plan['audio']:
        if 'loudness' in item:
            if measure_loudness is None:
                raise ValueError('Native loudness source measurement is required')
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            material = loudness_material(item, measure_loudness(item))
            content['materials'].setdefault('loudnesses', []).append(material)
            segment['extra_material_refs'].append(material['id'])
            content['new_version'] = '187.0.0'
        if 'denoise' in item:
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            material = denoise_material(item['denoise'])
            content['materials'].setdefault('realtime_denoises', []).append(material)
            segment['extra_material_refs'].append(material['id'])
            content['new_version'] = '187.0.0'
    for item in plan['captions']:
        if 'text_curve' in item:
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            material = materials[segment['material_id']]
            material['text_curve'] = {'enable': True, 'angle': item['text_curve']['angle']}
            # Jianying 11.5 native toggle sample: style 47 -> 175; preserve all existing style bits.
            material['check_flag'] |= 128
    for item in plan['effects']:
        if not item.get('keyframes'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        replacement = blur_material(item, user_data)
        replacement['id'] = segment['material_id']
        actual_material = materials[segment['material_id']]
        actual_material.clear()
        actual_material.update(replacement)
        group = linear_group('effects_adjust_blur', [(round(k['time'] * 1_000_000), k['value'] / 100) for k in item['keyframes']])
        group.update(id=uid(), material_id='')
        for point in group['keyframe_list']:
            point['id'] = uid()
        segment['common_keyframes'].append(group)
    for item in plan['clips']:
        if not item.get('grading'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        additions = color_materials(item['grading'], materials[segment['material_id']], user_data)
        for kind, material in additions.items():
            for entry in material if isinstance(material, list) else [material]:
                content['materials'].setdefault(kind, []).append(entry)
                segment['extra_material_refs'].append(entry['id'])
        segment.update(enable_adjust=True, enable_hsl='hsl' in additions, enable_color_curves='color_curves' in additions,
                       enable_color_wheels=False, enable_hsl_curves=False, enable_adjust_mask='common_mask' in additions)
        if 'common_mask' in additions:
            segment['enable_video_mask'] = True
        content['new_version'] = '187.0.0'


def verify_extensions(content, plan, user_data, measure_loudness=None):
    segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    counts = {'audio_volume_keyframes': 0, 'blur_keyframes': 0, 'grading': 0}
    if any('speed_curve' in item for item in plan['clips']):
        counts['speed_curve'] = sum('speed_curve' in item for item in plan['clips'])
    if any('motion_easing' in item for item in plan['clips'] + plan['captions'] + plan.get('composites', [])):
        counts['motion_easing'] = sum('motion_easing' in item for item in plan['clips'] + plan['captions'] + plan.get('composites', []))
    def points(groups):
        return [(g['property_type'], [(p['time_offset'], p['values'], p['curveType']) for p in g['keyframe_list']]) for g in groups]
    for item in plan['audio']:
        if 'loudness' in item:
            if measure_loudness is None:
                raise ValueError('Native loudness source measurement is required')
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            actual = [m for m in content['materials'].get('loudnesses', []) if m['id'] in segment['extra_material_refs']]
            expected = loudness_material(item, measure_loudness(item))
            if (len(actual) != 1 or {k: v for k, v in actual[0].items() if k != 'id'} !=
                    {k: v for k, v in expected.items() if k != 'id'}):
                raise ValueError('Native loudness source identity, range, target or measured parameters mismatch')
            counts['audio_loudness'] = counts.get('audio_loudness', 0) + 1
        if 'denoise' in item:
            segment = segments[item['track'], round(item['start'] * 1_000_000)]
            actual = [m for m in content['materials'].get('realtime_denoises', [])
                      if m['id'] in segment['extra_material_refs']]
            expected = denoise_material(item['denoise'])
            if (len(actual) != 1 or {k: v for k, v in actual[0].items() if k != 'id'} !=
                    {k: v for k, v in expected.items() if k != 'id'}):
                raise ValueError('Native realtime denoise model, parameters or reference mismatch')
            counts['audio_realtime_denoise'] = counts.get('audio_realtime_denoise', 0) + 1
        if not item.get('keyframes'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        actual = [g for g in segment['common_keyframes'] if g['property_type'] == 'KFTypeVolume']
        expected = [linear_group('KFTypeVolume', [(round((item['source_in'] + k['time'] * item['speed']) * 1_000_000), k['value']) for k in item['keyframes']])]
        if points(actual) != points(expected):
            raise ValueError('Audio volume keyframe source-time readback mismatch')
        counts['audio_volume_keyframes'] += 1
    for item in plan['effects']:
        if not item.get('keyframes'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        actual = [g for g in segment['common_keyframes'] if g['property_type'] == 'effects_adjust_blur']
        expected = [linear_group('effects_adjust_blur', [(round(k['time'] * 1_000_000), k['value'] / 100) for k in item['keyframes']])]
        if any(g['material_id'] != '' for g in actual) or points(actual) != points(expected):
            raise ValueError('Blur effect-local keyframe readback mismatch')
        material = materials[segment['material_id']]
        expected_material = blur_material(item, user_data)
        if {k: v for k, v in material.items() if k != 'id'} != {k: v for k, v in expected_material.items() if k != 'id'}:
            raise ValueError('Native 11.5 blur resource readback mismatch')
        counts['blur_keyframes'] += 1
    for item in plan['clips']:
        if not item.get('grading'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        source = materials[segment['material_id']]
        expected = color_materials(item['grading'], source, user_data)
        for kind, material in expected.items():
            actual = [m for m in content['materials'][kind] if m['id'] in segment['extra_material_refs']]
            expected_entries = material if isinstance(material, list) else [material]
            if len(actual) != len(expected_entries):
                raise ValueError('Grading material count/reference mismatch')
            excluded = {'id', 'constant_material_id'}
            if [{k: v for k, v in entry.items() if k not in excluded} for entry in actual] != [{k: v for k, v in entry.items() if k not in excluded} for entry in expected_entries]:
                raise ValueError(f'Grading {kind} parameter readback mismatch')
            if 'lightness' in item['grading'].get('green_hsl', {}):
                green_lightness_runtime()
                if (content.get('fps') != 30 or (content['canvas_config']['width'], content['canvas_config']['height']) != (640, 360) or
                        any(type(entry.get('hsl_color_type')) is not int or any(type(entry.get(k)) not in (int, float) or
                            not math.isfinite(entry[k]) for k in ('hue', 'saturation', 'lightness')) for entry in actual)):
                    raise ValueError('Green lightness native canvas/channel/scalar types changed')
            if isinstance(material, list):
                expected_ids = [entry['id'] for entry in actual]
                if [ref for ref in segment['extra_material_refs'] if ref in expected_ids] != expected_ids:
                    raise ValueError('Paired HSL reference order mismatch')
        flags = {'enable_adjust': True, 'enable_hsl': 'hsl' in expected,
                 'enable_color_curves': 'color_curves' in expected, 'enable_color_wheels': False,
                 'enable_hsl_curves': False, 'enable_adjust_mask': 'common_mask' in expected}
        if 'common_mask' in expected:
            flags['enable_video_mask'] = True
        if any(segment.get(k) != v for k, v in flags.items()):
            raise ValueError('Grading enable flags mismatch')
        if 'lightness' in item['grading'].get('green_hsl', {}) and any(type(segment.get(k)) is not bool for k in flags):
            raise ValueError('Green lightness flags must remain boolean')
        counts['grading'] += 1
    import native_hsl_channels
    native_hsl_channels.verify(content, plan)
    return counts


def mask_frames(item, width, height):
    groups = []
    for prop, native in MASK_PROPERTIES.items():
        points = [k for k in item['mask'].get('keyframes', []) if k['property'] == prop]
        if not points:
            continue
        divisor = {'center_x': width / 2, 'center_y': height / 2,
                   'width': width, 'height': height, 'feather': 100}.get(prop, 1)
        groups.append({'property_type': native, 'keyframe_list': [
            {'curveType': 'Line', 'graphID': '', 'left_control': {'x': 0.0, 'y': 0.0},
             'right_control': {'x': 0.0, 'y': 0.0}, 'string_value': '',
             'time_offset': source_keyframe_time(item, k['time']),
             'values': [k['value'] / divisor]}
            for k in points]})
    return groups


def circle_material(item, source, user_data):
    material = cached_material('common_mask', user_data)
    spec = item['mask']
    material.update(type='mask', category='video', category_id='jichu', category_name='基础')
    material['config'].update(aspectRatio=1, centerX=spec['center_x'] / (source['width'] / 2),
        centerY=spec['center_y'] / (source['height'] / 2), height=spec['size'],
        width=spec['size'] * source['height'] / source['width'],
        rotation=spec['rotation'], invert=spec['invert'], feather=0, roundCorner=0)
    return material


def apply_masks(content, plan, user_data=None):
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    segments = {(t['name'], s['target_timerange']['start']): s
                for t in content['tracks'] for s in t['segments']}
    for item in plan['clips']:
        name = item.get('mask', {}).get('name')
        if name not in ('矩形', '圆形'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        mask = next(materials[r] for r in segment['extra_material_refs']
                    if materials[r].get('type') == 'mask')
        source = materials[segment['material_id']]
        if name == '圆形':
            replacement = circle_material(item, source, user_data)
            replacement['id'] = mask['id']
            mask.clear()
            mask.update(replacement)
        # Keep generated geometry but supply the native identity and enable switch.
        if name == '矩形':
            measured = cached_material('rectangle_mask', user_data)
            mask.update({k: copy.deepcopy(v) for k, v in measured.items()
                         if k not in ('id', 'config', 'constant_material_id')})
        mask['constant_material_id'] = uid()
        content['new_version'] = '187.0.0'
        content['materials'].setdefault('common_mask', []).append(mask)
        content['materials']['masks'] = [m for m in content['materials'].get('masks', []) if m['id'] != mask['id']]
        segment['enable_video_mask'] = True
        for group in mask_frames(item, source['width'], source['height']):
            group.update(id=uid(), material_id=mask['constant_material_id'])
            for point in group['keyframe_list']:
                point['id'] = uid()
            segment['common_keyframes'].append(group)


def perspective_frames(spec):
    groups = []
    for prop, native in CORNER_PIN_PROPERTIES.items():
        points = [k for k in spec.get('perspective', {}).get('keyframes', []) if k['property'] == prop]
        if points:
            groups.append({'property_type': native, 'keyframe_list': [
                {'curveType': 'Line', 'graphID': '', 'left_control': {'x': 0.0, 'y': 0.0},
                 'right_control': {'x': 0.0, 'y': 0.0}, 'string_value': '',
                 'time_offset': round(k['time'] * 1_000_000), 'values': list(k['values'])}
                for k in points]})
    return groups


def apply_composites(content, plan):
    if 'layer_order' in plan or plan.get('composites'):
        content['free_render_index_mode_on'] = True
        content['render_index_track_mode_on'] = False
    measured = templates()
    for spec in plan.get('composites', []):
        content['new_version'] = '187.0.0'
        names = spec['tracks']
        selected = [next(t for t in content['tracks'] if t['name'] == name) for name in names]
        start, duration = composite_range(spec, content['duration'])
        nested = copy.deepcopy(content)
        nested.update(id=uid(), name=spec['name'], duration=duration)
        nested['tracks'] = copy.deepcopy(selected)
        nested['materials']['drafts'] = []
        refs = {r for t in selected for s in t['segments']
                for r in [s['material_id'], *s['extra_material_refs']]}
        for kind, values in nested['materials'].items():
            if isinstance(values, list):
                nested['materials'][kind] = [m for m in values if isinstance(m, dict) and m.get('id') in refs]
        for index, track in enumerate(nested['tracks']):
            track['flag'] = 0 if index == 0 else 2
            for segment in track['segments']:
                segment['target_timerange']['start'] -= start
                segment['render_index'] = index
        combination = copy.deepcopy(measured['combination'])
        combination.update(id=uid(), combination_id=uid(), draft=nested)
        video = copy.deepcopy(measured['video'])
        video.update(id=uid(), material_name=spec['name'], duration=duration,
                     width=plan['canvas']['width'], height=plan['canvas']['height'])
        outer = copy.deepcopy(measured['segment'])
        outer.update(id=uid(), material_id=video['id'], extra_material_refs=[combination['id']],
                       target_timerange={'start': start, 'duration': duration},
                       source_timerange={'start': 0, 'duration': duration})
        outer['common_keyframes'] = []
        if spec.get('perspective'):
            video['corner_pin'] = {'corner_pin_info': copy.deepcopy(CORNER_PIN_INFO),
                                   'enable_corner_pin': True, 'id': uid(), 'sub_type': 'video'}
            if 'static_up_right' in spec['perspective']:
                x, y = spec['perspective']['static_up_right']
                video['corner_pin']['corner_pin_info'].update(upper_right_x=x, upper_right_y=y)
            for group in perspective_frames(spec):
                group.update(id=uid(), material_id='')
                for point in group['keyframe_list']:
                    point['id'] = uid()
                outer['common_keyframes'].append(group)
        outer['clip'] = {'alpha': 1.0, 'flip': {'horizontal': False, 'vertical': False},
                         'rotation': 0.0, 'scale': {'x': 1.0, 'y': 1.0},
                         'transform': {'x': 0.0, 'y': 0.0}}
        for group in composite_motion(spec):
            group.update(id=uid(), material_id='')
            for point in group['keyframe_list']:
                point['id'] = uid()
            outer['common_keyframes'].append(group)
        track = copy.deepcopy(selected[0])
        track.update(id=uid(), segments=[outer])
        first = content['tracks'].index(selected[0])
        content['tracks'] = [t for t in content['tracks'] if t['name'] not in names]
        content['tracks'].insert(first, track)
        content['materials'].setdefault('drafts', []).append(combination)
        content['materials']['videos'].append(video)
    for index, track in enumerate(content['tracks']):
        for segment in track['segments']:
            segment['render_index'] = index


def write_sidecars(content, physical_root, final_root, write_json):
    """Relocate combination paths after build/publish and recreate readable sidecars."""
    measured = templates()
    for combination in content['materials'].get('drafts', []):
        nested = combination['draft']
        relative = Path('subdraft') / nested['id']
        physical = Path(physical_root) / relative
        final = Path(final_root) / relative
        physical.mkdir(parents=True, exist_ok=True)
        for key, filename in (('draft_file_path', 'draft_content.json'),
                              ('draft_config_path', 'sub_draft_config.json'),
                              ('draft_cover_path', 'draft_cover.jpg')):
            combination[key] = str(final / filename)
        wrapper = copy.deepcopy(content)
        wrapper.update(id=nested['id'], name=nested['name'])
        wrapper['tracks'] = [t for t in wrapper['tracks']
                             if any(combination['id'] in s.get('extra_material_refs', []) for s in t['segments'])]
        wrapper['materials']['drafts'] = [copy.deepcopy(combination)]
        write_json(physical / 'draft_content.json', wrapper)
        config = copy.deepcopy(measured['config'])
        config.update(id=nested['id'], project_id=nested['id'], name=nested['name'],
                      rough_cut_duration=nested['duration'])
        write_json(physical / 'sub_draft_config.json', config)


def expanded_content(content, plan):
    """Recover plan tracks for the existing timeline/native readback validators."""
    result = copy.deepcopy(content)
    specs = plan.get('composites', [])
    combinations = content['materials'].get('drafts', [])
    if len(combinations) != len(specs):
        raise ValueError('Composite count mismatch')
    for index, track in enumerate(content['tracks']):
        if any(s.get('render_index') != index for s in track['segments']):
            raise ValueError('Composite outer render order mismatch')
    for spec in specs:
        matches = [m for m in combinations if m['draft']['name'] == spec['name']]
        if len(matches) != 1:
            raise ValueError('Composite identity mismatch')
        combination = matches[0]
        nested = combination['draft']
        if nested.get('free_render_index_mode_on') is not True or nested.get('render_index_track_mode_on') is not False:
            raise ValueError('Composite internal layer order requires free render index mode')
        start, duration = composite_range(spec, content['duration'])
        if ([t['name'] for t in nested['tracks']] != spec['tracks'] or
                nested['duration'] != duration or nested['canvas_config'] != content['canvas_config'] or
                nested['fps'] != content['fps']):
            raise ValueError('Composite internal tracks/canvas/duration mismatch')
        for index, track in enumerate(nested['tracks']):
            if any(s.get('render_index') != index for s in track['segments']):
                raise ValueError('Composite internal render order mismatch')
        track = next((t for t in result['tracks'] if any(combination['id'] in s.get('extra_material_refs', [])
                                                       for s in t['segments'])), None)
        if track is None or len(track['segments']) != 1:
            raise ValueError('Composite outer reference missing')
        segment = track['segments'][0]
        source_range = {'start': 0, 'duration': duration}
        target_range = {'start': start, 'duration': duration}
        videos = {m['id']: m for m in content['materials']['videos']}
        video = videos.get(segment['material_id'], {})
        if (segment['source_timerange'] != source_range or segment['target_timerange'] != target_range or
                video.get('path') != '' or video.get('duration') != duration or
                video.get('width') != content['canvas_config']['width'] or
                video.get('height') != content['canvas_config']['height']):
            raise ValueError('Composite outer material/range mismatch')
        motion = [g for g in segment['common_keyframes'] if g['property_type'] not in CORNER_PIN_PROPERTIES.values()]
        verify_motion_curves(segment, spec)
        def comparable(groups):
            return [(g['property_type'], [(p['time_offset'], p['values'], p['curveType'])
                                          for p in g['keyframe_list']]) for g in groups]
        if any(g['material_id'] != '' for g in motion) or comparable(motion) != comparable(composite_motion(spec)):
            raise ValueError('Composite outer motion readback mismatch')
        index = result['tracks'].index(track)
        result['tracks'][index:index+1] = copy.deepcopy(nested['tracks'])
        for track in result['tracks'][index:index+len(nested['tracks'])]:
            for inner in track['segments']:
                if not (0 <= inner['target_timerange']['start'] and
                        inner['target_timerange']['start'] + inner['target_timerange']['duration'] <= duration):
                    raise ValueError('Composite internal range escapes local duration')
                inner['target_timerange']['start'] += start
        for kind, values in nested['materials'].items():
            if isinstance(values, list):
                existing = {m.get('id') for m in result['materials'].get(kind, [])}
                result['materials'].setdefault(kind, []).extend(copy.deepcopy(m) for m in values if m.get('id') not in existing)
                # Nested parameters are authoritative, even when outer unused materials remain.
                replacement = {m.get('id'): m for m in values}
                result['materials'][kind] = [copy.deepcopy(replacement.get(m.get('id'), m))
                                             for m in result['materials'][kind]]
    for index, track in enumerate(result['tracks']):
        for segment in track['segments']:
            segment['render_index'] = index
    return result


def verify_masks(content, plan, user_data=None, physical_root=None, final_root=None):
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    segments = {(t['name'], s['target_timerange']['start']): s
                for t in content['tracks'] for s in t['segments']}
    count = 0
    for item in plan['clips']:
        name = item.get('mask', {}).get('name')
        if name not in ('矩形', '圆形'):
            continue
        segment = segments[item['track'], round(item['start'] * 1_000_000)]
        mask = next((materials[r] for r in segment['extra_material_refs']
                     if materials[r].get('type') == 'mask'), {})
        native_ids = {m['id'] for m in content['materials'].get('common_mask', [])}
        if (not segment.get('enable_video_mask') or not mask.get('constant_material_id') or
                mask.get('id') not in native_ids):
            raise ValueError('Native mask identity/enable mismatch')
        source = materials[segment['material_id']]
        if name == '矩形':
            if 'rendering' in item['mask']:
                import native_mask_feather
                expected_resource = native_mask_feather.expected_resource(mask.get('path',''), physical_root, final_root)
                config = mask.get('config', {})
                expected_keys = {'aspectRatio','centerX','centerY','feather','height','invert','rotation','roundCorner','width'}
                if (set(config) != expected_keys or type(config['invert']) is not bool or
                        any(type(value) not in (int,float) or not math.isfinite(value)
                            for key,value in config.items() if key != 'invert')):
                    raise ValueError('Derived mask config has missing/extra fields or invalid numeric/boolean types')
            else:
                expected_resource = cached_material('rectangle_mask', user_data)
            excluded = {'id', 'constant_material_id', 'config'}
            if {k:v for k,v in mask.items() if k not in excluded} != {k:v for k,v in expected_resource.items() if k not in excluded}:
                raise ValueError('Native rectangle mask resource/category/path mismatch')
        if name == '圆形':
            expected_material = circle_material(item, source, user_data)
            excluded = {'id', 'constant_material_id'}
            if {k:v for k,v in mask.items() if k not in excluded} != {k:v for k,v in expected_material.items() if k not in excluded}:
                raise ValueError('Native circle mask resource/category/parameter mismatch')
        expected = mask_frames(item, source['width'], source['height'])
        actual = [k for k in segment['common_keyframes'] if k['property_type'] in MASK_PROPERTIES.values()]
        for group in actual:
            if group['material_id'] != mask['constant_material_id']:
                raise ValueError('Mask keyframe material reference mismatch')
        def comparable(groups):
            return sorted((g['property_type'], [(p['time_offset'], p['values'], p['curveType'],
                                                 p.get('left_control'), p.get('right_control'),
                                                 p.get('graphID'), p.get('string_value'), p.get('graph'))
                                                for p in g['keyframe_list']]) for g in groups)
        if comparable(expected) != comparable(actual):
            raise ValueError('Mask keyframe parameter readback mismatch')
        count += bool(expected) and name == '矩形'
    return count


def verify_perspectives(content, plan):
    videos = {m['id']: m for m in content['materials'].get('videos', [])}
    count = 0
    for spec in plan.get('composites', []):
        if not spec.get('perspective'):
            continue
        combination = next((d for d in content['materials'].get('drafts', [])
                            if d.get('draft', {}).get('name') == spec['name']), None)
        if not combination:
            raise ValueError('Perspective composite reference missing')
        outer = next((s for t in content['tracks'] for s in t['segments']
                      if combination['id'] in s.get('extra_material_refs', [])), None)
        if not outer:
            raise ValueError('Perspective outer segment missing')
        expected_info = copy.deepcopy(CORNER_PIN_INFO)
        if 'static_up_right' in spec['perspective']:
            x, y = spec['perspective']['static_up_right']
            expected_info.update(upper_right_x=x, upper_right_y=y)
        pin = videos.get(outer['material_id'], {}).get('corner_pin') or {}
        if (pin.get('enable_corner_pin') is not True or not pin.get('id') or
                pin.get('sub_type') != 'video' or pin.get('corner_pin_info') != expected_info):
            raise ValueError('Corner pin identity/enable/geometry mismatch')
        expected = perspective_frames(spec)
        actual = [g for g in outer.get('common_keyframes', [])
                  if g.get('property_type') in CORNER_PIN_PROPERTIES.values()]
        def comparable(groups):
            return sorted((g['property_type'], [(p['time_offset'], p['values'], p['curveType'])
                                                for p in g['keyframe_list']]) for g in groups)
        if any(g.get('material_id') != '' for g in actual) or comparable(expected) != comparable(actual):
            raise ValueError('Corner pin keyframe parameter readback mismatch')
        if other_corner_requested(spec):
            corner_runtime()
            default_clip = {'alpha': 1.0, 'flip': {'horizontal': False, 'vertical': False}, 'rotation': 0.0,
                            'scale': {'x': 1.0, 'y': 1.0}, 'transform': {'x': 0.0, 'y': 0.0}}
            clip = outer.get('clip')
            if (content.get('fps') != 30 or (content['canvas_config']['width'], content['canvas_config']['height']) != (640, 360) or
                    clip != default_clip or outer['extra_material_refs'] != [combination['id']]):
                raise ValueError('Additional corner native scene/transform/refs changed')
            if (any(type(v) not in (int, float) for v in (clip['alpha'], clip['rotation'], *clip['scale'].values(), *clip['transform'].values())) or
                    any(v is not False for v in clip['flip'].values())):
                raise ValueError('Additional corner outer transform must use canonical scalar types')
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in pin['corner_pin_info'].values()):
                raise ValueError('Additional corner base geometry must use finite numeric coordinates')
            for group in actual:
                for point in group['keyframe_list']:
                    if (set(point) != {'id', 'curveType', 'graphID', 'left_control', 'right_control', 'string_value', 'time_offset', 'values'} or
                            type(point['time_offset']) is not int or len(point['values']) != 2 or
                            any(type(v) not in (int, float) or not math.isfinite(v) for v in point['values']) or
                            point['graphID'] != '' or point['string_value'] != '' or any(
                                set(point[k]) != {'x', 'y'} or any(type(v) not in (int, float) or v != 0 for v in point[k].values())
                                for k in ('left_control', 'right_control'))):
                        raise ValueError('Additional corner motion requires canonical linear finite points without graph or handles')
        count += 1
    return count


def animation_holders(content):
    """Yield every draft body that carries animation materials: the outer draft and each composite."""
    yield content
    for combination in content['materials'].get('drafts', []):
        yield combination['draft']


def animation_bundle(cache, identifier):
    """The single cached effect bundle folder for an animation id; None when it is absent or ambiguous.

    The app resolves an animation at Cache/effect/<id>/<hash> and draws nothing for an entry whose
    bundle it cannot find, so only an id with exactly one bundle folder counts as available.
    """
    if cache is None:
        return None
    folder = Path(cache) / str(identifier)
    bundles = sorted(entry for entry in folder.iterdir() if entry.is_dir()) if folder.is_dir() else []
    return bundles[0] if len(bundles) == 1 else None


def bind_animation_paths(content, user_data=None):
    """Point every animation entry at the effect bundle the app itself caches, like it does on save.

    An entry the local cache cannot resolve is dropped here — with its material and the segment
    reference that would otherwise dangle — because 剪映 draws no animation without a bundle: it
    marks the segment as lost and keeps hunting for the resource while the draft opens.
    """
    cache = Path(user_data) / 'Cache' / 'effect' if user_data else None
    bound, missing, ambiguous, unverified, dropped, dropped_materials = [], [], [], [], [], []
    for holder in animation_holders(content):
        materials = holder.get('materials', {}).get('material_animations')
        if not materials:
            continue
        kept, removed = [], set()
        for material in materials:
            entries = []
            for animation in material.get('animations', []):
                identifier = str(animation.get('id', ''))
                bundle = animation_bundle(cache, identifier)
                if bundle is not None:
                    animation['path'] = bundle.as_posix()
                    bound.append(identifier)
                    entries.append(animation)
                    continue
                folder = cache / identifier if cache is not None else None
                if folder is None:
                    unverified.append(identifier)
                elif not folder.is_dir() or not any(entry.is_dir() for entry in folder.iterdir()):
                    missing.append(identifier)
                else:
                    ambiguous.append(identifier)
                dropped.append({'id': identifier, 'name': animation.get('name', ''),
                                'type': animation.get('type', '')})
            if entries:
                material['animations'] = entries
                kept.append(material)
            else:
                removed.add(material['id'])
        materials[:] = kept
        if removed:
            dropped_materials.extend(sorted(removed))
            for track in holder.get('tracks', []):
                for segment in track.get('segments', []):
                    refs = segment.get('extra_material_refs')
                    if refs and removed.intersection(refs):
                        segment['extra_material_refs'] = [r for r in refs if r not in removed]
    return {'cache': str(cache) if cache is not None else '', 'bound': bound, 'missing': missing,
            'ambiguous': ambiguous, 'unverified': unverified, 'dropped': dropped,
            'dropped_materials': dropped_materials}


def verify_animation_paths(content):
    """Read back every animation path; each entry that ships must resolve to a cached bundle folder."""
    count = 0
    for holder in animation_holders(content):
        for material in holder.get('materials', {}).get('material_animations', []):
            for animation in material.get('animations', []):
                path = animation.get('path')
                if not path or not Path(path).is_dir():
                    raise ValueError(f'Animation bundle is unavailable: {path or animation.get("id")}')
                count += 1
    return count


def verify_sidecars(content, physical_root, final_root, read_json):
    for combination in content['materials'].get('drafts', []):
        nested = combination['draft']
        relative = Path('subdraft') / nested['id']
        final = Path(final_root) / relative
        for key, filename in (('draft_file_path', 'draft_content.json'),
                              ('draft_config_path', 'sub_draft_config.json'),
                              ('draft_cover_path', 'draft_cover.jpg')):
            if combination[key] != str(final / filename):
                raise ValueError('Composite path relocation mismatch')
        wrapper = read_json(Path(physical_root) / relative / 'draft_content.json')
        config = read_json(Path(physical_root) / relative / 'sub_draft_config.json')
        if wrapper['materials']['drafts'] != [combination] or config['id'] != nested['id'] or config['rough_cut_duration'] != nested['duration']:
            raise ValueError('Composite sidecar readback mismatch')
