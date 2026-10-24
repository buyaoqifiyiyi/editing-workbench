"""Caption CLI to editable karaoke draft integration and refusal checks; no ASR or UI."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
import captions,editor,transcribe,karaoke

def check(root):
    root.mkdir(parents=True,exist_ok=False);checks=[]
    video=root/'video.mp4';editor.run([editor.tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-n','-f','lavfi','-i','color=c=0x18243c:s=640x360:r=30:d=12','-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',video])
    plan={'schema_version':1,'name':'Karaoke flow','mode':'script','canvas':{'width':640,'height':360,'fps':30},'clips':[{'path':str(video),'start':0,'duration':4,'source_in':2},{'path':str(video),'start':4,'duration':2,'source_in':3,'speed':2}]}
    path=root/'plan.json';editor.write_json(path,plan);original=path.read_bytes();srt=root/'words.srt';srt.write_text('1\n00:00:02,200 --> 00:00:02,800\n今天\n\n2\n00:00:03,000 --> 00:00:03,800\n开始\n\n3\n00:00:04,000 --> 00:00:04,800\n剪辑\n',encoding='utf-8');srt_before=srt.read_bytes()
    def options(**kw):
        d=dict(plan=str(path),srt=str(srt),transcript=None,timebase='source',group='clips',index=[0,1],out=str(root/'out.json'),track='karaoke',level='segment',karaoke_mode='active',karaoke_joiner='');d.update(kw);return Args(**d)
    def reject(label,**kw):
        out=root/'rejected.json';assert not out.exists()
        try:captions.execute(options(out=str(out),**kw))
        except (ValueError,TypeError,OSError):checks.append(label)
        else:raise AssertionError('Expected refusal: '+label)
        assert not out.exists()
    for mode in ('active','cumulative'):
        out=root/(mode+'.json');r=captions.execute(options(out=str(out),karaoke_mode=mode));p=editor.read_json(out);c=p['captions'];assert len(c)==8 and [v['text'] for v in c]==['今天开始剪辑']*5+['开始剪辑']*3
        assert (c[0]['start'],c[0]['duration'],c[5]['start'],c[5]['duration'])==(.2,.6,4.,.4)
        assert c[2]['highlights']==[{'start':2 if mode=='active' else 0,'end':4,'color':'#FFD54F'}]
        assert ('highlights' not in c[1]) if mode=='active' else c[1]['highlights'][0]['end']==2
        assert all('_karaoke_group' not in v for v in c);assert r['karaoke']['continuous_color_sweep'] is False
        built=editor.build_draft(Args(plan=str(out),out=str(root/(mode+'-draft'))));content=editor.read_json(Path(built['draft'])/'draft_content.json');n,_,_=editor.validate_plan(out);editor.verify_structure(content,n)
        texts=content['materials']['texts'];assert len(texts)==8 and all(json.loads(t['content'])['text'] in ('今天开始剪辑','开始剪辑') for t in texts)
        checks.extend([mode+' timeline/phrase/highlights/gaps',mode+' native editable material/readback'])
    out=root/'reordered.json';captions.execute(options(out=str(out),index=[1,0]));assert editor.read_json(out)['captions'][0]['text']=='今天开始剪辑';checks.append('reordered/repeated source indices mapped independently')
    # Exercise the real endpoint through captions, not just copied timing arithmetic.
    touching=root/'touching.srt';touching.write_text('1\n00:00:02,100 --> 00:00:02,300\na\n\n2\n00:00:02,300 --> 00:00:02,500\nb\n',encoding='utf-8')
    out=root/'touching.json';captions.execute(options(out=str(out),srt=str(touching),index=[0]));c=editor.read_json(out)['captions'];assert len(c)==2 and editor.us(c[0]['start'])+editor.us(c[0]['duration'])==editor.us(c[1]['start']);checks.append('actual captions touching boundaries use integer microseconds')
    seg=[{'start':2.2,'end':4.8,'text':'今天开始剪辑','words':[{'word':t,'start':a,'end':b,'probability':.9} for t,a,b in [('今天',2.2,2.8),('开始',3,3.8),('剪辑',4,4.8)]]}]
    transcript=root/'transcript.json';report=root/'transcript-report.json';editor.write_json(transcript,{'segments':seg});editor.write_json(report,{'status':'passed','input':str(video),'segments':seg,'params':{'word_timestamps':True,'level':'word'},'audio':{'bytes':video.stat().st_size,'seconds':12,'sha256':transcribe.sha256(video)},'test_fixture':'synthetic intervals, not model inference'})
    out=root/'model.json';r=captions.execute(options(out=str(out),srt=None,transcript=str(transcript),timebase=None,level='word',karaoke_joiner=None));assert editor.read_json(out)['captions']==editor.read_json(root/'active.json')['captions'];assert 'model word estimates' in r['karaoke']['timing'];checks.append('ASR report/source SHA and preserved original words')
    for label,kw in [('wrong mode',{'karaoke_mode':'sweep'}),('options without mode',{'karaoke_mode':None}),('word SRT not implicit',{'level':'word'}),('segment model not words',{'srt':None,'transcript':str(transcript),'timebase':None,'level':'segment','karaoke_joiner':None}),('model separator forbidden',{'srt':None,'transcript':str(transcript),'timebase':None,'level':'word'}),('invalid color',{'karaoke_color':'red'}),('invalid size',{'karaoke_size':False}),('oversized unit',{'karaoke_max_chars':1}),('duplicate index',{'index':[0,0]}),('invalid maxwords',{'karaoke_max_words':False}),('invalid gap',{'karaoke_max_gap':float('nan')})]:reject(label,**kw)
    bad=copy.deepcopy(plan);bad['clips'][0]['source_in']=2.5;editor.write_json(path,bad);reject('partial word cut refused');editor.write_json(path,plan)
    bad=editor.read_json(report);bad['audio']['sha256']='0'*64;editor.write_json(report,bad);reject('stale transcript source SHA',srt=None,transcript=str(transcript),timebase=None,level='word',karaoke_joiner=None)
    assert path.read_bytes()==original and srt.read_bytes()==srt_before;checks.append('original plan and timing source unchanged')
    result={'status':'passed','check_count':len(checks),'checks':checks,'UI_save_reopen_export':'unverified'};editor.write_json(root/'verification.json',result);return result
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
