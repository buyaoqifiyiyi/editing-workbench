"""Text glow contract, cache identity, generation and offline publication."""
from pathlib import Path
from types import SimpleNamespace as Args
import copy,json,argparse
import editor,native_text_glow


def check(root):
    root.mkdir(exist_ok=False,parents=True)
    source=Path(__file__).resolve().parents[1]/'assets/native-text-glow-11.5.json'
    # Reuse the bundled Python PNG dependency, no generated video fixtures.
    from PIL import Image
    background=root/'black.png';Image.new('RGB',(640,360),'black').save(background)
    base={'schema_version':1,'name':'文字发光边界','mode':'script','canvas':{'width':640,'height':360,'fps':60},
          'clips':[{'path':str(background),'start':0,'duration':2}],
          'captions':[{'text':'原生 ABC','start':.2,'duration':1.6,'size':12,'y':0,'border':False,'font':'励字志向黑简_特粗',
                       'style':{'auto_wrapping':False},'glow':{'name':'放射发光','color':'#4080FF','strength':.2,'range':.3}},
                      {'text':'关闭对照','start':0,'duration':.2,'border':False}]}
    checks=[]
    def rejected(label,call):
        try:call()
        except (ValueError,FileNotFoundError):checks.append(label)
        else:raise AssertionError('Accepted '+label)
    path=root/'plan.json';editor.write_json(path,base)
    built=editor.build_draft(Args(plan=str(path),out=str(root/'draft')))
    content=editor.read_json(root/'draft/draft_content.json');plan=editor.read_json(root/'draft/edit-plan.json')
    assert built['verification']['native_effects']['counts']['text_glows']==1
    checks.append('build/readback')
    (root/'offline').mkdir();pub=editor.publish_draft(Args(draft=str(root/'draft'),draft_root=str(root/'offline'),offline_copy=True))
    editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),plan)
    assert 'glow' in (Path(pub['draft'])/'原生效果.md').read_text('utf-8')
    checks.append('offline publish/sidecars/delivery')
    for key,value in [('name','unknown'),('color','#FFF'),('strength',0),('strength',True),('range',1.1),('path','C:/guess')]:
        p=copy.deepcopy(base);p['captions'][0]['glow'][key]=value;editor.write_json(root/'invalid.json',p)
        rejected('invalid glow '+key+repr(value),lambda:editor.validate_plan(root/'invalid.json'))
    for key,value in [('border',True),('bold',True),('color','#FF0000'),('style',{}),('style',{'auto_wrapping':False,'shadow':{'alpha':.2}}),
                      ('keyframes',[{'time':0,'property':'position_x','value':0}]),('transform',{'rotation':0}),
                      ('text_curve',{'angle':20}),('text','emoji😀'),('text','two\nlines')]:
        p=copy.deepcopy(base);p['captions'][0][key]=value;editor.write_json(root/'invalid.json',p)
        rejected('unverified combination '+key+repr(value),lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);p['allow_vip_effects']=False;editor.write_json(root/'invalid.json',p)
    rejected('VIP disabled',lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);del p['captions'][0]['font'];editor.write_json(root/'invalid.json',p)
    rejected('unverified default font',lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);p['captions'][0]['style']=[];editor.write_json(root/'invalid.json',p)
    rejected('malformed style',lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);p['composites']=[{'name':'group','tracks':['video','captions']}];editor.write_json(root/'invalid.json',p)
    rejected('composite glow',lambda:editor.validate_plan(root/'invalid.json'))
    for key,value in [('resource_id','wrong'),('effect_id','wrong'),('path','C:/missing'),('type','video_effect'),
                      ('bloom_params',{'color':'#4080FFFF','strength':.21,'range':.3,'dir_x':.5,'dir_y':.5})]:
        d=copy.deepcopy(content);d['materials']['effects'][0][key]=value
        rejected('tamper '+key,lambda d=d:editor.verify_native(d,plan))
    for kind in ('missing-ref','duplicate-ref','missing-flag','orphan','unrequested-flag'):
        d=copy.deepcopy(content);seg=next(s for t in d['tracks'] if t['type']=='text' for s in t['segments'] if s['target_timerange']['start']==200000)
        text=next(t for t in d['materials']['texts'] if t['id']==seg['material_id'])
        if kind=='missing-ref':seg['extra_material_refs'].clear()
        elif kind=='duplicate-ref':seg['extra_material_refs']*=2
        elif kind=='missing-flag':text['check_flag']&=~64
        elif kind=='orphan':m=copy.deepcopy(d['materials']['effects'][0]);m['id']='orphan';d['materials']['effects'].append(m)
        else:next(t for t in d['materials']['texts'] if t['id']!=seg['material_id'])['check_flag']|=64
        rejected(kind,lambda d=d:editor.verify_native(d,plan))
    spec=base['captions'][0]['glow']
    rejected('absent cache',lambda:native_text_glow.material(spec,root/'absent'))
    admitted=json.loads(source.read_text('utf-8'))['放射发光'];fake=root/'fake'/admitted['cache_relative'];fake.mkdir(parents=True)
    first=next(iter(admitted['files']));(fake/first).parent.mkdir(exist_ok=True,parents=True);(fake/first).write_bytes(b'changed')
    rejected('changed package bytes',lambda:native_text_glow.material(spec,root/'fake'))
    catalog=editor.native_catalog(Args(kind='text_glows',search='',free_only=False,limit=10,details=True))
    assert catalog['text_glows']==['放射发光'];checks.append('catalog scope and parameters')
    assert editor.native_catalog(Args(kind='text_glows',search='',free_only=True,limit=10,details=False))['text_glows']==[]
    checks.append('catalog free-only filter')
    editor.write_json(root/'checks.json',{'status':'passed','checks_count':len(checks),'checks':checks})
    return {'status':'passed','checks_count':len(checks)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True,type=Path)
    editor.emit(check(p.parse_args().out.resolve()))
