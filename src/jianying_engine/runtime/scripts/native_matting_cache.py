"""Identity-checked existing custom masks. Never computes a new person cutout."""
from pathlib import Path
import hashlib,json,re,tempfile
import file_hash


def sha(path):
    return file_hash.sha256(path)


def binding(item, canvas, info=None):
    spec=item['matting_cache'];manifest=Path(spec['manifest'])
    if not manifest.is_file() or sha(manifest)!=spec['sha256']:
        raise ValueError('Matting cache manifest missing or changed')
    data=json.loads(manifest.read_text('utf-8-sig'))
    expected={'schema_version','kind','jianying_version','source_sha256','source_width','source_height','fps',
              'source_range_us','custom_id','flag','cache_root','files','quality_review','new_computation'}
    if (not isinstance(data,dict) or set(data)!=expected or data['schema_version']!=1 or
            data['kind']!='native_custom_mask_cache' or data['jianying_version']!='11.5.0.14471' or
            type(data['flag']) is not int or data['flag']!=5 or data['fps']!=60 or data['new_computation'] is not False or
            data['quality_review']!='UI_subject_repair_verified_minor_residue_retained' or
            not isinstance(data['custom_id'],str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',data['custom_id'])):
        raise ValueError('Unadmitted native matting manifest')
    if canvas!={'width':1280,'height':720,'fps':60}:
        raise ValueError('Existing matting cache currently admits 1280x720/60fps canvas only')
    if sha(item['path'])!=data['source_sha256']:
        raise ValueError('Matting cache belongs to different source bytes')
    if info and (info['width']!=data['source_width'] or info['height']!=data['source_height'] or abs(info['fps']-60)>.001):
        raise ValueError('Matting cache source dimensions/fps mismatch')
    root=Path(data['cache_root'])
    if not root.is_absolute() or not root.is_dir():raise ValueError('Matting cache root missing')
    root=root.resolve();files=data['files']
    if not isinstance(files,dict) or not files:raise ValueError('Matting cache identity list empty')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if set(files)!=actual:raise ValueError('Matting cache file inventory changed')
    for relative,digest in files.items():
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not re.fullmatch(r'[0-9a-f]{64}',digest) or sha(path)!=digest:
            raise ValueError('Matting cache file identity mismatch: '+relative)
    folder=root/data['custom_id']/'5';stamps=sorted(int(p.name) for p in (folder/'mask').iterdir() if p.is_file())
    declared=data['source_range_us']
    if not isinstance(declared,list) or len(declared)!=2 or any(type(v) is not int for v in declared) or declared[0]>=declared[1]:
        raise ValueError('Invalid matting source range')
    ranges=json.loads((folder/'matting_result.json').read_text('utf-8'))['result_time_range']
    if ranges!=[{'start':declared[0],'end':declared[1]}]:raise ValueError('Matting result coverage metadata mismatch')
    wanted=[round(declared[0]+i*1_000_000/60) for i in range(round((declared[1]-declared[0])*60/1_000_000)+1)]
    if len(stamps)!=len(wanted) or any(abs(a-b)>1 for a,b in zip(stamps,wanted)):
        raise ValueError('Matting cache has incomplete 60fps source coverage')
    for stamp in stamps:
        mask=folder/'mask'/str(stamp);box=json.loads((folder/'maskinfo'/str(stamp)).read_text('utf-8'))['boundingBox']
        if box['width']!=512 or box['height']!=288 or mask.stat().st_size!=512*288:
            raise ValueError('Unadmitted matting mask dimensions or payload')
    start=round(item['source_in']*1_000_000);end=round((item['source_in']+item['duration'])*1_000_000)
    if not declared[0]<=start<end<=declared[1]:raise ValueError('Clip extends beyond computed matting source range')
    return {'flag':5,'path':str(root),'feather':0,'custom_matting_id':data['custom_id']}


def apply(content, plan):
    materials={m['id']:m for m in content['materials']['videos']}
    segments={(t['name'],s['target_timerange']['start']):s for t in content['tracks'] for s in t['segments']}
    for item in plan['clips']:
        if 'matting_cache' not in item:continue
        segment=segments[item['track'],round(item['start']*1_000_000)]
        materials[segment['material_id']]['matting']=binding(item,plan['canvas'])


def verify(content, plan):
    materials={m['id']:m for m in content['materials']['videos']}
    segments={(t['name'],s['target_timerange']['start']):s for t in content['tracks'] for s in t['segments']}
    count=0
    for item in plan['clips']:
        segment=segments[item['track'],round(item['start']*1_000_000)]
        actual=materials[segment['material_id']].get('matting')
        expected=binding(item,plan['canvas']) if 'matting_cache' in item else None
        if actual!=expected:raise ValueError('Unrequested or changed matting binding')
        if expected:
            groups=segment.get('common_keyframes',[])
            keys=item.get('keyframes',[])
            axis={'position_x':'KFTypePositionX','position_y':'KFTypePositionY'}.get(keys[0]['property']) if keys else None
            if (len(groups)!=int(bool(item.get('keyframes'))) or
                    any(g['property_type']!=axis for g in groups)):
                raise ValueError('Unrequested or duplicate matting motion keyframe groups')
        count+=expected is not None
    return count


def admit(source, cache, custom_id, review_path, output, info):
    """Register an externally reviewed cache; never calculate or rate a cutout."""
    source=Path(source).resolve();cache=Path(cache).resolve();output=Path(output).resolve()
    if output.exists():raise FileExistsError('Matting manifest already exists')
    review_path=Path(review_path).resolve()
    review=json.loads(review_path.read_text('utf-8-sig'))
    quality='UI_subject_repair_verified_minor_residue_retained'
    if (not isinstance(review,dict) or review.get('status')!=quality or
            review.get('source_sha256')!=sha(source) or review.get('custom_id')!=custom_id or
            not isinstance(review.get('evidence'),list) or not review['evidence'] or
            any(not isinstance(p,str) or not (review_path.parent/p).resolve().is_file() for p in review['evidence'])):
        raise ValueError('Missing, mismatched or failed external UI quality review')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',custom_id):raise ValueError('Invalid custom matting ID')
    folder=(cache/custom_id/'5').resolve()
    if not folder.is_relative_to(cache) or not folder.is_dir():raise ValueError('Custom mask folder missing')
    files={p.relative_to(cache).as_posix():sha(p) for p in folder.rglob('*') if p.is_file()}
    identity=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if identity!=review.get('cache_identity_sha256'):raise ValueError('Reviewed mask bytes changed')
    data={'schema_version':1,'kind':'native_custom_mask_cache','jianying_version':'11.5.0.14471',
          'source_sha256':sha(source),'source_width':info['width'],'source_height':info['height'],'fps':60,
          'source_range_us':review.get('source_range_us'),'custom_id':custom_id,'flag':5,
          'cache_root':str(cache),'files':files,'quality_review':quality,'new_computation':False}
    # Validate through the same binding gate before writing the caller's manifest.
    with tempfile.TemporaryDirectory(prefix='matting-admission-',dir=output.parent) as temporary:
        candidate=Path(temporary)/'candidate.json';candidate.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')
        span=data['source_range_us']
        if not isinstance(span,list) or len(span)!=2 or any(type(n) is not int for n in span):raise ValueError('Invalid reviewed source range')
        item={'path':str(source),'source_in':span[0]/1e6,'duration':(span[1]-span[0])/1e6,
              'matting_cache':{'manifest':str(candidate),'sha256':sha(candidate)}}
        binding(item,{'width':1280,'height':720,'fps':60},info)
        with output.open('x',encoding='utf-8') as stream:stream.write(candidate.read_text('utf-8'))
    return {'manifest':str(output),'sha256':sha(output),'cache_files':len(files),'new_computation':False,
            'quality_review':'external_record_not_automated_rating'}
