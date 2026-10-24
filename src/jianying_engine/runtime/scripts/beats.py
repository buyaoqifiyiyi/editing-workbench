"""Offline librosa beat candidates with source/sample provenance; no host beat-marker claims."""
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import wave
import numpy as np
import editor
import file_hash

RATE=22050
HOP=256
LIBROSA='0.11.0'
NUMBA='0.68.0'
ALGORITHM='librosa.beat.beat_track'

def implementation_sha():return file_hash.sha256(Path(__file__))

def validate_data(data):
    editor.fields(data,{'schema_version','source','selection','beat_samples','tempo_bpm','algorithm','params'},'beats',
                  ('schema_version','source','selection','beat_samples','tempo_bpm','algorithm','params'))
    if type(data['schema_version']) is not int or data['schema_version']!=1:raise ValueError('Unknown beat schema')
    source=data['source'];editor.fields(source,{'path','sha256'},'beat.source',('path','sha256'))
    if not isinstance(source['path'],str) or not Path(source['path']).is_absolute():raise ValueError('Beat source must be an absolute local file')
    if not isinstance(source['sha256'],str) or len(source['sha256'])!=64 or any(c not in '0123456789abcdef' for c in source['sha256']):
        raise ValueError('Invalid beat source SHA256')
    selection=data['selection'];editor.fields(selection,{'start_us','sample_rate','sample_count'},'beat.selection',('start_us','sample_rate','sample_count'))
    if (any(type(selection[k]) is not int for k in selection) or selection['start_us']<0 or selection['sample_rate']!=RATE or
            not RATE*2<=selection['sample_count']<=RATE*180):raise ValueError('Beat selection requires 2..180 seconds at the recorded sample clock')
    editor.number(data['tempo_bpm'],'tempo_bpm',0,1000)
    if data['algorithm']!={'name':ALGORITHM,'librosa':LIBROSA,'numba':NUMBA,'implementation_sha256':implementation_sha()}:
        raise ValueError('Beat algorithm/version/implementation identity changed; rerun detection')
    if data['params']!={'hop_length':HOP,'start_bpm':120.0,'tightness':100.0,'trim':True,'units':'frames'}:
        raise ValueError('Beat analysis parameters changed')
    if data['params']['trim'] is not True:raise ValueError('Beat trim must be the recorded boolean')
    points=data['beat_samples']
    if not isinstance(points,list) or any(type(v) is not int or not 0<=v<selection['sample_count'] or v%HOP for v in points):
        raise ValueError('Beat candidates require integer in-range hop-aligned sample indices')
    if any(b<=a for a,b in zip(points,points[1:])):raise ValueError('Beat sample clock must strictly increase')
    if bool(points)!=(data['tempo_bpm']>0):raise ValueError('Empty beat/tempo status mismatch')
    return data

def load_result(directory):
    directory=Path(directory).resolve()
    report=editor.read_json(directory/'beat-report.json');path=directory/'beats.json'
    editor.fields(report,{'schema_version','status','beats_file','beats_sha256','source_sha256','implementation_sha256','dependencies',
                          'boundaries','auditory_review'},'beat-report',('schema_version','status','beats_file','beats_sha256','source_sha256',
                          'implementation_sha256','dependencies','boundaries','auditory_review'))
    if (report['schema_version']!=1 or type(report['schema_version']) is not int or report['status']!='succeeded' or
            report['beats_file']!='beats.json' or report['beats_sha256']!=file_hash.sha256(path)):
        raise ValueError('Beat output/report identity or completion status changed')
    data=validate_data(editor.read_json(path))
    if report['source_sha256']!=data['source']['sha256'] or report['implementation_sha256']!=implementation_sha():
        raise ValueError('Beat report source/implementation mismatch')
    dependencies=report['dependencies']
    if (not isinstance(dependencies,dict) or set(dependencies)!={'librosa','numba','numpy','scipy','llvmlite'} or
            dependencies['librosa']!=LIBROSA or dependencies['numba']!=NUMBA or
            any(not isinstance(v,str) or not v for v in dependencies.values()) or report['auditory_review']!='unverified'):
        raise ValueError('Beat dependency/review metadata changed')
    if file_hash.sha256(Path(data['source']['path']))!=data['source']['sha256']:
        raise ValueError('Beat source media changed; rerun detection')
    return data

def decode(path,start,duration):
    with tempfile.TemporaryDirectory(prefix='jianying-beats-') as temporary:
        target=Path(temporary)/'analysis.wav'
        editor.run([editor.tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-n','-protocol_whitelist','file,pipe',
                    '-i',path,'-ss',start,'-t',duration,'-map','0:a:0','-vn','-ac','1','-ar',RATE,'-c:a','pcm_s16le',target],timeout=600)
        with wave.open(str(target),'rb') as handle:
            if (handle.getframerate(),handle.getnchannels(),handle.getsampwidth())!=(RATE,1,2):raise ValueError('Unexpected beat PCM format')
            n=handle.getnframes();samples=np.frombuffer(handle.readframes(n),dtype='<i2').copy()
    if abs(n-round(duration*RATE))>1:raise ValueError('Beat decoder did not deliver the complete selected interval')
    return samples.astype(np.float32)/32768.

