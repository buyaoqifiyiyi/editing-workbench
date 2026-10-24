"""Explicit measured mix modes using unchanged installed resources."""
import json
from pathlib import Path
from native_chroma import sha, tree, validate_media

MODE = 'installed_mix_v1'


def contract():
    return json.loads((Path(__file__).resolve().parents[1] / 'assets/mix-modes-v1.json').read_text(encoding='utf-8'))


def requested(item):
    return isinstance(item.get('mix_mode'), dict) and item['mix_mode'].get('rendering') == MODE


def resource(name, install=None):
    import editor
    spec = contract()
    if name not in spec['modes']:
        raise ValueError('Only measured mix modes are admitted')
    install = Path(install) if install is not None else editor.jianying_install()
    if not install or install.name != spec['runtime_version']:
        raise ValueError('installed_mix_v1 requires measured Jianying ' + spec['runtime_version'])
    if any(p.is_symlink() or p.is_junction() for p in (install, install / 'Resources', install / 'Resources/MixMode')):
        raise ValueError('Mix installation must not be redirected through links')
    for relative, expected in spec['runtime_sha256'].items():
        path = install / relative
        if not path.is_file() or path.is_symlink() or sha(path) != expected:
            raise ValueError('Mix runtime identity changed: ' + str(path))
    index = install / 'Resources/MixMode/MixMode.json'
    if not index.is_file() or index.is_symlink() or sha(index) != spec['index_sha256']:
        raise ValueError('Mix resource mapping changed')
    path = install / spec['modes'][name]['resource']
    if tree(path) != spec['modes'][name]['files']:
        raise ValueError('Mix resource identity changed or files are missing/added')
    return str(path)


def validate_spec(item, plan):
    import editor
    spec = item['mix_mode']
    editor.fields(spec, {'name', 'rendering'}, 'mix_mode', ('name', 'rendering'))
    if spec['rendering'] != MODE or spec['name'] not in contract()['modes']:
        raise ValueError('mix_mode requires explicit installed_mix_v1 and a measured name')
    if (len(plan['clips']) != 2 or item['track'] == 'video' or plan.get('layer_order') != ['video', item['track']] or
            plan.get('composites') or plan.get('effects') or plan.get('filters') or plan.get('captions') or
            plan['canvas'] != {'width': 1280, 'height': 720, 'fps': 30} or item['fit'] != 'contain' or
            item['speed'] != 1 or item['transform'] or 'speed_curve' in item or
            any(item.get(k) for k in ('keyframes', 'motion_easing', 'animations', 'effects', 'filters', 'grading',
                                     'mask', 'matting_cache', 'background', 'transition', 'chroma'))):
        raise ValueError('Measured mix requires a 1280x720/30fps two-clip contain speed=1 overlay without motion/effects/composites')
    base = next(x for x in plan['clips'] if x is not item)
    if (base['track'] != 'video' or base['fit'] != 'contain' or base['speed'] != 1 or base['transform'] or
            any(base.get(k) for k in ('keyframes', 'motion_easing', 'animations', 'effects', 'filters', 'grading',
                                     'mask', 'matting_cache', 'background', 'transition', 'chroma', 'mix_mode', 'speed_curve'))):
        raise ValueError('Measured mix requires an unmodified base image')


def check_media(item, plan, probes):
    validate_media(item, probes[item['path']])
    if probes[item['path']]['kind'] not in contract()['modes'][item['mix_mode']['name']].get('media_kinds', ('image', 'video')):
        raise ValueError('This measured mix mode currently admits PNG overlays only; video remains unverified')
    base = next(x for x in plan['clips'] if x is not item)
    info = probes[base['path']]
    validate_media(base, info)
    if info['kind'] != 'image':
        raise ValueError('Measured mix base must be a PNG image')


