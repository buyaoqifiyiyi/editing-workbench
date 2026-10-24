"""Formal native chroma build/publish, identity and refusal regression; no UI."""
import argparse
import copy
from pathlib import Path
import shutil
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor
import native_chroma as chroma


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    Image.new('RGB', (1280, 720), (20, 40, 90)).save(root / 'background.png')
    Image.new('RGB', (1280, 720), (0, 255, 0)).save(root / 'green.png')
    plan = {'schema_version': 1, 'name': '原生色键检查', 'mode': 'script',
            'canvas': {'width': 1280, 'height': 720, 'fps': 30},
            'clips': [{'path': str(root / 'background.png'), 'start': 0, 'duration': 2},
                      {'path': str(root / 'green.png'), 'start': 0, 'duration': 2, 'track': 'keyed',
                       'chroma': {'rendering': chroma.MODE, 'color': '#00FF00FF'}}],
            'layer_order': ['video', 'keyed']}
    path = root / 'plan.json'; editor.write_json(path, plan)
    frozen = path.read_bytes()
    source = Path(chroma.resource()); original = chroma.tree(source)
    normalized, _, _ = editor.validate_plan(path)
    result = editor.build_draft(Args(plan=str(path), out=str(root / 'draft')))
    assert result['verification']['native_effects']['installed_chroma2'] == 1
    content = editor.read_json(root / 'draft/draft_content.json')
    assert content['materials']['chromas'][0]['path'] == str(source)
    (root / 'archive').mkdir()
    published = editor.publish_draft(Args(draft=str(root / 'draft'), draft_root=str(root / 'archive'),
                                         offline_copy=True, user_data=None))
    editor.verify_structure(editor.read_json(Path(published['draft']) / 'draft_content.json'), normalized)
    cases = ['formal validate/build/verify/publish and installed resource binding']
    def rejects(label, callback):
        try:
            callback()
        except (ValueError, TypeError):
            cases.append(label)
        else:
            raise AssertionError('Expected rejection: ' + label)
    def invalid(label, mutate):
        candidate = copy.deepcopy(plan); mutate(candidate)
        editor.write_json(root / 'invalid.json', candidate)
        rejects(label, lambda: editor.validate_plan(root / 'invalid.json'))
    def field(key, value):
        return lambda p: p['clips'][1]['chroma'].update({key: value})
    invalid('legacy missing explicit mode', lambda p: p['clips'][1]['chroma'].pop('rendering'))
    invalid('not an object', lambda p: p['clips'][1].update(chroma=[]))
    for key, value in [('rendering', 'unknown'), ('path', str(source)), ('resource_id', 'fake'),
                       ('version', 'v2'), ('keyframes', []), ('color', '#00FF0080'), ('color', '#FF0000FF'),
                       ('intensity', True), ('intensity', float('nan')), ('intensity', float('inf')),
                       ('intensity', -1), ('intensity', 101), ('shadow', 1), ('edge_smooth', 1), ('spill', 1)]:
        invalid('invalid ' + key + '=' + str(value), field(key, value))
    for key, value in [('speed', 2), ('fit', 'cover'), ('transform', {'alpha': .5}),
                       ('mask', {'name': '矩形', 'experimental': True}), ('background', {'type': 'color', 'color': '#000000FF'}),
                       ('transition', {'name': '叠化', 'duration': .1}), ('keyframes', [{'property': 'position_x', 'time': 0, 'value': 0}])]:
        invalid('unsupported combination ' + key, lambda p, k=key, v=value: p['clips'][1].update({k: v}))
    invalid('canvas subset', lambda p: p['canvas'].update(width=640))
    invalid('base track chroma', lambda p: p['clips'][0].update(chroma=p['clips'][1].pop('chroma')))
    def incoming(p):
        p['clips'][1].update(start=.5, duration=1.5)
        p['clips'].append({'path': str(root / 'green.png'), 'start': 0, 'duration': .499999,
                          'track': 'keyed', 'transition': {'name': '叠化', 'duration': .1}})
    invalid('incoming transition with one microsecond gap', incoming)
    for label, mutate in [
        ('changed path', lambda c: c['materials']['chromas'][0].update(path='missing')),
        ('changed version', lambda c: c['materials']['chromas'][0].update(version='v1')),
        ('changed resource ID', lambda c: c['materials']['chromas'][0].update(resource_id='fake')),
        ('changed transfer flag', lambda c: c['materials']['chromas'][0].update(should_transfer_color=1)),
        ('changed source dimensions', lambda c: c['materials']['videos'][1].update(width=640, height=360)),
        ('boolean scalar', lambda c: c['materials']['chromas'][0].update(shadow_value=False)),
        ('extra configuration', lambda c: c['materials']['chromas'][0].update(extra=1)),
        ('duplicate material', lambda c: c['materials']['chromas'].append(copy.deepcopy(c['materials']['chromas'][0]))),
        ('missing reference', lambda c: c['tracks'][1]['segments'][0]['extra_material_refs'].remove(c['materials']['chromas'][0]['id'])),
        ('duplicate reference', lambda c: c['tracks'][1]['segments'][0]['extra_material_refs'].append(c['materials']['chromas'][0]['id'])),
        ('wrong segment', lambda c: c['tracks'][0]['segments'][0]['extra_material_refs'].append(c['materials']['chromas'][0]['id']))]:
        bad = copy.deepcopy(content); mutate(bad)
        rejects(label, lambda: editor.verify_structure(bad, normalized))
    # Only mutate an owned copy; production installation and cache are never written.
    install = root / 'mock' / chroma.contract()['runtime_version']
    bundle = install / chroma.contract()['resource']; shutil.copytree(source, bundle)
    spec = chroma.contract(); spec['runtime_sha256'] = {}
    with patch.object(editor, 'jianying_install', return_value=install), patch.object(chroma, 'contract', return_value=spec):
        assert chroma.resource() == str(bundle)
        for relative in original:
            file = bundle / relative; before = file.read_bytes()
            file.write_bytes(before + b'\n')
            rejects('modified companion ' + relative, chroma.resource)
            file.write_bytes(before)
        extra = bundle / 'unexpected.lua'; extra.write_text('unexpected')
        rejects('extra script', chroma.resource); extra.unlink()
        file = bundle / 'config.json'; backup = file.read_bytes(); file.unlink()
        rejects('missing config', chroma.resource); file.write_bytes(backup)
    spec = chroma.contract(); spec['runtime_sha256'] = {'missing.node': '0' * 64}
    with patch.object(chroma, 'contract', return_value=spec):
        rejects('runtime missing or changed', chroma.resource)
    with patch.object(editor, 'jianying_install', return_value=root):
        rejects('runtime version changed', chroma.resource)
    nested = {'materials': {'drafts': [{'draft': {'materials': {'chromas': [{'type': 'chroma'}]}}}]}}
    rejects('nested chroma preview', lambda: chroma.preview_check(nested))
    assert chroma.tree(source) == original and path.read_bytes() == frozen
    report = {'status': 'passed', 'checks': cases, 'check_count': len(cases),
              'source_plan_unchanged': True, 'installed_bundle_unchanged': True,
              'UI_save_reopen_export': 'unverified'}
    editor.write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(editor.json.dumps(check(args.out.resolve()), ensure_ascii=False, indent=2))
