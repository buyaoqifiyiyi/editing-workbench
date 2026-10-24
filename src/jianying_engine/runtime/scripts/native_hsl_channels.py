"""Measured static HSL controls, with a bounded saturation/lightness pair."""
import math
from pathlib import Path

CHANNELS={'orange_hsl':(2,'橙色'),'yellow_hsl':(3,'黄色'),'cyan_hsl':(5,'青色'),
          'purple_hsl':(7,'紫色'),'magenta_hsl':(8,'紫红色')}
CONTROLS={'saturation':(-60,0,60),'lightness':(-30,0,30),'hue':(-30,0,30)}

def requested(grade):return isinstance(grade,dict) and bool(CHANNELS.keys() & grade.keys())

def runtime(install=None,executable=None):
    import native_advanced
    try:native_advanced.green_lightness_runtime(install,executable)
    except ValueError as exc:raise ValueError('Additional HSL runtime identity: '+str(exc)) from exc

def validate(item,info):
    import editor
    grade=item['grading']
    if len(grade)!=1 or next(iter(grade)) not in CHANNELS:raise ValueError('Additional HSL admits one channel without other grading')
    name=next(iter(grade));spec=grade[name];editor.fields(spec,set(CONTROLS),'grading.'+name)
    if len(spec)!=1 and set(spec)!={'saturation','lightness'}:
        raise ValueError('Additional HSL requires one measured control or the saturation/lightness pair')
    for control,value in spec.items():
        values=CONTROLS[control]
        editor.number(value,'grading.'+name+'.'+control,min(values),max(values))
        if value not in values:raise ValueError('Additional HSL '+control+' requires '+str(values)+' only')
    if (info['kind']!='image' or (info['width'],info['height'])!=(640,360) or Path(item['path']).suffix.lower()!='.png' or
            item['fit']!='contain' or item['source_in']!=0 or item['speed']!=1 or item['transform'] or any(item.get(k) for k in
                ('keyframes','motion_easing','transition','effects','filters','animations','mix_mode','chroma','background','mask','matting_cache','speed_curve'))):
        raise ValueError('Additional HSL requires static 640x360 contain PNG, source_in0/speed1 and no other effects')
    runtime()

def material(grade,source,user_data):
    import native_advanced
    name=next(iter(grade));m=native_advanced.color_materials({'red_hsl':{'hue':0,'saturation':0,'lightness':0}},source,user_data)['hsl']
    m.update(hsl_color_type=CHANNELS[name][0],**grade[name])
    return {'hsl':m}

def verify(content,plan):
    for item in plan['clips']:
        if not requested(item.get('grading')):continue
        runtime()
        if content.get('fps')!=30 or (content['canvas_config']['width'],content['canvas_config']['height'])!=(640,360):
            raise ValueError('Additional HSL native canvas/fps changed')
        segments=[s for t in content['tracks'] for s in t['segments']]
        if len(content['tracks'])!=1 or len(segments)!=1:raise ValueError('Additional HSL native track scene changed')
        s=segments[0];m=content['materials']['hsl']
        default={'alpha':1.,'flip':{'horizontal':False,'vertical':False},'rotation':0.,'scale':{'x':1,'y':1},'transform':{'x':0.,'y':0.}}
        clip=s.get('clip');source=next((v for v in content['materials'].get('videos',[]) if v['id']==s['material_id']),{})
        crop={'upper_left_x':0.,'upper_left_y':0.,'upper_right_x':1.,'upper_right_y':0.,
              'lower_left_x':0.,'lower_left_y':1.,'lower_right_x':1.,'lower_right_y':1.}
        by_id={v['id']:v for values in content['materials'].values() if isinstance(values,list) for v in values if isinstance(v,dict) and 'id' in v}
        refs=s['extra_material_refs']
        if (clip!=default or s.get('common_keyframes') or s.get('keyframe_refs') or s.get('reverse') is not False or s.get('visible') is not True or
                s.get('source_timerange')!={'start':0,'duration':2000000} or any(type(v) is not int for v in s['source_timerange'].values()) or
                source.get('type')!='photo' or source.get('crop')!=crop or source.get('crop_ratio')!='free' or source.get('crop_scale')!=1 or
                len(refs)!=2 or len(set(refs))!=2 or sorted(by_id.get(r,{}).get('type','') for r in refs)!=['hsl','speed']):
            raise ValueError('Additional HSL native motion/crop/source clock/extra refs changed')
        scalars=[clip['alpha'],clip['rotation'],*clip['scale'].values(),*clip['transform'].values(),source['crop_scale'],*source['crop'].values()]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in scalars) or any(v is not False for v in clip['flip'].values()):
            raise ValueError('Additional HSL native static geometry requires finite numeric types')
        if len(m)!=1 or type(m[0].get('hsl_color_type')) is not int or any(type(m[0].get(k)) not in (int,float) or not math.isfinite(m[0][k]) for k in ('hue','saturation','lightness')):
            raise ValueError('Additional HSL native channel/scalar types changed')
        for k in ('enable_adjust','enable_hsl','enable_color_curves','enable_color_wheels','enable_hsl_curves','enable_adjust_mask'):
            if type(s.get(k)) is not bool:raise ValueError('Additional HSL native flags must remain boolean')

def preview_check(content,root,install,executable=None):
    import editor
    path=Path(root)/'edit-plan.json'
    declared=path.is_file() and any(requested(c.get('grading')) for c in editor.read_json(path).get('clips',[]))
    observed=any(m.get('hsl_color_type') in (2,3,5,7,8) for m in content.get('materials',{}).get('hsl',[]))
    if declared or observed:
        runtime(install,executable)
        if not path.is_file():raise ValueError('Additional HSL preview requires formal edit-plan.json')
        plan,_,_=editor.validate_plan(path);editor.verify_structure(content,plan)
