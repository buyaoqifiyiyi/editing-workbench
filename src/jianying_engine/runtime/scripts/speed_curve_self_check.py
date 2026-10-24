"""Muted curve-speed admission: build/readback/offline publish, bounds and tampering.

Rendering and UI evidence are separate. Never opens Jianying or registers home drafts.
"""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, video):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'offline').mkdir()
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    base = dict(schema_version=1, name='曲线变速正式测试', mode='reference',
                canvas=dict(width=640, height=360, fps=60),
                clips=[dict(path=str(video), start=0, duration=3.2, source_in=.5,
                            speed_curve=[dict(x=0, y=.5), dict(x=1, y=2)])])
    checks = []
    cases = [('two', .5, 3.2, [dict(x=0,y=.5),dict(x=1,y=2)]),
             ('three', .25, 3, [dict(x=0,y=1),dict(x=.5,y=2),dict(x=1,y=1)]),
             ('asymmetric', .2, 3.36, [dict(x=0,y=.5),dict(x=.25,y=2),dict(x=1,y=1)])]
    for name, offset, duration, points in cases:
        candidate = copy.deepcopy(base); candidate['name'] += '-' + name
        candidate['clips'][0].update(source_in=offset, duration=duration, speed_curve=points)
        path = root / (name + '.json'); editor.write_json(path, candidate)
        result = editor.build_draft(Args(plan=str(path), out=str(root / name)))
        assert result['verification']['native_effects']['extensions']['speed_curve'] == 1
        assert 'speed_curve' in (root / name / '原生效果.md').read_text('utf-8')
        editor.publish_draft(Args(draft=str(root/name), draft_root=str(root/'offline'), offline_copy=True))
        content = editor.read_json(root / name / 'draft_content.json')
        plan = editor.read_json(root / name / 'edit-plan.json')
        editor.verify_structure(editor.read_json(root/'offline'/candidate['name']/'draft_content.json'), plan)
        checks.extend([name+' build/readback', name+' native delivery', name+' offline publish/readback'])
    def corrupt(name, mutation):
        altered = copy.deepcopy(content); mutation(altered)
        try: editor.verify_structure(altered, plan)
        except ValueError: checks.append(name+' rejected')
        else: raise AssertionError(name+' accepted')
    def segment(d): return d['tracks'][0]['segments'][0]
    def speed(d): return d['materials']['speeds'][0]
    for name, mutation in {
        'speed mode': lambda d: speed(d).update(mode=0),
        'stale speed scalar': lambda d: speed(d).update(speed=2),
        'segment scalar': lambda d: segment(d).update(speed=1),
        'curve point': lambda d: speed(d)['curve_speed']['speed_points'][1].update(x=.5),
        'preset id': lambda d: speed(d)['curve_speed'].update(id='unverified'),
        'missing speed reference': lambda d: segment(d)['extra_material_refs'].remove(speed(d)['id']),
        'source in': lambda d: segment(d)['source_timerange'].update(start=0),
        'source duration': lambda d: segment(d)['source_timerange'].update(duration=1),
        'target duration': lambda d: segment(d)['target_timerange'].update(duration=1),
        'source identity': lambda d: d['materials']['videos'][0].update(path='other.mp4'),
        'unrequested source-time keys': lambda d: segment(d).update(common_keyframes=[{}]),
    }.items(): corrupt(name, mutation)
    def reject(name, mutation):
        candidate = copy.deepcopy(base); mutation(candidate)
        path = root / 'invalid.json'; editor.write_json(path, candidate)
        try: editor.validate_plan(path)
        except ValueError: checks.append(name+' rejected')
        else: raise AssertionError(name+' accepted')
    def item(p): return p['clips'][0]
    for name, mutation in {
        'wrong container': lambda p: item(p).update(speed_curve={}),
        'one point': lambda p: item(p)['speed_curve'].pop(),
        'four points': lambda p: item(p).update(speed_curve=[dict(x=x,y=1) for x in (0,.2,.6,1)]),
        'missing endpoint': lambda p: item(p)['speed_curve'][0].update(x=.1),
        'last endpoint': lambda p: item(p)['speed_curve'][-1].update(x=.9),
        'duplicate x': lambda p: item(p).update(speed_curve=[dict(x=0,y=1),dict(x=0,y=2),dict(x=1,y=1)]),
        'unordered x': lambda p: item(p)['speed_curve'].reverse(),
        'zero speed': lambda p: item(p)['speed_curve'][0].update(y=0),
        'negative speed': lambda p: item(p)['speed_curve'][0].update(y=-1),
        'unmeasured speed': lambda p: item(p)['speed_curve'][0].update(y=3),
        'nan': lambda p: item(p)['speed_curve'][0].update(y=float('nan')),
        'infinity': lambda p: item(p)['speed_curve'][0].update(x=float('inf')),
        'boolean': lambda p: item(p)['speed_curve'][0].update(y=True),
        'unknown point field': lambda p: item(p)['speed_curve'][0].update(handle=0),
        'conflicting scalar': lambda p: item(p).update(speed=2),
        'source overrun': lambda p: item(p).update(duration=5),
        'keys combination': lambda p: item(p).update(keyframes=[dict(property='position_x',time=0,value=0)]),
        'motion easing combination': lambda p: item(p).update(motion_easing='smoothstep'),
        'nonzero gain': lambda p: item(p).update(volume=1),
        'independent effect overlap': lambda p: p.update(effects=[dict(name='模糊',start=0,duration=1)]),
        'caption field': lambda p: p.update(captions=[dict(text='x',start=0,duration=1,speed_curve=item(p)['speed_curve'])]),
        'audio field': lambda p: p.update(audio=[dict(path=str(video),start=0,duration=1,speed_curve=item(p)['speed_curve'])]),
        'composite': lambda p: p.update(captions=[dict(text='x',start=0,duration=1)],composites=[dict(name='group',tracks=['video','captions'])]),
        'incoming transition': lambda p: p['clips'].insert(0,dict(path=str(video),start=0,duration=.5,transition=dict(name='叠化',duration=.1))) or item(p).update(start=0) or p['clips'][1].update(start=.5),
    }.items(): reject(name, mutation)
    rectangle = dict(name='矩形', experimental=True, size=.6, rect_width=.64, feather=0)
    def masked(p, **values): item(p).update(mask=dict(rectangle, **values))
    for name, values in {
        'curve circle mask': dict(name='圆形'),
        'curve mask feather': dict(feather=1),
        'curve mask rotation': dict(rotation=10),
        'curve mask invert': dict(invert=True),
        'curve mask round corner': dict(round_corner=10),
        'curve mask time-zero key': dict(keyframes=[dict(property='width',time=0,value=300)]),
        'curve dynamic mask': dict(keyframes=[dict(property='width',time=0,value=300),dict(property='width',time=1,value=200)]),
        'curve empty mask keys': dict(keyframes=[]),
        'curve mask experimental false': dict(experimental=False),
    }.items(): reject(name, lambda p, values=values: masked(p, **values))
    candidate=copy.deepcopy(base);masked(candidate)
    path=root/'rectangle.json';editor.write_json(path,candidate)
    editor.build_draft(Args(plan=str(path),out=str(root/'rectangle')))
    editor.publish_draft(Args(draft=str(root/'rectangle'),draft_root=str(root/'offline'),offline_copy=True))
    masked_content=editor.read_json(root/'rectangle/draft_content.json')
    masked_plan=editor.read_json(root/'rectangle/edit-plan.json')
    editor.verify_structure(editor.read_json(root/'offline'/candidate['name']/'draft_content.json'),masked_plan)
    checks.extend(['curve static rectangle build/readback','curve static rectangle offline publish/readback'])
    for name, mutation in {
        'curve mask resource path': lambda d: d['materials']['common_mask'][0].update(path=''),
        'curve mask width': lambda d: d['materials']['common_mask'][0]['config'].update(width=.9),
        'curve mask feather readback': lambda d: d['materials']['common_mask'][0]['config'].update(feather=.1),
        'curve mask enable': lambda d: segment(d).update(enable_video_mask=False),
        'curve mask injected key': lambda d: segment(d).update(common_keyframes=[dict(property_type='KFTypePositionX',material_id='',keyframe_list=[])]),
    }.items():
        altered=copy.deepcopy(masked_content);mutation(altered)
        try: editor.verify_structure(altered,masked_plan)
        except ValueError: checks.append(name+' rejected')
        else: raise AssertionError(name+' accepted')
    # Audio stream boundary must be measured, even if requested volume=0.
    with_audio = root/'with-audio.mp4'
    editor.run([editor.tool('ffmpeg'),'-v','error','-nostdin','-n','-i',str(video),
                '-f','lavfi','-i','sine=frequency=440:duration=5','-c:v','copy','-c:a','aac','-shortest',str(with_audio)])
    reject('muted gain does not remove source audio',lambda p:item(p).update(path=str(with_audio)))
    image = root/'image.png'
    editor.run([editor.tool('ffmpeg'),'-v','error','-nostdin','-n','-i',str(video),'-frames:v','1',str(image)])
    reject('image boundary',lambda p:item(p).update(path=str(image)))
    plain = copy.deepcopy(base); plain['clips'][0].pop('speed_curve'); plain['clips'][0]['speed']=1.25
    path=root/'plain.json'; editor.write_json(path,plain)
    editor.build_draft(Args(plan=str(path),out=str(root/'plain')))
    content=editor.read_json(root/'plain/draft_content.json'); plan=editor.read_json(root/'plain/edit-plan.json')
    assert speed(content)['mode']==0 and speed(content)['curve_speed'] is None
    checks.append('ordinary speed remains normal')
    corrupt('unrequested curve',lambda d:speed(d).update(mode=1,curve_speed={'speed_points':[]}))
    plain['clips'][0].update(duration=.123456, source_in=0, speed=1.37)
    path=root/'rounded-normal.json'; editor.write_json(path,plain)
    editor.build_draft(Args(plan=str(path),out=str(root/'rounded-normal')))
    rounded=editor.read_json(root/'rounded-normal/draft_content.json')
    assert abs(speed(rounded)['speed']-round(.123456*1.37*1e6)/123456)<1e-9
    checks.append('ordinary fractional speed preserves rounded-microsecond behavior')
    assert hashlib.sha256(video.read_bytes()).hexdigest()==digest; checks.append('input bytes unchanged')
    report=dict(status='passed',checks_count=len(checks),checks=checks,rendering='separate native frame evidence',
                save_reopen='unverified',audio_sync='excluded',home_published_drafts=0)
    editor.write_json(root/'回归结果.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path); parser.add_argument('--video',required=True,type=Path)
    args=parser.parse_args(); editor.emit(check(args.out.resolve(),args.video.resolve()))
