"""Real offline beat tracking, source/report integrity and bounded refusal checks."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
import wave
import numpy as np
import beats,editor,file_hash

def click_fixture(path,bpm=120,duration=16,rate=48000):
    y=np.zeros(round(duration*rate));truth=[]
    for t in np.arange(.3,duration-.1,60/bpm):
        i=round(t*rate);u=np.arange(round(.035*rate))/rate
        y[i:i+len(u)]+=.75*np.sin(2*np.pi*1800*u)*np.exp(-u*150);truth.append(float(t))
    x=np.arange(len(y))/rate;y+=.03*np.sin(2*np.pi*220*x)
    with wave.open(str(path),'wb') as w:
        w.setparams((2,2,rate,0,'NONE','not compressed'));w.writeframes(np.repeat((y*32767).astype('<i2')[:,None],2,axis=1).tobytes())
    return truth

def check(root):
    root.mkdir(parents=True,exist_ok=False);checks=[];path=root/'pulse.wav';truth=click_fixture(path)
    original=path.read_bytes();args=Args(input=path,start=1.,duration=12.,out=root/'detected')
    # No network helper may be used by the actual tracking call.
    with patch('socket.create_connection',side_effect=AssertionError('Unexpected network')):result=beats.detect(args)
    data=beats.load_result(args.out);found=np.array([1+s/beats.RATE for s in data['beat_samples']])
    targets=[t for t in truth if 2<t<12];errors=[float(abs(found-t).min()) for t in targets]
    assert len(found)>10 and max(errors)<.05 and abs(result['tempo_bpm']-120)<3
    checks+=['real stereo 48k to mono sample-clock beat tracking matches known 120 BPM pulses within 50ms',
             'nonzero selected source time is preserved without manual timing correction','actual tracking makes no socket connection']
    repeat=copy.copy(args);repeat.out=root/'repeat';beats.detect(repeat)
    assert editor.read_json(repeat.out/'beats.json')==data;checks.append('repeat tracking candidates and tempo are deterministic')
    with wave.open(str(root/'silence.wav'),'wb') as w:w.setparams((1,2,22050,0,'NONE','not compressed'));w.writeframes(bytes(22050*3*2))
    silent=beats.detect(Args(input=root/'silence.wav',start=0,duration=3,out=root/'silence'))
    assert silent['candidate_count']==0 and silent['tempo_bpm']==0;checks.append('silence returns zero tempo and zero candidates')
    def reject(label,call):
        try:call()
        except (ValueError,TypeError,OSError):checks.append(label)
        else:raise AssertionError('Expected refusal: '+label)
    for label,changes in [('missing source',{'input':root/'missing.wav'}),('negative start',{'start':-1}),
                          ('bool duration',{'duration':True}),('too short',{'duration':1}),('too long',{'duration':181}),
                          ('range beyond media',{'start':15,'duration':3}),('fractional microsecond',{'start':.0000001})]:
        a=copy.copy(args);a.out=root/label.replace(' ','-');a.__dict__.update(changes)
        reject(label,lambda:beats.detect(a));assert not a.out.exists()
    frozen={p:p.read_bytes() for p in args.out.iterdir()}
    reject('existing output refused',lambda:beats.detect(args));assert all(p.read_bytes()==b for p,b in frozen.items())
    report_path=args.out/'beat-report.json';data_path=args.out/'beats.json'
    def tamper(label,mutate_data=None,mutate_report=None):
        d=copy.deepcopy(data);r=editor.read_json(report_path)
        if mutate_data:mutate_data(d);editor.write_json(data_path,d);r['beats_sha256']=file_hash.sha256(data_path)
        if mutate_report:mutate_report(r)
        editor.write_json(report_path,r);reject(label,lambda:beats.load_result(args.out))
        for p,b in frozen.items():p.write_bytes(b)
    for label,mutate in [
        ('bool sample',lambda d:d['beat_samples'].__setitem__(0,True)),
        ('duplicate sample',lambda d:d['beat_samples'].__setitem__(1,d['beat_samples'][0])),
        ('out-of-range sample',lambda d:d['beat_samples'].__setitem__(-1,d['selection']['sample_count'])),
        ('non-hop sample',lambda d:d['beat_samples'].__setitem__(0,d['beat_samples'][0]+1)),
        ('boolean sample rate',lambda d:d['selection'].update(sample_rate=True)),
        ('algorithm changed',lambda d:d['algorithm'].update(name='fake')),
        ('boolean trim',lambda d:d['params'].update(trim=1)),
        ('nonfinite tempo',lambda d:d.update(tempo_bpm=float('nan')))]:tamper(label,mutate_data=mutate)
    for label,mutate in [('failed analysis report',lambda r:r.update(status='failed')),('report path traversal',lambda r:r.update(beats_file='../beats.json')),
                         ('wrong checksum',lambda r:r.update(beats_sha256='0'*64)),('stale implementation',lambda r:r.update(implementation_sha256='0'*64)),
                         ('wrong dependencies',lambda r:r['dependencies'].update(librosa='wrong')),('forged listening',lambda r:r.update(auditory_review='passed'))]:tamper(label,mutate_report=mutate)
    path.write_bytes(original+b'changed');reject('source media changed',lambda:beats.load_result(args.out));path.write_bytes(original)
    assert path.read_bytes()==original
    real_open=Path.open;fault=copy.copy(args);fault.out=root/'copy-fault'
    def broken_copy(p,mode='r',*a,**kw):
        if p==fault.out/'beat-report.json' and mode=='xb':raise OSError('injected publication failure')
        return real_open(p,mode,*a,**kw)
    with patch.object(Path,'open',broken_copy):reject('publication failure rolls back owned output',lambda:beats.detect(fault))
    assert not fault.out.exists()
    real_loader=beats.load_result;fault.out=root/'verification-fault'
    def broken_verify(directory):
        if Path(directory)==fault.out:raise ValueError('injected final verification failure')
        return real_loader(directory)
    with patch.object(beats,'load_result',broken_verify):reject('post-publication verification failure rolls back output',lambda:beats.detect(fault))
    assert not fault.out.exists()
    report={'status':'passed','check_count':len(checks),'checks':checks,'max_fixture_error_seconds':max(errors),
            'source_unchanged':True,'listening_UI_save_reopen_export':'unverified'}
    editor.write_json(root/'verification.json',report);return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(check(p.parse_args().out.resolve()),ensure_ascii=True,indent=2))
