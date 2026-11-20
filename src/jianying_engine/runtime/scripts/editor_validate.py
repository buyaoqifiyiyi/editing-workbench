"""Edit-plan validation, native catalog discovery, and animation cache helpers."""
import copy
from pathlib import Path
import re

import local_font
import native_advanced
import native_chroma
import native_hsl_channels
import native_mask_feather
import native_matting_cache
import native_mix_mode
import native_protocol
import native_text_alpha
import native_text_glow
import native_text_highlight


def validate_grading(item, info):
    import editor

    grade = item['grading']
    if isinstance(grade, dict) and 'primary_wheel' in grade:
        return
    if native_hsl_channels.requested(grade):
        native_hsl_channels.validate(item, info)
        return
    editor.fields(grade, {'red_hsl', 'blue_hsl', 'green_hsl', 'luma_curve', 'rgb_curves', 'local_circle'}, 'grading')
    if not grade or not ({'red_hsl', 'blue_hsl', 'green_hsl', 'luma_curve', 'rgb_curves'} & set(grade)):
        raise ValueError('Grading must specify measured HSL or three-anchor curves')
    for field in ('blue_hsl', 'green_hsl'):
        if field not in grade:
            continue
        spec = grade[field]
        allowed = {'saturation', 'hue', 'lightness'}
        editor.fields(spec, allowed, 'grading.' + field)
        if len(spec) != 1:
            raise ValueError('Blue/green HSL requires exactly one measured control')
        control = next(iter(spec))
        if field == 'green_hsl' and control == 'lightness':
            native_advanced.green_lightness_runtime()
        values = (-30, 0, 30) if control in ('hue', 'lightness') else (-60, 0, 60)
        editor.number(spec[control], 'grading.' + field + '.' + control, values[0], values[-1])
        paired = (set(grade) == {'blue_hsl', 'green_hsl'} and
                  grade['blue_hsl'] == {'saturation': -60} and grade['green_hsl'] == {'saturation': -60})
        measured_video = (info['kind'] == 'video' and field in ('blue_hsl', 'green_hsl') and
                          control == 'saturation' and set(grade) == {field} and
                          not info.get('has_audio') and info['fps'] == 30 and
                          item.get('source_in', 0) == 0.5 and item.get('speed', 1) == 1 and
                          item.get('volume', 0) == 0)
        if (spec[control] not in values or (set(grade) != {field} and not paired) or
                (info['kind'] != 'image' and not measured_video) or (info['width'], info['height']) != (640, 360) or
                item.get('fit', 'contain') != 'contain' or item.get('transform') or
                any(item.get(k) for k in ('keyframes', 'motion_easing', 'transition', 'effects', 'filters',
                                          'animations', 'mix_mode', 'chroma', 'background', 'mask', 'matting_cache'))):
            raise ValueError('Blue/green HSL admits 640x360 contain static images or bounded silent blue/green-saturation video only')
    if 'red_hsl' in grade:
        editor.fields(grade['red_hsl'], {'hue', 'saturation', 'lightness'}, 'grading.red_hsl')
        for key in ('hue', 'saturation', 'lightness'):
            editor.number(grade['red_hsl'].setdefault(key, 0), 'grading.red_hsl.' + key, -100, 100)
    curves = {'luma': grade['luma_curve']} if 'luma_curve' in grade else {}
    if 'rgb_curves' in grade:
        rgb = grade['rgb_curves']
        editor.fields(rgb, {'red', 'green', 'blue'}, 'grading.rgb_curves')
        if not rgb:
            raise ValueError('RGB curves must include at least one channel')
        curves.update(rgb)
    for channel, points in curves.items():
        if not isinstance(points, list) or len(points) != 3 or points[0] != [0, 0] or points[-1] != [1, 1]:
            raise ValueError(f'Measured {channel} curve requires three anchors with endpoints [0,0] and [1,1]')
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError('Curve anchors must be [x,y]')
            for value in point:
                editor.number(value, 'grading curve ' + channel, 0, 1)
        if not 0 < points[1][0] < 1:
            raise ValueError('Curve anchor x must strictly increase')
    if 'local_circle' in grade:
        if 'mask' in item:
            raise ValueError('Local grading mask cannot coexist with a video mask')
        mask = grade['local_circle']
        editor.fields(mask, {'width', 'height', 'feather'}, 'grading.local_circle', ('width', 'height'))
        editor.number(mask['width'], 'grading.local_circle.width', 0.000001, info['width'] * 4)
        editor.number(mask['height'], 'grading.local_circle.height', 0.000001, info['height'] * 4)
        editor.number(mask.setdefault('feather', 0), 'grading.local_circle.feather', 0, 100)
    # Do not deliver grading that references a missing cache resource.
    native_advanced.color_materials(grade, info, None)


def validate_visual(item, label):
    import editor

    editor.fields(item.setdefault('transform', {}), editor.TRANSFORMS, label + '.transform')
    for k, value in item['transform'].items():
        if k.startswith('flip_'):
            if type(value) is not bool:
                raise ValueError(f'{label}.transform.{k} must be a boolean')
        else:
            editor.number(
                value,
                label + '.transform.' + k,
                0.000001 if k.startswith('scale_') else 0 if k == 'alpha' else -10000,
                1 if k == 'alpha' else 10000,
            )
    if not isinstance(item.setdefault('keyframes', []), list):
        raise ValueError(f'{label}.keyframes must be an array')
    seen, source_seen, properties = set(), set(), set()
    for key in item['keyframes']:
        editor.fields(key, {'property', 'time', 'value'}, 'keyframe', ('property', 'time', 'value'))
        prop = key['property']
        if prop not in editor.KEYFRAMES:
            raise ValueError(f'Unsupported keyframe property: {prop}')
        editor.number(key['time'], 'keyframe.time', 0, item['duration'])
        editor.number(
            key['value'],
            'keyframe.value',
            0.000001 if 'scale' in prop else 0 if prop == 'alpha' else -10000,
            1 if prop == 'alpha' else 10000,
        )
        identity = (prop, editor.us(key['time']))
        if identity in seen:
            raise ValueError('Duplicate keyframe property/time')
        seen.add(identity)
        if 'source_in' in item:
            source_identity = (prop, editor.us(item['source_in'] + key['time'] * item['speed']))
            if source_identity in source_seen:
                raise ValueError('Video source-time keyframes must remain distinct after speed mapping')
            source_seen.add(source_identity)
        properties.add(prop)
    if 'uniform_scale' in properties and properties & {'scale_x', 'scale_y'}:
        raise ValueError('Cannot mix uniform_scale with scale_x/scale_y keyframes')


def native_enum(backend, kind, name, allow_vip=True):
    import editor

    enum = getattr(backend, editor.NATIVE_TYPES[kind])
    if not isinstance(name, str) or name not in enum.__members__:
        raise ValueError(f'Unknown {kind} name: {name}; run capabilities --kind {kind}')
    value = enum[name]
    if getattr(value.value, 'is_vip', False) and not allow_vip:
        raise ValueError(f'VIP resource {kind}/{name}: disabled by allow_vip_effects=false')
    return value