def detect(args):
    path=Path(args.input).expanduser().resolve();output=Path(args.out).expanduser().resolve()
    if not path.is_file():raise ValueError('Beat input is not a local file')
    if output.exists():raise FileExistsError('Beat output already exists; choose a new directory')
    start=getattr(args,'start',0);duration=args.duration
    editor.number(start,'beat.start',0);editor.number(duration,'beat.duration',2,180)
    if editor.us(start)/1e6!=start:raise ValueError('Beat start must have integer-microsecond precision')
    info=editor.probe(path)
    if not info['has_audio']:raise ValueError('Beat analysis requires an audio stream')
    if start+duration>info['duration']+1/RATE:raise ValueError('Beat selection extends beyond source media')
    try:
        import librosa,numba
        if librosa.__version__!=LIBROSA or numba.__version__!=NUMBA:
            raise ValueError('Measured rhythm dependencies require librosa '+LIBROSA+' and numba '+NUMBA)
    except ImportError as exc:raise ValueError('Offline beat analysis needs the optional jianying-engine[rhythm] dependencies') from exc
    before=file_hash.sha256(path);samples=decode(path,start,duration)
    tempo,frames=librosa.beat.beat_track(y=samples,sr=RATE,hop_length=HOP,start_bpm=120.,tightness=100.,trim=True,units='frames')
    value=float(np.asarray(tempo).reshape(-1)[0]);indices=np.asarray(frames)
    if indices.dtype.kind not in 'iu':raise ValueError('Beat tracker returned noninteger frames')
    points=[int(v)*HOP for v in indices if 0<=int(v)*HOP<len(samples)]
    if not points:value=0.
    data={'schema_version':1,'source':{'path':str(path),'sha256':before},
          'selection':{'start_us':editor.us(start),'sample_rate':RATE,'sample_count':len(samples)},
          'beat_samples':points,'tempo_bpm':value,'algorithm':{'name':ALGORITHM,'librosa':LIBROSA,'numba':NUMBA,'implementation_sha256':implementation_sha()},
          'params':{'hop_length':HOP,'start_bpm':120.0,'tightness':100.0,'trim':True,'units':'frames'}}
    validate_data(data)
    if file_hash.sha256(path)!=before:raise ValueError('Beat source changed during analysis')
    dependencies={name:importlib.metadata.version(name) for name in ('librosa','numba','numpy','scipy','llvmlite')}
    with tempfile.TemporaryDirectory(prefix='jianying-beat-report-') as temporary:
        stage=Path(temporary);editor.write_json(stage/'beats.json',data)
        report={'schema_version':1,'status':'succeeded','beats_file':'beats.json','beats_sha256':file_hash.sha256(stage/'beats.json'),
                'source_sha256':before,'implementation_sha256':implementation_sha(),'dependencies':dependencies,'auditory_review':'unverified',
                'boundaries':['Candidate beats/global tempo, not calibrated accuracy, downbeat/meter or a guaranteed musical grid.',
                              'Librosa may choose half/double tempo; no manual timing correction or missing-beat invention.',
                              'Source times use the selected decoded sample clock; normal-speed listening and codec alignment remain task-specific.',
                              'No Jianying host beat calculation or native beat-marker writing, model calls, or runtime downloads.']}
        editor.write_json(stage/'beat-report.json',report)
        load_result(stage)
        # Claim a fresh directory, then only clean the two files this call owns on failure.
        output.mkdir(parents=True,exist_ok=False);created=[]
        try:
            for name in ('beats.json','beat-report.json'):
                target=output/name
                with target.open('xb') as handle:
                    created.append(target);handle.write((stage/name).read_bytes())
            load_result(output)
        except BaseException:
            for target in created:
                try:target.unlink(missing_ok=True)
                except OSError:pass
            try:output.rmdir()
            except OSError:pass
            raise
    return {'status':'succeeded','candidate_count':len(points),'tempo_bpm':value,'beats':str(output/'beats.json'),
            'report':str(output/'beat-report.json'),'source_modified':False,'auditory_review':'unverified'}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--start',type=float,default=0)
    p.add_argument('--duration',type=float,required=True);p.add_argument('--out',required=True);a=p.parse_args(argv)
    try:editor.emit(detect(a));return 0
    except (ValueError,TypeError,OSError,KeyError,wave.Error,subprocess.TimeoutExpired) as exc:
        editor.emit({'status':'failed','error':str(exc),'source_modified':False});return 1
if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
