"""Regression checks for missing legacy motion, explicit time semantics and safe source lookup."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile
import sys
from types import SimpleNamespace

import template_borrow as borrow


def rejects(function, *args):
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError('Expected a rejected unsupported or inconsistent input')


def main():
    backend = borrow.editor.load_backend()
    item = {'text': '文字引用回归', 'size': 12, 'color': '#FFFFFF', 'bold': False, 'border': True,
            'x': 0, 'y': 0, 'style': {}, 'transform': {}, 'keyframes': []}
    text_segment = borrow.editor.text_segment(item, backend.Timerange(0, 1000000), backend)
    assert text_segment.speed.global_id not in text_segment.extra_material_refs
    assert text_segment.export_json()['extra_material_refs'] == []
    segment = {'target_timerange': {'start': 3000000, 'duration': 2000000},
               'source_timerange': {'start': 10000000, 'duration': 4000000}, 'speed': 2,
               'keyframe_refs': ['a', 'b']}
    keys = {'a': {'flags': 1, 'time_offset': 10000000, 'position': {'x': -0.5, 'y': 0}},
            'b': {'flags': 2, 'time_offset': 14000000, 'scale': {'x': 1.2, 'y': 1.3}}}
    result = borrow.motion(segment, keys, 'source_absolute', 4)
    assert result == [{'property': 'position_x', 'time': 0.0, 'value': -0.5},
                      {'property': 'position_y', 'time': 0.0, 'value': 0},
                      {'property': 'scale_x', 'time': 4.0, 'value': 1.2},
                      {'property': 'scale_y', 'time': 4.0, 'value': 1.3}]
    rejects(borrow.motion, segment, keys, 'local', 4)
    rejects(borrow.motion, segment, {'a': keys['a']}, 'source_absolute', 4)
    duplicate = copy.deepcopy(segment)
    duplicate['keyframe_refs'] = ['a', 'a']
    rejects(borrow.motion, duplicate, keys, 'source_absolute', 4)
    modern = copy.deepcopy(segment)
    modern['keyframe_refs'] = []
    modern['common_keyframes'] = [{'property_type': 'KFTypeRotation', 'keyframe_list':
                                  [{'time_offset': 12000000, 'values': [90]}]}]
    assert borrow.motion(modern, {}, 'source_absolute', 6) == [
        {'property': 'rotation', 'time': 3.0, 'value': 90}]
    modern['common_keyframes'][0]['property_type'] = 'KFTypeVolume'
    rejects(borrow.motion, modern, {}, 'source_absolute', 6)
    alpha_segment = copy.deepcopy(segment)
    alpha_segment['keyframe_refs'] = ['a']
    rejects(borrow.motion, alpha_segment, {'a': {'flags': 8, 'time_offset': 10000000}},
            'source_absolute', 6)
    curve_keys = copy.deepcopy(keys)
    curve_keys['a']['graph'] = {'curve': 'unknown'}
    rejects(borrow.motion, segment, curve_keys, 'source_absolute', 6)
    materials = {'a': {'category': 'videos', 'data': {'id': 'a', 'nested_id': 'b'}},
                 'b': {'category': 'speeds', 'data': {'id': 'b', 'nested_id': 'a'}}}
    deps, missing = borrow.dependencies({'material_id': 'a', 'extra_material_refs': ['lost']}, materials)
    assert len(deps) == 2 and missing == ['lost']
    data = {'materials': {'effects': [{'id': 'same', 'value': 1}, {'id': 'same', 'value': 1},
                                     {'id': 'same', 'value': 2}]}}
    deps, missing = borrow.dependencies({'material_id': 'same'}, borrow.tables(data)[0])
    assert len(deps) == 2 and {m['data']['value'] for m in deps} == {1, 2}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        raw = json.dumps({'tracks': []}).encode()
        project = {'sources': [{'kind': 'folder', 'path': 'draft.json'}],
                   'sha256': hashlib.sha256(raw).hexdigest()}
        (root / 'draft.json').write_bytes(raw)
        assert borrow.load(project, root)[0] == {'tracks': []}
        (root / 'draft.json').write_bytes(b'{}')
        rejects(borrow.load, project, root)
        with zipfile.ZipFile(root / 'sample.zip', 'w') as archive:
            archive.writestr('中文工程/draft_content.json', raw)
        project['sources'] = [{'kind': 'zip', 'path': 'sample.zip', 'member': '中文工程/draft_content.json'}]
        assert borrow.load(project, root)[0] == {'tracks': []}
        project['sources'][0]['path'] = '../outside.json'
        rejects(borrow.load, project, root)
        (root / 'media.jpg').write_bytes(b'test')
        locator = borrow.resource_locator('##_draftpath_placeholder_TEST_##/media.jpg',
                                         {'kind': 'folder', 'path': 'draft.json'}, root, set())
        assert locator['status'] == 'package_exact'
        assert borrow.resource_locator('https://example.test/media', {'kind': 'folder', 'path': 'draft.json'},
                                       root, set())['status'] == 'remote'
        # Draft JSON at the ZIP root: resources resolve beside it, never as 'draft_content.json/...' or '/...'.
        rooted = borrow.resource_locator('##_draftpath_placeholder_TEST_##/media.jpg',
                                        {'kind': 'zip', 'path': 'sample.zip', 'member': 'draft_content.json'},
                                        root, {'draft_content.json', 'media.jpg'})
        assert rooted == {'status': 'zip_entry', 'original': '##_draftpath_placeholder_TEST_##/media.jpg',
                          'member': 'media.jpg'}
        nested = borrow.resource_locator('media.jpg', {'kind': 'zip', 'path': 'sample.zip',
                                                       'member': '中文工程\\draft_content.json'},
                                        root, {'中文工程/media.jpg'})
        assert nested['member'] == '中文工程/media.jpg'
    # A null clip (seen in real drafts) falls back to default transform/position instead of crashing.
    null_clip = {'canvas_config': {'width': 320, 'height': 180},
                 'tracks': [{'type': 'text', 'segments': [{'material_id': 't', 'clip': None,
                             'target_timerange': {'start': 0, 'duration': 1000000}}]}]}
    null_project = {'id': 'TPL-null-clip', 'title': 'null clip', 'sha256': '0' * 64}
    null_source = {'kind': 'folder', 'path': 'draft.json'}
    selection = {'segment': '0:0', 'text': '空 clip', 'size': 12, 'start': 0, 'duration': 2,
                 'track': 'text', 'time_basis': 'local', 'motion_mode': 'linear_rebuild'}
    plan, _ = borrow.adapt(null_clip, null_project, borrow.report(null_clip, null_project, null_source),
                           {'name': 'null-clip', 'canvas': {'width': 320, 'height': 180, 'fps': 30},
                            'selections': [selection]}, None, Path.cwd())
    caption = plan['captions'][0]
    assert (caption['x'], caption['y']) == (0, 0) and caption['transform']['scale_x'] == 1
    print('template borrowing regression checks passed')


def admission_checks():
    """Exercise actual new-template registration and borrowing in an isolated index/registry."""
    import verify_templates as verifier
    old_index, old_registry, old_args = borrow.templates.INDEX, verifier.REGISTRY, sys.argv
    try:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            verifier.blank(root / 'image.bmp')
            source_plan = {'schema_version': 1, 'name': '测试来源', 'mode': 'script',
                           'canvas': {'width': 320, 'height': 180, 'fps': 30},
                           'clips': [{'path': str(root / 'image.bmp'), 'start': 0, 'duration': 2,
                                      'keyframes': [{'property': 'position_x', 'time': 0, 'value': 0},
                                                    {'property': 'position_x', 'time': 1.5, 'value': 0.5}]}]}
            borrow.write(root / 'source-plan.json', source_plan)
            borrow.editor.build_draft(SimpleNamespace(plan=root / 'source-plan.json', out=root / 'source'))
            borrow.templates.INDEX = root / 'index.json'
            verifier.REGISTRY = root / 'registry.json'
            rejects(borrow.templates.read_index)
            project = verifier.new_project(root / 'source/draft_content.json')
            selection = {'segment': '0:0', 'path': str(root / 'image.bmp'), 'start': 0,
                         'duration': 3, 'track': 'video', 'fit': 'contain',
                         'time_basis': 'local', 'motion_mode': 'linear_rebuild'}
            rejects(verifier.require_admission, project, [selection])
            old_environment = copy.deepcopy(verifier.fingerprint())
            old_environment['implementation'] = 'previous-implementation'
            legacy_project = dict(project, id='TPL-isolated-legacy')
            borrow.write(verifier.REGISTRY, {'schema_version': 1, 'environment': old_environment,
                'projects': {legacy_project['id']: {'source_sha256': project['sha256'],
                    'status': 'partial_verified', 'segments': {'0:0': {'status': 'passed', 'time_bases': ['local']}},
                    'summary': {'fixture': 'isolated stale admission record'}}}})
            sys.argv = ['verify_templates.py', '--draft', str(root / 'source/draft_content.json'),
                        '--out', str(root / 'audit'), '--register']
            verifier.main()
            index = borrow.read(borrow.templates.INDEX)
            assert len(index['projects']) == 1
            verifier.require_admission(index['projects'][0], [selection])
            preserved = borrow.read(verifier.REGISTRY)
            assert preserved['projects'][legacy_project['id']]['environment'] == old_environment
            assert verifier.admission(legacy_project, preserved)['status'] == 'stale_environment'
            rejects(verifier.require_admission, legacy_project, [selection])
            assert preserved['projects'][project['id']]['environment'] == verifier.fingerprint()
            # A missing alternate root must not replace the index root for later aliases.
            fallback_project = index['projects'][0]
            fallback_project['sources'] = [dict(fallback_project['sources'][0], root=str(root/'missing-alternate')),
                {'kind': 'folder', 'path': 'draft_content.json'}]
            borrow.write(borrow.templates.INDEX, index)
            sys.argv = ['verify_templates.py', '--id', project['id'], '--out', str(root/'audit-fallback'), '--register']
            verifier.main()
            verifier.require_admission(fallback_project, [selection])
            borrow.write(root / 'mapping.json', {'name': '新模板借鉴测试', 'canvas': source_plan['canvas'],
                                                'selections': [selection]})
            sys.argv = ['template_borrow.py', '--id', project['id'], '--mapping', str(root / 'mapping.json'),
                        '--out', str(root / 'adapted')]
            borrow.main()
            borrow.editor.build_draft(SimpleNamespace(plan=root / 'adapted/edit-plan.json', out=root / 'rebuilt'))
            assert borrow.read(root / 'rebuilt/verification.json')['draft_structure'] == 'passed'
            assert not (root / 'audit/临时测试').exists()
            rejects(verifier.require_admission, dict(project, sha256='0' * 64), [selection])
            rejects(verifier.require_admission, project, [dict(selection, segment='999:999')])
            registry = borrow.read(verifier.REGISTRY)
            registry['environment']['implementation'] = 'changed'
            borrow.write(verifier.REGISTRY, registry)
            rejects(verifier.require_admission, project, [selection])
    finally:
        borrow.templates.INDEX, verifier.REGISTRY, sys.argv = old_index, old_registry, old_args
    print('New-template registration, borrowing, stale-source/environment and cleanup checks passed')


def data_location_checks():
    """External data discovery must fail clearly for invalid or missing locations."""
    import os
    template = borrow.templates
    key = 'JIANYING_TEMPLATE_DATA_DIR'
    previous = os.environ.get(key)
    try:
        with tempfile.TemporaryDirectory(prefix='jianying-template-location-') as temp:
            os.environ[key] = temp
            assert template.data_directory() == Path(temp).resolve()
            os.environ[key] = 'relative-template-data'
            rejects(template.data_directory)
            os.environ[key] = str(Path(template.__file__).resolve().parents[1] / 'test-data')
            rejects(template.data_directory)
            for installed in ('.codex', '.dsh', '.agents'):
                os.environ[key] = str(Path.home() / installed / 'skills/template-data')
                rejects(template.data_directory)
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
    print('External template data directory and invalid configuration checks passed')


if __name__ == '__main__':
    data_location_checks()
    main()
    admission_checks()
