"""Admission regression for measured 11.5 extensions; no desktop control or effect rendering."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, video, music):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'offline').mkdir()
    source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    mask = {'name': '矩形', 'experimental': True, 'size': 0.5, 'rect_width': 0.28, 'feather': 0,
            'keyframes': [dict(property=prop, time=time, value=value)
                          for prop, values in [('width', [1075.2, 1612.8, 1075.2]),
                                               ('height', [1080, 1620, 1080]), ('feather', [0, 0, 0])]
                          for time, value in zip([0, 1, 59/30], values)]}
    grade = {'red_hsl': {'hue': -35, 'saturation': 15, 'lightness': -10},
             'luma_curve': [[0, 0], [0.5, 0.4], [1, 1]]}
    plan = {'schema_version': 1, 'name': '扩展正式接口集成', 'mode': 'script',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': str(video), 'start': 0, 'duration': 2, 'source_in': 24, 'grading': grade},
                      {'path': str(video), 'start': 2, 'duration': 3, 'source_in': 26,
                       'grading': {**grade, 'local_circle': {'width': 1075.2, 'height': 1080, 'feather': 20}}},
                      {'path': str(video), 'start': 1, 'duration': 2, 'source_in': 20, 'speed': 1.5,
                       'track': 'panel', 'mask': mask, 'transform': {'scale_x': 0.55, 'scale_y': 0.55}}],
            'captions': [{'text': '正式接口局部复合', 'start': 1, 'duration': 2, 'track': 'title', 'size': 14, 'y': -0.75}],
            'audio': [{'path': str(music), 'start': 1, 'duration': 2, 'source_in': 5, 'speed': 1.5,
                       'track': 'music', 'volume': 0.5,
                       'keyframes': [{'time': time, 'value': gain} for time, gain in [(0, 0.5), (1, 0.12), (59/30, 0.5)]]}],
            'effects': [{'name': '模糊', 'start': 0, 'duration': 5, 'track': 'blur', 'params': {'effects_adjust_blur': 0},
                         'keyframes': [{'time': time, 'value': value} for time, value in [(0, 0), (2.5, 45), (149/30, 0)]]}],
            'layer_order': ['video', 'panel', 'title', 'music', 'blur'],
            'composites': [{'name': '两秒局部组件', 'tracks': ['panel', 'title', 'music'], 'start': 1, 'duration': 2,
                            'keyframes': [{'property': 'rotation', 'time': 0, 'value': 0},
                                          {'property': 'rotation', 'time': 59/30, 'value': 12}]}]}
    results, checks = [], []
    for run in ['r1', 'r2']:
        candidate = copy.deepcopy(plan)
        candidate['name'] += '-' + run
        path = root / (run + '.json')
        editor.write_json(path, candidate)
        result = editor.build_draft(Args(plan=str(path), out=str(root / run)))
        normalized = editor.read_json(root / run / 'edit-plan.json')
        content = editor.read_json(root / run / 'draft_content.json')
        assert result['verification']['editing_status'] == 'complete'
        assert result['verification']['native_effects']['extensions'] == {'audio_volume_keyframes': 1, 'blur_keyframes': 1, 'grading': 2}
        nested = content['materials']['drafts'][0]['draft']
        assert nested['duration'] == 2000000 and [t['name'] for t in nested['tracks']] == ['panel', 'title', 'music']
        audio = next(t for t in nested['tracks'] if t['type'] == 'audio')['segments'][0]
        assert [p['time_offset'] for p in audio['common_keyframes'][0]['keyframe_list']] == [5000000, 6500000, 7950000]
        assert audio['target_timerange'] == {'start': 0, 'duration': 2000000}
        editor.publish_draft(Args(draft=str(root / run), draft_root=str(root / 'offline'), offline_copy=True))
        copied = editor.read_json(root / 'offline' / candidate['name'] / 'draft_content.json')
        editor.verify_structure(copied, normalized)
        editor.native_advanced.verify_sidecars(copied, root / 'offline' / candidate['name'], root / 'offline' / candidate['name'], editor.read_json)
        results.append({'run': run, 'draft': str(root / run), 'verification': result['verification']})
    checks += ['two independent formal CLI builds', 'nonzero source and speed 1.5 audio/mask mapping',
               'local group target rebasing preserves media source ranges', 'text/audio/video group membership',
               'color enable flags and cached references', 'offline publish relocation and sidecar consistency',
               'effect-local blur keyframes']
    # Change serialized output, not the plan; each must be rejected by readback.
    mutations = {
        'audio source time': lambda c: c['materials']['drafts'][0]['draft']['tracks'][2]['segments'][0]['common_keyframes'][0]['keyframe_list'][1].update(time_offset=1000000),
        'mask feather': lambda c: c['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['common_keyframes'][2]['keyframe_list'][1].update(values=[0.9]),
        'group target': lambda c: c['tracks'][1]['segments'][0]['target_timerange'].update(start=0),
        'group rotation': lambda c: c['tracks'][1]['segments'][0]['common_keyframes'][0]['keyframe_list'][1].update(values=[99]),
        'red HSL': lambda c: c['materials']['hsl'][0].update(hue=99),
        'blur local time': lambda c: c['tracks'][2]['segments'][0]['common_keyframes'][0]['keyframe_list'][1].update(time_offset=3500000),
        'blur resource identity': lambda c: c['materials']['video_effects'][0].update(resource_id='6739752722289824269'),
    }
    for label, mutate in mutations.items():
        corrupt = copy.deepcopy(content)
        mutate(corrupt)
        try:
            editor.verify_structure(corrupt, normalized)
        except ValueError:
            checks.append('tamper rejected: ' + label)
        else:
            raise AssertionError('Readback accepted corruption: ' + label)
    invalid = {
        'negative feather': lambda p: p['clips'][2]['mask']['keyframes'][6].update(value=-1),
        'audio key at end': lambda p: p['audio'][0]['keyframes'][-1].update(time=2),
        'audio duplicate after rounding': lambda p: p['audio'][0]['keyframes'].insert(1, {'time': 0.0000001, 'value': 0.2}),
        'unsupported effect animation': lambda p: p['effects'][0].update(name='_1998'),
        'unsupported grading channel': lambda p: p['clips'][0]['grading'].update(blue_hsl={}),
        'grading plus video mask': lambda p: p['clips'][1].update(mask=mask),
        'group cuts source clip': lambda p: p['composites'][0].update(duration=1),
        'unknown group property': lambda p: p['composites'][0]['keyframes'][0].update(property='alpha'),
        'noncontiguous group': lambda p: p['composites'][0].update(tracks=['panel', 'music']),
        'curve anchors reversed': lambda p: p['clips'][0]['grading']['luma_curve'][1].__setitem__(0, 1),
    }
    for label, mutate in invalid.items():
        candidate = copy.deepcopy(plan)
        mutate(candidate)
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError:
            checks.append('invalid plan rejected: ' + label)
        else:
            raise AssertionError('Accepted invalid plan: ' + label)
    assert hashlib.sha256(video.read_bytes()).hexdigest() == source_hash
    checks.append('original source bytes preserved')
    for kind in ('blur', 'hsl', 'color_curves', 'common_mask'):
        try:
            editor.native_advanced.cached_material(kind, root / 'missing-user-data')
        except ValueError as exc:
            assert 'unavailable' in str(exc)
            checks.append('missing cache rejected: ' + kind)
        else:
            raise AssertionError('Accepted absent native cache: ' + kind)
    report = {'status': 'passed', 'checks': checks, 'checks_count': len(checks), 'builds': results,
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'extensions-check.json', report)
    return report


if __name__ == '__main__':
    editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True, help='Existing 3840x2160 video with at least 29 seconds')
    parser.add_argument('--music', type=Path, required=True, help='Existing audio with at least 8 seconds')
    args = parser.parse_args()
    editor.emit(check(args.out.resolve(), args.video.resolve(), args.music.resolve()))
