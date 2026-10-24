"""Measured native circle/ellipse mask admission; no UI or prerendering."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, foreground, background, video):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'offline').mkdir()
    inputs = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (foreground,background,video)}
    mask = {'name':'圆形','experimental':True,'size':0.44,'center_x':75,'center_y':-24,'feather':0,'invert':True}
    keys = [{'property':prop,'time':time,'value':value} for prop,values in
            [('center_x',[-110,70]),('center_y',[28,-36]),('width',[190,250]),
             ('height',[260,130]),('rotation',[-15,35])] for time,value in zip([0,1.8],values)]
    plan = {'schema_version':1,'name':'圆形正式验收','mode':'script',
            'canvas':{'width':640,'height':360,'fps':24},
            'clips':[{'path':str(background),'start':0,'duration':6},
                     {'path':str(foreground),'start':1,'duration':2,'track':'static','mask':mask},
                     {'path':str(video),'start':3,'duration':2,'source_in':0.5,'speed':1.25,'volume':0,
                      'track':'dynamic','mask':{'name':'圆形','experimental':True,'size':0.44,'feather':0,'keyframes':keys}}],
            'captions':[{'text':'MASK','start':3,'duration':2,'track':'label','size':12,'y':-0.6}],
            'layer_order':['video','static','dynamic','label'],
            'composites':[{'name':'局部圆形组件','tracks':['dynamic','label'],'start':3,'duration':2}]}
    checks,builds = [],[]
    for run in ('r1','r2'):
        candidate=copy.deepcopy(plan);candidate['name']+='-'+run
        path=root/(run+'.json');editor.write_json(path,candidate)
        result=editor.build_draft(Args(plan=str(path),out=str(root/run)))
        content=editor.read_json(root/run/'draft_content.json')
        normalized=editor.read_json(root/run/'edit-plan.json')
        assert result['verification']['editing_status']=='complete'
        static=content['materials']['common_mask'][0]
        assert static['category']=='video' and static['type']=='mask'
        assert static['resource_id']=='7356934080102928946'
        assert static['config']['centerX']==75/320 and static['config']['centerY']==-24/180
        assert static['config']['height']==0.44 and static['config']['width']==0.44*360/640
        assert static['config']['invert'] and static['config']['feather']==0
        nested=content['materials']['drafts'][0]['draft']
        segment=nested['tracks'][0]['segments'][0]
        assert segment['target_timerange']=={'start':0,'duration':2000000}
        assert segment['source_timerange']=={'start':500000,'duration':2500000}
        groups={g['property_type']:g for g in segment['common_keyframes']}
        expected={'KFTypeCommonMaskPositionX':[-110/320,70/320],
                  'KFTypeCommonMaskPositionY':[28/180,-36/180],
                  'KFTypeCommonMaskSizeWidth':[190/640,250/640],
                  'KFTypeCommonMaskSizeHeight':[260/360,130/360],
                  'KFTypeCommonMaskRotation':[-15,35]}
        for name,values in expected.items():
            assert [p['time_offset'] for p in groups[name]['keyframe_list']]==[500000,2750000]
            assert [p['values'][0] for p in groups[name]['keyframe_list']]==values
        assert '圆形' in (root/run/'原生效果.md').read_text(encoding='utf-8')
        editor.publish_draft(Args(draft=str(root/run),draft_root=str(root/'offline'),offline_copy=True))
        copied=editor.read_json(root/'offline'/candidate['name']/'draft_content.json')
        editor.verify_structure(copied,normalized)
        editor.native_advanced.verify_sidecars(copied,root/'offline'/candidate['name'],root/'offline'/candidate['name'],editor.read_json)
        checks.append(run+': formal build/anchors/source map/enable/default/delivery/offline relocation')
        builds.append({'run':run,'verification':result['verification']})
    mutations = {
        'resource':lambda c:c['materials']['common_mask'][0].update(resource_id='wrong'),
        'category':lambda c:c['materials']['common_mask'][0].update(category='adjust'),
        'cache path':lambda c:c['materials']['common_mask'][0].update(path='missing'),
        'geometry':lambda c:c['materials']['common_mask'][0]['config'].update(centerX=0),
        'invert':lambda c:c['materials']['common_mask'][0]['config'].update(invert=False),
        'feather':lambda c:c['materials']['common_mask'][0]['config'].update(feather=0.2),
        'legacy storage':lambda c:c['materials'].setdefault('masks',[]).append(c['materials']['common_mask'].pop(0)),
        'enable':lambda c:c['tracks'][1]['segments'][0].update(enable_video_mask=False),
        'nested key time':lambda c:c['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][1].update(time_offset=1800000),
        'nested material id':lambda c:c['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['common_keyframes'][0].update(material_id='wrong'),
        'nested handle':lambda c:c['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][1]['left_control'].update(x=1),
    }
    for label, mutate in mutations.items():
        corrupt=copy.deepcopy(content);mutate(corrupt)
        try: editor.verify_structure(corrupt,normalized)
        except ValueError as exc: checks.append({'tamper_rejected':label,'error':str(exc)})
        else: raise AssertionError('Accepted corruption: '+label)
    invalid={
        'nonzero feather':lambda p:p['clips'][1]['mask'].update(feather=1),
        'feather animation':lambda p:p['clips'][2]['mask']['keyframes'].append({'property':'feather','time':0,'value':0}),
        'rectangle width':lambda p:p['clips'][1]['mask'].update(rect_width=0.5),
        'round corner':lambda p:p['clips'][1]['mask'].update(round_corner=10),
        'boolean size':lambda p:p['clips'][1]['mask'].update(size=True),
        'NaN center':lambda p:p['clips'][1]['mask'].update(center_x=float('nan')),
        'invert number':lambda p:p['clips'][1]['mask'].update(invert=1),
        'empty keys':lambda p:p['clips'][2]['mask'].update(keyframes=[]),
        'key at end':lambda p:p['clips'][2]['mask']['keyframes'][1].update(time=2),
        'missing zero':lambda p:p['clips'][2]['mask']['keyframes'][0].update(time=0.1),
        'unknown prop':lambda p:p['clips'][2]['mask']['keyframes'][0].update(property='path'),
        'negative width':lambda p:p['clips'][2]['mask']['keyframes'][4].update(value=-1),
        'unknown contour':lambda p:p['clips'][1]['mask'].update(contour_path='not_admitted'),
        'local grading conflict':lambda p:p['clips'][1].update(grading={'rgb_curves':{'red':[[0,0],[0.5,0.6],[1,1]]},'local_circle':{'width':200,'height':200}}),
    }
    for label,mutate in invalid.items():
        candidate=copy.deepcopy(plan);mutate(candidate);path=root/'invalid.json';editor.write_json(path,candidate)
        try:editor.validate_plan(path)
        except ValueError as exc:checks.append({'invalid_rejected':label,'error':str(exc)})
        else:raise AssertionError('Accepted input: '+label)
    try:editor.native_advanced.circle_material(normalized['clips'][1],{'width':640,'height':360},root/'missing-cache')
    except ValueError as exc:checks.append({'missing_cache_rejected':str(exc)})
    else:raise AssertionError('Accepted missing circle cache')
    assert inputs=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (foreground,background,video)}
    checks.append('input bytes unchanged')
    report={'status':'passed','checks_count':len(checks),'checks':checks,'builds':builds,'input_sha256':inputs,
            'ui_edit_save_reopen':'unverified','rendering':'separate native frame verification'}
    editor.write_json(root/'circle-mask-check.json',report)
    return report


if __name__=='__main__':
    editor.sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--foreground',type=Path,required=True)
    parser.add_argument('--background',type=Path,required=True)
    parser.add_argument('--video',type=Path,required=True)
    args=parser.parse_args()
    editor.emit(check(args.out.resolve(),args.foreground.resolve(),args.background.resolve(),args.video.resolve()))
