"""Exercise admitted font generation, resource identity, tamper rejection and offline publication."""
import argparse,copy,hashlib,json
from pathlib import Path
from types import SimpleNamespace as Args
from PIL import Image
import editor


def check(root):
    root.mkdir(parents=True,exist_ok=False)
    source=root/'control.png';Image.new('RGB',(640,360),'#102030').save(source)
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    name='励字志向黑简_特粗'
    base={'schema_version':1,'name':'原生字体检查','mode':'script','canvas':{'width':640,'height':360,'fps':24},
          'clips':[{'path':str(source),'start':0,'duration':3}],
          'captions':[{'text':'原生字体 ABC 123\n保留编辑','start':0.3,'duration':2.2,'font':name,'y':0,
                       'size':12,'border':True,'bold':True,'style':{'shadow':{'alpha':0.5},'auto_wrapping':False}},
                      {'text':'默认字体','start':2.5,'duration':0.5,'border':False}]}
    checks=[];builds=[];ids=[]
    def rejected(label,call):
        try:call()
        except (ValueError,FileNotFoundError):checks.append(label)
        else:raise AssertionError('Accepted: '+label)
    for run in ('ordinary','composite'):
        p=copy.deepcopy(base);p['name']+='-'+run
        if run=='composite':p['composites']=[{'name':'原生字体组件','tracks':['video','captions']}]
        plan=root/(run+'.json');editor.write_json(plan,p)
        draft=root/run;built=editor.build_draft(Args(plan=str(plan),out=str(draft)))
        normalized=editor.read_json(draft/'edit-plan.json');content=editor.read_json(draft/'draft_content.json')
        expanded=editor.native_advanced.expanded_content(content,normalized) if run=='composite' else content
        assert built['verification']['native_effects']['counts']['text_fonts']==1
        text=expanded['materials']['texts'][0];rich=json.loads(text['content'])
        assert rich['styles'][0]['font']==editor.native_font(name)
        assert rich['text']==base['captions'][0]['text'] and rich['styles'][0]['bold']
        assert 'font' not in json.loads(expanded['materials']['texts'][1]['content'])['styles'][0]
        ids.append(text['id']);checks.append(run+': font/style/default readback')
        for label,mutation in {
            'font id':lambda s:s['font'].update(id='wrong'),
            'font path':lambda s:s['font'].update(path='D:'),
            'font removed':lambda s:s.pop('font'),
            'style changed':lambda s:s.update(bold=False),
            'duplicate font range':lambda s:s.update(range=[0,1]),
        }.items():
            corrupted=copy.deepcopy(expanded);r=json.loads(corrupted['materials']['texts'][0]['content'])
            mutation(r['styles'][0]);corrupted['materials']['texts'][0]['content']=json.dumps(r,ensure_ascii=False)
            rejected(run+': '+label,lambda c=corrupted:editor.verify_native(c,normalized))
        corrupted=copy.deepcopy(expanded);r=json.loads(corrupted['materials']['texts'][1]['content'])
        r['styles'][0]['font']=editor.native_font(name);corrupted['materials']['texts'][1]['content']=json.dumps(r,ensure_ascii=False)
        rejected(run+': unrequested font',lambda:editor.verify_native(corrupted,normalized))
        corrupted=copy.deepcopy(expanded);corrupted['materials']['texts'][0]['font_path']='D:'
        rejected(run+': injected top-level font',lambda:editor.verify_native(corrupted,normalized))
        (root/'offline').mkdir(exist_ok=True)
        editor.publish_draft(Args(draft=str(draft),draft_root=str(root/'offline'),offline_copy=True))
        published=root/'offline'/p['name']
        editor.verify_structure(editor.read_json(published/'draft_content.json'),normalized)
        editor.native_advanced.verify_sidecars(content,draft,draft,editor.read_json)
        assert name in (published/'原生效果.md').read_text(encoding='utf-8')
        checks.append(run+': offline publish/sidecars/delivery')
        builds.append(built)
    assert ids[0]!=ids[1];checks.append('fresh text IDs')
    for value in ('Arial','7316819223840297481','../font.ttf',True,{},None):
        p=copy.deepcopy(base);p['captions'][0]['font']=value;editor.write_json(root/'invalid.json',p)
        rejected('invalid name '+repr(value),lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);p['allow_vip_effects']=False;editor.write_json(root/'invalid.json',p)
    rejected('VIP disabled',lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(base);p['captions'][0]['text']='字😀';editor.write_json(root/'invalid.json',p)
    rejected('unverified non-BMP text',lambda:editor.validate_plan(root/'invalid.json'))
    for key,value in [('animations',[{'stage':'intro','name':'右上弹入','duration':0.2}]),
                      ('transform',{'rotation':10}),
                      ('keyframes',[{'time':0,'property':'position_y','value':0}])]:
        p=copy.deepcopy(base);p['captions'][0][key]=value;editor.write_json(root/'invalid.json',p)
        rejected('unverified font combination '+key,lambda:editor.validate_plan(root/'invalid.json'))
    rejected('absent cache',lambda:editor.native_font(name,user_data=root/'absent'))
    spec=editor.read_json(editor.SKILL_ROOT/'assets/native-fonts-11.5.json')[name]
    fake=root/'fake-user-data'/spec['cache_relative'];fake.parent.mkdir(parents=True);fake.write_bytes(b'not-a-font')
    rejected('changed cache bytes',lambda:editor.native_font(name,user_data=root/'fake-user-data'))
    real=Path(editor.native_font(name)['path'])
    assert hashlib.sha256(real.read_bytes()).hexdigest()==spec['sha256']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original
    checks+=['real font identity unchanged','source unchanged']
    result={'status':'passed','checks_count':len(checks),'checks':checks,'builds':builds,
            'font_sha256':spec['sha256'],'ui_edit_save_reopen':'unverified','native_render':'separate_evidence'}
    editor.write_json(root/'font-check.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    editor.emit(check(p.parse_args().out.resolve()))
