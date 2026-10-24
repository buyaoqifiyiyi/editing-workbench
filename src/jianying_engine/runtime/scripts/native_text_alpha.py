"""Text-specific global-opacity keyframes; video alpha serialization is unchanged."""
import json
from pathlib import Path
import file_hash

PROPERTY = 'KFTypeGlobalAlpha'


def runtime(install=None):
    import editor
    spec = json.loads((Path(__file__).resolve().parents[1] / 'assets/text-alpha-v1.json').read_text(encoding='utf-8'))
    install = Path(install) if install is not None else editor.jianying_install()
    if not install or install.name != spec['runtime_version']:
        raise ValueError('Text alpha requires measured Jianying ' + spec['runtime_version'])
    files = dict(spec['runtime_sha256']); files[spec['font_relative']] = spec['font_sha256']
    for relative, digest in files.items():
        if not file_hash.matches(install / relative, digest):
            raise ValueError('Text alpha runtime/font identity changed: ' + relative)


def requested(item):
    return any(k['property'] == 'alpha' for k in item.get('keyframes', []))


def validate(item, plan):
    import editor
    keys = sorted(item['keyframes'], key=lambda k: k['time'])
    if (len(keys) not in (2, 4) or any(k['property'] != 'alpha' for k in keys) or keys[0]['time'] != 0 or
            editor.us(keys[-1]['time']) >= editor.us(item['duration'])):
        raise ValueError('Text alpha requires two or four pure alpha keys, first at 0 and last strictly before text end')
    if len(keys) == 4 and [k['value'] for k in keys] != [0, 1, 1, 0]:
        raise ValueError('Four-point text alpha requires fade-in/hold/fade-out values 0,1,1,0')
    if (plan.get('composites') or item.get('style') != {'auto_wrapping': False} or item['color'].upper() != '#FFFFFF' or
            item['bold'] or item.get('transform') or any(item.get(k) for k in ('animations', 'font', 'glow', 'text_curve', 'motion_easing')) or
            any(ord(c) > 0xffff or 0xd800 <= ord(c) <= 0xdfff or c in '\r\n' for c in item['text'])):
        raise ValueError('Text alpha requires static single-line BMP white default-font text, auto_wrapping=false and no other motion/effects/composites')
    if editor.default_text_font() is None:
        raise ValueError('Text alpha requires the installed default font')
    runtime()


def adapt(segment, item):
    if requested(item):
        groups = [g for g in segment['common_keyframes'] if g['property_type'] == 'KFTypeAlpha']
        if len(groups) != 1:
            raise ValueError('Expected exactly one backend alpha group before text adaptation')
        groups[0]['property_type'] = PROPERTY


def verify(content, plan):
    import editor
    if any(g['property_type'] == PROPERTY for t in content['tracks'] if t['type'] != 'text'
           for s in t['segments'] for g in s.get('common_keyframes', [])):
        raise ValueError('KFTypeGlobalAlpha is admitted for text only')
    segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    materials = {m['id']: m for m in content['materials'].get('texts', [])}
    count = 0
    for item in plan['captions']:
        segment = segments[item['track'], editor.us(item['start'])]
        groups = [g for g in segment.get('common_keyframes', []) if g['property_type'] in (PROPERTY, 'KFTypeAlpha', 'KFTypeTextAlpha')]
        if not requested(item):
            if groups:
                raise ValueError('Unrequested native text alpha group')
            continue
        if len(groups) != 1 or groups[0]['property_type'] != PROPERTY:
            raise ValueError('Text alpha requires its unique KFTypeGlobalAlpha group')
        points = groups[0]['keyframe_list']
        keys = sorted(item['keyframes'], key=lambda k: k['time'])
        if (len(points) != len(keys) or [(p['time_offset'], p['values']) for p in points] !=
                [(editor.us(k['time']), [k['value']]) for k in keys] or any(
                    type(p['time_offset']) is not int or len(p['values']) != 1 or type(p['values'][0]) not in (int, float) for p in points)):
            raise ValueError('Text alpha local clock/value changed')
        material = materials[segment['material_id']]
        if (material.get('global_alpha') != 1 or type(material.get('global_alpha')) not in (int, float) or
                segment['clip'].get('alpha') != 1 or type(segment['clip'].get('alpha')) not in (int, float)):
            raise ValueError('Text alpha requires static material/clip alpha=1')
        count += 1
    if count:
        runtime()
    return count


def preview_check(content, root, install=None):
    import editor
    def contains(value):
        if isinstance(value, dict):
            return value.get('property_type') == PROPERTY or any(contains(v) for v in value.values())
        return isinstance(value, list) and any(contains(v) for v in value)
    path = Path(root) / 'edit-plan.json'
    text_keys = any(g['property_type'] in (PROPERTY, 'KFTypeAlpha', 'KFTypeTextAlpha')
                    for t in content.get('tracks', []) if t['type'] == 'text'
                    for s in t['segments'] for g in s.get('common_keyframes', []))
    declared = path.is_file() and any(requested(item) for item in editor.read_json(path).get('captions', []))
    if contains(content) or text_keys or declared:
        runtime(install)
        if not path.is_file():
            raise ValueError('Text alpha preview requires its formal edit-plan.json')
        plan, _, _ = editor.validate_plan(path)
        editor.verify_structure(content, plan)
