"""Opt-in native Chroma2 binding to an authenticated, unmodified installation."""
import hashlib
import json
from pathlib import Path

MODE = 'installed_chroma2_v1'


def contract():
    return json.loads((Path(__file__).resolve().parents[1] / 'assets/chroma2-v1.json').read_text(encoding='utf-8'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError('Native resource requires a regular directory')
    files = {}
    def visit(directory):
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError('Native resource contains a link')
            if path.is_dir():
                visit(path)
            elif path.is_file():
                files[path.relative_to(root).as_posix()] = sha(path)
            else:
                raise ValueError('Unsupported native resource entry')
    visit(root)
    return files


def resource():
    import editor
    spec = contract()
    install = editor.jianying_install()
    if not install or install.name != spec['runtime_version']:
        raise ValueError('installed_chroma2_v1 requires measured Jianying ' + spec['runtime_version'])
    if any(p.is_symlink() or p.is_junction() for p in (install, install / 'Resources')):
        raise ValueError('Chroma2 installation must not be redirected through links')
    for relative, expected in spec['runtime_sha256'].items():
        path = install / relative
        if not path.is_file() or path.is_symlink() or sha(path) != expected:
            raise ValueError('Chroma2 runtime identity changed: ' + str(path))
    path = install / spec['resource']
    if tree(path) != spec['source_files']:
        raise ValueError('Chroma2 resource identity changed or files are missing/added')
    return str(path)


def requested(item):
    return isinstance(item.get('chroma'), dict) and item['chroma'].get('rendering') == MODE


def validate_spec(item, plan):
    import editor
    spec = item['chroma']
    editor.fields(spec, {'rendering', 'color', 'intensity', 'shadow', 'edge_smooth', 'spill'}, 'chroma', ('rendering', 'color'))
    if spec['rendering'] != MODE:
        raise ValueError('chroma rendering requires explicit installed_chroma2_v1; legacy parameter-only mode is not admitted')
    if not isinstance(spec['color'], str) or spec['color'].lower() not in ('#00ff00ff', '#0000ffff'):
        raise ValueError('Measured chroma color requires opaque green #00FF00FF or blue #0000FFFF')
    spec['color'] = spec['color'].lower()
    for key, default in (('intensity', 35), ('shadow', 0), ('edge_smooth', 0), ('spill', 0)):
        editor.number(spec.setdefault(key, default), 'chroma.' + key, 0, 100)
        if key != 'intensity' and spec[key] != 0:
            raise ValueError('Measured chroma requires shadow/edge_smooth/spill=0')
    if (plan.get('composites') or plan.get('effects') or plan.get('filters') or
            plan['canvas'] != {'width': 1280, 'height': 720, 'fps': 30} or item['track'] == 'video' or
            item['fit'] != 'contain' or item['speed'] != 1 or item['transform'] or
            any(item.get(k) for k in ('keyframes', 'motion_easing', 'animations', 'effects', 'filters', 'grading',
                                     'mask', 'matting_cache', 'background', 'transition', 'mix_mode')) or 'speed_curve' in item):
        raise ValueError('Measured chroma requires a standalone 1280x720/30fps contain speed=1 overlay without motion/effects/composites')
    # An outgoing transition on the previous clip is also an incoming effect here.
    if any(x is not item and x.get('transition') and x['track'] == item['track'] and
           abs(editor.end_us(x) - editor.us(item['start'])) <= 1 for x in plan['clips']):
        raise ValueError('Measured chroma does not admit an incoming transition')


def validate_media(item, info):
    if (info['width'], info['height']) != (1280, 720) or (info['kind'] == 'image' and Path(item['path']).suffix.lower() != '.png'):
        raise ValueError('Measured native source requires 1280x720 PNG or silent 30fps video')
    if info['kind'] == 'video' and (info['has_audio'] or abs(info['fps'] - 30) > 1e-6):
        raise ValueError('Measured native video requires no audio stream and 30fps')


def selected(content, plan):
    materials = content['materials'].get('chromas', [])
    by_id = {m['id']: m for m in materials}
    if len(by_id) != len(materials):
        raise ValueError('Duplicate chroma material ID')
    segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    used = set()
    for item in plan['clips']:
        segment = segments[item['track'], round(item['start'] * 1e6)]
        refs = [r for r in segment['extra_material_refs'] if r in by_id]
        if len(refs) != int(requested(item)) or any(r in used for r in refs):
            raise ValueError('Chroma binding must be unique and match its planned segment')
        if refs:
            used.add(refs[0])
            sources = [m for m in content['materials'].get('videos', []) if m['id'] == segment['material_id']]
            if len(sources) != 1 or (sources[0].get('width'), sources[0].get('height')) != (1280, 720):
                raise ValueError('Chroma native source dimensions changed')
            yield by_id[refs[0]], item
    if used != set(by_id):
        raise ValueError('Orphaned chroma material')


def bind(content, plan):
    if any(requested(item) for item in plan['clips']):
        path = resource()
        for material, _ in selected(content, plan):
            material.update(path=path, resource_id='')


def verify(content, plan):
    pairs = list(selected(content, plan))
    path = resource() if pairs else None
    for material, item in pairs:
        expected = {'id': material['id'], 'type': 'chroma', 'version': 'v2', 'should_transfer_color': True,
                    'path': path, 'resource_id': '', 'color': item['chroma']['color']}
        expected.update({k + '_value': item['chroma'][k] / 100 for k in ('intensity', 'shadow', 'edge_smooth', 'spill')})
        if material != expected or material.get('should_transfer_color') is not True or any(
                type(material.get(k + '_value')) not in (int, float) for k in ('intensity', 'shadow', 'edge_smooth', 'spill')):
            raise ValueError('Chroma native path, identity, parameters or configuration changed')
    return len(pairs)


def preview_check(content):
    def contains_chroma(value):
        if isinstance(value, dict):
            return bool(value.get('chromas')) or any(contains_chroma(v) for v in value.values())
        return isinstance(value, list) and any(contains_chroma(v) for v in value)
    if contains_chroma(content.get('materials', {}).get('drafts', [])):
        raise ValueError('Native preview refuses unadmitted nested chroma')
    for material in content.get('materials', {}).get('chromas', []):
        if material.get('path') != resource() or material.get('version') != 'v2' or material.get('resource_id') != '':
            raise ValueError('Native preview refuses an unadmitted or missing chroma resource')
