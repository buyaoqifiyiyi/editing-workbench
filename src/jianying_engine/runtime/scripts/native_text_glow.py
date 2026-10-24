"""One identified native text bloom package; bounded standalone captions only."""
from pathlib import Path
import json,uuid
import file_hash
import native_advanced


def material(spec, user_data, allow_vip=True):
    admitted=json.loads((Path(__file__).resolve().parents[1]/'assets/native-text-glow-11.5.json').read_text('utf-8'))
    name=spec['name']
    if not isinstance(name,str) or name not in admitted:
        raise ValueError('Unadmitted text glow resource')
    resource=admitted[name]
    if resource['is_vip'] and not allow_vip:
        raise ValueError('Text glow requires allowed VIP resources')
    root=native_advanced.cache_root(resource['cache_relative'],user_data)/resource['cache_relative']
    for relative,digest in resource['files'].items():
        path=root/relative
        if not file_hash.matches(path,digest):
            raise ValueError('Text glow cache missing or changed: '+relative)
    return {'id':uuid.uuid4().hex,'type':'bloom','path':root.as_posix(),
            'resource_id':resource['resource_id'],'effect_id':resource['effect_id'],
            'bloom_params':{'color':spec['color'].upper()+'FF','strength':spec['strength'],
                            'range':spec['range'],'dir_x':.5,'dir_y':.5}}


def apply(content, plan, user_data):
    texts={m['id']:m for m in content['materials']['texts']}
    segments={(t['name'],s['target_timerange']['start']):s for t in content['tracks'] for s in t['segments']}
    for item in plan['captions']:
        if 'glow' not in item:continue
        segment=segments[item['track'],round(item['start']*1_000_000)]
        bloom=material(item['glow'],user_data,plan['allow_vip_effects'])
        content['materials']['effects'].append(bloom)
        segment['extra_material_refs'].append(bloom['id'])
        texts[segment['material_id']]['check_flag']|=64


def verify(content, plan, user_data):
    materials={m['id']:m for values in content['materials'].values() if isinstance(values,list)
               for m in values if isinstance(m,dict) and 'id' in m}
    segments={(t['name'],s['target_timerange']['start']):s for t in content['tracks'] for s in t['segments']}
    requested=[]
    for item in plan['captions']:
        segment=segments[item['track'],round(item['start']*1_000_000)]
        blooms=[materials[r] for r in segment['extra_material_refs'] if materials[r].get('type')=='bloom']
        if 'glow' in item:
            if len(blooms)!=1:raise ValueError('Text glow binding count mismatch')
            expected=material(item['glow'],user_data,plan['allow_vip_effects']);expected['id']=blooms[0]['id']
            params=blooms[0].get('bloom_params',{})
            if blooms[0]!=expected or any(type(params.get(k)) not in (int,float) for k in ('strength','range','dir_x','dir_y')):
                raise ValueError('Text glow resource or parameters changed')
            requested.append(blooms[0]['id'])
        elif blooms:raise ValueError('Unrequested text glow binding')
        text=materials[segment['material_id']]
        if bool(text['check_flag']&64)!=('glow' in item):raise ValueError('Text glow flag mismatch')
    actual=[m['id'] for values in content['materials'].values() if isinstance(values,list)
            for m in values if isinstance(m,dict) and m.get('type')=='bloom']
    if sorted(actual)!=sorted(requested) or len(set(requested))!=len(requested):
        raise ValueError('Orphaned, duplicate or shared text glow material')
    return len(requested)
