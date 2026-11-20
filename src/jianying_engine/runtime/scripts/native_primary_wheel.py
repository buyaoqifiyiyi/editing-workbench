"""Strictly bounded 11.7 primary-wheel still-image edits; native UI editing remains unverified."""
import json
import math
from pathlib import Path
import uuid

import file_hash
import native_chroma
import native_protocol

CONTRACT = Path(__file__).resolve().parents[1] / 'assets/primary-wheel-11.7-v1.json'
CONTROLS = {'lift': ('red', .5), 'gamma': ('green', -.5), 'gain': ('blue', .5)}


def requested(plan):
    return any(isinstance(item, dict) and isinstance(item.get('grading'), dict) and 'primary_wheel' in item['grading']
               for item in plan.get('clips', []))


def runtime(install=None):
    import editor
    spec = json.loads(CONTRACT.read_text(encoding='utf-8'))
    install = Path(install) if install is not None else editor.jianying_install()
    if install is None or install.name != spec['runtime_version']:
        raise ValueError('Primary wheel requires measured Jianying ' + spec['runtime_version'])
    if any(p.is_symlink() or p.is_junction() for p in (install, install / 'Resources')):
        raise ValueError('Primary wheel installation must not be redirected')
    def regular_path(rel):
        if not isinstance(rel, str) or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise ValueError('Primary wheel contract path is not relative to its installation')
        path = install
        for part in Path(rel).parts:
            path = path / part
            if path.is_symlink() or path.is_junction():
                raise ValueError('Primary wheel path must not contain a link: ' + rel)
        return path
    for rel, expected in spec['runtime_sha256'].items():
        path = regular_path(rel)
        if not file_hash.matches(path, expected):
            raise ValueError('Primary wheel runtime changed: ' + rel)
    resource = regular_path(spec['resource'])
    if native_chroma.tree(resource) != spec['source_files']:
        raise ValueError('Primary wheel resource files changed')
    return str(resource)


def validate(plan):
    if not requested(plan):
        return
    runtime()
    if (len(plan.get('clips', [])) != 1 or any(plan.get(k) for k in
            ('audio', 'captions', 'effects', 'filters', 'composites', 'jianying_tasks', 'missing_assets')) or
            plan.get('canvas') != {'width': 640, 'height': 360, 'fps': 30} or 'layer_order' in plan):
        raise ValueError('Primary wheel requires the measured single-still 640x360/30 scene')
    item = plan['clips'][0]
    if set(item['grading']) != {'primary_wheel'}:
        raise ValueError('Primary wheel does not admit other grading controls')
    wheel = item['grading']['primary_wheel']
    if not isinstance(wheel, dict) or len(wheel) != 1:
        raise ValueError('Primary wheel requires one measured axis')
    axis, selection = next(iter(wheel.items()))
    if axis not in CONTROLS or not isinstance(selection, dict) or len(selection) != 1:
        raise ValueError('Primary wheel axis is not measured')
    channel, value = next(iter(selection.items()))
    expected_channel, expected_value = CONTROLS[axis]
    if channel != expected_channel or type(value) not in (int, float) or value not in (0, expected_value):
        raise ValueError('Primary wheel channel/value is not measured')
    for key, default, expected in (('start', 0, 0), ('duration', None, 2), ('source_in', 0, 0),
                                    ('speed', 1, 1), ('volume', 0, 0)):
        value = item.get(key, default)
        if type(value) not in (int, float) or value != expected:
            raise ValueError('Primary wheel timing and gain must match the measured scene')
    if (item.get('track', 'video') != 'video' or item.get('fit', 'contain') != 'contain' or
            any(item.get(k) for k in ('transform', 'keyframes', 'animations', 'effects', 'filters', 'motion_easing',
                'mask', 'chroma', 'matting_cache', 'background', 'transition', 'mix_mode', 'speed_curve')) or
            Path(item.get('path', '')).suffix.lower() != '.png'):
        raise ValueError('Primary wheel requires an untransformed PNG still without effects')


def validate_media(plan, probes):
    if requested(plan):
        from PIL import Image
        info = probes[plan['clips'][0]['path']]
        if info.get('kind') != 'image' or (info.get('width'), info.get('height')) != (640, 360):
            raise ValueError('Primary wheel requires one 640x360 PNG image')
        with Image.open(plan['clips'][0]['path']) as image:
            if (image.mode != 'RGB' or image.format != 'PNG' or image.size != (640, 360) or
                    getattr(image, 'n_frames', 1) != 1 or any(key in image.info for key in
                    ('transparency', 'icc_profile', 'gamma', 'chromaticity', 'srgb', 'exif'))):
                raise ValueError('Primary wheel requires one static opaque unprofiled RGB PNG')
            image.load()  # A valid header is not proof that the image data decodes.


def material(plan, identity=None, install=None):
    item = plan['clips'][0]
    axis, selection = next(iter(item['grading']['primary_wheel'].items()))
    channel, value = next(iter(selection.items()))
    result = dict(id=identity or str(uuid.uuid4()).upper(), type='primary_color_wheels', panel='', resource_id='',
                  path=runtime(install), intensity=1, lumi_hub_path='', source_platform=1)
    for name in ('lift', 'gamma', 'gain', 'offset'):
        result[name] = dict(red=0, green=0, blue=0, luma=0, saturation=0)
    result[axis][channel] = value
    return result


