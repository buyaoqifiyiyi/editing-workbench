"""Five measured static HSL channels with single-axis controls and saturation/lightness pairs: formal paths and refusal boundaries."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from PIL import Image
import editor,native_hsl_channels as hsl
def check(root):
    root.mkdir(parents=True,exist_ok=False);Image.new('RGB',(640,360),(192,120,48)).save(root/'base.png');(root/'archive').mkdir();checks=[]
    base={'schema_version':1,'name':'extra-HSL-check','mode':'script','canvas':{'width':640,'height':360,'fps':30},
          'clips':[{'path':str(root/'base.png'),'start':0,'duration':2,'grading':{'orange_hsl':{'saturation':0}}}]}
    def reject(label,call):
        try:call()
        except (ValueError,TypeError,OSError):checks.append(label)
        else:raise AssertionError('Expected refusal: '+label)
    for name,(channel,title) in hsl.CHANNELS.items():
        cases=[({k:v},k+str(v)) for k,values in hsl.CONTROLS.items() for v in values]+[({'saturation':sat,'lightness':light},'pairS'+str(sat)+'L'+str(light)) for sat in hsl.CONTROLS['saturation'] for light in hsl.CONTROLS['lightness']]
        for spec,suffix in cases:
            p=copy.deepcopy(base);p['name']+=name+suffix;p['clips'][0]['grading']={name:spec}
            path=root/(name+suffix+'.json');editor.write_json(path,p);before=path.read_bytes();n,_,_=editor.validate_plan(path)
            built=editor.build_draft(Args(plan=str(path),out=str(root/(name+suffix+'-draft'))));draft=Path(built['draft']);c=editor.read_json(draft/'draft_content.json')
            m=c['materials']['hsl'][0];assert m['hsl_color_type']==channel and all(m[k]==spec.get(k,0) for k in ('hue','saturation','lightness'))
            assert all(title+{'saturation':'饱和度','lightness':'明度','hue':'色相'}[k]+'：'+str(v) in (draft/'剪辑交付.md').read_text(encoding='utf-8') for k,v in spec.items())
            pub=editor.publish_draft(Args(draft=str(draft),draft_root=str(root/'archive'),offline_copy=True,user_data=None));editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),n)
            hsl.preview_check(c,draft,editor.jianying_install());assert path.read_bytes()==before;checks.append('formal '+name+suffix)
            for label,mutate in [('wrong channel',lambda c:c['materials']['hsl'][0].update(hsl_color_type=1)),
                ('bool hue',lambda c:c['materials']['hsl'][0].update(hue=False)),
                ('missing material',lambda c:c['materials']['hsl'].clear()),('missing ref',lambda c:c['tracks'][0]['segments'][0]['extra_material_refs'].remove(c['materials']['hsl'][0]['id'])),
                ('wrong fps',lambda c:c.update(fps=24)),('bool flag',lambda c:c['tracks'][0]['segments'][0].update(enable_hsl=1))]+[
                ('other axis '+axis,lambda c,a=axis:c['materials']['hsl'][0].update(**{a:10})) for axis in hsl.CONTROLS if axis not in spec]+[('wrong value '+axis,lambda c,a=axis:c['materials']['hsl'][0].update(**{a:10})) for axis in spec]:
                bad=copy.deepcopy(c);mutate(bad);reject('readback '+name+label,lambda:editor.verify_structure(bad,n));reject('preview '+name+label,lambda:hsl.preview_check(bad,draft,editor.jianying_install()))
            reject('actual install '+name,lambda:hsl.preview_check(c,draft,root/'11.5.0.14471'))
    for label,mutate in [('native alpha',lambda c:c['tracks'][0]['segments'][0]['clip'].update(alpha=.5)),
        ('native rotation',lambda c:c['tracks'][0]['segments'][0]['clip'].update(rotation=20)),
        ('native bool scale',lambda c:c['tracks'][0]['segments'][0]['clip']['scale'].update(x=True)),
        ('native key injection',lambda c:c['tracks'][0]['segments'][0]['common_keyframes'].append({'property_type':'KFTypeAlpha','keyframe_list':[]})),
        ('crop change',lambda c:c['materials']['videos'][0]['crop'].update(upper_left_x=.2)),
        ('crop bool',lambda c:c['materials']['videos'][0]['crop'].update(upper_left_x=False)),
        ('source clock',lambda c:c['tracks'][0]['segments'][0]['source_timerange'].update(start=1000)),
        ('extra effect ref',lambda c:c['tracks'][0]['segments'][0]['extra_material_refs'].append('unknown'))]:
        bad=copy.deepcopy(c);mutate(bad);reject('readback '+label,lambda:editor.verify_structure(bad,n));reject('preview '+label,lambda:hsl.preview_check(bad,draft,editor.jianying_install()))
    def invalid(label,mutate):
        p=copy.deepcopy(base);mutate(p);editor.write_json(root/'invalid.json',p);reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    for value in (-61,-30,1,61,True,float('nan')):invalid('invalid value '+str(value),lambda p,v=value:p['clips'][0]['grading']['orange_hsl'].update(saturation=v))
    for value in (-31,-1,1,31,60,True,float('nan'),float('inf')):invalid('invalid lightness '+str(value),lambda p,v=value:p['clips'][0]['grading'].update(orange_hsl={'lightness':v}))
    for value in (-31,-1,1,31,60,False,True,float('nan'),float('inf'),'30'):invalid('invalid hue '+str(value),lambda p,v=value:p['clips'][0]['grading'].update(orange_hsl={'hue':v}))
    invalid('lightness plus hue',lambda p:p['clips'][0]['grading'].update(orange_hsl={'lightness':0,'hue':0}))
    for label,mutate in [('empty control',lambda p:p['clips'][0]['grading'].update(orange_hsl={})),
        ('all three controls',lambda p:p['clips'][0]['grading']['orange_hsl'].update(lightness=0,hue=0)),('extra component',lambda p:p['clips'][0]['grading']['orange_hsl'].update(hue=0)),
        ('paired new channels',lambda p:p['clips'][0]['grading'].update(cyan_hsl={'saturation':0})),('old mixed',lambda p:p['clips'][0]['grading'].update(red_hsl={})),
        ('curve',lambda p:p['clips'][0]['grading'].update(luma_curve=[[0,0],[.5,.4],[1,1]])),('wrong duration',lambda p:p['clips'][0].update(duration=1)),
        ('wrong canvas',lambda p:p['canvas'].update(width=1280)),('transform',lambda p:p['clips'][0].update(transform={'alpha':.5})),
        ('caption',lambda p:p.update(captions=[{'text':'extra','start':0,'duration':2}])),('extra clip',lambda p:p['clips'].append(dict(p['clips'][0],start=2)))]:invalid(label,mutate)
    for axis,values in [('saturation',(-61,-30,1,61,True,float('nan'))),('lightness',(-31,1,31,60,False,float('inf'),'30'))]:
        for value in values:invalid('invalid pair '+axis+str(value),lambda p,k=axis,v=value:p['clips'][0]['grading'].update(orange_hsl={'saturation':v if k=='saturation' else 0,'lightness':v if k=='lightness' else 0}))
    editor.run([editor.tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-n','-f','lavfi','-i','color=c=cyan:s=640x360:r=30:d=3','-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',root/'video.mp4'])
    invalid('video not admitted',lambda p:p['clips'][0].update(path=str(root/'video.mp4'),source_in=.5))
    foreign=root/'foreign.exe';foreign.write_bytes(b'unverified Bun');reject('actual foreign Bun',lambda:hsl.preview_check(c,draft,editor.jianying_install(),foreign))
    reject('missing formal plan',lambda:hsl.preview_check(c,root,editor.jianying_install()))
    report={'status':'passed','check_count':len(checks),'checks':checks,'UI_save_reopen_export':'unverified'};editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
