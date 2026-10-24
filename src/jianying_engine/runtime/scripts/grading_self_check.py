"""Formal RGB grading admission checks; isolated builds, no UI or rendering."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, image, video):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'offline').mkdir()
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (image, video)}
    up, down = [[0, 0], [0.4, 0.65], [1, 1]], [[0, 0], [0.6, 0.35], [1, 1]]
    cases = [('baseline', None)]
    cases += [(channel + '-' + direction, {'rgb_curves': {channel: points}})
              for channel in ('red', 'green', 'blue') for direction, points in [('up', up), ('down', down)]]
    cases += [
        ('mixed', {'rgb_curves': {'red': down, 'green': up, 'blue': [[0, 0], [0.55, 0.7], [1, 1]]}}),
        ('luma-red', {'rgb_curves': {'red': up}, 'luma_curve': down}),
        ('hsl-blue', {'rgb_curves': {'blue': up}, 'red_hsl': {'hue': -20, 'saturation': -40, 'lightness': 5}}),
        ('circle-blue', {'rgb_curves': {'blue': up}, 'local_circle': {'width': 280, 'height': 220, 'feather': 0}}),
        ('video-green', {'rgb_curves': {'green': up}})]
    plan = {'schema_version': 1, 'name': 'RGB正式验收', 'mode': 'script',
            'canvas': {'width': 640, 'height': 360, 'fps': 24}, 'clips': []}
    for index, (label, grade) in enumerate(cases):
        item = {'path': str(video if label == 'video-green' else image), 'start': index, 'duration': 1}
        if grade:
            item['grading'] = grade
        if label == 'video-green':
            item.update(source_in=0.4, speed=1.6, volume=0)
        plan['clips'].append(item)
    checks, builds = [], []
    for run in ('r1', 'r2'):
        candidate = copy.deepcopy(plan)
        candidate['name'] += '-' + run
        path = root / (run + '.json')
        editor.write_json(path, candidate)
        result = editor.build_draft(Args(plan=str(path), out=str(root / run)))
        normalized = editor.read_json(root / run / 'edit-plan.json')
        content = editor.read_json(root / run / 'draft_content.json')
        assert result['verification']['editing_status'] == 'complete'
        assert result['verification']['native_effects']['extensions']['grading'] == len(cases) - 1
        # Independently check literal anchors and default channels, not generator output.
        segments = content['tracks'][0]['segments']
        for segment, (_, grade) in zip(segments, cases):
            actual = [m for m in content['materials']['color_curves'] if m['id'] in segment['extra_material_refs']]
            if grade is None:
                # Backend defaults enable this flag even without a curve reference.
                assert not actual
                continue
            assert len(actual) == 1
            for channel in ('red', 'green', 'blue'):
                points = grade['rgb_curves'].get(channel, [[0, 0], [1, 1]])
                assert [[p['anchor']['x'], p['anchor']['y']] for p in actual[0][channel]] == points
            assert actual[0]['select_channel'] == 0 and segment['enable_color_curves']
        assert segments[-1]['source_timerange'] == {'start': 400000, 'duration': 1600000}
        assert 'rgb_curves' in (root / run / '原生效果.md').read_text(encoding='utf-8')
        editor.publish_draft(Args(draft=str(root / run), draft_root=str(root / 'offline'), offline_copy=True))
        copied = editor.read_json(root / 'offline' / candidate['name'] / 'draft_content.json')
        editor.verify_structure(copied, normalized)
        builds.append({'run': run, 'draft': str(root / run), 'verification': result['verification']})
        checks += [run + ': formal build/readback, anchors/defaults, source/speed, delivery, offline publish']
    curve = content['materials']['color_curves'][0]
    curve_index = content['materials']['color_curves'].index(curve)
    mutations = {
        'RGB anchor': lambda c: c['materials']['color_curves'][curve_index]['red'][1]['anchor'].update(y=0.2),
        'control handle': lambda c: c['materials']['color_curves'][curve_index]['red'][1]['left_control'].update(x=0.123),
        'unrequested green': lambda c: c['materials']['color_curves'][curve_index]['green'][0]['anchor'].update(y=0.1),
        'unrequested luma': lambda c: c['materials']['color_curves'][curve_index]['luma'][0]['anchor'].update(y=0.1),
        'resource': lambda c: c['materials']['color_curves'][curve_index].update(resource_id='wrong'),
        'cache path': lambda c: c['materials']['color_curves'][curve_index].update(path='missing'),
        'reference': lambda c: c['tracks'][0]['segments'][1]['extra_material_refs'].remove(curve['id']),
        'enable': lambda c: c['tracks'][0]['segments'][1].update(enable_color_curves=False),
        'channel selector': lambda c: c['materials']['color_curves'][curve_index].update(select_channel=3),
        'local dimensions': lambda c: c['materials']['common_mask'][0]['config'].update(width=0.99),
    }
    for label, mutate in mutations.items():
        corrupt = copy.deepcopy(content)
        mutate(corrupt)
        try:
            editor.verify_structure(corrupt, normalized)
        except ValueError as exc:
            checks.append({'tamper_rejected': label, 'error': str(exc)})
        else:
            raise AssertionError('Accepted corruption: ' + label)
    invalid = {
        'empty': {}, 'unknown channel': {'cyan': up}, 'not object': [],
        'channel not array': {'red': 'curve'}, 'two points': {'red': [[0, 0], [1, 1]]},
        'four points': {'red': [[0, 0], [0.3, 0.2], [0.6, 0.8], [1, 1]]},
        'first endpoint': {'red': [[0, 0.1], [0.4, 0.7], [1, 1]]},
        'last endpoint': {'red': [[0, 0], [0.4, 0.7], [0.9, 1]]},
        'x zero': {'red': [[0, 0], [0, 0.7], [1, 1]]},
        'x one': {'red': [[0, 0], [1, 0.7], [1, 1]]},
        'y over': {'red': [[0, 0], [0.4, 1.1], [1, 1]]},
        'y negative': {'red': [[0, 0], [0.4, -0.1], [1, 1]]},
        'bool endpoint': {'red': [[False, 0], [0.4, 0.7], [1, 1]]},
        'NaN': {'red': [[0, 0], [0.4, float('nan')], [1, 1]]},
        'infinity': {'red': [[0, 0], [0.4, float('inf')], [1, 1]]},
        'anchor object': {'red': [[0, 0], {'x': 0.4, 'y': 0.7}, [1, 1]]},
        'string number': {'red': [[0, 0], ['0.4', 0.7], [1, 1]]},
        'point arity': {'red': [[0, 0], [0.4, 0.7, 0.8], [1, 1]]},
    }
    for label, rgb in invalid.items():
        candidate = copy.deepcopy(plan)
        candidate['clips'][1]['grading'] = {'rgb_curves': rgb}
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError as exc:
            checks.append({'invalid_rejected': label, 'error': str(exc)})
        else:
            raise AssertionError('Accepted invalid input: ' + label)
    for field, value in [('handles', []), ('keyframes', []), ('blue_hsl', {})]:
        candidate = copy.deepcopy(plan)
        candidate['clips'][1]['grading'][field] = value
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError as exc:
            checks.append({'invalid_rejected': field, 'error': str(exc)})
        else:
            raise AssertionError('Accepted unsupported grading: ' + field)
    candidate = copy.deepcopy(plan)
    candidate['clips'][10]['mask'] = {'name': '矩形', 'experimental': True, 'size': 0.5}
    path = root / 'invalid.json'
    editor.write_json(path, candidate)
    try:
        editor.validate_plan(path)
    except ValueError as exc:
        assert 'coexist' in str(exc)
        checks.append({'invalid_rejected': 'local mask conflict', 'error': str(exc)})
    else:
        raise AssertionError('Accepted local/video mask conflict')
    for kind in ('color_curves', 'hsl', 'common_mask'):
        try:
            editor.native_advanced.cached_material(kind, root / 'missing-user-data')
        except ValueError as exc:
            assert 'unavailable' in str(exc)
            checks.append({'cache_rejected': kind, 'error': str(exc)})
        else:
            raise AssertionError('Accepted missing cache')
    assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (image, video)}
    checks.append('input bytes unchanged')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks, 'builds': builds,
              'cases': [{'label': label, 'grading': grade, 'preview_time': index + 0.5}
                        for index, (label, grade) in enumerate(cases)],
              'input_sha256': hashes, 'ui_save_reopen': 'unverified', 'native_frames': 'separate verification'}
    editor.write_json(root / 'grading-check.json', report)
    return report


if __name__ == '__main__':
    editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True, help='Existing 640x360 control image')
    parser.add_argument('--video', type=Path, required=True, help='Existing 640x360 video, at least 2 seconds')
    args = parser.parse_args()
    editor.emit(check(args.out.resolve(), args.image.resolve(), args.video.resolve()))