def validate_native(plan, mask_resource_root=None):
    """Validate native parameters only. Never render or substitute external effects."""
    import editor

    used, backend = set(), None
    allow_vip = plan.setdefault('allow_vip_effects', True)
    if type(allow_vip) is not bool:
        raise ValueError('allow_vip_effects must be a boolean')

    def enum(kind, name):
        nonlocal backend
        backend = backend or editor.load_backend()
        used.add(kind)
        return editor.native_enum(backend, kind, name, allow_vip)

    def effect(item):
        editor.fields(item, {'kind', 'name', 'params', 'keyframes'}, 'effect', ('name',))
        if item.setdefault('kind', 'scene') not in ('scene', 'character'):
            raise ValueError('effect.kind must be scene or character')
        member = enum(item['kind'] + '_effects', item['name'])
        params = item.setdefault('params', {})
        allowed = {p.name for p in member.value.params}
        editor.fields(params, allowed, 'effect.params')
        for name, value in params.items():
            if value is not None:
                editor.number(value, 'effect.params.' + name, 0, 100)

    def filter_(item):
        editor.fields(item, {'name', 'intensity'}, 'filter', ('name',))
        enum('filters', item['name'])
        editor.number(item.setdefault('intensity', 100), 'filter.intensity', 0, 100)

    for group in ('clips', 'captions'):
        for item in plan[group]:
            animations = item.setdefault('animations', [])
            if not isinstance(animations, list):
                raise ValueError('animations must be an array')
            stages, durations = set(), {}
            for animation in animations:
                editor.fields(animation, {'stage', 'name', 'duration'}, 'animation', ('stage', 'name'))
                stage = animation['stage']
                if stage not in ('intro', 'outro', 'group' if group == 'clips' else 'loop') or stage in stages:
                    raise ValueError('Invalid or duplicate animation stage')
                member = enum(('video_' if group == 'clips' else 'text_') + stage, animation['name'])
                if stage == 'loop':
                    if 'duration' in animation:
                        raise ValueError('Text loop duration is computed between intro and outro; omit duration')
                else:
                    value = item['duration'] if stage == 'group' else member.value.duration / 1_000_000
                    editor.number(
                        animation.setdefault('duration', min(value, item['duration'])),
                        'animation.duration',
                        0.000001,
                        item['duration'],
                    )
                    durations[stage] = editor.us(animation['duration'])
                stages.add(stage)
            if 'group' in stages and len(stages) > 1:
                raise ValueError('Video group animation cannot coexist with intro/outro')
            occupied = durations.get('intro', 0) + durations.get('outro', 0)
            if occupied > editor.us(item['duration']) or ('loop' in stages and occupied >= editor.us(item['duration'])):
                raise ValueError('Intro/outro overlap or leave no duration for loop')
            if group == 'captions':
                continue
            for key, check in (('effects', effect), ('filters', filter_)):
                values = item.setdefault(key, [])
                if not isinstance(values, list):
                    raise ValueError(key + ' must be an array')
                seen = set()
                for value in values:
                    check(value)
                    if 'keyframes' in value:
                        raise ValueError('Effect keyframes currently require a standalone blur track')
                    identity = (value.get('kind'), value['name'])
                    if identity in seen:
                        raise ValueError('Duplicate clip effect/filter')
                    seen.add(identity)
            if 'mix_mode' in item:
                native_mix_mode.validate_spec(item, plan)
                native_mix_mode.resource(item['mix_mode']['name'])
                used.add('mix_mode')
            if 'background' in item:
                editor.fields(item['background'], {'type', 'blur', 'color'}, 'background', ('type',))
                background = item['background']
                if item['track'] != 'video' or background['type'] not in ('blur', 'color'):
                    raise ValueError('Background filling requires main video and type blur/color')
                if background['type'] == 'blur':
                    if 'color' in background:
                        raise ValueError('Blur background does not accept color')
                    editor.number(background.setdefault('blur', 0.0625), 'background.blur', 0, 1)
                else:
                    if 'blur' in background:
                        raise ValueError('Color background does not accept blur')
                    if not isinstance(background.get('color'), str) or not re.fullmatch(r'#[0-9a-fA-F]{8}', background['color']):
                        raise ValueError('Background color must be #RRGGBBAA')
                used.add('background')
            if 'chroma' in item:
                native_chroma.validate_spec(item, plan)
                native_chroma.resource()
                used.add('chroma')
            if 'mask' in item:
                mask = item['mask']
                editor.fields(
                    mask,
                    {'name', 'experimental', 'center_x', 'center_y', 'size', 'rotation',
                     'feather', 'invert', 'rect_width', 'round_corner', 'keyframes', 'rendering'},
                    'mask',
                    ('name', 'experimental'),
                )
                if mask['experimental'] is not True:
                    raise ValueError('Native mask is experimental on newer Jianying; experimental must be true')
                enum('masks', mask['name'])
                if mask['name'] not in ('矩形', '圆形'):
                    raise ValueError('Only measured rectangle and zero-feather circle masks are admitted; other shapes remain research-only')
                for name, low, high, default in (('center_x', -1e6, 1e6, 0), ('center_y', -1e6, 1e6, 0),
                                                 ('size', 0.000001, 4, 0.5), ('rotation', -36000, 36000, 0), ('feather', 0, 100, 0)):
                    editor.number(mask.setdefault(name, default), 'mask.' + name, low, high)
                if type(mask.setdefault('invert', False)) is not bool:
                    raise ValueError('mask.invert must be boolean')
                if mask['name'] == '圆形' and mask['feather'] != 0:
                    raise ValueError('Measured circle mask requires feather=0; nonzero feather rendering is not admitted')
                derived = 'rendering' in mask
                if derived and (mask['rendering'] != native_mask_feather.MODE or mask['name'] != '矩形'):
                    raise ValueError('mask.rendering requires red_coverage_v1 rectangle')
                if mask['feather'] != 0 and not derived:
                    raise ValueError('mask.feather must be 0; nonzero feather rendering is not admitted')
                for name in ('rect_width', 'round_corner'):
                    if name in mask:
                        if mask['name'] != '矩形':
                            raise ValueError('rect_width/round_corner require a rectangle mask')
                        editor.number(
                            mask[name],
                            'mask.' + name,
                            0.000001 if name == 'rect_width' else 0,
                            4 if name == 'rect_width' else 100,
                        )
                if 'keyframes' in mask:
                    if mask['name'] not in ('矩形', '圆形') or not isinstance(mask['keyframes'], list) or not mask['keyframes']:
                        raise ValueError('Dynamic mask keyframes require a measured rectangle/circle and a nonempty array')
                    last = {}
                    source_last = {}
                    for key in mask['keyframes']:
                        editor.fields(key, {'property', 'time', 'value'}, 'mask.keyframe', ('property', 'time', 'value'))
                        prop = key['property']
                        if prop not in native_advanced.MASK_PROPERTIES:
                            raise ValueError('Unsupported mask keyframe property')
                        if mask['name'] == '圆形' and prop == 'feather':
                            raise ValueError('Circle feather keyframes are not admitted')
                        if prop == 'feather' and key['value'] != 0 and not derived:
                            raise ValueError('mask.feather keyframes must remain 0; nonzero feather rendering is not admitted')
                        editor.number(key['time'], 'mask.keyframe.time', 0, item['duration'] - 0.000001)
                        low, high = ((0, 100) if prop == 'feather' else (0.000001, 1e6) if prop in ('width', 'height') else
                                     (-36000, 36000) if prop == 'rotation' else (-1e6, 1e6))
                        editor.number(key['value'], 'mask.keyframe.value', low, high)
                        timestamp = editor.us(key['time'])
                        if prop not in last and timestamp != 0:
                            raise ValueError('Each mask keyframe property must start at time 0')
                        if prop in last and timestamp <= last[prop]:
                            raise ValueError('Mask keyframes must be strictly increasing per property')
                        source_timestamp = native_advanced.source_keyframe_time(item, key['time'])
                        if prop in source_last and source_timestamp <= source_last[prop]:
                            raise ValueError('Mask source-time keyframes must remain distinct after speed mapping')
                        last[prop] = timestamp
                        source_last[prop] = source_timestamp
                if mask['name'] == '圆形':
                    native_advanced.cached_material('common_mask', None)
                elif mask['name'] == '矩形':
                    if derived:
                        native_mask_feather.validate_spec(item, plan)
                        native_mask_feather.check_runtime()
                        if mask_resource_root is None:
                            native_mask_feather.source_path()
                        else:
                            native_mask_feather.verify_directory(mask_resource_root)
                    else:
                        native_advanced.cached_material('rectangle_mask', None)
    for key, check in (('effects', effect), ('filters', filter_)):
        for item in plan[key]:
            selection = {k: v for k, v in item.items() if k not in ('start', 'duration', 'track')}
            check(selection)
            item.update(selection)
    return sorted(used)


def effect_params(backend, item):
    import editor

    member = editor.native_enum(backend, item['kind'] + '_effects', item['name'], True)
    return member, [item['params'].get(p.name) for p in member.value.params]


def apply_animations(segment, item, backend, text=False):
    import editor

    animations = sorted(item['animations'], key=lambda a: {'intro': 0, 'outro': 1, 'group': 2, 'loop': 3}[a['stage']])
    for animation in animations:
        kind = ('text_' if text else 'video_') + animation['stage']
        member = editor.native_enum(backend, kind, animation['name'], True)
        segment.add_animation(member, editor.us(animation['duration']) if 'duration' in animation else None)
        if text and animation['stage'] == 'loop':
            # 0.3.0 starts loops at intro.start, not intro.end. Correct native timing, not pixels.
            start = sum(editor.us(a['duration']) for a in animations if a['stage'] == 'intro')
            outro = sum(editor.us(a['duration']) for a in animations if a['stage'] == 'outro')
            loop = segment.animations_instance.animations[-1]
            loop.start, loop.duration = start, editor.us(item['duration']) - start - outro


def planned_animation_id(backend, group, animation):
    import editor

    kind = ('video_' if group == 'clips' else 'text_') + animation['stage']
    return str(editor.native_enum(backend, kind, animation['name'], True).value.effect_id)


def cached_animation_id(backend, kind, name):
    """The effect id whose cached bundle decides whether this animation can ship at all."""
    import editor

    return str(editor.native_enum(backend, kind, name, True).value.effect_id)


