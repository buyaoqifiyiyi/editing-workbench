"""Native multi-style color ranges plus static/global-alpha opacity contract."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from PIL import Image
import editor
import native_text_highlight


def check(root):
    root.mkdir(parents=True,exist_ok=False)
    Image.new('RGB',(1280,720),(20,40,90)).save(root/'base.png')
    plan={'schema_version':1,'name':'多色字幕透明度组合','mode':'script','canvas':{'width':1280,'height':720,'fps':30},
          'clips':[{'path':str(root/'base.png'),'start':0,'duration':3}],
          'captions':[{'text':'甲乙丙丁戊己庚辛','start':.5,'duration':2,'size':16,'x':0,'y':0,'border':True,
                       'style':{'auto_wrapping':False},'highlights':[{'start':2,'end':5,'color':'#FF0000'},{'start':6,'end':8,'color':'#00FF00'}],
                       'keyframes':[{'property':'alpha','time':0,'value':0},{'property':'alpha','time':1.5,'value':1}]}]}
    path=root/'plan.json';editor.write_json(path,plan);before=path.read_bytes()
    p,_,_=editor.validate_plan(path);result=editor.build_draft(Args(plan=str(path),out=str(root/'draft')))
    assert result['verification']['native_effects']['text_global_alpha']==1
    assert result['verification']['native_effects']['counts']['caption_highlights']==2
    content=editor.read_json(root/'draft/draft_content.json')
    assert len(content['materials']['texts'])==1
    rich=json.loads(content['materials']['texts'][0]['content'])
    assert [s['range'] for s in rich['styles']]==[[0,2],[2,5],[5,6],[6,8]]
    assert rich['styles'][1]['fill']['content']['solid']['color']==[1.,0.,0.]
    (root/'archive').mkdir()
    pub=editor.publish_draft(Args(draft=str(root/'draft'),draft_root=str(root/'archive'),offline_copy=True,user_data=None))
    editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),p)
    static=copy.deepcopy(plan);static['captions'][0].pop('keyframes');static['captions'][0]['style']['alpha']=.5
    editor.write_json(root/'static.json',static);s,_,_=editor.validate_plan(root/'static.json')
    editor.build_draft(Args(plan=str(root/'static.json'),out=str(root/'static-draft')))
    c=editor.read_json(root/'static-draft/draft_content.json');assert c['materials']['texts'][0]['global_alpha']==.5
    assert len(json.loads(c['materials']['texts'][0]['content'])['styles'])==4
    checks=['formal one editable text material preserves color spans and dynamic GlobalAlpha',
            'formal static style.alpha preserves same native ranges','offline publication keeps both contracts']
    def reject(label,callback):
        try:callback()
        except (ValueError,TypeError):checks.append(label)
        else:raise AssertionError('Expected refusal: '+label)
    def invalid(label,mutate):
        q=copy.deepcopy(plan);mutate(q);editor.write_json(root/'invalid.json',q)
        reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    reject('static color opacity preview on another install',
           lambda:native_text_highlight.preview_check(c,root/'static-draft',root/'11.5.0.14471'))
    for key,value in [('bold',True),('color','#FF0000'),('style',{'auto_wrapping':False,'alpha':.5}),
                       ('keyframes',[{'property':'position_x','time':0,'value':0},{'property':'alpha','time':1.5,'value':1}]),
                       ('font','励字志向黑简_特粗'),('text','甲😀乙丙丁戊己庚辛'),('transform',{'scale_x':.5})]:
        invalid('unsupported '+key,lambda q,k=key,v=value:q['captions'][0].update({k:v}))
    for key,value in [('alpha',True),('alpha',float('nan')),('alpha',1.1)]:
        q=copy.deepcopy(static);q['captions'][0]['style'][key]=value;editor.write_json(root/'invalid.json',q)
        reject('invalid static opacity '+str(value),lambda:editor.validate_plan(root/'invalid.json'))
    for kind in ('color','key-clock','key-kind','missing-style'):
        bad=copy.deepcopy(content)
        if kind in ('color','missing-style'):
            r=json.loads(bad['materials']['texts'][0]['content'])
            if kind=='color':r['styles'][1]['fill']['content']['solid']['color']=[0.,0.,1.]
            else:r['styles'].pop(0)
            bad['materials']['texts'][0]['content']=json.dumps(r,ensure_ascii=False)
        else:
            seg=next(t['segments'][0] for t in bad['tracks'] if t['type']=='text')
            if kind=='key-kind':seg['common_keyframes'][0]['property_type']='KFTypeTextAlpha'
            else:seg['common_keyframes'][0]['keyframe_list'][0]['time_offset']=500000
        reject('readback '+kind,lambda:editor.verify_structure(bad,p))
    assert path.read_bytes()==before
    report={'status':'passed','check_count':len(checks),'checks':checks,'plan_unchanged':True,'UI_save_reopen_export':'unverified'}
    editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--out',type=Path,required=True)
    print(json.dumps(check(a.parse_args().out.resolve()),ensure_ascii=True,indent=2))
