"""Admission checks for measured image/video/text smoothstep and group translation; rendering is separate."""
import argparse,copy,hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, plan_path):
    root.mkdir(exist_ok=False);(root/'offline').mkdir()
    base=editor.read_json(plan_path)
    checks=[]
    sources={Path(x['path']):hashlib.sha256(Path(x['path']).read_bytes()).hexdigest() for x in base['clips']}
    for label in ['forward','backward']:
        candidate=copy.deepcopy(base);candidate['name']='缓动回归-'+label
        item=candidate['clips'][1]
        if label=='backward':
            for key in item['keyframes']:key['value']=-key['value']
        path=root/(label+'.json');editor.write_json(path,candidate)
        result=editor.build_draft(Args(plan=str(path),out=str(root/label)))
        content=editor.read_json(root/label/'draft_content.json');plan=editor.read_json(root/label/'edit-plan.json')
        assert result['verification']['native_effects']['extensions']['motion_easing']==1
        assert 'motion_easing' in (root/label/'原生效果.md').read_text(encoding='utf-8')
        editor.publish_draft(Args(draft=str(root/label),draft_root=str(root/'offline'),offline_copy=True))
        editor.verify_structure(editor.read_json(root/'offline'/candidate['name']/'draft_content.json'),plan)
        checks.extend([label+' build and full readback',label+' delivery exposes native settings',label+' offline publish and readback'])
    def mutate_output(name,mutation):
        altered=copy.deepcopy(content)
        segment=next(t for t in altered['tracks'] if t['name']=='ball')['segments'][0]
        group=segment['common_keyframes'][0]
        mutation(group)
        try:editor.verify_structure(altered,plan)
        except ValueError:checks.append(name+' rejected')
        else:raise AssertionError(name+' accepted')
    for name,mutation in {
        'curve type':lambda g:g['keyframe_list'][0].update(curveType='Line'),
        'right time handle':lambda g:g['keyframe_list'][0]['right_control'].update(x=0),
        'right value handle':lambda g:g['keyframe_list'][0]['right_control'].update(y=.1),
        'left time handle':lambda g:g['keyframe_list'][1]['left_control'].update(x=1),
        'graph reference':lambda g:g['keyframe_list'][0].update(graphID='unexpected'),
        'inline graph':lambda g:g['keyframe_list'][0].update(graph={}),
        'missing point':lambda g:g['keyframe_list'].pop(),
        'wrong time':lambda g:g['keyframe_list'][1].update(time_offset=1),
        'wrong value':lambda g:g['keyframe_list'][0].update(values=[.2]),
    }.items():mutate_output(name,mutation)
    def reject(name,mutation):
        candidate=copy.deepcopy(base);mutation(candidate);path=root/'invalid.json';editor.write_json(path,candidate)
        try:editor.validate_plan(path)
        except ValueError:checks.append(name+' rejected')
        else:raise AssertionError(name+' accepted')
    for name,mutation in {
        'unknown preset':lambda p:p['clips'][1].update(motion_easing='elastic'),
        'wrong type':lambda p:p['clips'][1].update(motion_easing=True),
        'third point':lambda p:p['clips'][1]['keyframes'].append(dict(property='position_x',time=2,value=0)),
        'vertical property':lambda p:p['clips'][1]['keyframes'][0].update(property='position_y'),
        'missing points':lambda p:p['clips'][1].update(keyframes=[]),
        'nonzero first':lambda p:p['clips'][1]['keyframes'][0].update(time=.1),
        'end boundary':lambda p:p['clips'][1]['keyframes'][1].update(time=p['clips'][1]['duration']),
        'duplicate time':lambda p:p['clips'][1]['keyframes'][1].update(time=0),
        'nonfinite value':lambda p:p['clips'][1]['keyframes'][0].update(value=float('nan')),
        'caption missing points':lambda p:p.update(captions=[dict(text='test',start=0,duration=1,motion_easing='smoothstep')]),
        'composite combination':lambda p:p.update(composites=[dict(name='group',tracks=['video','ball'])]),
        'malformed composite tracks':lambda p:p.update(composites=[dict(name='group',tracks=None)]),
    }.items():reject(name,mutation)
    # A real speed=1 video is admitted; retiming remains a separate boundary.
    video=root/'video-fixture.mp4'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=white:s=320x180:r=30:d=1.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)])
    candidate=copy.deepcopy(base);candidate['clips'][1].update(path=str(video),duration=1.1)
    candidate['clips'][1]['keyframes'][1]['time']=1
    path=root/'video-boundary.json';editor.write_json(path,candidate)
    editor.build_draft(Args(plan=str(path),out=str(root/'video-admitted')))
    checks.append('speed=1 video easing build/readback admitted')
    candidate['clips'][1]['source_in']=.2
    editor.write_json(path,candidate)
    editor.build_draft(Args(plan=str(path),out=str(root/'video-source-offset')))
    checks.append('video easing nonzero source-time build/readback admitted')
    vertical=copy.deepcopy(candidate)
    for key in vertical['clips'][1]['keyframes']:key['property']='position_y'
    editor.write_json(root/'video-vertical.json',vertical)
    editor.build_draft(Args(plan=str(root/'video-vertical.json'),out=str(root/'video-vertical')))
    output=editor.read_json(root/'video-vertical/draft_content.json');normalized=editor.read_json(root/'video-vertical/edit-plan.json')
    segment=next(t for t in output['tracks'] if t['name']=='ball')['segments'][0]
    group=segment['common_keyframes'][0]
    assert group['property_type']=='KFTypePositionY' and [k['time_offset'] for k in group['keyframe_list']]==[200000,1200000]
    checks.append('video vertical easing axis and source-time readback')
    pub=editor.publish_draft(Args(draft=str(root/'video-vertical'),draft_root=str(root/'offline'),offline_copy=True))
    editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),normalized)
    checks.append('video vertical easing offline publish/readback')
    for label,mutate in {
        'axis':lambda g:g.update(property_type='KFTypePositionX'),
        'handle':lambda g:g['keyframe_list'][0]['right_control'].update(x=123),
        'time':lambda g:g['keyframe_list'][1].update(time_offset=1000000),
    }.items():
        previous=copy.deepcopy(segment['common_keyframes']);mutate(segment['common_keyframes'][0])
        try:editor.verify_structure(output,normalized)
        except ValueError:checks.append('video vertical '+label+' tamper rejected')
        else:raise AssertionError('video vertical '+label+' accepted')
        segment['common_keyframes']=previous
    candidate['clips'][1]['speed']=1.1
    editor.write_json(path,candidate)
    try:editor.validate_plan(path)
    except ValueError as error:
        assert 'motion_easing' in str(error),str(error);checks.append('video easing with retiming rejected')
    else:raise AssertionError('Untested retimed video easing accepted')
    plain=copy.deepcopy(base);plain['name']='未请求缓动对照';plain['clips'][1].pop('motion_easing')
    path=root/'plain.json';editor.write_json(path,plain);editor.build_draft(Args(plan=str(path),out=str(root/'plain')))
    line=editor.read_json(root/'plain/draft_content.json');normal=editor.read_json(root/'plain/edit-plan.json')
    group=next(t for t in line['tracks'] if t['name']=='ball')['segments'][0]['common_keyframes'][0]
    assert all(k['curveType']=='Line' for k in group['keyframe_list']);checks.append('unrequested curve stays linear')
    group['keyframe_list'][0]['right_control']['x']=123
    try:editor.verify_structure(line,normal)
    except ValueError:checks.append('unrequested linear handle mutation rejected')
    else:raise AssertionError('Unrequested handle accepted')
    textbase=copy.deepcopy(plain);textbase['clips']=textbase['clips'][:1];textbase.pop('layer_order',None)
    textbase['captions']=[dict(text='文字停读 ABC',font='励字志向黑简_特粗',start=.3,duration=2.6,
        track='title',x=0,y=0,size=9,border=False,motion_easing='smoothstep',
        keyframes=[dict(property='position_x',time=0,value=-.3),dict(property='position_x',time=2,value=.3)])]
    for label in ('text-forward','text-backward','text-linear'):
        candidate=copy.deepcopy(textbase);candidate['name']=label;cap=candidate['captions'][0]
        if label=='text-backward':
            for key in cap['keyframes']:key['value']=-key['value']
        if label=='text-linear':cap.pop('motion_easing')
        path=root/(label+'.json');editor.write_json(path,candidate)
        built=editor.build_draft(Args(plan=str(path),out=str(root/label)))
        normalized=editor.read_json(root/label/'edit-plan.json');output=editor.read_json(root/label/'draft_content.json')
        segment=next(t for t in output['tracks'] if t['name']=='title')['segments'][0]
        assert [k['time_offset'] for k in segment['common_keyframes'][0]['keyframe_list']]==[0,2000000]
        assert built['verification']['native_effects']['extensions'].get('motion_easing',0)==int(label!='text-linear')
        pub=editor.publish_draft(Args(draft=str(root/label),draft_root=str(root/'offline'),offline_copy=True))
        editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),normalized)
        checks.extend([label+' local-time keys and font readback',label+' count and offline publish'])
        for name,mutate in {
            'curve':lambda g:g['keyframe_list'][0].update(curveType='CurveIn'),
            'handle':lambda g:g['keyframe_list'][0]['right_control'].update(x=123),
            'time':lambda g:g['keyframe_list'][1].update(time_offset=2300000),
            'value':lambda g:g['keyframe_list'][0].update(values=[0]),
            'graph':lambda g:g['keyframe_list'][0].update(graph={}),
            'extra group':lambda g:segment['common_keyframes'].append(copy.deepcopy(g)),
        }.items():
            original=copy.deepcopy(segment['common_keyframes']);mutate(segment['common_keyframes'][0])
            try:editor.verify_structure(output,normalized)
            except ValueError:checks.append(label+' '+name+' tamper rejected')
            else:raise AssertionError(label+' '+name+' tamper accepted')
            segment['common_keyframes']=original
    for name,mutate in {
        'preset':lambda c:c.update(motion_easing='elastic'),
        'vertical key':lambda c:c['keyframes'][0].update(property='position_y'),
        'third point':lambda c:c['keyframes'].append(dict(property='position_x',time=2.1,value=0)),
        'nonzero first':lambda c:c['keyframes'][0].update(time=.1),
        'end boundary':lambda c:c['keyframes'][1].update(time=2.6),
        'transform':lambda c:c.update(transform={'rotation':10}),
        'text curve':lambda c:c.update(text_curve={'angle':30}),
        'vertical layout':lambda c:c.update(style={'vertical':True}),
        'animation':lambda c:c.update(animations=[dict(stage='intro',name='渐显',duration=.2)]),
    }.items():
        candidate=copy.deepcopy(textbase);mutate(candidate['captions'][0]);path=root/'invalid-text.json';editor.write_json(path,candidate)
        try:editor.validate_plan(path)
        except ValueError:checks.append('text '+name+' rejected')
        else:raise AssertionError('text '+name+' accepted')
    candidate=copy.deepcopy(textbase);candidate['layer_order']=['video','title']
    candidate['composites']=[dict(name='text-group',tracks=['video','title'])]
    path=root/'invalid-text.json';editor.write_json(path,candidate)
    try:editor.validate_plan(path)
    except ValueError as error:
        assert 'motion_easing' in str(error);checks.append('text inside composite rejected')
    else:raise AssertionError('text inside composite accepted')
    groupbase=copy.deepcopy(textbase)
    groupbase['captions'][0].pop('motion_easing');groupbase['captions'][0].pop('keyframes')
    groupbase['layer_order']=['video','title']
    groupbase['composites']=[dict(name='shared',tracks=['video','title'],
        keyframes=[dict(property='position_x',time=0,value=-.2),dict(property='position_x',time=2,value=.2)])]
    for prop in ('position_x','position_y','rotation','uniform_scale'):
        candidate=copy.deepcopy(groupbase);candidate['name']='group-'+prop
        for key in candidate['composites'][0]['keyframes']:key['property']=prop
        if prop=='uniform_scale':
            for key,value in zip(candidate['composites'][0]['keyframes'],(.8,1.1)):key['value']=value
        path=root/(candidate['name']+'.json');editor.write_json(path,candidate)
        editor.build_draft(Args(plan=str(path),out=str(root/candidate['name'])))
        output=editor.read_json(root/candidate['name']/'draft_content.json');normalized=editor.read_json(root/candidate['name']/'edit-plan.json')
        pub=editor.publish_draft(Args(draft=str(root/candidate['name']),draft_root=str(root/'offline'),offline_copy=True))
        published=Path(pub['draft']);editor.verify_structure(editor.read_json(published/'draft_content.json'),normalized)
        editor.native_advanced.verify_sidecars(editor.read_json(published/'draft_content.json'),published,published,editor.read_json)
        checks.append('group '+prop+' build/readback/publish/sidecars')
        segment=output['tracks'][0]['segments'][0]
        for name,mutate in {
            'value':lambda g:g['keyframe_list'][0].update(values=[1.19]),
            'time':lambda g:g['keyframe_list'][0].update(time_offset=300000),
            'handle':lambda g:g['keyframe_list'][0]['right_control'].update(x=123),
            'curve':lambda g:g['keyframe_list'][0].update(curveType='CurveOut'),
            'property':lambda g:g.update(property_type='KFTypeAlpha'),
            'extra group':lambda g:segment['common_keyframes'].append(copy.deepcopy(g)),
        }.items():
            original=copy.deepcopy(segment['common_keyframes']);mutate(segment['common_keyframes'][0])
            try:editor.verify_structure(output,normalized)
            except ValueError:checks.append('group '+prop+' '+name+' tamper rejected')
            else:raise AssertionError('group '+prop+' '+name+' accepted')
            segment['common_keyframes']=original
    for name,mutate in {
        'mixed properties':lambda p:p['composites'][0]['keyframes'][1].update(property='rotation'),
        'third point':lambda p:p['composites'][0]['keyframes'].append(dict(property='position_x',time=3,value=.1)),
        'perspective':lambda p:p['composites'][0].update(perspective={'keyframes':[dict(property='up_left',time=0,values=[-1,-1])]}),
        'out of range':lambda p:p['composites'][0]['keyframes'][0].update(value=1.1),
        'text motion':lambda p:p['captions'][0].update(keyframes=[dict(property='position_x',time=0,value=0)]),
        'image motion':lambda p:p['clips'][0].update(keyframes=[dict(property='position_x',time=0,value=0)]),
        'unmeasured alpha':lambda p:p['composites'][0]['keyframes'][0].update(property='alpha'),
    }.items():
        candidate=copy.deepcopy(groupbase);mutate(candidate);path=root/'invalid-group.json';editor.write_json(path,candidate)
        try:editor.validate_plan(path)
        except ValueError:checks.append('group '+name+' rejected')
        else:raise AssertionError('group '+name+' accepted')
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in sources.items());checks.append('input bytes unchanged')
    report={'status':'passed','checks_count':len(checks),'checks':checks,'rendering':'separate measured native frames','ui':'separate save/reopen/edit evidence'}
    editor.write_json(root/'回归结果.json',report);return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True,type=Path);parser.add_argument('--plan',required=True,type=Path)
    args=parser.parse_args();editor.emit(check(args.out.resolve(),args.plan.resolve()))
