"""Formal text global-alpha mapping and strict native clock/curve checks."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor,native_text_alpha as alpha


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    Image.new('RGB',(1280,720),(20,40,90)).save(root/'base.png')
    plan={'schema_version':1,'name':'文字透明度测试','mode':'script','canvas':{'width':1280,'height':720,'fps':30},
          'clips':[{'path':str(root/'base.png'),'start':0,'duration':3}],
          'captions':[{'text':'文字透明度','start':.5,'duration':2,'size':16,'x':0,'y':0,'border':True,
                       'style':{'auto_wrapping':False},'keyframes':[{'property':'alpha','time':0,'value':0},
                                                                 {'property':'alpha','time':1.5,'value':1}]}]}
    path=root/'plan.json';editor.write_json(path,plan);frozen=path.read_bytes()
    normalized,_,_=editor.validate_plan(path)
    result=editor.build_draft(Args(plan=str(path),out=str(root/'draft')))
    assert result['verification']['native_effects']['text_global_alpha']==1
    content=editor.read_json(root/'draft/draft_content.json')
    target=next(t['segments'][0] for t in content['tracks'] if t['type']=='text')
    assert target['common_keyframes'][0]['property_type']==alpha.PROPERTY
    assert [(p['time_offset'],p['values']) for p in target['common_keyframes'][0]['keyframe_list']]==[(0,[0]),(1500000,[1])]
    (root/'archive').mkdir()
    pub=editor.publish_draft(Args(draft=str(root/'draft'),draft_root=str(root/'archive'),offline_copy=True,user_data=None))
    editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),normalized)
    checks=['formal build/verify/publish maps text to GlobalAlpha and keeps local key clock','original plan unchanged']
    def reject(label,callback):
        try:callback()
        except (ValueError,TypeError):checks.append(label)
        else:raise AssertionError('Expected rejection: '+label)
    def invalid(label,mutate):
        p=copy.deepcopy(plan);mutate(p);editor.write_json(root/'invalid.json',p)
        reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    envelope=[{'property':'alpha','time':t,'value':v} for t,v in ((0,0),(.4,1),(.8,1),(1.5,0))]
    for label,colored,border in [('white',False,True),('colored',True,True),('no-border',True,False),('unordered',False,False)]:
        p=copy.deepcopy(plan);p['name']+='-'+label;item=p['captions'][0];item['keyframes']=copy.deepcopy(envelope);item['border']=border
        if colored:item['highlights']=[{'start':1,'end':3,'color':'#FF0000'}]
        if label=='unordered':item['keyframes'].reverse()
        envelope_path=root/(label+'-plan.json');editor.write_json(envelope_path,p);before=envelope_path.read_bytes()
        envelope_plan,_,_=editor.validate_plan(envelope_path)
        built=editor.build_draft(Args(plan=str(envelope_path),out=str(root/(label+'-draft'))))
        c=editor.read_json(Path(built['draft'])/'draft_content.json')
        segment=next(t['segments'][0] for t in c['tracks'] if t['type']=='text')
        assert [(k['time_offset'],k['values']) for k in segment['common_keyframes'][0]['keyframe_list']]==[
            (editor.us(k['time']),[k['value']]) for k in envelope]
        assert len(c['materials']['texts'])==1 and envelope_path.read_bytes()==before
        pub=editor.publish_draft(Args(draft=built['draft'],draft_root=str(root/'archive'),offline_copy=True,user_data=None))
        editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),envelope_plan)
        alpha.preview_check(c,Path(built['draft']))
        checks.append('four-point build/readback/preview/publish '+label)
        if label=='white':envelope_content,envelope_normalized,envelope_root=c,envelope_plan,Path(built['draft'])
    def invalid_envelope(label,mutate):
        def change(p):
            p['captions'][0]['keyframes']=copy.deepcopy(envelope);mutate(p['captions'][0])
        invalid('four-point '+label,change)
    for label,mutate in [
        ('five keys',lambda i:i['keyframes'].append({'property':'alpha','time':1.7,'value':0})),
        ('non-unit plateau',lambda i:i['keyframes'][1].update(value=.5)),
        ('nonzero end',lambda i:i['keyframes'][-1].update(value=.5)),
        ('reverse envelope',lambda i:[k.update(value=1-k['value']) for k in i['keyframes']]),
        ('duplicate middle time',lambda i:i['keyframes'][2].update(time=.4)),
        ('microsecond collision',lambda i:i['keyframes'][2].update(time=.4000001)),
        ('nonzero first',lambda i:i['keyframes'][0].update(time=.1)),
        ('quantized end',lambda i:i['keyframes'][-1].update(time=1.9999999)),
        ('mixed axis',lambda i:i['keyframes'][1].update(property='position_x')),
        ('static alpha',lambda i:i['style'].update(alpha=.5))]:
        invalid_envelope(label,mutate)
    def envelope_text(c):return next(t['segments'][0] for t in c['tracks'] if t['type']=='text')
    for label,mutate in [
        ('missing middle key',lambda g:g['keyframe_list'].pop(1)),
        ('wrong plateau value',lambda g:g['keyframe_list'][2].update(values=[.5])),
        ('wrong middle clock',lambda g:g['keyframe_list'][2].update(time_offset=900000)),
        ('wrong alpha kind',lambda g:g.update(property_type='KFTypeAlpha')),
        ('nonlinear curve',lambda g:g['keyframe_list'][1].update(curveType='CurveOut'))]:
        bad=copy.deepcopy(envelope_content);mutate(envelope_text(bad)['common_keyframes'][0])
        reject('four-point readback '+label,lambda:editor.verify_structure(bad,envelope_normalized))
        reject('four-point preview '+label,lambda:alpha.preview_check(bad,envelope_root))
    for key,value in [('style',{'auto_wrapping':True}),('style',{'auto_wrapping':False,'alpha':.5}),
                      ('color','#FF0000'),('bold',True),('transform',{'alpha':.5}),
                      ('text','文字\n透明度'),('text','文字😀透明度'),('font','励字志向黑简_特粗'),
                      ('highlights',[{'start':0,'end':99,'color':'#FF0000'}]),('motion_easing','smoothstep'),
                      ('glow',{}),('text_curve',{'angle':20})]:
        invalid('unsupported '+key+'='+str(value),lambda p,k=key,v=value:p['captions'][0].update({k:v}))
    for label,keys in [('three points',[{'property':'alpha','time':0,'value':0},{'property':'alpha','time':.5,'value':.5},{'property':'alpha','time':1.5,'value':1}]),
                       ('mixed axis',[{'property':'position_x','time':0,'value':0},{'property':'alpha','time':1,'value':1}]),
                       ('not at zero',[{'property':'alpha','time':.1,'value':0},{'property':'alpha','time':1,'value':1}]),
                       ('end at duration',[{'property':'alpha','time':0,'value':0},{'property':'alpha','time':2,'value':1}]),
                       ('bool value',[{'property':'alpha','time':0,'value':False},{'property':'alpha','time':1,'value':1}])]:
        invalid(label,lambda p,k=keys:p['captions'][0].update(keyframes=k))
    def text(c):return next(t['segments'][0] for t in c['tracks'] if t['type']=='text')
    for label,mutate in [
        ('video Alpha kind',lambda c:text(c)['common_keyframes'][0].update(property_type='KFTypeAlpha')),
        ('fill-only TextAlpha kind',lambda c:text(c)['common_keyframes'][0].update(property_type='KFTypeTextAlpha')),
        ('duplicate group',lambda c:text(c)['common_keyframes'].append(copy.deepcopy(text(c)['common_keyframes'][0]))),
        ('missing group',lambda c:text(c)['common_keyframes'].clear()),
        ('wrong local clock',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][0].update(time_offset=500000)),
        ('wrong value',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][0].update(values=[.5])),
        ('boolean value',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][1].update(values=[True])),
        ('nonlinear curve',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][0].update(curveType='CurveOut')),
        ('boolean handle',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][0]['left_control'].update(x=False)),
        ('graph',lambda c:text(c)['common_keyframes'][0]['keyframe_list'][0].update(graph={'extra':True})),
        ('material static alpha',lambda c:c['materials']['texts'][0].update(global_alpha=.5)),
        ('clip static alpha',lambda c:text(c)['clip'].update(alpha=.5)),
        ('boolean static alpha',lambda c:text(c)['clip'].update(alpha=True)),
        ('wrong video binding',lambda c:c['tracks'][0]['segments'][0]['common_keyframes'].append(copy.deepcopy(text(c)['common_keyframes'][0])))]:
        bad=copy.deepcopy(content);mutate(bad)
        reject('readback '+label,lambda:editor.verify_structure(bad,normalized))
        reject('preview '+label,lambda:alpha.preview_check(bad,root/'draft'))
    plain=copy.deepcopy(normalized);plain['captions'][0]['keyframes']=[]
    reject('unrequested group',lambda:editor.verify_structure(content,plain))
    with patch.object(editor,'default_text_font',return_value=None):reject('missing font',lambda:editor.validate_plan(path))
    with patch.object(editor,'jianying_install',return_value=root):reject('runtime version changed',lambda:editor.validate_plan(path))
    with patch.object(alpha.file_hash,'matches',return_value=False):reject('runtime identity changed',lambda:editor.validate_plan(path))
    reject('missing formal preview plan',lambda:alpha.preview_check(content,root))
    reject('explicit preview version differs from default installation',
           lambda:alpha.preview_check(content,root/'draft',root/'11.5.0.14471'))
    # Public video alpha keeps its original backend type.
    backend=editor.load_backend();segment=backend.VideoSegment(str(root/'base.png'),backend.Timerange(0,2000000))
    segment.add_keyframe(backend.KeyframeProperty.alpha,0,.5)
    assert segment.export_json()['common_keyframes'][0]['property_type']=='KFTypeAlpha'
    checks.append('video alpha remains KFTypeAlpha')
    assert path.read_bytes()==frozen
    report={'status':'passed','check_count':len(checks),'checks':checks,'source_plan_unchanged':True,'UI_save_reopen_export':'unverified'}
    editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
