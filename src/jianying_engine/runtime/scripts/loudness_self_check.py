"""Regression for restricted native loudness parameters; no desktop control or baked audio."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, plan_path):
    root.mkdir(parents=True, exist_ok=False)
    base = editor.read_json(plan_path)
    base['name'] = '响度正式接口回归'
    source = Path(base['audio'][0]['path'])
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = []
    editor.write_json(root / 'plan.json', base)
    result = editor.build_draft(Args(plan=str(root / 'plan.json'), out=str(root / 'draft')))
    content = editor.read_json(root / 'draft/draft_content.json')
    normalized = editor.read_json(root / 'draft/edit-plan.json')
    assert result['verification']['native_effects']['extensions']['audio_loudness'] == 1
    material = next(m for m in content['materials']['loudnesses'] if m.get('enable'))
    assert material['file_id'] == '' and material['target_loudness'] == -23
    checks.append('formal build and readback; schema-default identity, target and measured range')
    (root / 'offline').mkdir()
    editor.publish_draft(Args(draft=str(root / 'draft'), draft_root=str(root / 'offline'), offline_copy=True))
    copied = editor.read_json(root / 'offline' / base['name'] / 'draft_content.json')
    assert copied['materials']['loudnesses'] == content['materials']['loudnesses']
    checks.append('offline publication preserves loudness materials')
    damaged=copy.deepcopy(content)
    segment=next(t for t in damaged['tracks'] if t['type']=='audio')['segments'][0]
    segment['extra_material_refs'].remove(material['id'])
    try:
        editor.verify_structure(damaged,normalized)
    except ValueError:
        checks.append('readback rejects missing material reference')
    else:
        raise AssertionError('Unreferenced loudness accepted')
    composite=copy.deepcopy(base)
    composite['layer_order']=['video','music']
    composite['audio'][0]['track']='music'
    composite['composites']=[{'name':'unverified loudness composite','tracks':['video','music']}]
    editor.write_json(root/'composite.json',composite)
    try:
        editor.validate_plan(root/'composite.json')
    except ValueError as e:
        assert 'standalone audio' in str(e),str(e)
        checks.append('validation rejects loudness inside unverified composite')
    else:
        raise AssertionError('Composite loudness accepted')
    for label, mutate in {
        'disabled': lambda m: m.update(enable=False),
        'target': lambda m: m.update(target_loudness=-16),
        'guessed identity': lambda m: m.update(file_id='wrong'),
        'range': lambda m: m['time_range'].update(duration=1),
        'measurement': lambda m: m['loudness_param'].update(avg_loudness=-40),
    }.items():
        damaged = copy.deepcopy(content)
        mutate(next(m for m in damaged['materials']['loudnesses'] if m.get('enable')))
        try:
            editor.verify_structure(damaged, normalized)
        except ValueError:
            checks.append('readback rejects ' + label)
        else:
            raise AssertionError('Accepted corrupted loudness: ' + label)
    for label, mutate in {
        'unsupported target': lambda a: a['loudness'].update(target_lufs=-16),
        'boolean target': lambda a: a['loudness'].update(target_lufs=True),
        'unknown setting': lambda a: a['loudness'].update(limiter=True),
        'speed': lambda a: a.update(speed=1.1),
        'gain': lambda a: a.update(volume=.5),
        'fade': lambda a: a.update(fade_in=.1),
        'keys': lambda a: a.update(keyframes=[{'time':0,'value':1}]),
        'denoise': lambda a: a.update(denoise={}),
        'short range': lambda a: a.update(duration=2),
    }.items():
        candidate = copy.deepcopy(base)
        mutate(candidate['audio'][0])
        editor.write_json(root / 'invalid.json', candidate)
        try:
            editor.validate_plan(root / 'invalid.json')
        except ValueError:
            checks.append('validation rejects ' + label)
        else:
            raise AssertionError('Accepted unsupported loudness: ' + label)
    for label, args in [('mono',['-ac','1']), ('48k',['-ar','48000']), ('silence',['-af','volume=0'])]:
        path = root / (label+'.wav')
        editor.run([editor.tool('ffmpeg'),'-v','error','-nostdin','-n','-i',source,*args,path])
        candidate=copy.deepcopy(base)
        candidate['audio'][0]['path']=str(path)
        editor.write_json(root/'invalid.json',candidate)
        try:
            editor.validate_plan(root/'invalid.json')
        except ValueError:
            checks.append('validation rejects '+label)
        else:
            raise AssertionError('Accepted invalid input: '+label)
    # A transient-dominated signal needs a limiter to reach the target safely.
    peak_path=root/'transient.wav'
    editor.run([editor.tool('ffmpeg'),'-v','error','-nostdin','-n','-f','lavfi','-i',
                r'aevalsrc=if(lt(t\,0.00005)\,0.99\,0.0001*sin(2*PI*200*t)):s=44100:d=8:c=stereo',peak_path])
    candidate=copy.deepcopy(base);candidate['audio'][0]['path']=str(peak_path)
    editor.write_json(root/'invalid.json',candidate)
    try:
        editor.validate_plan(root/'invalid.json')
    except ValueError as e:
        assert 'headroom' in str(e),str(e)
        checks.append('true-peak headroom rejects transient-dominated normalization')
    else:
        raise AssertionError('Unsafe normalization accepted')
    plain=copy.deepcopy(base);plain['audio'][0].pop('loudness')
    editor.write_json(root/'plain.json',plain)
    editor.build_draft(Args(plan=str(root/'plain.json'),out=str(root/'plain')))
    assert not any(m.get('enable') for m in editor.read_json(root/'plain/draft_content.json')['materials'].get('loudnesses',[]))
    checks.append('unrequested loudness absent')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==fingerprint
    checks.append('source bytes unchanged')
    report={'status':'passed','checks_count':len(checks),'checks':checks,'ui':'separate v13/v14 evidence','headless_export':False}
    editor.write_json(root/'回归结果.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    editor.emit(check(args.out.resolve(),args.plan.resolve()))
