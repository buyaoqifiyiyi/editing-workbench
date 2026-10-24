"""Check static native text curves, style preservation, nesting and rejected inputs; no UI or rendering."""
import argparse
import copy
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, video):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'offline').mkdir()
    plan = {'schema_version': 1, 'name': '弯曲后台接入验收', 'mode': 'script',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': str(video), 'start': 0, 'duration': 8, 'source_in': 24, 'volume': 0}],
            'captions': [{'text': '原生弯曲 ABC 123', 'start': i * 2, 'duration': 2,
                          'size': 20, 'y': 0, 'border': i == 2, 'bold': i == 2,
                          'style': {'auto_wrapping': False, **({'shadow': {'alpha': 0.5}} if i == 2 else {})},
                          **({'text_curve': {'angle': angle}} if angle is not None else {})}
                         for i, angle in enumerate([None, 0, 180, 360])]}
    checks, builds, ids = [], [], []
    for run in ('r1', 'r2'):
        candidate = copy.deepcopy(plan)
        candidate['name'] += '-' + run
        if run == 'r2':
            candidate['composites'] = [{'name': '弯曲原生复合', 'tracks': ['video', 'captions']}]
        path = root / (run + '.json')
        editor.write_json(path, candidate)
        result = editor.build_draft(Args(plan=str(path), out=str(root / run)))
        assert result['verification']['editing_status'] == 'complete'
        assert not any('EXPERIMENTAL text curve' in warning for warning in result['warnings'])
        normalized = editor.read_json(root / run / 'edit-plan.json')
        content = editor.read_json(root / run / 'draft_content.json')
        assert result['verification']['native_effects']['counts']['text_curves'] == 3
        expanded = editor.native_advanced.expanded_content(content, normalized) if run == 'r2' else content
        texts = expanded['materials']['texts']
        assert 'text_curve' not in texts[0]
        assert [t['text_curve'] for t in texts[1:]] == [{'enable': True, 'angle': a} for a in (0, 180, 360)]
        assert [t['check_flag'] for t in texts] == [7, 135, 175, 135]
        ids.append({t['id'] for t in texts})
        editor.publish_draft(Args(draft=str(root / run), draft_root=str(root / 'offline'), offline_copy=True))
        copied_root = root / 'offline' / candidate['name']
        editor.verify_structure(editor.read_json(copied_root / 'draft_content.json'), normalized)
        editor.native_advanced.verify_sidecars(content, root / run, root / run, editor.read_json)
        builds.append(result)
        for label, mutate in {
            'curve angle': lambda c: c['materials']['texts'][2]['text_curve'].update(angle=90),
            'curve enable': lambda c: c['materials']['texts'][2]['text_curve'].update(enable=False),
            'curve style flag': lambda c: c['materials']['texts'][2].update(check_flag=199),
        }.items():
            corrupted = copy.deepcopy(expanded)
            mutate(corrupted)
            try:
                editor.verify_native(corrupted, normalized)
            except ValueError:
                checks.append(run + ': tamper rejected: ' + label)
            else:
                raise AssertionError('Accepted corruption: ' + label)
    assert ids[0].isdisjoint(ids[1])
    checks += ['fresh material IDs per build', 'unrequested flat text unchanged',
               'static curve production build completes', 'obsolete research warning removed',
               '0/180/360 degree static curves', 'bold/border/shadow flags preserved',
               'nonzero caption starts', 'nested text remains structured and editable',
               'offline publication relocation and sidecar readback']
    for value in (-1, 361, True, '180', float('nan'), float('inf')):
        candidate = copy.deepcopy(plan)
        candidate['captions'][2]['text_curve']['angle'] = value
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError:
            checks.append('invalid angle rejected: ' + repr(value))
        else:
            raise AssertionError('Accepted invalid angle: ' + repr(value))
    for legacy in (True, False):
        candidate = copy.deepcopy(plan)
        candidate['captions'][2]['text_curve']['experimental'] = legacy
        path = root / 'legacy.json'
        editor.write_json(path, candidate)
        editor.validate_plan(path)
        checks.append('legacy boolean accepted: ' + repr(legacy))
    for label, value in [('missing angle', {'experimental': True}),
                         ('wrong research gate type', {'angle': 180, 'experimental': 1}),
                         ('unknown enable', {'angle': 180, 'experimental': True, 'enable': False}),
                         ('unsupported curve animation', {'angle': 180, 'experimental': True, 'keyframes': []}),
                         ('nonobject curve', [])]:
        candidate = copy.deepcopy(plan)
        candidate['captions'][2]['text_curve'] = value
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError:
            checks.append('invalid curve rejected: ' + label)
        else:
            raise AssertionError('Accepted invalid curve: ' + label)
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks, 'builds': builds,
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'text-curve-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True, help='Video with at least 32 seconds')
    args = parser.parse_args()
    editor.emit(check(args.out.resolve(), args.video.resolve()))