def selected(content, plan):
    materials = [m for m in content['materials'].get('effects', []) if m.get('type') == 'mix_mode']
    for slot, values in content['materials'].items():
        if slot != 'effects' and isinstance(values, list) and any(isinstance(m, dict) and m.get('type') == 'mix_mode' for m in values):
            raise ValueError('Mix materials must be in the native effects slot')
    by_id = {m['id']: m for m in materials}
    if len(by_id) != len(materials):
        raise ValueError('Duplicate mix material ID')
    segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    sources = {m['id']: m for m in content['materials'].get('videos', [])}
    used = set()
    for item in plan['clips']:
        segment = segments[item['track'], round(item['start'] * 1e6)]
        refs = [r for r in segment['extra_material_refs'] if r in by_id]
        if len(refs) != int(requested(item)) or any(r in used for r in refs):
            raise ValueError('Mix binding must be unique and match its planned segment')
        if refs:
            if any((sources.get(s['material_id'], {}).get('width'), sources.get(s['material_id'], {}).get('height')) != (1280, 720)
                   for t in content['tracks'] if t['type'] == 'video' for s in t['segments']):
                raise ValueError('Mix native source dimensions changed')
            used.add(refs[0])
            yield by_id[refs[0]], item
    if used != set(by_id):
        raise ValueError('Orphaned mix material')


def bind(content, plan):
    for material, item in selected(content, plan):
        material['path'] = resource(item['mix_mode']['name'])


def verify(content, plan):
    pairs = list(selected(content, plan))
    if pairs:
        import editor
        default = {'alpha': 1., 'flip': {'horizontal': False, 'vertical': False}, 'rotation': 0.,
                   'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0., 'y': 0.}}
        if ((content['canvas_config']['width'], content['canvas_config']['height'], content.get('fps')) != (1280, 720, 30) or
                len(content['tracks']) != 2 or any(t['type'] != 'video' or len(t['segments']) != 1 for t in content['tracks'])):
            raise ValueError('Mix native scene is outside the measured two-track canvas')
        refs = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                for m in values if isinstance(m, dict) and 'id' in m}
        for track in content['tracks']:
            segment = track['segments'][0]
            clip = segment.get('clip')
            if clip != default or segment.get('common_keyframes') or any(
                    refs.get(r, {}).get('type') not in ('speed', 'mix_mode') for r in segment['extra_material_refs']):
                raise ValueError('Mix native transform, motion or extra effect is unadmitted')
            for value in (clip['alpha'], clip['rotation'], *clip['scale'].values(), *clip['transform'].values()):
                editor.number(value, 'mix clip scalar', -1e6)
            if any(value is not False for value in clip['flip'].values()):
                raise ValueError('Mix native flip must remain disabled')
    for material, item in pairs:
        name = item['mix_mode']['name']
        expected = dict(contract()['modes'][name]['material'], id=material['id'], path=resource(name))
        if material != expected or any(type(material.get(k)) is not int for k in ('apply_target_type', 'source_platform')) or type(material.get('value')) is not float:
            raise ValueError('Mix native identity, path, value or configuration changed')
    return len(pairs)


def preview_check(content, root=None, install=None):
    def contains(value):
        if isinstance(value, dict):
            return value.get('type') == 'mix_mode' or any(contains(v) for v in value.values())
        return isinstance(value, list) and any(contains(v) for v in value)
    if contains(content.get('materials', {}).get('drafts', [])):
        raise ValueError('Native preview refuses unadmitted nested mix modes')
    found = False
    for slot, materials in content.get('materials', {}).items():
        if not isinstance(materials, list):
            continue
        for m in materials:
            if isinstance(m, dict) and m.get('type') == 'mix_mode':
                found = True
                mode = contract()['modes'].get(m.get('name'))
                if (slot != 'effects' or not mode or m != dict(mode['material'], id=m.get('id'), path=resource(m['name'], install))):
                    raise ValueError('Native preview refuses an unadmitted mix resource')
    import editor
    plan_path = Path(root) / 'edit-plan.json' if root is not None else None
    declared = plan_path is not None and plan_path.is_file() and any(requested(i) for i in editor.read_json(plan_path).get('clips', []))
    if found or declared:
        if plan_path is None or not plan_path.is_file():
            raise ValueError('Native mix preview requires its formal edit-plan.json')
        plan, _, _ = editor.validate_plan(plan_path)
        for item in plan['clips']:
            if requested(item):
                resource(item['mix_mode']['name'], install)
        editor.verify_structure(content, plan)
