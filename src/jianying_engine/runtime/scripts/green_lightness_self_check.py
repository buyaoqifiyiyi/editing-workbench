"""Bounded native green-lightness construction, publication and actual-preview guards."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor,native_advanced as native

def check(root):
    root.mkdir(parents=True,exist_ok=False);Image.new('RGB',(640,360),(48,192,48)).save(root/'base.png')
    base={'schema_version':1,'name':'green-lightness','mode':'script','canvas':{'width':640,'height':360,'fps':30},
          'clips':[{'path':str(root/'base.png'),'start':0,'duration':2,'grading':{'green_hsl':{'lightness':0}}}]}
    (root/'archive').mkdir();checks=[]
    def reject(label,call):
        try:call()
        except (ValueError,TypeError,OSError):checks.append(label)
        else:raise AssertionError('Expected rejection: '+label)
    for value in (-30,0,30):
        p=copy.deepcopy(base);p['name']+=str(value);p['clips'][0]['grading']['green_hsl']['lightness']=value
        path=root/(str(value)+'.json');editor.write_json(path,p);before=path.read_bytes();n,_,_=editor.validate_plan(path)
        built=editor.build_draft(Args(plan=str(path),out=str(root/(str(value)+'-draft'))));draft=Path(built['draft'])
        c=editor.read_json(draft/'draft_content.json');m=c['materials']['hsl'][0]
        assert (m['hsl_color_type'],m['lightness'],m['hue'],m['saturation'])==(4,value,0,0)
        assert '绿色明度' in (draft/'剪辑交付.md').read_text(encoding='utf-8')
        pub=editor.publish_draft(Args(draft=str(draft),draft_root=str(root/'archive'),offline_copy=True,user_data=None))
        editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),n)
        native.green_lightness_preview_check(c,draft,editor.jianying_install());assert path.read_bytes()==before
        checks.append('formal build/readback/publish/preview lightness '+str(value))
        for label,mutate in [('wrong channel',lambda c:c['materials']['hsl'][0].update(hsl_color_type=6)),
            ('wrong value',lambda c:c['materials']['hsl'][0].update(lightness=10)),
            ('bool unused component',lambda c:c['materials']['hsl'][0].update(hue=False)),
            ('missing material',lambda c:c['materials']['hsl'].clear()),
            ('wrong path',lambda c:c['materials']['hsl'][0].update(path='missing')),
            ('wrong flag',lambda c:c['tracks'][0]['segments'][0].update(enable_hsl=False)),
            ('bool-like flag',lambda c:c['tracks'][0]['segments'][0].update(enable_hsl=1)),
            ('wrong fps',lambda c:c.update(fps=24))]:
            bad=copy.deepcopy(c);mutate(bad)
            reject('readback '+label,lambda:editor.verify_structure(bad,n))
            reject('preview '+label,lambda:native.green_lightness_preview_check(bad,draft,editor.jianying_install()))
        reject('actual preview install '+str(value),lambda:native.green_lightness_preview_check(c,draft,root/'11.5.0.14471'))
    def invalid(label,mutate):
        p=copy.deepcopy(base);mutate(p);editor.write_json(root/'invalid.json',p)
        reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    for value in (-31,-15,1,31,True,float('nan')):invalid('unmeasured value '+str(value),lambda p,v=value:p['clips'][0]['grading']['green_hsl'].update(lightness=v))
    for label,mutate in [('mixed green controls',lambda p:p['clips'][0]['grading']['green_hsl'].update(hue=0)),
        ('blue pairing',lambda p:p['clips'][0]['grading'].update(blue_hsl={'saturation':-60})),
        ('other duration',lambda p:p['clips'][0].update(duration=1)),('wrong canvas',lambda p:p['canvas'].update(width=1280)),
        ('transform',lambda p:p['clips'][0].update(transform={'alpha':.5})),
        ('caption',lambda p:p.update(captions=[{'text':'extra','start':0,'duration':2}]))]:invalid(label,mutate)
    editor.run([editor.tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-n','-f','lavfi','-i','color=c=green:s=640x360:r=30:d=3',
        '-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',root/'video.mp4'])
    invalid('video lightness excluded',lambda p:p['clips'][0].update(path=str(root/'video.mp4'),source_in=.5))
    with patch.object(native.file_hash,'matches',return_value=False):reject('runtime files changed',lambda:editor.validate_plan(path))
    reject('missing formal plan',lambda:native.green_lightness_preview_check(c,root,editor.jianying_install()))
    foreign=root/'foreign-bun.exe';foreign.write_bytes(b'unverified runtime')
    reject('actual foreign Bun override',lambda:native.green_lightness_preview_check(c,draft,editor.jianying_install(),foreign))
    report={'status':'passed','check_count':len(checks),'checks':checks,'UI_save_reopen_export':'unverified'}
    editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
