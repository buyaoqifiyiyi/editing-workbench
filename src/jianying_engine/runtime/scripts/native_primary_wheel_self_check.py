"""Synthetic, isolated primary-wheel plan/structure/publish checks; does not start Jianying."""
import argparse
import copy
from contextlib import ExitStack
import hashlib
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace as Args
from unittest.mock import patch

from PIL import Image
import editor
import migrate
import native_primary_wheel as wheel


def refused(action):
    try:
        action()
    except (ValueError, TypeError, KeyError, OSError):
        return
    raise AssertionError('Expected refusal')


def check(out):
    root = Path(out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    install = root / '11.7.0.14652'
    resource = install / 'Resources/wheel'
    resource.mkdir(parents=True)
    (install / 'fixture.bin').write_bytes(b'SYNTHETIC REFERENCE ONLY')
    (resource / 'resource.bin').write_bytes(b'SYNTHETIC RESOURCE ONLY')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    contract = root / 'fixture-contract.json'
    editor.write_json(contract, {'runtime_version': install.name,
        'runtime_sha256': {'fixture.bin': sha(install / 'fixture.bin')}, 'resource': 'Resources/wheel',
        'source_files': {'resource.bin': sha(resource / 'resource.bin')}})
    source = root / 'source.png'
    Image.new('RGB', (640, 360), (120, 90, 60)).save(source)
    archive = root / 'archive'
    archive.mkdir()
    checks = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(wheel, 'CONTRACT', contract))
        stack.enter_context(patch.object(editor, 'jianying_install', return_value=install))
        stack.enter_context(patch.object(editor, 'configured_user_data', return_value=None))
        stack.enter_context(patch.object(editor, 'default_text_font', return_value=None))
        stack.enter_context(patch.object(editor, 'draft_location', return_value={
            'active_draft_root': None, 'homepage_index': str(root / 'unused-index.json')}))
        base = {'schema_version': 1, 'name': 'wheel', 'mode': 'script',
                'canvas': {'width': 640, 'height': 360, 'fps': 30},
                'clips': [{'path': str(source), 'start': 0, 'duration': 2}]}
        for axis, (channel, value) in wheel.CONTROLS.items():
            for amount in (0, value):
                plan = copy.deepcopy(base)
                plan['name'] = axis + str(amount)
                plan['clips'][0]['grading'] = {'primary_wheel': {axis: {channel: amount}}}
                path = root / (plan['name'] + '.json')
                editor.write_json(path, plan)
                normalized, _, _ = editor.validate_plan(path)
                draft = Path(editor.build_draft(Args(plan=path, out=root / (plan['name'] + '-draft')))['draft'])
                content = editor.read_json(draft / 'draft_content.json')
                assert wheel.verify(content, normalized) == 1
                assert f'{axis}/{channel}' in (draft / '剪辑交付.md').read_text(encoding='utf-8')
                wheel.preview_check(content, draft, install)
                refused(lambda: wheel.preview_check(content, root, install))
                refused(lambda: wheel.preview_check(content, draft, root / '11.6.0.14552'))
                published = Path(editor.publish_draft(Args(draft=draft, draft_root=archive, offline_copy=True))['draft'])
                assert wheel.verify(editor.read_json(published / 'draft_content.json'), normalized) == 1
                checks.append(plan['name'] + ': validate/build/structure/preview gate/offline publish')
        moved = root / 'moved'
        moved.mkdir()
        shutil.copy2(source, moved / source.name)
        mapped, changes, missing = migrate.remap_plan(draft / 'edit-plan.json',
            migrate.mappings([str(root) + '=' + str(moved)]))
        assert not missing and len(changes) == 1
        editor.write_json(root / 'mapped.json', mapped)
        editor.build_draft(Args(plan=root / 'mapped.json', out=root / 'mapped-draft'))
        checks.append('migration and rebuild preserve primary wheel')
        initial = editor.read_json(draft / 'draft_content.json')
        wheel_id = initial['materials']['primary_color_wheels'][0]['id']
        def segment(c): return c['tracks'][0]['segments'][0]
        changes = [lambda c: c['materials']['primary_color_wheels'][0]['lift'].update(red=True),
                   lambda c: c['materials']['primary_color_wheels'][0].update(source_platform=True),
                   lambda c: c['materials']['primary_color_wheels'][0].update(path='bad'),
                   lambda c: c['materials']['primary_color_wheels'].append(copy.deepcopy(c['materials']['primary_color_wheels'][0])),
                   lambda c: c['materials']['videos'][0].update(id=wheel_id),
                   lambda c: c['materials']['effects'].append({'id':'orphan', 'type':'video_effect'}),
                   lambda c: c['materials']['effects'].append(copy.deepcopy(c['materials']['speeds'][0])),
                   lambda c: c['materials']['videos'].append(copy.deepcopy(c['materials']['videos'][0])),
                   lambda c: c['materials']['drafts'].append({'materials': {'primary_color_wheels': [{'id':'nested'}]}}),
                   lambda c: segment(c)['extra_material_refs'].remove(wheel_id),
                   lambda c: segment(c).update(enable_color_wheels=False),
                   lambda c: segment(c)['clip'].update(alpha=.5),
                   lambda c: segment(c)['source_timerange'].update(start=1),
                   lambda c: c['materials']['videos'][0]['crop'].update(upper_left_x=.1)]
        for mutate in changes:
            altered = copy.deepcopy(initial)
            mutate(altered)
            refused(lambda: editor.verify_structure(altered, normalized))
            refused(lambda: wheel.preview_check(altered, draft, install))
        legacy = copy.deepcopy(normalized)
        del legacy['clips'][0]['grading']
        refused(lambda: wheel.verify(initial, legacy))
        for selection in ({}, {'lift': {'red': True}}, {'lift': {'red': .1}}, {'gain': {'red': .5}},
                          {'offset': {'red': .5}}, {'lift': {'red': .5}, 'gamma': {'green': -.5}}):
            bad = copy.deepcopy(plan)
            bad['clips'][0]['grading']['primary_wheel'] = selection
            refused(lambda: wheel.validate(bad))
        corrupted = bytearray(source.read_bytes())
        offset = 8
        while offset < len(corrupted):
            size = struct.unpack('>I', corrupted[offset:offset + 4])[0]
            if corrupted[offset + 4:offset + 8] == b'IDAT':
                corrupted[offset + 8 + size // 2] ^= 0xff
                break
            offset += size + 12
        else:
            raise AssertionError('Synthetic PNG lacks IDAT')
        (root / 'corrupt.png').write_bytes(corrupted)
        Image.new('RGB', (640, 360), 'red').save(root / 'animated.png', save_all=True,
            append_images=[Image.new('RGB', (640, 360), 'blue')], duration=[500, 500], loop=0)
        for name in ('corrupt.png', 'animated.png'):
            bad = copy.deepcopy(plan)
            bad['clips'][0]['path'] = str(root / name)
            bad_path = root / (name + '.json')
            editor.write_json(bad_path, bad)
            refused(lambda: editor.validate_plan(bad_path))
        (resource / 'extra').write_bytes(b'wrong')
        refused(lambda: wheel.runtime(install))
        checks.append(f'{len(changes)} structure/preview mutations, corrupt/animated PNG and invalid plans/resources refused')
    report = {'status': 'passed', 'checks': checks, 'evidence': 'synthetic fixture only',
              'native_ui': 'not_run', 'admission': 'not_written'}
    editor.write_json(root / 'primary-wheel-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out))