def apply(content, plan):
    if not requested(plan):
        return
    video = next(t for t in content['tracks'] if t['name'] == 'video')['segments'][0]
    result = material(plan)
    ids = {obj['id'] for obj in objects(content) if isinstance(obj.get('id'), str)}
    while result['id'] in ids:
        result['id'] = str(uuid.uuid4()).upper()
    content['materials'].setdefault('primary_color_wheels', []).append(result)
    video['extra_material_refs'].append(result['id'])
    video.update(enable_adjust=True, enable_color_wheels=True, enable_hsl=False, enable_color_curves=False,
                 enable_hsl_curves=False, enable_adjust_mask=False)


objects = native_protocol.iter_objects


def verify(content, plan, install=None):
    entries = [obj for obj in objects(content) if obj.get('type') == 'primary_color_wheels']
    planned = requested(plan)
    if len(entries) != int(planned):
        raise ValueError('Primary wheel material count does not match plan')
    if not planned:
        if any(obj.get('primary_color_wheels') for obj in objects(content)):
            raise ValueError('Unplanned or nested primary wheel material')
        return 0
    validate(plan)
    materials = content.get('materials')
    if (not isinstance(materials, dict) or any(not isinstance(values, list) for values in materials.values()) or
            {kind for kind, values in materials.items() if values} != {'videos', 'speeds', 'primary_color_wheels'} or
            any(len(materials[kind]) != 1 for kind in ('videos', 'speeds', 'primary_color_wheels'))):
        raise ValueError('Primary wheel scene contains extra, missing or malformed materials')
    ids = [materials[kind][0].get('id') for kind in ('videos', 'speeds', 'primary_color_wheels')
           if isinstance(materials[kind][0], dict)]
    if len(ids) != 3 or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != 3:
        raise ValueError('Primary wheel native materials require unique IDs')
    actual = entries[0]
    identity = actual.get('id')
    if not isinstance(identity, str) or not identity:
        raise ValueError('Primary wheel identity missing')
    expected = material(plan, identity, install)
    if (actual != expected or type(actual.get('source_platform')) is not int or
            type(actual.get('intensity')) not in (int, float)):
        raise ValueError('Primary wheel material differs from its plan and resource')
    for axis in ('lift', 'gamma', 'gain', 'offset'):
        if any(type(value) not in (int, float) for value in actual[axis].values()):
            raise ValueError('Primary wheel component type changed')
    if content.get('materials', {}).get('primary_color_wheels') != [actual]:
        raise ValueError('Primary wheel material is missing or nested')
    if (sum(obj.get('id') == identity for obj in objects(content)) != 1 or
            any(obj is not content['materials'] and obj.get('primary_color_wheels') for obj in objects(content))):
        raise ValueError('Duplicate identity or nested primary-wheel material')
    segments = [s for t in content.get('tracks', []) for s in t.get('segments', [])]
    if len(content['tracks']) != 1 or len(segments) != 1:
        raise ValueError('Primary wheel requires one native track and segment')
    s = segments[0]
    refs = s.get('extra_material_refs', [])
    by_id = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
             for m in values if isinstance(m, dict) and 'id' in m}
    if len(refs) != 2 or len(set(refs)) != 2 or refs.count(identity) != 1 or sorted(
            by_id.get(ref, {}).get('type', '') for ref in refs) != ['primary_color_wheels', 'speed']:
        raise ValueError('Primary wheel must be referenced exactly once by the sole segment')
    flags = dict(enable_adjust=True, enable_color_wheels=True, enable_hsl=False, enable_color_curves=False,
                 enable_hsl_curves=False, enable_adjust_mask=False)
    if any(s.get(key) is not value for key, value in flags.items()):
        raise ValueError('Primary wheel enable flags changed')
    default = {'alpha': 1., 'flip': {'horizontal': False, 'vertical': False}, 'rotation': 0.,
               'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0., 'y': 0.}}
    source = by_id.get(s.get('material_id'), {})
    crop = {'upper_left_x': 0., 'upper_left_y': 0., 'upper_right_x': 1., 'upper_right_y': 0.,
            'lower_left_x': 0., 'lower_left_y': 1., 'lower_right_x': 1., 'lower_right_y': 1.}
    if (content.get('fps') != 30 or content.get('canvas_config', {}).get('width') != 640 or
            content.get('canvas_config', {}).get('height') != 360 or s.get('clip') != default or
            s.get('common_keyframes') or s.get('keyframe_refs') or s.get('reverse') is not False or
            s.get('visible') is not True or s.get('source_timerange') != {'start': 0, 'duration': 2000000} or
            any(type(value) is not int for value in s['source_timerange'].values()) or
            source.get('type') != 'photo' or (source.get('width'), source.get('height')) != (640, 360) or
            source.get('crop') != crop or source.get('crop_ratio') != 'free' or source.get('crop_scale') != 1):
        raise ValueError('Primary wheel native canvas, source clock or geometry changed')
    clip = s['clip']
    scalars = [clip['alpha'], clip['rotation'], *clip['scale'].values(), *clip['transform'].values(),
               source['crop_scale'], *source['crop'].values()]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in scalars) or any(
            value is not False for value in clip['flip'].values()):
        raise ValueError('Primary wheel native geometry types changed')
    return 1


def preview_check(content, root, install):
    observed = any(obj.get('primary_color_wheels') or obj.get('type') == 'primary_color_wheels'
                   for obj in objects(content))
    native_protocol.verify_plan_preview(
        content,
        root,
        observed=observed,
        plan_declared=requested,
        missing_plan_error='Primary wheel preview requires its validated plan',
        runtime_check=lambda: runtime(install),
        runtime_first=False,
        pass_root=True,
    )
