"""Existing-mask binding checks; input manifest is an external reviewed artifact."""
from pathlib import Path
from types import SimpleNamespace as Args
import argparse,copy
import editor,native_matting_cache


def check(root, plan_path, review_path=None):
    root.mkdir(parents=True,exist_ok=False);base=editor.read_json(plan_path);checks=[]
    path=root/'plan.json';editor.write_json(path,base)
    built=editor.build_draft(Args(plan=str(path),out=str(root/'draft')))
    plan=editor.read_json(root/'draft/edit-plan.json');content=editor.read_json(root/'draft/draft_content.json')
    assert built['verification']['native_effects']['existing_matting_caches']==1
    assert built['verification']['native_effects']['new_matting_computation'] is False
    checks.append('new build/full readback, no new computation claim')
    (root/'offline').mkdir();pub=editor.publish_draft(Args(draft=str(root/'draft'),draft_root=str(root/'offline'),offline_copy=True))
    editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),plan)
    assert 'matting_cache' in (Path(pub['draft'])/'原生效果.md').read_text('utf-8')
    checks.append('offline publication/sidecars/delivery')
    def reject(label,call):
        try:call()
        except (ValueError,FileNotFoundError,FileExistsError):checks.append(label)
        else:raise AssertionError('Accepted '+label)
    original=plan['clips'][1];manifest=editor.read_json(original['matting_cache']['manifest'])
    for label,mutate in {
        'speed':lambda c:c.update(speed=1.1),
        'sound':lambda c:c.update(volume=1),
        'out-of-range':lambda c:c.update(source_in=23.9),
        'vertical shift':lambda c:c.update(transform={'transform_y':.1}),
        'scale':lambda c:c.update(transform={'scale_x':.8,'scale_y':.8}),
        'motion':lambda c:c.update(keyframes=[{'property':'position_x','time':0,'value':0}]),
        'alpha':lambda c:c.update(transform={'alpha':1}),
        'mask':lambda c:c.update(mask={'name':'矩形','width':100,'height':100}),
        'unknown field':lambda c:c['matting_cache'].update(flag=5),
        'changed manifest hash':lambda c:c['matting_cache'].update(sha256='0'*64),
        'missing manifest':lambda c:c['matting_cache'].update(manifest=str(root/'missing.json')),
        'different source':lambda c:c.update(path=plan['clips'][0]['path']),
    }.items():
        p=copy.deepcopy(plan);mutate(p['clips'][1]);editor.write_json(root/'invalid.json',p)
        reject(label,lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(plan);p['canvas']['fps']=30;editor.write_json(root/'invalid.json',p)
    reject('canvas fps',lambda:editor.validate_plan(root/'invalid.json'))
    p=copy.deepcopy(plan);p['composites']=[{'name':'组','tracks':['video','subject']}];editor.write_json(root/'invalid.json',p)
    reject('composite',lambda:editor.validate_plan(root/'invalid.json'))
    seg=next(t for t in content['tracks'] if t['name']=='subject')['segments'][0];mid=seg['material_id']
    for key,value in [('flag',7),('custom_matting_id','wrong'),('path','C:/missing'),('feather',1)]:
        d=copy.deepcopy(content);m=next(m for m in d['materials']['videos'] if m['id']==mid);m['matting'][key]=value
        reject('binding '+key,lambda d=d:editor.verify_structure(d,plan))
    d=copy.deepcopy(content);next(m for m in d['materials']['videos'] if m['id']==mid).pop('matting')
    reject('missing binding',lambda:editor.verify_structure(d,plan))
    d=copy.deepcopy(content);plain=next(m for m in d['materials']['videos'] if m['id']!=mid)
    plain['matting']=next(m for m in d['materials']['videos'] if m['id']==mid)['matting']
    reject('unrequested matting',lambda:editor.verify_structure(d,plan))
    for label,mutate in {
        'source identity':lambda d:d.update(source_sha256='0'*64),
        'missing cache':lambda d:d.update(cache_root=str(root/'absent')),
        'wrong flag':lambda d:d.update(flag=7),
        'compute claim':lambda d:d.update(new_computation=True),
        'unreviewed':lambda d:d.update(quality_review='unverified'),
        'range metadata':lambda d:d.update(source_range_us=[24000000,25900000]),
        'file inventory':lambda d:d['files'].pop(next(iter(d['files']))),
        'file hash':lambda d:d['files'].update({next(iter(d['files'])):'0'*64}),
    }.items():
        data=copy.deepcopy(manifest);mutate(data);ref=root/'bad-manifest.json';editor.write_json(ref,data)
        item=copy.deepcopy(original);item['matting_cache']={'manifest':str(ref),'sha256':native_matting_cache.sha(ref)}
        reject(label,lambda item=item:native_matting_cache.binding(item,plan['canvas']))
    if review_path is not None:
        review=editor.read_json(review_path);info=editor.probe(original['path']);output=root/'admitted.json'
        admitted=native_matting_cache.admit(original['path'],manifest['cache_root'],manifest['custom_id'],review_path,output,info)
        assert editor.read_json(output)==manifest and admitted['new_computation'] is False
        checks.append('reviewed cache admission')
        reject('admission overwrite',lambda:native_matting_cache.admit(original['path'],manifest['cache_root'],manifest['custom_id'],review_path,output,info))
        for label,mutate in {
            'failed quality':lambda d:d.update(status='failed'),
            'review source':lambda d:d.update(source_sha256='0'*64),
            'review custom ID':lambda d:d.update(custom_id='wrong'),
            'review mask identity':lambda d:d.update(cache_identity_sha256='0'*64),
            'missing review evidence':lambda d:d.update(evidence=[]),
            'absent review evidence':lambda d:d.update(evidence=['absent.png']),
            'review coverage':lambda d:d.update(source_range_us=[manifest['source_range_us'][0],manifest['source_range_us'][1]-100000])
        }.items():
            data=copy.deepcopy(review);mutate(data);p=root/'bad-review.json';editor.write_json(p,data);out=root/'not-admitted.json'
            reject(label,lambda:native_matting_cache.admit(original['path'],manifest['cache_root'],manifest['custom_id'],p,out,info))
            assert not out.exists()
    result={'status':'passed','checks_count':len(checks),'checks':checks,'new_computation':False}
    editor.write_json(root/'checks.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True,type=Path);p.add_argument('--plan',required=True,type=Path)
    p.add_argument('--review',type=Path,help='Optional external UI review to test admission')
    args=p.parse_args();editor.emit(check(args.out.resolve(),args.plan.resolve(),args.review.resolve() if args.review else None))