def animation_availability(backend, user_data, kinds=None):
    """Split every animation enum name by whether the local effect cache resolves its bundle now.

    Measured 2026-10-17 on 11.6: of 737 animation names only 20 carry a package here, because the app
    downloads packages lazily, per use. Only the available names can be recommended without a retry.

    A machine without a Cache/effect directory at all (a fresh installation that never downloaded an
    effect) has no per-name verdict to give; every name comes back missing and effect_cache_dir says why.
    """
    import editor

    if kinds is None:
        kinds = editor.ANIMATION_KINDS
    cache = editor.effect_cache_dir(user_data) or Path(user_data) / 'Cache' / 'effect'
    available, missing = {}, {}
    for kind in kinds:
        enum = getattr(backend, editor.NATIVE_TYPES[kind])
        here = [name for name in enum.__members__
                if native_advanced.animation_bundle(cache, str(enum.__members__[name].value.effect_id)) is not None]
        available[kind] = here
        missing[kind] = [name for name in enum.__members__ if name not in here]
    return available, missing


def animation_cache_status(user_data):
    """'local_cache_absent' when this machine has never created the effect cache, else 'local_cache_checked'."""
    import editor

    return 'local_cache_checked' if editor.effect_cache_dir(user_data) else 'local_cache_absent'


def animation_preflight(plan, backend, user_data):
    """Report every planned animation the local cache cannot resolve before the build drops it.

    Only a cache-resolved entry survives building, and an unresolved one used to disappear silently;
    this names it at validate time together with the names that do resolve locally. The status is
    'local_cache_absent' when the cache itself is missing, so a first run is not read as 737 verdicts.
    """
    import editor

    available, _ = editor.animation_availability(backend, user_data)
    unresolvable = []
    for group in ('clips', 'captions'):
        for item in plan.get(group, []):
            for animation in item.get('animations', []):
                kind = ('video_' if group == 'clips' else 'text_') + animation['stage']
                name = animation['name']
                if name not in available[kind]:
                    unresolvable.append(f"{group}/{animation['stage']}/{name}(id {editor.cached_animation_id(backend, kind, name)})")
    return available, unresolvable, editor.animation_cache_status(user_data)


def drop_unavailable_animations(plan, backend, user_data=None):
    """Keep only the planned animations whose effect bundle the local effect cache resolves.

    剪映 resolves an animation at Cache/effect/<id>/<hash>. An entry without a bundle is not drawn at
    all: the app marks the segment as lost and keeps hunting for the resource while the draft opens
    (measured 2026-10-16 on 11.6: `path` empty in the app's own save, bundle never downloaded). Such
    an animation therefore never enters the draft; dropping it here also keeps the derived text loop
    timing correct, because apply_animations reads the surviving intros and outros.
    """
    import editor

    user_data = user_data if user_data is not None else editor.configured_user_data()
    cache = editor.effect_cache_dir(user_data)
    if cache is None:
        # No cache yet no longer means "all 737 names are unavailable": naming it that way made a
        # first-run machine look like a machine that rejected every request.
        if user_data:
            print('Animation packages were not checked: this user data has no Cache/effect directory yet.', file=editor.sys.stderr)
        return []
    dropped = []
    for group in ('clips', 'captions'):
        for item in plan.get(group, []):
            requested = item.get('animations', [])
            kept = []
            for animation in requested:
                identifier = editor.planned_animation_id(backend, group, animation)
                if native_advanced.animation_bundle(cache, identifier) is None:
                    dropped.append({'group': group, 'stage': animation['stage'],
                                    'name': animation['name'], 'id': identifier})
                else:
                    kept.append(animation)
            if len(kept) != len(requested):
                item['animations'] = kept
    return dropped


def apply_native(segment, item, backend):
    import editor

    editor.apply_animations(segment, item, backend)
    for effect in item['effects']:
        member, params = editor.effect_params(backend, effect)
        segment.add_effect(member, params)
    for filter_ in item['filters']:
        segment.add_filter(editor.native_enum(backend, 'filters', filter_['name'], True), filter_['intensity'])
    if 'mix_mode' in item:
        segment.set_mix_mode(editor.native_enum(backend, 'mix_modes', item['mix_mode']['name'], True))
    if 'chroma' in item:
        segment.add_chroma(**{k: v for k, v in item['chroma'].items() if k != 'rendering'})
    if 'background' in item:
        background = dict(item['background'])
        segment.add_background_filling(background.pop('type'), **background)
    if 'mask' in item:
        mask = dict(item['mask'])
        mask.pop('experimental')
        mask.pop('keyframes', None)
        mask.pop('rendering', None)
        segment.add_mask(editor.native_enum(backend, 'masks', mask.pop('name'), True), **mask)


def native_catalog(args):
    import editor

    if args.kind == 'text_glows':
        admitted = editor.read_json(editor.SKILL_ROOT / 'assets/native-text-glow-11.5.json')
        matches = [name for name, spec in admitted.items() if args.search in name and not (args.free_only and spec['is_vip'])]
        result = {'text_glows': matches[:args.limit], 'total_matches': len(matches),
                  'truncated': len(matches) > args.limit, 'resource_availability': 'identity_checked_on_use'}
        if args.details:
            result['details'] = [
                {'name': name, 'label': admitted[name]['label'], 'is_vip': admitted[name]['is_vip'],
                 'parameters': {'color': '#RRGGBB', 'strength': [.01, 1], 'range': [.01, 1]},
                 'scope': 'static single-line admitted-font white text only'}
                for name in matches[:args.limit]
            ]
        return result
    backend = editor.load_backend()
    values = list(getattr(backend, editor.NATIVE_TYPES[args.kind]))
    if args.kind == 'keyframes':
        values = [v for v in values if v.name in editor.KEYFRAMES]
    if args.kind == 'fonts':
        admitted = editor.read_json(editor.SKILL_ROOT / 'assets/native-fonts-11.5.json')
        values = [v for v in values if v.name in admitted]
    matches = [v for v in values if args.search in v.name and
               (not args.free_only or not getattr(v.value, 'is_vip', False))]
    selected = matches[:args.limit]
    result = {args.kind: [v.name for v in selected], 'total_matches': len(matches),
              'truncated': len(matches) > len(selected), 'resource_availability': 'unverified'}
    if args.kind in editor.ANIMATION_KINDS and args.user_data:
        available, missing = editor.animation_availability(backend, args.user_data, (args.kind,))
        result['available_here'] = available[args.kind]
        result['missing_here_count'] = len(missing[args.kind])
        result['resource_availability'] = editor.animation_cache_status(args.user_data)
    if args.details:
        result['details'] = [
            {'name': v.name, 'label': getattr(v.value, 'name', getattr(v.value, 'title', v.name)),
             'is_vip': getattr(v.value, 'is_vip', None),
             'parameters': [{'name': p.name, 'input_min': 0, 'input_max': 100,
                             'native_min': p.min_value, 'native_max': p.max_value,
                             'native_default': p.default_value} for p in getattr(v.value, 'params', [])],
             'default_duration_seconds': getattr(v.value, 'duration', 0) / 1_000_000 or None,
             'experimental': args.kind == 'masks'}
            for v in selected
        ]
    return result


def _validate_plan_header(plan):
    import editor

    editor.fields(plan, {'schema_version', 'name', 'mode', 'canvas', 'clips', 'audio', 'captions', 'notes', 'missing_assets',
                         'effects', 'filters', 'allow_vip_effects', 'jianying_tasks', 'layer_order', 'composites'},
                  'plan', ('schema_version', 'name', 'mode', 'canvas', 'clips'))
    if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    name = plan['name']
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith(('.', ' ')):
        raise ValueError('Invalid Windows project name')
    if name in ('.', '..') or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', name):
        raise ValueError('Reserved Windows project name')
    if plan['mode'] not in ('script', 'reference', 'hybrid'):
        raise ValueError('mode must be script, reference or hybrid')
    editor.fields(plan['canvas'], {'width', 'height', 'fps'}, 'canvas', ('width', 'height', 'fps'))
    for k in ('width', 'height', 'fps'):
        value = plan['canvas'][k]
        if type(value) is not int:
            raise ValueError(f'canvas.{k} must be an integer')
        editor.number(value, f'canvas.{k}', 1 if k == 'fps' else 2, 120 if k == 'fps' else 16384)
        if k != 'fps' and value % 2:
            raise ValueError(f'canvas.{k} must be even')
    for key in ('clips', 'audio', 'captions', 'notes', 'missing_assets', 'effects', 'filters', 'jianying_tasks', 'composites'):
        value = plan.setdefault(key, [])
        if not isinstance(value, list):
            raise ValueError(f'{key} must be an array')
    if not plan['clips']:
        raise ValueError('At least one main video clip is required')
    for key in ('notes', 'missing_assets'):
        if not all(isinstance(x, str) for x in plan[key]):
            raise ValueError(f'{key} entries must be strings')


