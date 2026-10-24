"""Formal additional corner motion, strict scene and native readback/preview guards."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor,native_advanced as native

def check(root):
    root.mkdir(parents=True,exist_ok=False);Image.new('RGB',(640,360),(40,90,170)).save(root/'base.png')
    base={'schema_version':1,'name':'corner-motion-check','mode':'script','canvas':{'width':640,'height':360,'fps':30},
          'clips':[{'path':str(root/'base.png'),'start':0,'duration':2}],
          'captions':[{'text':'透视PIN','start':0,'duration':2,'track':'title','size':10,'x':0,'y':0}],
          'layer_order':['video','title'],'composites':[{'name':'pin-group','tracks':['video','title'],
          'perspective':{'keyframes':[{'property':'up_right','time':0,'values':[1,-1]},
                                    {'property':'up_right','time':1.5,'values':[.5,-.5]}]}}]}
    cases=[];(root/'archive').mkdir()
    def reject(label,call):
        try:call()
        except (ValueError,TypeError):cases.append(label)
        else:raise AssertionError('Expected refusal: '+label)
    def invalid(label,mutate):
        p=copy.deepcopy(base);mutate(p);editor.write_json(root/'invalid.json',p)
        reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    for prop,kind,origin in [('up_right','KFTypeCornerPinUpRight',[1,-1]),('down_left','KFTypeCornerPinDownLeft',[-1,1]),
                             ('down_right','KFTypeCornerPinDownRight',[1,1])]:
        for reverse in (False,True):
            p=copy.deepcopy(base);p['name']+=prop+str(reverse);keys=p['composites'][0]['perspective']['keyframes']
            for key,value in zip(keys,[origin,[x*.5 for x in origin]][::(-1 if reverse else 1)]):key.update(property=prop,values=value)
            path=root/(p['name']+'.json');editor.write_json(path,p);before=path.read_bytes();normalized,_,_=editor.validate_plan(path)
            built=editor.build_draft(Args(plan=str(path),out=str(root/(p['name']+'-draft'))));draft=Path(built['draft'])
            c=editor.read_json(draft/'draft_content.json');outer=c['tracks'][0]['segments'][0]
            assert outer['common_keyframes'][0]['property_type']==kind
            assert [(k['time_offset'],k['values']) for k in outer['common_keyframes'][0]['keyframe_list']]==[(editor.us(k['time']),k['values']) for k in keys]
            pub=editor.publish_draft(Args(draft=str(draft),draft_root=str(root/'archive'),offline_copy=True,user_data=None))
            editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),normalized)
            native.corner_preview_check(c,draft,editor.jianying_install());assert path.read_bytes()==before
            cases.append('formal build/readback/publish/preview admission '+prop+' reverse='+str(reverse))
            for label,mutate in [
                ('missing group',lambda s:s['common_keyframes'].clear()),
                ('wrong kind',lambda s:s['common_keyframes'][0].update(property_type='KFTypeCornerPinUpLeft')),
                ('wrong clock',lambda s:s['common_keyframes'][0]['keyframe_list'][1].update(time_offset=1000000)),
                ('bool coordinate',lambda s:s['common_keyframes'][0]['keyframe_list'][0]['values'].__setitem__(0,True)),
                ('nonlinear curve',lambda s:s['common_keyframes'][0]['keyframe_list'][0].update(curveType='CurveOut')),
                ('graph',lambda s:s['common_keyframes'][0]['keyframe_list'][0].update(graphID='extra')),
                ('control handle',lambda s:s['common_keyframes'][0]['keyframe_list'][0]['left_control'].update(x=.1))]:
                bad=copy.deepcopy(c);mutate(bad['tracks'][0]['segments'][0])
                reject('readback '+prop+label,lambda:editor.verify_structure(bad,normalized))
                reject('preview '+prop+label,lambda:native.corner_preview_check(bad,draft,editor.jianying_install()))
            reject('actual runtime mismatch '+prop,lambda:native.corner_preview_check(c,draft,root/'11.5.0.14471'))
            bad=copy.deepcopy(c)
            video=next(m for m in bad['materials']['videos'] if m['id']==bad['tracks'][0]['segments'][0]['material_id'])
            video['corner_pin']['corner_pin_info']['lower_left_y']=True
            reject('boolean base corner '+prop,lambda:editor.verify_structure(bad,normalized))
            reject('preview boolean base corner '+prop,lambda:native.corner_preview_check(bad,draft,editor.jianying_install()))
    for label,mutate in [
        ('outer alpha',lambda c:c['tracks'][0]['segments'][0]['clip'].update(alpha=.1)),
        ('outer rotation',lambda c:c['tracks'][0]['segments'][0]['clip'].update(rotation=45)),
        ('outer flip',lambda c:c['tracks'][0]['segments'][0]['clip']['flip'].update(horizontal=True)),
        ('outer boolean scale',lambda c:c['tracks'][0]['segments'][0]['clip']['scale'].update(x=True)),
        ('outer dangling effect',lambda c:c['tracks'][0]['segments'][0]['extra_material_refs'].append('unresolved-effect')),
        ('synchronized fps',lambda c:(c.update(fps=24),c['materials']['drafts'][0]['draft'].update(fps=24))),
        ('synchronized dimensions',lambda c:(c['canvas_config'].update(width=1280),c['materials']['drafts'][0]['draft']['canvas_config'].update(width=1280)))]:
        bad=copy.deepcopy(c);mutate(bad)
        reject('readback '+label,lambda:editor.verify_structure(bad,normalized))
        reject('preview '+label,lambda:native.corner_preview_check(bad,draft,editor.jianying_install()))
    foreign=root/'foreign-bun.exe';foreign.write_bytes(b'not the verified runtime')
    reject('actual Bun override',lambda:native.corner_preview_check(c,draft,editor.jianying_install(),foreign))
    for label,mutate in [
        ('three points',lambda p:p['composites'][0]['perspective']['keyframes'].append({'property':'up_right','time':1.8,'values':[1,-1]})),
        ('mixed corners',lambda p:p['composites'][0]['perspective']['keyframes'][1].update(property='up_left')),
        ('unmeasured position',lambda p:p['composites'][0]['perspective']['keyframes'][1].update(values=[.6,-.6])),
        ('zero first',lambda p:p['composites'][0]['perspective']['keyframes'][0].update(time=.1)),
        ('duplicate clock',lambda p:p['composites'][0]['perspective']['keyframes'][1].update(time=.0000001)),
        ('key at end',lambda p:p['composites'][0]['perspective']['keyframes'][1].update(time=2)),
        ('wrong canvas',lambda p:p['canvas'].update(width=1280)),
        ('wrong fps',lambda p:p['canvas'].update(fps=24)),
        ('wrong duration',lambda p:p['composites'][0].update(start=0,duration=1.9)),
        ('text style',lambda p:p['captions'][0].update(style={'auto_wrapping':False})),
        ('text newline',lambda p:p['captions'][0].update(text='A\nB')),
        ('clip transform',lambda p:p['clips'][0].update(transform={'alpha':.5})),
        ('cover',lambda p:p['clips'][0].update(fit='cover'))]:invalid(label,mutate)
    with patch.object(native.file_hash,'matches',return_value=False):reject('runtime identity',lambda:editor.validate_plan(path))
    reject('missing formal plan',lambda:native.corner_preview_check(c,root,editor.jianying_install()))
    # Existing left-upper motion remains unbounded by the new preset-only subset.
    p=copy.deepcopy(base)
    for k in p['composites'][0]['perspective']['keyframes']:k['property']='up_left'
    p['composites'][0]['perspective']['keyframes'][1]['values']=[-.684,-.714]
    editor.write_json(root/'legacy.json',p);editor.validate_plan(root/'legacy.json');cases.append('existing up_left contract retained')
    report={'status':'passed','check_count':len(cases),'checks':cases,'UI_save_reopen_export':'unverified'}
    editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
