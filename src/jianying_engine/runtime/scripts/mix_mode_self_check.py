"""Formal measured mix binding, publication and refusal checks, no UI."""
import argparse
import copy
from pathlib import Path
import shutil
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor
import native_mix_mode as mix


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    for name, color in (('base', (32, 80, 160)), ('overlay', (128, 64, 32))):
        Image.new('RGB', (1280, 720), color).save(root / (name + '.png'))
    plan = {'schema_version': 1, 'name': '混合模式检查', 'mode': 'script',
            'canvas': {'width': 1280, 'height': 720, 'fps': 30},
            'clips': [{'path': str(root / 'base.png'), 'start': 0, 'duration': 2},
                      {'path': str(root / 'overlay.png'), 'start': 0, 'duration': 2, 'track': 'overlay',
                       'mix_mode': {'rendering': mix.MODE, 'name': '滤色'}}], 'layer_order': ['video', 'overlay']}
    path = root / 'plan.json'; editor.write_json(path, plan); frozen = path.read_bytes()
    bundles = {name: (Path(mix.resource(name)), mix.tree(mix.resource(name))) for name in mix.contract()['modes']}
    normalized, _, _ = editor.validate_plan(path)
    result = editor.build_draft(Args(plan=str(path), out=str(root / 'draft')))
    assert result['verification']['native_effects']['installed_mix'] == 1
    content = editor.read_json(root / 'draft/draft_content.json')
    (root / 'archive').mkdir()
    published = editor.publish_draft(Args(draft=str(root / 'draft'), draft_root=str(root / 'archive'), offline_copy=True, user_data=None))
    editor.verify_structure(editor.read_json(Path(published['draft']) / 'draft_content.json'), normalized)
    cases = ['formal validate/build/verify/offline publish with installed native mix binding']
    video = root / 'silent-video.mp4'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-f', 'lavfi',
                '-i', 'color=c=red:s=1280x720:r=30:d=2', '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', video])
    def reject(label, callback):
        try:
            callback()
        except (ValueError, TypeError):
            cases.append(label)
        else:
            raise AssertionError('Expected refusal: ' + label)
    def invalid(label, mutate):
        candidate = copy.deepcopy(plan); mutate(candidate)
        editor.write_json(root / 'invalid.json', candidate)
        reject(label, lambda: editor.validate_plan(root / 'invalid.json'))
    for value in ('滤色', None, [], {}, {'name': '滤色'}, {'rendering': mix.MODE, 'name': '不存在的混合模式'},
                  {'rendering': 'unknown', 'name': '滤色'}, {'rendering': mix.MODE, 'name': '滤色', 'path': 'fake'},
                  {'rendering': mix.MODE, 'name': '滤色', 'resource_id': 'fake'}):
        invalid('invalid mix specification ' + str(value), lambda p, v=value: p['clips'][1].update(mix_mode=v))
    for key, value in [('fit', 'cover'), ('speed', 2), ('transform', {'alpha': .5}),
                       ('mask', {'name': '矩形', 'experimental': True}), ('chroma', {'color': '#00FF00FF'}),
                       ('keyframes', [{'property': 'position_x', 'time': 0, 'value': 0}]),
                       ('transition', {'name': '叠化', 'duration': .1})]:
        invalid('unsupported overlay ' + key, lambda p, k=key, v=value: p['clips'][1].update({k: v}))
    invalid('base transform', lambda p: p['clips'][0].update(transform={'alpha': .5}))
    invalid('base transition', lambda p: p['clips'][0].update(transition={'name': '叠化', 'duration': .1}))
    invalid('canvas outside subset', lambda p: p['canvas'].update(width=640))
    invalid('wrong layer order', lambda p: p.update(layer_order=['overlay', 'video']))
    invalid('missing explicit layer order', lambda p: p.pop('layer_order'))
    invalid('mixed base track', lambda p: p['clips'][0].update(mix_mode=p['clips'][1].pop('mix_mode')))
    for label, mutate in [
        ('path', lambda c: c['materials']['effects'][0].update(path='missing')),
        ('id', lambda c: c['materials']['effects'][0].update(resource_id='fake')),
        ('effect id', lambda c: c['materials']['effects'][0].update(effect_id='fake')),
        ('boolean value', lambda c: c['materials']['effects'][0].update(value=True)),
        ('boolean target', lambda c: c['materials']['effects'][0].update(apply_target_type=False)),
        ('extra field', lambda c: c['materials']['effects'][0].update(extra=True)),
        ('source size', lambda c: c['materials']['videos'][1].update(width=640)),
        ('alpha outside subset', lambda c: c['tracks'][1]['segments'][0]['clip'].update(alpha=.3)),
        ('canvas outside subset', lambda c: c['canvas_config'].update(width=640)),
        ('duplicate material', lambda c: c['materials']['effects'].append(copy.deepcopy(c['materials']['effects'][0]))),
        ('duplicate ref', lambda c: c['tracks'][1]['segments'][0]['extra_material_refs'].append(c['materials']['effects'][0]['id'])),
        ('wrong ref', lambda c: c['tracks'][0]['segments'][0]['extra_material_refs'].append(c['materials']['effects'][0]['id'])),
        ('missing ref', lambda c: c['tracks'][1]['segments'][0]['extra_material_refs'].remove(c['materials']['effects'][0]['id'])),
        ('wrong slot', lambda c: c['materials'].update(mix_modes=c['materials']['effects']))]:
        bad = copy.deepcopy(content); mutate(bad)
        reject('native tamper ' + label, lambda: editor.verify_structure(bad, normalized))
    nested = {'materials': {'drafts': [{'draft': content}]}}
    reject('nested preview mix', lambda: mix.preview_check(nested))
    bad = copy.deepcopy(content); bad['materials']['effects'][0].pop('path')
    reject('missing path before preview', lambda: mix.preview_check(bad))
    for label, mutate in [('missing ref', lambda c: c['tracks'][1]['segments'][0]['extra_material_refs'].remove(c['materials']['effects'][0]['id'])),
                          ('alpha', lambda c: c['tracks'][1]['segments'][0]['clip'].update(alpha=.3)),
                          ('canvas', lambda c: c['canvas_config'].update(width=640)),
                          ('boolean material', lambda c: c['materials']['effects'][0].update(value=True))]:
        bad = copy.deepcopy(content); mutate(bad)
        reject('preview ' + label, lambda: mix.preview_check(bad, root / 'draft'))
    install = root / 'mock' / mix.contract()['runtime_version']
    index = install / 'Resources/MixMode/MixMode.json'; index.parent.mkdir(parents=True)
    shutil.copyfile(editor.jianying_install() / 'Resources/MixMode/MixMode.json', index)
    for name, (bundle, _) in bundles.items():
        shutil.copytree(bundle, install / mix.contract()['modes'][name]['resource'])
    spec = mix.contract(); spec['runtime_sha256'] = {}
    with patch.object(editor, 'jianying_install', return_value=install), patch.object(mix, 'contract', return_value=spec):
        for name in bundles:
            root_bundle = Path(mix.resource(name))
            for relative in bundles[name][1]:
                file = root_bundle / relative; before = file.read_bytes(); file.write_bytes(before + b'\n')
                reject('modified ' + name + '/' + relative, lambda: mix.resource(name)); file.write_bytes(before)
        extra = root_bundle / 'extra.lua'; extra.write_text('extra')
        reject('added resource', lambda: mix.resource(name)); extra.unlink()
        file = root_bundle / 'config.json'; before = file.read_bytes(); file.unlink()
        reject('missing resource', lambda: mix.resource(name)); file.write_bytes(before)
        before = index.read_bytes(); index.write_bytes(before + b'\n')
        reject('mapping changed', lambda: mix.resource(name)); index.write_bytes(before)
    spec = mix.contract(); spec['runtime_sha256'] = {'missing.node': '0' * 64}
    with patch.object(mix, 'contract', return_value=spec):
        reject('runtime changed', lambda: mix.resource('滤色'))
    with patch.object(editor, 'jianying_install', return_value=root):
        reject('version changed', lambda: mix.resource('滤色'))
    for name in bundles:
        candidate = copy.deepcopy(plan); candidate['name'] += '-' + name
        candidate['clips'][1]['mix_mode']['name'] = name
        mode_path = root / (name + '-plan.json'); editor.write_json(mode_path, candidate); before = mode_path.read_bytes()
        p, _, _ = editor.validate_plan(mode_path)
        built = editor.build_draft(Args(plan=str(mode_path), out=str(root / (name + '-draft'))))
        c = editor.read_json(Path(built['draft']) / 'draft_content.json')
        material = next(m for m in c['materials']['effects'] if m['type'] == 'mix_mode')
        assert material == dict(mix.contract()['modes'][name]['material'], id=material['id'], path=mix.resource(name))
        pub = editor.publish_draft(Args(draft=built['draft'], draft_root=str(root / 'archive'), offline_copy=True, user_data=None))
        editor.verify_structure(editor.read_json(Path(pub['draft']) / 'draft_content.json'), p)
        mix.preview_check(c, Path(built['draft']), editor.jianying_install())
        assert mode_path.read_bytes() == before
        cases.append('formal validate/build/verify/preview/publish ' + name)
        video_plan = copy.deepcopy(candidate); video_plan['name'] += '-video'; video_plan['clips'][1]['path'] = str(video)
        video_path = root / (name + '-video-plan.json'); editor.write_json(video_path, video_plan)
        if name not in ('滤色', '正片叠底', '叠加', '柔光'):
            reject('unverified video overlay ' + name, lambda: editor.validate_plan(video_path))
        else:
            editor.validate_plan(video_path); cases.append('existing video support retained ' + name)
        reject('actual preview install version ' + name, lambda: mix.preview_check(c, Path(built['draft']), root / '11.5.0.14471'))
        reject('actual preview install files ' + name, lambda: mix.preview_check(c, Path(built['draft']), root / '11.6.0.14552'))
        bad = copy.deepcopy(c); bad['materials']['effects'] = []
        reject('missing all declared preview mix ' + name, lambda: mix.preview_check(bad, Path(built['draft'])))
        bad = copy.deepcopy(c); names = list(bundles); other = names[(names.index(name) + 1) % len(names)]
        target = next(m for m in bad['materials']['effects'] if m['type'] == 'mix_mode')
        target.update(mix.contract()['modes'][other]['material'], path=mix.resource(other))
        reject('swapped mode readback ' + name, lambda: editor.verify_structure(bad, p))
        reject('swapped mode preview ' + name, lambda: mix.preview_check(bad, Path(built['draft'])))
    assert path.read_bytes() == frozen and all(mix.tree(p) == files for p, files in bundles.values())
    report = {'status': 'passed', 'check_count': len(cases), 'checks': cases, 'source_plan_and_install_unchanged': True,
              'UI_save_reopen_export': 'unverified'}
    editor.write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); print(editor.json.dumps(check(args.out.resolve()), ensure_ascii=False, indent=2))