def _validate_caption_item(item, label, plan, plan_path):
    import editor

    if not isinstance(item['text'], str) or not item['text'].strip() or '\x00' in item['text']:
        raise ValueError(f'{label}.text must be non-empty text')
    if 'font' in item and 'font_path' in item:
        raise ValueError(f'{label}: font and font_path are mutually exclusive')
    if 'font_path' in item:
        if not isinstance(item['font_path'], str) or not item['font_path'] or '://' in item['font_path'] or '\x00' in item['font_path']:
            raise ValueError(f'{label}.font_path must be a local TTF or OTF path')
        if plan.get('composites'):
            raise ValueError('Local font text inside native composites is not verified')
        source = (plan_path.parent / item['font_path']).resolve()
        item['font_path'] = str(source)
        local_font.inspect(source)
        if (item.get('animations') or item.get('transform') or item.get('keyframes')
                or 'motion_easing' in item or 'text_curve' in item
                or 'glow' in item or 'highlights' in item
                or 'alpha' in item.get('style', {}) or item.get('style', {}).get('vertical')):
            raise ValueError('Local font initially supports static horizontal text only; '
                             'animations, motion, highlights, alpha, text effects and vertical layout are unverified')
    if 'font' in item:
        editor.native_font(item['font'], plan.get('allow_vip_effects', True))
    if 'font' in item or 'font_path' in item:
        if any(ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF for c in item['text']):
            raise ValueError('Local and admitted fonts currently require BMP text without surrogate characters')
    editor.number(item.setdefault('size', 8), label + '.size', 1, 100)
    for axis in ('x', 'y'):
        editor.number(item.setdefault(axis, -0.8 if axis == 'y' else 0), label + '.' + axis, -10, 10)
    if not isinstance(item.setdefault('color', '#FFFFFF'), str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', item['color']):
        raise ValueError(f'{label}.color must be #RRGGBB')
    for flag in ('bold', 'border'):
        if type(item.setdefault(flag, flag == 'border')) is not bool:
            raise ValueError(f'{label}.{flag} must be a boolean')
    editor.validate_visual(item, label)
    if 'highlights' in item:
        native_text_highlight.validate(item, plan)
    if 'glow' in item:
        glow = item['glow']
        editor.fields(glow, {'name', 'color', 'strength', 'range'}, label + '.glow', ('name', 'color', 'strength', 'range'))
        if not isinstance(glow['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', glow['color']):
            raise ValueError('Text glow color must be #RRGGBB')
        editor.number(glow['strength'], label + '.glow.strength', .01, 1)
        editor.number(glow['range'], label + '.glow.range', .01, 1)
        style = item.get('style', {})
        editor.fields(style, {'auto_wrapping'}, label + '.style', ('auto_wrapping',))
        if ('font' not in item or item['border'] or item['bold'] or item['color'] != '#FFFFFF' or item.get('animations') or
                item.get('transform') or 'text_curve' in item or
                style.get('auto_wrapping') is not False or set(style) - {'auto_wrapping'} or
                any(ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF for c in item['text']) or '\n' in item['text'] or '\r' in item['text']):
            raise ValueError('Text glow requires an admitted font and single-line white BMP text, border/bold off, style.auto_wrapping=false only')
        keys = sorted(item['keyframes'], key=lambda k: k['time'])
        if keys and (len(keys) != 2 or any(k['property'] != 'position_x' or not -.35 <= k['value'] <= .35 for k in keys) or
                     keys[0]['time'] != 0 or editor.us(keys[-1]['time']) >= editor.us(item['duration']) or item['x'] != 0):
            raise ValueError('Text glow motion requires two position_x keys within +/-0.35, first at 0 and last before text end, x=0; linear or admitted smoothstep only')
        native_text_glow.material(glow, None, plan.get('allow_vip_effects', True))
    if 'font' in item and (item.get('animations') or item.get('transform') or
            any(k['property'] != 'position_x' for k in item['keyframes'])):
        raise ValueError('Admitted font permits static layout and position_x keys; other motion combinations are unverified')
    if 'motion_easing' in item:
        keys = item['keyframes']
        if (item['motion_easing'] != 'smoothstep' or len(keys) != 2 or
                any(k['property'] != 'position_x' for k in keys) or
                keys[0]['time'] != 0 or keys[-1]['time'] >= item['duration'] or
                item.get('animations') or item.get('transform') or 'text_curve' in item or
                item.get('style', {}).get('vertical')):
            raise ValueError('Measured text motion_easing requires smoothstep with two position_x keys, first at 0 and last before text end; no animations, transform, text_curve or vertical layout')
    if 'text_curve' in item:
        curve = item['text_curve']
        editor.fields(curve, {'angle', 'experimental'}, label + '.text_curve', ('angle',))
        if 'experimental' in curve and type(curve['experimental']) is not bool:
            raise ValueError(f'{label}.text_curve.experimental must be a boolean if supplied')
        editor.number(curve['angle'], label + '.text_curve.angle', 0, 360)
    if any(k['property'] == 'alpha' for k in item['keyframes']):
        native_text_alpha.validate(item, plan)
    if {'transform_x', 'transform_y'} & set(item['transform']):
        raise ValueError('Text position uses x/y or position keyframes, not transform_x/transform_y')
    style = item.setdefault('style', {})
    editor.fields(style, {'alpha', 'italic', 'vertical', 'letter_spacing', 'line_spacing', 'auto_wrapping',
                          'border_color', 'border_width', 'shadow'}, label + '.style')
    for flag in ('italic', 'vertical', 'auto_wrapping'):
        if flag in style and type(style[flag]) is not bool:
            raise ValueError(f'{label}.style.{flag} must be a boolean')
    for k in ('letter_spacing', 'line_spacing'):
        if k in style:
            if type(style[k]) is not int:
                raise ValueError(f'{label}.style.{k} must be an integer')
            editor.number(style[k], label + '.style.' + k, -100, 100)
    for k, high in (('alpha', 1), ('border_width', 100)):
        if k in style:
            editor.number(style[k], label + '.style.' + k, 0, high)
    if 'border_color' in style and (not isinstance(style['border_color'], str) or
            not re.fullmatch(r'#[0-9a-fA-F]{6}', style['border_color'])):
        raise ValueError('Text border color must be #RRGGBB')
    if 'shadow' in style:
        shadow = style['shadow']
        editor.fields(shadow, {'color', 'alpha', 'diffuse', 'distance', 'angle'}, 'text.shadow')
        if 'color' in shadow and (not isinstance(shadow['color'], str) or
                not re.fullmatch(r'#[0-9a-fA-F]{6}', shadow['color'])):
            raise ValueError('Text shadow color must be #RRGGBB')
        for k, low, high in (('alpha', 0, 1), ('diffuse', 0, 100), ('distance', 0, 100), ('angle', -180, 180)):
            if k in shadow:
                editor.number(shadow[k], 'text.shadow.' + k, low, high)


def _validate_speed_curve(item, label, info, plan):
    import editor

    points = item['speed_curve']
    if not isinstance(points, list) or len(points) not in (2, 3):
        raise ValueError('speed_curve requires two or three source-normalized points')
    for point in points:
        editor.fields(point, {'x', 'y'}, label + '.speed_curve point', ('x', 'y'))
        editor.number(point['x'], 'speed_curve.x', 0, 1)
        editor.number(point['y'], 'speed_curve.y', 0.5, 2)
    if points[0]['x'] != 0 or points[-1]['x'] != 1 or any(a['x'] >= b['x'] for a, b in zip(points, points[1:])):
        raise ValueError('speed_curve x must strictly increase from 0 to 1')
    average = native_advanced.average_curve_speed(points)
    if 'speed' in item:
        editor.number(item['speed'], label + '.speed', 0.5, 2)
        if abs(item['speed'] - average) > 1e-9:
            raise ValueError('speed must equal the speed_curve derived average; normally omit speed')
    item['speed'] = average
    excluded = ('keyframes', 'motion_easing', 'transition', 'effects', 'filters', 'animations',
                'chroma', 'background', 'grading', 'mix_mode')
    if (info['kind'] != 'video' or info.get('has_audio') or item.get('volume', 0) != 0 or
            any(item.get(k) for k in excluded)):
        raise ValueError('Measured speed_curve requires video without an audio stream, volume=0 and no clip effects, keys or transitions')
    if 'mask' in item:
        mask = item['mask']
        if (not isinstance(mask, dict) or mask.get('name') != '矩形' or
                any(mask.get(k, 0) != 0 for k in ('feather', 'rotation', 'round_corner')) or
                mask.get('invert', False) is not False):
            raise ValueError('Measured speed_curve mask requires a rectangle with feather/rotation/round_corner=0, invert=false')
        if 'keyframes' in mask:
            keys = mask['keyframes']
            axis = keys[0].get('property') if isinstance(keys, list) and keys else None
            dimensions = (info['width'], info['height'])
            window_scale = 2 if dimensions == (1280, 720) else 1
            limits = {'center_x': (-24 * window_scale, 24 * window_scale),
                      'width': (520 * window_scale, 600 * window_scale)}.get(axis)
            if (not isinstance(keys, list) or len(keys) != 2 or limits is None or
                    any(k.get('property') != axis for k in keys) or keys[0].get('time') != 0 or
                    any(not isinstance(k.get('value'), (int, float)) or not limits[0] <= k['value'] <= limits[1] for k in keys) or
                    dimensions not in ((640, 360), (1280, 720)) or abs(info['fps'] - 60) > .001 or
                    plan['canvas'] != {'width': info['width'], 'height': info['height'], 'fps': 60} or
                    item.get('fit', 'contain') != 'contain' or item.get('transform') or
                    mask.get('size', .5) != .9 or mask.get('center_y', 0) != 0 or
                    axis == 'center_x' and (mask.get('rect_width', mask.get('size', .5)) != .9 or mask.get('center_x', 0) != keys[0]['value']) or
                    axis == 'width' and (mask.get('center_x', 0) != 0 or mask.get('rect_width', mask.get('size', .5)) != keys[0]['value'] / info['width'])):
                raise ValueError('Curve dynamic rectangle admits matching source/canvas 640x360 or 1280x720 at 60fps, contain with no transform, two center_x +/-24 or width 520..600 points at 640; doubled source-pixel limits at 1280, height 0.9, centered Y, initial static geometry matching the first key')


def _validate_audio_item(item, label, info, plan_path):
    import editor

    if 'denoise' in item:
        spec = item['denoise']
        editor.fields(spec, {'model_path'}, label + '.denoise', ('model_path',))
        if not isinstance(spec['model_path'], str) or not spec['model_path'] or '://' in spec['model_path']:
            raise ValueError('denoise.model_path must be the measured local native model')
        spec['model_path'] = str((plan_path.parent / spec['model_path']).resolve())
        if info.get('sample_rate') != 44100 or info.get('channels') != 2 or item['speed'] != 1:
            raise ValueError('Measured realtime denoise currently requires 44100 Hz stereo audio at speed=1')
        native_advanced.denoise_material(spec)
    for k in ('fade_in', 'fade_out'):
        editor.number(item.setdefault(k, 0), label + '.' + k)
    if item['fade_in'] + item['fade_out'] > item['duration']:
        raise ValueError(f'{label}: fades exceed clip duration')
    if 'loudness' in item:
        editor.fields(item['loudness'], {'target_lufs'}, label + '.loudness', ('target_lufs',))
        if item['loudness']['target_lufs'] != -23 or isinstance(item['loudness']['target_lufs'], bool):
            raise ValueError('Only the measured native -23 LUFS target is admitted')
        if (info.get('sample_rate') != 44100 or info.get('channels') != 2 or item['speed'] != 1 or
                item['volume'] != 1 or item['fade_in'] or item['fade_out'] or
                item.get('keyframes') or 'denoise' in item):
            raise ValueError('Measured loudness requires 44100 Hz stereo, speed=1, gain=1 and no fades, keys or denoise')
        if item['duration'] < 3:
            raise ValueError('Measured native loudness profile requires at least 3 seconds')
        editor.measure_loudness(item)
    if 'keyframes' in item:
        editor.validate_parameter_keys(item['keyframes'], item['duration'], label + '.keyframes', 0, 10,
                                       item['source_in'], item['speed'])


def _validate_clip_item(item, label, info, plan, plan_path):
    import editor

    if item.setdefault('fit', 'contain') not in ('contain', 'cover'):
        raise ValueError(f'{label}.fit must be contain or cover')
    editor.validate_visual(item, label)
    if 'matting_cache' in item:
        spec = item['matting_cache']
        editor.fields(spec, {'manifest', 'sha256'}, label + '.matting_cache', ('manifest', 'sha256'))
        if not isinstance(spec['manifest'], str) or not spec['manifest'] or '://' in spec['manifest'] or not isinstance(spec['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', spec['sha256']):
            raise ValueError('Matting cache requires a local manifest and SHA256')
        spec['manifest'] = str((plan_path.parent / spec['manifest']).resolve())
        if (info['kind'] != 'video' or item['speed'] != 1 or item['volume'] != 0 or item['fit'] != 'contain' or
                any(item.get(k) for k in ('speed_curve', 'transition', 'effects', 'filters', 'animations', 'mix_mode', 'chroma', 'background', 'mask', 'grading')) or
                set(item['transform']) - {'scale_x', 'scale_y', 'transform_x', 'transform_y'} or
                item['transform'].get('scale_x', 1) != 1 or item['transform'].get('scale_y', 1) != 1 or item['transform'].get('transform_y', 0) != 0 or
                not -.32 <= item['transform'].get('transform_x', 0) <= .32):
            raise ValueError('Existing matting admits muted speed=1 contain video with scale=1, y=0, x within +/-0.32 only')
        keys = sorted(item['keyframes'], key=lambda k: k['time'])
        axis = keys[0]['property'] if keys else None
        if keys and (len(keys) != 2 or axis not in ('position_x', 'position_y') or
                     any(k['property'] != axis or not ((-.32 <= k['value'] <= .32) if axis == 'position_x' else (0 <= k['value'] <= .24)) for k in keys) or
                     keys[0]['time'] != 0 or editor.us(keys[-1]['time']) >= editor.us(item['duration']) or
                     ('transform_x' if axis == 'position_x' else 'transform_y') in item['transform'] or
                     (axis == 'position_y' and item['transform'].get('transform_x', 0) != 0)):
            raise ValueError('Existing matting motion requires two single-axis keys: position_x +/-0.32 at y=0, or position_y 0..0.24 at x=0; first 0, last before clip end, omit active-axis static transform; linear or smoothstep only')
        native_matting_cache.binding(item, plan['canvas'], info)
    if 'motion_easing' in item:
        keys = sorted(item['keyframes'], key=lambda k: k['time'])
        properties = {k['property'] for k in keys}
        allowed_axes = {'position_x', 'position_y'} if info['kind'] == 'video' else {'position_x'}
        if (item['motion_easing'] != 'smoothstep' or info['kind'] not in ('image', 'video') or
                len(keys) != 2 or len(properties) != 1 or not properties <= allowed_axes or
                keys[0]['time'] != 0 or editor.us(keys[1]['time']) >= editor.us(item['duration'])):
            raise ValueError('Measured motion_easing requires two smoothstep points on one axis: image position_x, video position_x/position_y; first at 0 and last before clip end')
        if info['kind'] == 'video' and (item['speed'] != 1 or any(item.get(k) for k in
                ('speed_curve', 'transition', 'effects', 'filters', 'animations', 'mix_mode',
                 'chroma', 'background', 'mask', 'grading'))):
            raise ValueError('Measured video motion_easing requires speed=1 and no clip effects, masks, grading or transitions')
    if 'grading' in item:
        if isinstance(item['grading'], dict) and ({'blue_hsl', 'green_hsl'} | native_hsl_channels.CHANNELS.keys()) & item['grading'].keys() and (
                len(plan['clips']) != 1 or
                plan['canvas'] != {'width': 640, 'height': 360, 'fps': 30} or
                item['start'] != 0 or item['duration'] != 2 or
                any(plan.get(k) for k in ('audio', 'captions', 'effects', 'filters', 'composites'))):
            raise ValueError('Measured HSL requires one standalone two-second clip on a matching 640x360/30 canvas, with no other tracks; input types and controls must match the admitted channel subset')
        editor.validate_grading(item, info)
    if 'transition' in item:
        editor.fields(item['transition'], {'name', 'duration'}, 'transition', ('name', 'duration'))
        if not isinstance(item['transition']['name'], str):
            raise ValueError('Transition name must be a string')
        editor.number(item['transition']['duration'], 'transition.duration', 0.000001)


def _validate_effect_filter_tracks(plan, tracks, track_types):
    import editor

    for group, track_type in (('effects', 'effect'), ('filters', 'filter')):
        for item in plan[group]:
            editor.fields(
                item,
                {'start', 'duration', 'track', 'name', 'kind', 'params', 'keyframes'} if group == 'effects' else
                {'start', 'duration', 'track', 'name', 'intensity'},
                group,
                ('start', 'duration', 'name'),
            )
            editor.number(item['start'], group + '.start')
            editor.number(item['duration'], group + '.duration', 0.000001)
            editor.snap_range(item, group)
            if 'keyframes' in item:
                if item['name'] != '模糊' or item.get('kind', 'scene') != 'scene':
                    raise ValueError('Only measured standalone scene blur supports effect keyframes')
                editor.validate_parameter_keys(item['keyframes'], item['duration'], 'effect.keyframes', 0, 100)
            track = item.setdefault('track', group)
            if not isinstance(track, str) or not track.strip() or (track in track_types and track_types[track] != track_type):
                raise ValueError('Invalid or conflicting native effect/filter track name')
            track_types[track] = track_type
            tracks.setdefault(track, []).append(item)


def _validate_timeline_and_tasks(plan, tracks, track_types, probes):
    import editor

    for track, items in tracks.items():
        items.sort(key=lambda x: x['start'])
        for before, after in zip(items, items[1:]):
            if editor.end_us(before) > editor.us(after['start']):
                raise ValueError(f'Overlapping clips on track: {track} at {after["start"]:.6f}s')
        for index, item in enumerate(items):
            if 'transition' in item:
                if index == len(items) - 1 or abs(editor.end_us(item) - editor.us(items[index + 1]['start'])) > 1:
                    raise ValueError('Transition needs an immediately adjacent following clip')
                if ('matting_cache' in items[index + 1] or 'speed_curve' in items[index + 1] or 'motion_easing' in items[index + 1] and
                        probes[items[index + 1]['path']]['kind'] == 'video'):
                    raise ValueError('Measured speed_curve/video motion_easing does not admit an incoming transition')
                if item['transition']['duration'] > min(item['duration'], items[index + 1]['duration']) / 2:
                    raise ValueError('Transition is too long for adjacent clips')
    main = tracks.get('video', [])
    if track_types.get('video') != 'video' or not main or editor.us(main[0]['start']) != 0:
        raise ValueError('Main video track must be named video and start at 0')
    cursor = 0
    for item in main:
        if abs(editor.us(item['start']) - cursor) > 1:
            raise ValueError(f'Gap in main video track at {cursor / 1_000_000:.6f}s; use an explicit black image if intentional')
        cursor = editor.end_us(item)
    if any(editor.end_us(x) > cursor for group in ('clips', 'audio', 'captions', 'effects', 'filters') for x in plan[group]):
        raise ValueError('Overlay, audio or text extends past main video')
    for task in plan['jianying_tasks']:
        editor.fields(task, {'operation', 'track', 'start', 'duration', 'instructions'}, 'jianying_task',
                      ('operation', 'track', 'start', 'duration', 'instructions'))
        if task['operation'] not in ('smart_cutout', 'tracking'):
            raise ValueError('jianying_task.operation must be smart_cutout or tracking')
        if not isinstance(task['instructions'], str) or not task['instructions'].strip():
            raise ValueError('jianying_task.instructions must explain the native action')
        if not isinstance(task['track'], str) or track_types.get(task['track']) != 'video':
            raise ValueError('jianying_task must target an existing video track')
        editor.number(task['start'], 'jianying_task.start')
        editor.number(task['duration'], 'jianying_task.duration', 0.000001)
        editor.snap_range(task, 'jianying_task')
        task_start, task_end = editor.us(task['start']), editor.us(task['start']) + editor.us(task['duration'])
        if not any(editor.us(c['start']) <= task_start and editor.us(c['start']) + editor.us(c['duration']) >= task_end for c in tracks[task['track']]):
            raise ValueError('jianying_task must fit within a target clip')
    transitions = [x['transition']['name'] for x in plan['clips'] if 'transition' in x]
    if transitions:
        backend = editor.load_backend()
        for name in transitions:
            if name not in backend.TransitionType.__members__:
                raise ValueError(f'Unknown transition enum: {name}; run capabilities')
    if 'layer_order' in plan:
        order = plan['layer_order']
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order) or len(order) != len(set(order)) or set(order) != set(track_types):
            raise ValueError('layer_order must contain each actual track name exactly once, from background to foreground')
        visual_order = [x for x in order if track_types[x] in ('video', 'text')]
        if visual_order[0] != 'video':
            raise ValueError('Main video must remain the lowest visual layer')
    order = plan.get('layer_order', list(dict.fromkeys(
        x['track'] for group in ('audio', 'clips', 'captions', 'effects', 'filters') for x in plan[group])))
    # Build always places video before the other video tracks.
    if 'layer_order' not in plan:
        video_names = [x for x in order if track_types[x] == 'video']
        order = [x for x in order if track_types[x] == 'audio'] + ['video'] + [x for x in video_names if x != 'video'] + [
            x for x in order if track_types[x] not in ('audio', 'video')]
    return cursor, order, transitions


def _validate_composites(plan, tracks, track_types, order, cursor, probes):
    import editor

    grouped, composite_names = set(), set()
    dynamic_curves = [item for item in plan['clips'] if 'speed_curve' in item and item.get('mask', {}).get('keyframes')]
    for spec in plan['composites']:
        editor.fields(spec, {'name', 'tracks', 'perspective', 'start', 'duration', 'keyframes', 'motion_easing'}, 'composite', ('name', 'tracks'))
        if ('start' in spec) != ('duration' in spec):
            raise ValueError('Local composites require start and duration together')
        editor.number(spec.get('start', 0), 'composite.start', 0)
        editor.number(spec.get('duration', cursor / 1_000_000), 'composite.duration', 0.000001)
        if 'start' in spec:
            editor.snap_range(spec, 'composite')
        group_start, group_duration = native_advanced.composite_range(spec, cursor)
        if group_start + group_duration > cursor:
            raise ValueError('Composite extends past main video')
        if 'keyframes' in spec:
            editor.validate_parameter_keys(spec['keyframes'], group_duration / 1_000_000, 'composite.keyframes', -36000, 36000, rotation=True)
            properties = {k['property'] for k in spec['keyframes']}
            if len(properties) != 1:
                raise ValueError('Composite motion supports one measured property at a time')
            if properties & {'position_x', 'position_y', 'uniform_scale'}:
                if 'perspective' in spec or len(spec['keyframes']) != 2:
                    raise ValueError('Composite translation/uniform_scale requires two points and no perspective')
                for key in spec['keyframes']:
                    limits = (0.6, 1.2) if key['property'] == 'uniform_scale' else (-1, 1)
                    editor.number(key['value'], 'composite.' + key['property'], *limits)
        if 'motion_easing' in spec:
            keys = spec.get('keyframes', [])
            if (spec['motion_easing'] != 'smoothstep' or len(keys) != 2 or
                    any(k['property'] != 'position_x' for k in keys) or
                    keys[0]['time'] != 0 or 'perspective' in spec or 'start' not in spec):
                raise ValueError('Composite easing requires smoothstep with two horizontal points starting at zero, no perspective')
            for key in keys:
                editor.number(key['value'], 'composite.eased_position_x', -.35, .35)
        if 'perspective' in spec:
            perspective = spec['perspective']
            if 'static_up_right' in perspective:
                editor.fields(perspective, {'static_up_right'}, 'perspective', ('static_up_right',))
                corner = perspective['static_up_right']
                if not isinstance(corner, list) or len(corner) != 2:
                    raise ValueError('Static upper-right requires [x,y]')
                for value in corner:
                    editor.number(value, 'static_up_right coordinate', -1, 1)
                source_info = editor.probe(plan['clips'][0]['path'])
                if (perspective['static_up_right'] not in ([1, -1], [0.5, -0.5]) or
                        plan['canvas'] != {'width': 640, 'height': 360, 'fps': 30} or
                        len(plan['clips']) != 1 or len(plan['captions']) != 1 or
                        group_duration != 2000000 or spec.get('start', 0) != 0 or
                        spec.get('keyframes') or spec.get('motion_easing') or plan['audio'] or plan['effects'] or plan['filters'] or
                        source_info['kind'] != 'image' or (source_info['width'], source_info['height']) != (640, 360) or len(plan['composites']) != 1 or len(spec['tracks']) != 2):
                    raise ValueError('Static upper-right admits standalone 640x360 image/text two-second composites and two measured positions only')
                for item in plan['clips'] + plan['captions']:
                    if item['start'] != 0 or item['duration'] != 2 or any(item.get(k) for k in ('transform', 'keyframes', 'animations', 'effects', 'filters', 'grading', 'mask', 'chroma', 'background', 'mix_mode', 'motion_easing')):
                        raise ValueError('Static upper-right requires static full-range image and text')
                image, text = plan['clips'][0], plan['captions'][0]
                if (image.get('fit', 'contain') != 'contain' or
                        any(text.get(k) for k in ('text_curve', 'glow', 'font', 'style'))):
                    raise ValueError('Static upper-right admits contain image and plain default-font text only')
            else:
                editor.fields(perspective, {'keyframes'}, 'perspective', ('keyframes',))
                if not isinstance(perspective['keyframes'], list) or not perspective['keyframes']:
                    raise ValueError('Perspective requires a nonempty keyframe array')
                last = {}
                for key in perspective['keyframes']:
                    editor.fields(key, {'property', 'time', 'values'}, 'perspective.keyframe', ('property', 'time', 'values'))
                    if key['property'] not in native_advanced.CORNER_PIN_PROPERTIES:
                        raise ValueError('Only measured corner pin keyframe properties are supported')
                    editor.number(key['time'], 'perspective.keyframe.time', 0, group_duration / 1_000_000 - 0.000001)
                    if not isinstance(key['values'], list) or len(key['values']) != 2:
                        raise ValueError('Perspective values must be [x, y]')
                    for value in key['values']:
                        editor.number(value, 'perspective.keyframe.value', -10, 10)
                    timestamp = editor.us(key['time'])
                    prop = key['property']
                    if prop not in last and timestamp != 0:
                        raise ValueError('Perspective keyframes must start at time zero')
                    if prop in last and timestamp <= last[prop]:
                        raise ValueError('Perspective keyframes must strictly increase')
                    last[prop] = timestamp
                if native_advanced.other_corner_requested(spec):
                    native_advanced.validate_other_corner(plan, spec, group_duration, editor.probe(plan['clips'][0]['path']))
        if not isinstance(spec['name'], str) or not spec['name'].strip() or spec['name'] in composite_names:
            raise ValueError('Composite names must be nonempty and unique')
        names = spec['tracks']
        if dynamic_curves:
            keys = spec.get('keyframes', [])
            subject = dynamic_curves[0]
            group_axis = keys[0]['property'] if keys else None
            limits = {'position_x': (-.3, .3), 'position_y': (-.15, .15),
                      'uniform_scale': (.9, 1.1)}.get(group_axis)
            canvas_720 = plan['canvas'] == {'width': 1280, 'height': 720, 'fps': 60}
            if canvas_720:
                limits = {'position_x': (-.2, .2), 'position_y': (-.1, .1),
                          'uniform_scale': (.9, 1.1)}.get(group_axis)
            valid_names = isinstance(names, list) and all(isinstance(name, str) and name in order for name in names)
            foreground_range = (valid_names and all(order.index(name) > order.index(subject['track']) for name in names) and
                                editor.us(subject['start']) == group_start and editor.us(subject['duration']) == group_duration)
            rear_static = canvas_720 and 'keyframes' not in spec and 'motion_easing' not in spec
            lead = editor.us(subject['start']) - group_start
            trail = group_start + group_duration - editor.us(subject['start']) - editor.us(subject['duration'])
            ordered_windows = sorted(dynamic_curves, key=lambda item: editor.us(item['start']))
            two_shot_static = (rear_static and valid_names and len(ordered_windows) == 2 and
                               ordered_windows[0]['track'] == ordered_windows[1]['track'] and
                               ordered_windows[0]['path'] == ordered_windows[1]['path'] and
                               tuple(editor.us(item['duration']) for item in ordered_windows) in
                               ((2200000, 2200000), (1600000, 2400000), (2400000, 1600000)) and
                               editor.us(ordered_windows[0]['source_in'] + ordered_windows[0]['duration'] * ordered_windows[0]['speed']) <= editor.us(ordered_windows[1]['source_in']) and
                               editor.us(ordered_windows[0]['start']) + editor.us(ordered_windows[0]['duration']) == editor.us(ordered_windows[1]['start']) and
                               sum(item['track'] == subject['track'] for item in plan['clips']) == 2 and
                               all(order.index(name) > order.index(subject['track']) for name in names) and
                               900000 <= editor.us(ordered_windows[0]['start']) - group_start <= 1200000 and
                               800000 <= group_start + group_duration - editor.us(ordered_windows[1]['start']) - editor.us(ordered_windows[1]['duration']) <= 1100000)
            if two_shot_static:
                lead = editor.us(ordered_windows[0]['start']) - group_start
                trail = group_start + group_duration - editor.us(ordered_windows[1]['start']) - editor.us(ordered_windows[1]['duration'])
            foreground_static = (rear_static and valid_names and
                                 all(order.index(name) > order.index(subject['track']) for name in names) and
                                 900000 <= lead <= 1200000 and 800000 <= trail <= 1100000)
            rear_interval = (500000 <= lead <= 600000 and 400000 <= trail <= 500000) or (
                rear_static and 900000 <= lead <= 1200000 and 800000 <= trail <= 1100000)
            rear_envelope = (canvas_720 and valid_names and (rear_static or group_axis in ('position_x', 'position_y', 'uniform_scale')) and
                             (group_axis == 'position_x' or 'motion_easing' not in spec) and
                             all(order.index(name) < order.index(subject['track']) for name in names) and
                             rear_interval)
            if (plan['canvas'] not in ({'width': 640, 'height': 360, 'fps': 60},
                                       {'width': 1280, 'height': 720, 'fps': 60}) or
                    (len(dynamic_curves) != 1 and not two_shot_static) or len(plan['composites']) != 1 or
                    'start' not in spec or 'perspective' in spec or spec.get('motion_easing') not in (None, 'smoothstep') or
                    (not rear_static and (len(keys) != 2 or limits is None or any(k['property'] != group_axis for k in keys) or
                     any(not limits[0] <= k['value'] <= limits[1] for k in keys))) or
                    (group_axis != 'position_x' and 'motion_easing' in spec) or
                    (not rear_static and keys[0]['time'] != 0) or not (foreground_range or rear_envelope or foreground_static) or
                    (rear_static and not (rear_envelope or foreground_static))):
                raise ValueError('Curve dynamic rectangle combination requires one independent window and one matching-range foreground contain-image/static-white-cached-font group: two X linear/smoothstep points within +/-0.3, two Y linear points within +/-0.15, or two uniform-scale linear points within 0.9..1.1 at 640x360; matching 1280x720 only two X linear/smoothstep points within +/-0.2 or two Y linear points within +/-0.1 or two uniform-scale linear points within 0.9..1.1, no perspective; 1280x720 additionally permits one rear X linear/smoothstep group within +/-0.2, Y default-linear group within +/-0.1, or uniform-scale default-linear group within 0.9..1.1, or static group omitting keyframes/easing, enclosing the window with 0.5..0.6s lead and 0.4..0.5s trail; static-only additionally admits 0.9..1.2s lead and 0.8..1.1s trail together; foreground static without keys/easing additionally admits that long interval; exactly two adjacent same-source same-track dynamic windows with durations (2.2,2.2), (1.6,2.4), or (2.4,1.6) seconds and ordered nonoverlap source ranges may share one foreground long static envelope')
        if any('matting_cache' in item and item['track'] in names for item in plan['clips']):
            raise ValueError('Existing matting cache inside composites is unverified')
        vertical_subjects = [item for item in plan['clips'] if 'matting_cache' in item and
                             any(k['property'] == 'position_y' for k in item['keyframes'])]
        if vertical_subjects:
            keys = spec.get('keyframes', [])
            group_axis = keys[0]['property'] if keys else None
            limits = {'position_x': (-.35, .35), 'position_y': (-.25, .25),
                      'uniform_scale': (.7, 1.1)}.get(group_axis)
            if (len(vertical_subjects) != 1 or len(plan['composites']) != 1 or
                    vertical_subjects[0].get('motion_easing') != 'smoothstep' or
                    'start' not in spec or 'perspective' in spec or len(keys) != 2 or
                    limits is None or any(k['property'] != group_axis for k in keys) or keys[0]['time'] != 0 or
                    any(not limits[0] <= k['value'] <= limits[1] for k in keys) or
                    editor.us(vertical_subjects[0]['start']) != group_start or
                    editor.us(vertical_subjects[0]['duration']) != group_duration or
                    not isinstance(names, list) or
                    any(name not in order or order.index(name) >= order.index(vertical_subjects[0]['track']) for name in names)):
                raise ValueError('Vertical matting combination requires one independent smoothstep subject and one matching-range behind two-point group: X +/-0.35 linear/smoothstep, Y +/-0.25 linear, or uniform scale 0.7..1.1 linear')
        if any('glow' in item and item['track'] in names for item in plan['captions']):
            raise ValueError('Text glow inside composites is unverified')
        if (not isinstance(names, list) or len(names) < 2 or not all(isinstance(x, str) for x in names) or
                len(names) != len(set(names)) or any(track_types.get(x) not in ('video', 'text', 'audio') for x in names) or
                track_types.get(names[0]) != 'video'):
            raise ValueError('Composite requires distinct video/text/audio tracks, beginning with video')
        if 'motion_easing' in spec or vertical_subjects or dynamic_curves:
            images = [x for x in plan['clips'] if x['track'] in names]
            texts = [x for x in plan['captions'] if x['track'] in names]
            if (len(names) != 2 or len(images) != 1 or len(texts) != 1 or
                    images[0]['track'] != names[0] or texts[0]['track'] != names[1] or
                    probes[images[0]['path']]['kind'] != 'image' or images[0]['fit'] != 'contain' or images[0]['transform'] or
                    any(editor.us(x['start']) != group_start or editor.us(x['duration']) != group_duration for x in images + texts) or
                    not texts[0].get('font') or any(c in texts[0]['text'] for c in '\r\n') or
                    texts[0]['color'].upper() != '#FFFFFF' or texts[0]['border'] or texts[0]['bold'] or texts[0]['transform'] or
                    set(texts[0]['style']) - {'auto_wrapping'} or texts[0]['style'].get('auto_wrapping', False) or
                    any(editor.us(x['start']) < group_start + group_duration and group_start < editor.us(x['start']) + editor.us(x['duration'])
                        for x in plan['effects'] + plan['filters'])):
                raise ValueError('Composite easing, vertical matting or dynamic curve-window combination admits one contain image and one plain white cached-font single-line caption, both spanning the group, with no overlapping effects/filters')
        if any('motion_easing' in item and item['track'] in names for item in plan['clips'] + plan['captions']):
            raise ValueError('Measured motion_easing is not admitted inside composites')
        if any('speed_curve' in item and item['track'] in names for item in plan['clips']):
            raise ValueError('Measured speed_curve is not admitted inside composites')
        if dynamic_curves or any(k['property'] in ('position_x', 'position_y', 'uniform_scale') for k in spec.get('keyframes', [])):
            for item in plan['clips']:
                if item['track'] in names and (item['speed'] != 1 or item.get('keyframes') or
                        any(item.get(k) for k in ('effects', 'filters', 'animations', 'mask', 'grading', 'chroma', 'background', 'mix_mode', 'transition'))):
                    raise ValueError('Composite translation/uniform_scale admits ordinary speed=1 media and static transforms only')
            if any(item['track'] in names and (item.get('keyframes') or item.get('animations') or item.get('text_curve')) for item in plan['captions']):
                raise ValueError('Composite translation/uniform_scale admits static text only')
            if any(item['track'] in names for item in plan['audio']):
                raise ValueError('Composite translation/uniform_scale audio combination is unverified')
        if any('loudness' in x and x['track'] in names for x in plan['audio']):
            raise ValueError('Measured loudness is restricted to standalone audio, outside composites')
        for name in names:
            if not all(group_start <= editor.us(x['start']) and editor.us(x['start']) + editor.us(x['duration']) <= group_start + group_duration for x in tracks[name]):
                raise ValueError('Selected composite tracks must lie entirely inside the group range; split clips first')
        indexes = [order.index(x) for x in names]
        if indexes != list(range(indexes[0], indexes[0] + len(names))) or grouped.intersection(names):
            raise ValueError('Composite tracks must be contiguous in layer order, ordered, and not shared')
        if any(t['track'] in names for t in plan['jianying_tasks']):
            raise ValueError('Pending native tasks must be completed before grouping their tracks')
        grouped.update(names)
        composite_names.add(spec['name'])


def _collect_plan_warnings(plan, transitions, native_features, user_data):
    import editor

    warnings = []
    if any(x['fit'] == 'cover' for x in plan['clips']):
        warnings.append('Cover fit may crop the subject; visually check framing.')
    if transitions:
        warnings.append('Transitions require visual/resource validation inside Jianying.')
    if native_features:
        warnings.append('Native effects/animations remain editable draft parameters; resource download, entitlement and rendering are unverified.')
    if 'masks' in native_features:
        warnings.append('Native rectangle and zero-feather circle geometry measured on 11.5; current project rendering and other shapes require separate verification. No prerender fallback.')
    if any(native_mask_feather.requested(item) for item in plan['clips']):
        warnings.append('red_coverage_v1 is integrated for the measured 11.6 rectangle subset with draft-owned authenticated resources; UI save/reopen and current-project playback/export remain unverified.')
    if plan['composites']:
        warnings.append('Native composites retain editable internal tracks; local groups rebase target times. Current draft rendering is unverified.')
    if plan['jianying_tasks']:
        warnings.append('Smart cutout/tracking tasks are pending inside Jianying; they were NOT executed or prerendered.')
    if any(item.get('animations') for item in plan['clips'] + plan['captions']):
        user_data = user_data if user_data is not None else editor.configured_user_data()
        if user_data is None:
            warnings.append('Animation availability was not checked: no configured local effect cache.')
        elif editor.effect_cache_dir(user_data) is None:
            warnings.append('Animation packages are not cached on this machine yet — this user data has no Cache/effect '
                            'directory, so build drops every planned animation until the app downloads one. Opening a '
                            'dropped animation once inside Jianying, then rebuilding, ships it.')
        else:
            available, unresolvable, _ = editor.animation_preflight(plan, editor.load_backend(), user_data)
            if unresolvable:
                locally = '; '.join(f"{kind}: {'、'.join(names) or 'none'}" for kind, names in available.items())
                warnings.append('Build drops animations whose package this machine does not cache yet — '
                                f"{', '.join(unresolvable)}. Locally resolvable names now: {locally}")
    return warnings


def validate_plan(path, user_data=None, mask_resource_root=None):
    import editor

    plan_path = Path(path).resolve()
    plan = copy.deepcopy(editor.read_json(plan_path))
    _validate_plan_header(plan)
    probes, tracks, track_types = {}, {}, {}
    native_protocol.validate_raw_plans(plan)
    common = {'path', 'start', 'duration', 'source_in', 'speed', 'volume', 'track', 'reason'}
    for group, track_type, default_track in (('clips', 'video', 'video'), ('audio', 'audio', 'voice'), ('captions', 'text', 'captions')):
        for index, item in enumerate(plan[group]):
            label = f'{group}[{index}]'
            allowed = (common | {'fit', 'transform', 'keyframes', 'motion_easing', 'speed_curve', 'transition', 'effects', 'filters', 'animations',
                                 'mix_mode', 'chroma', 'background', 'mask', 'grading', 'matting_cache'} if group == 'clips' else
                       common | {'fade_in', 'fade_out', 'keyframes', 'denoise', 'loudness', 'beat_marks'} if group == 'audio' else
                       {'text', 'start', 'duration', 'track', 'size', 'color', 'x', 'y', 'bold', 'border', 'animations',
                        'style', 'transform', 'keyframes', 'motion_easing', 'text_curve', 'font', 'font_path', 'glow',
                        'highlights'})
            editor.fields(item, allowed, label, ('start', 'duration', 'text' if group == 'captions' else 'path'))
            editor.number(item['start'], label + '.start')
            editor.number(item['duration'], label + '.duration', 0.000001)
            editor.snap_range(item, label)
            track = item.setdefault('track', default_track)
            if not isinstance(track, str) or not track.strip():
                raise ValueError(f'{label}.track must be a non-empty string')
            if track in track_types and track_types[track] != track_type:
                raise ValueError(f'Track name used by different media types: {track}')
            track_types[track] = track_type
            tracks.setdefault(track, []).append(item)
            if 'reason' in item and not isinstance(item['reason'], str):
                raise ValueError(f'{label}.reason must be text')
            if group == 'captions':
                _validate_caption_item(item, label, plan, plan_path)
                continue
            if not isinstance(item['path'], str) or not item['path'] or '://' in item['path']:
                raise ValueError(f'{label}.path must be a local media path')
            source = (plan_path.parent / item['path']).resolve()
            item['path'] = str(source)
            if item['path'] not in probes:
                probes[item['path']] = editor.probe(source)
            info = probes[item['path']]
            editor.number(item.setdefault('source_in', 0), label + '.source_in')
            item['source_in'] = editor.us(item['source_in']) / 1_000_000
            if 'speed_curve' in item:
                _validate_speed_curve(item, label, info, plan)
            editor.number(item.setdefault('speed', 1), label + '.speed', 0.01, 100)
            editor.number(item.setdefault('volume', 0 if group == 'clips' else 1), label + '.volume', 0, 10)
            if group == 'clips' and info['kind'] == 'audio':
                raise ValueError(f'{label} requires video or image')
            if group == 'clips' and info.get('alpha') and (info.get('codec'), info.get('pix_fmt')) not in editor.ALPHA_VIDEO_FORMATS:
                raise ValueError(f'{label}: transparent video {info.get("codec")}/{info.get("pix_fmt")} is not admitted; '
                                 'Jianying shows VP9 alpha as black and shifts ProRes 4444 colour. Re-encode as MOV with '
                                 '-c:v png -pix_fmt rgba or -c:v qtrle -pix_fmt argb')
            if group == 'audio' and info['kind'] != 'audio':
                raise ValueError(f'{label} requires an audio-only file; extract a copy first')
            if info['kind'] == 'image':
                if item['source_in'] != 0 or item['speed'] != 1:
                    raise ValueError('Still images require source_in=0, speed=1')
            elif editor.us(item['source_in']) + editor.us(item['duration'] * item['speed']) > editor.source_limit_us(info):
                raise ValueError(f'{label}: source range exceeds measured media duration')
            if group == 'audio':
                _validate_audio_item(item, label, info, plan_path)
                continue
            _validate_clip_item(item, label, info, plan, plan_path)
    _validate_effect_filter_tracks(plan, tracks, track_types)
    native_protocol.validate_pre_native_media(plan, probes)
    native_features = editor.validate_native(plan, mask_resource_root)
    native_protocol.validate_post_native_media(plan, probes)
    for item in plan['clips']:
        if ('matting_cache' in item or 'speed_curve' in item or 'motion_easing' in item and probes[item['path']]['kind'] == 'video'):
            if any(x['start'] < item['start'] + item['duration'] and
                   item['start'] < x['start'] + x['duration'] for x in plan['effects'] + plan['filters']):
                raise ValueError('Measured speed_curve/video motion_easing does not admit overlapping independent effects or filters')
    for effect in plan['effects']:
        if effect.get('keyframes'):
            native_advanced.blur_material(effect, None)
    cursor, order, transitions = _validate_timeline_and_tasks(plan, tracks, track_types, probes)
    _validate_composites(plan, tracks, track_types, order, cursor, probes)
    warnings = _collect_plan_warnings(plan, transitions, native_features, user_data)
    return plan, probes, warnings
