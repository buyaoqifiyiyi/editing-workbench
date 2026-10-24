"""Tracking-to-native-image/text checks with synthetic trajectories; no tracker/model/UI."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as Args

import editor
import tracking_apply


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    video, marker = root / 'source.mp4', root / 'marker.png'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=4', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', str(video)])
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=red:s=16x16', '-frames:v', '1', str(marker)])
    base = {'schema_version': 1, 'name': 'tracking-position-check', 'mode': 'script',
            'canvas': {'width': 160, 'height': 90, 'fps': 10},
            'clips': [{'path': 'source.mp4', 'start': 0, 'duration': 1},
                      {'path': 'source.mp4', 'start': 1, 'duration': 2, 'source_in': .5,
                       'speed': 1.5, 'fit': 'contain'},
                      {'path': 'marker.png', 'start': 1, 'duration': 2, 'track': 'marker',
                       'fit': 'contain', 'transform': {'scale_x': .2, 'scale_y': .2, 'alpha': .8}}],
            'layer_order': ['video', 'marker']}
    track = {'schema_version': 1, 'status': 'tracked', 'method': 'opencv_csrt',
             'source': {'path': str(video.resolve()), 'sha256': hashlib.sha256(video.read_bytes()).hexdigest(),
                        'bytes': video.stat().st_size, 'width': 160, 'height': 90},
             'samples': [{'time': i, 'bbox': [10 + 20 * i, 10 + 10 * i, 20, 10]} for i in range(4)],
             'coverage_start': 0, 'coverage_end': 4, 'lost_at': None, 'boundaries': []}
    plan_path, report_path = root / 'plan.json', root / 'tracking.json'
    checks = []

    def fixture(plan=None, report=None):
        editor.write_json(plan_path, base if plan is None else plan)
        editor.write_json(report_path, track if report is None else report)

    def args(name):
        return Args(plan=str(plan_path), tracking=str(report_path), source_index=1, target_index=2,
                    out=str(root / 'outputs' / (name + '.json')))

    def hashes():
        return [hashlib.sha256(p.read_bytes()).hexdigest() for p in (plan_path, report_path, video, marker)]

    fixture()
    before = hashes()
    request = args('applied')
    result = tracking_apply.execute(request)
    assert result['status'] == 'passed' and hashes() == before
    saved = editor.read_json(request.out)
    assert saved['clips'][2]['transform'] == base['clips'][2]['transform']
    assert saved['layer_order'] == base['layer_order']
    assert Path(saved['clips'][1]['path']) == video and Path(saved['clips'][2]['path']) == marker
    keys = saved['clips'][2]['keyframes']
    assert [k['property'] for k in keys] == ['position_x', 'position_y'] * 5
    assert [k['time'] for k in keys[::2]] == [0, .333333, 1, 1.666667, 1.999999]
    assert abs(keys[0]['value'] + .625) < 1e-12
    assert abs(keys[1]['value'] - 5 / 9) < 1e-12
    assert keys[-2]['value'] == 0 and keys[-1]['value'] == 0  # Last tracked frame center is (80, 45).
    checks.append('source_in interpolation, 1.5x speed and last-frame hold map to local x/y keys')
    built = editor.build_draft(Args(plan=request.out, out=str(root / 'draft')))
    assert built['verification']['draft_structure'] == 'passed'
    content = editor.read_json(root / 'draft/draft_content.json')
    overlay = next(t for t in content['tracks'] if t.get('name') == 'marker')['segments'][0]
    assert overlay['target_timerange'] == {'start': 1000000, 'duration': 2000000}
    groups = {g['property_type']: g['keyframe_list'] for g in overlay['common_keyframes']}
    x, y = groups['KFTypePositionX'], groups['KFTypePositionY']
    assert [p['time_offset'] for p in x] == [0, 333333, 1000000, 1666667, 1999999]
    assert [p['time_offset'] for p in y] == [0, 333333, 1000000, 1666667, 1999999]
    assert abs(x[0]['values'][0] + .625) < 1e-12 and abs(y[0]['values'][0] - 5 / 9) < 1e-12
    assert x[2]['values'] == [-.25] and abs(y[2]['values'][0] - 2 / 9) < 1e-12
    assert x[-1]['values'] == [0] and y[-1]['values'] == [0]
    checks.append('formal build independent readback: source timeline offset does not enter image key times')
    checks.append('cross-directory paths, static scale/alpha and layer order retained; four inputs unchanged')

    changed = copy.deepcopy(base)
    changed['clips'][1].update(duration=1.5, speed=2)
    changed['clips'][2]['duration'] = 1.5
    fixture(plan=changed)
    fast = args('speed2')
    tracking_apply.execute(fast)
    fast_keys = editor.read_json(fast.out)['clips'][2]['keyframes']
    assert [k['time'] for k in fast_keys[::2]] == [0, .25, .75, 1.25, 1.499999]
    assert editor.build_draft(Args(plan=fast.out, out=str(root / 'draft-speed2')))['verification']['draft_structure'] == 'passed'
    checks.append('2x speed produces valid editable image keys without changing target speed')

    def reject(label, change_plan=None, change_report=None, expected=None):
        p, r = copy.deepcopy(base), copy.deepcopy(track)
        if change_plan:
            change_plan(p)
        if change_report:
            change_report(r)
        fixture(p, r)
        before = hashes()
        request = args('reject-' + str(len(checks)))
        try:
            tracking_apply.execute(request)
        except (ValueError, OSError) as error:
            if expected:
                assert expected.lower() in str(error).lower(), (label, error)
        else:
            raise AssertionError(label + ' accepted')
        assert not Path(request.out).exists() and hashes() == before
        checks.append('refused: ' + label)

    for label, mutate in (
        ('lost report', lambda r: r.update(status='lost', lost_at=2)),
        ('lost_at despite tracked status', lambda r: r.update(lost_at=2)),
        ('insufficient end coverage', lambda r: r.update(coverage_end=3)),
        ('late start coverage', lambda r: r.update(coverage_start=1)),
        ('changed source hash', lambda r: r['source'].update(sha256='0' * 64)),
        ('changed source size', lambda r: r['source'].update(bytes=1)),
        ('different source path', lambda r: r['source'].update(path=str(marker))),
        ('wrong source dimensions', lambda r: r['source'].update(width=320)),
        ('unordered samples', lambda r: r['samples'].reverse()),
        ('duplicate microsecond samples', lambda r: r['samples'][1].update(time=.0000001)),
        ('nonfinite sample', lambda r: r['samples'][1].update(time=float('nan'))),
        ('out of bounds ROI', lambda r: r['samples'][1].update(bbox=[150, 10, 20, 10])),
        ('zero-size ROI', lambda r: r['samples'][1].update(bbox=[10, 10, 0, 10])),
        ('empty samples', lambda r: r.update(samples=[])),
        ('wrong method', lambda r: r.update(method='unknown')),
    ):
        reject(label, change_report=mutate)
    for label, mutate in (
        ('source cover crop', lambda p: p['clips'][1].update(fit='cover')),
        ('source flip', lambda p: p['clips'][1].update(transform={'flip_horizontal': True})),
        ('source motion', lambda p: p['clips'][1].update(keyframes=[{'property':'position_x','time':0,'value':0}])),
        ('curve speed', lambda p: p['clips'][1].update(speed_curve=[])),
        ('target existing motion', lambda p: p['clips'][2].update(keyframes=[{'property':'position_y','time':0,'value':0}])),
        ('target shifted position', lambda p: p['clips'][2]['transform'].update(transform_x=.2)),
        ('target timing mismatch', lambda p: p['clips'][2].update(duration=1)),
        ('composites', lambda p: p.update(composites=[{'name':'g','tracks':['video','marker']}])),
    ):
        reject(label, change_plan=mutate)
    fixture()
    existing = args('applied')
    frozen = Path(existing.out).read_bytes()
    try:
        tracking_apply.execute(existing)
    except FileExistsError:
        pass
    else:
        raise AssertionError('Existing output overwritten')
    assert Path(existing.out).read_bytes() == frozen
    checks.append('existing output refused and preserved')

    text_plan = copy.deepcopy(base)
    text_plan['captions'] = [{'text': 'FOLLOW', 'start': 1, 'duration': 2, 'x': 0, 'y': 0,
                              'size': 10, 'color': '#FF00FF', 'border': False, 'track': 'label',
                              'style': {'auto_wrapping': False}}]
    text_plan['layer_order'].append('label')

    def text_args(name):
        request = args(name)
        request.target_index, request.caption_index = None, 0
        request.offset_x, request.offset_y = 8, 9
        return request

    fixture(text_plan)
    before = hashes()
    request = text_args('text')
    result = tracking_apply.execute(request)
    saved = editor.read_json(request.out)
    assert result['target_group'] == 'captions' and result['target_index'] == 0
    assert result['pixel_offset'] == {'x_right': 8, 'y_up': 9}
    text_keys = saved['captions'][0].pop('keyframes')
    assert saved['captions'] == text_plan['captions'] and 'keyframes' not in saved['clips'][2]
    assert hashes() == before
    assert [k['time'] for k in text_keys[::2]] == [0, .333333, 1, 1.666667, 1.999999]
    assert abs(text_keys[0]['value'] + .525) < 1e-12
    assert abs(text_keys[1]['value'] - (5 / 9 + .2)) < 1e-12
    assert abs(text_keys[-2]['value'] - .1) < 1e-12 and abs(text_keys[-1]['value'] - .2) < 1e-12
    checks.append('text uses measured source interval with explicit right/up pixel offsets; text/style/input bytes retained')
    editor.build_draft(Args(plan=request.out, out=str(root / 'draft-text')))
    native = editor.read_json(root / 'draft-text/draft_content.json')
    label = next(t for t in native['tracks'] if t.get('name') == 'label')['segments'][0]
    groups = {g['property_type']: g['keyframe_list'] for g in label['common_keyframes']}
    assert label['target_timerange'] == {'start': 1000000, 'duration': 2000000}
    for axis in ('KFTypePositionX', 'KFTypePositionY'):
        assert [k['time_offset'] for k in groups[axis]] == [0, 333333, 1000000, 1666667, 1999999]
    assert abs(groups['KFTypePositionX'][0]['values'][0] + .525) < 1e-12
    checks.append('native text key times are local despite source_in, nonzero timeline start and 1.5x speed')
    changed = copy.deepcopy(text_plan)
    changed['clips'][1].update(duration=1.5, speed=2)
    changed['clips'][2]['duration'] = 1.5
    changed['captions'][0]['duration'] = 1.5
    fixture(changed)
    request = text_args('text-speed2')
    tracking_apply.execute(request)
    assert [k['time'] for k in editor.read_json(request.out)['captions'][0]['keyframes'][::2]] == [0, .25, .75, 1.25, 1.499999]
    checks.append('text target supports fixed 2x source speed')

    def reject_text(label, mutate=None, change_args=None):
        changed = copy.deepcopy(text_plan)
        if mutate:
            mutate(changed['captions'][0])
        fixture(changed)
        request = text_args('reject-text-' + str(len(checks)))
        if change_args:
            change_args(request)
        before = hashes()
        try:
            tracking_apply.execute(request)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError(label + ' accepted')
        assert not Path(request.out).exists() and hashes() == before
        checks.append('refused text: ' + label)

    for label, mutate in (
        ('timing mismatch', lambda c: c.update(duration=1)),
        ('prepositioned x', lambda c: c.update(x=.1)),
        ('prepositioned y', lambda c: c.update(y=-.8)),
        ('implicit subtitle y', lambda c: c.pop('y')),
        ('multiline', lambda c: c.update(text='a\nb')),
        ('vertical', lambda c: c['style'].update(vertical=True)),
        ('wrapping', lambda c: c['style'].update(auto_wrapping=True)),
        ('implicit wrapping', lambda c: c['style'].pop('auto_wrapping')),
        ('transform', lambda c: c.update(transform={'transform_x': .1})),
        ('existing keys', lambda c: c.update(keyframes=[{'property': 'position_x', 'time': 0, 'value': 0}])),
        ('curve', lambda c: c.update(text_curve={'degree': 10})),
        ('glow', lambda c: c.update(glow={})),
    ):
        reject_text(label, mutate)
    for label, mutate in (
        ('invalid index', lambda a: setattr(a, 'caption_index', 99)),
        ('bool index', lambda a: setattr(a, 'caption_index', True)),
        ('both targets', lambda a: setattr(a, 'target_index', 2)),
        ('nonfinite offset', lambda a: setattr(a, 'offset_x', float('nan'))),
        ('offscreen anchor', lambda a: setattr(a, 'offset_x', 160)),
        ('image offset', lambda a: (setattr(a, 'caption_index', None), setattr(a, 'target_index', 2))),
    ):
        reject_text(label, change_args=mutate)
    result = {'status':'passed', 'checks_count':len(checks), 'checks':checks,
              'evidence':'synthetic media; editor validate/build and independent native JSON readback',
              'model_inference':False, 'ui_operations':0, 'visual_audio_review':'unverified',
              'privacy_masking':'not guaranteed'}
    editor.write_json(root / 'tracking-apply-check.json', result)
    return result


if __name__ == '__main__':
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    editor.emit(check(args.out.resolve()))
