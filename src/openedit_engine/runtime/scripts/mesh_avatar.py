"""Render a bounded Mesh Avatar clip and attach it to an existing OpenEdit plan."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import re
from PIL import Image, ImageDraw
import file_hash

RANGES = {'angleX':(-30,30),'angleY':(-30,30),'angleZ':(-30,30),'breath':(0,1),
          'eyeLOpen':(0,1.25),'eyeROpen':(0,1.25),'mouthOpen':(0,1),'mouthForm':(-1,1)}

def config_path():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
    return base / 'OpenEditEngine' / 'mesh-avatar.json'

def studio_path(value):
    if value is None:
        path = config_path()
        if not path.is_file(): raise ValueError('Mesh Avatar Studio is not configured; use configure --studio or render --studio')
        value = Path(json.loads(path.read_text(encoding='utf-8-sig'))['studio'])
    studio = value.resolve(strict=True)
    if not (studio/'packages'/'runtime'/'dist'/'index.js').is_file() or not (studio/'node_modules'/'playwright').is_dir():
        raise ValueError('studio requires built runtime and installed Playwright')
    return studio

digest = file_hash.sha256

def video_info(path):
    ffprobe=shutil.which('ffprobe')
    if not ffprobe: raise ValueError('ffprobe must be available on PATH')
    data=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','json',str(path)],text=True,encoding='utf-8'))
    duration=float(data['format']['duration'])
    if not math.isfinite(duration) or duration<=0: raise ValueError('video duration is invalid')
    return duration

def resolve_render_avatar(args):
    avatar=args.avatar.resolve(strict=True)
    if avatar.suffix.lower()=='.mavatar': return avatar
    studio=studio_path(args.studio); project=avatar
    if not project.is_relative_to((studio/'projects').resolve()): raise ValueError('project folder must be inside the configured Studio projects directory')
    manifest=project/'work'/'video-context'/'imagegen-assignments.json'
    if not manifest.is_file(): raise ValueError('project has no checked image-generation assignment manifest')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    source=project/'variants'/'base.png'
    if digest(source)!=data.get('identity_base_sha256'): raise ValueError('identity base changed after variant planning')
    if digest(project/'source.png')!=data.get('source_sha256'): raise ValueError('source illustration changed after variant planning')
    identity_manifest=project/'work'/'identity-base.sha256'
    if not identity_manifest.is_file() or digest(identity_manifest)!=data.get('identity_manifest_sha256'):
        raise ValueError('identity manifest changed after variant planning')
    if identity_manifest.read_text(encoding='utf-8').strip()!=data['source_sha256']:
        raise ValueError('identity manifest does not match the original source illustration')
    if data.get('project')!=str(project): raise ValueError('assignment manifest project identity does not match')
    if digest(Path(data['video']).resolve(strict=True))!=data.get('video_sha256'):
        raise ValueError('source video changed after variant planning')
    if not data.get('variants') or len(data['variants'])>11: raise ValueError('project variant assignment list is invalid')
    for item in data['variants']:
        inputs=item.get('inputs',[]); hashes=item.get('input_sha256',[])
        if len(inputs)<3 or len(inputs)!=len(hashes): raise ValueError(f'variant input manifest is invalid: {item.get("name")}')
        if Path(inputs[0]).resolve()!= (project/'source.png').resolve(): raise ValueError('ImageGen identity input must be the original source illustration')
        if Path(inputs[1]).resolve()!=Path(item['mask']).resolve(): raise ValueError('ImageGen mask input does not match the assigned Mesh mask')
        for input_path,expected in zip(inputs,hashes):
            path=Path(input_path).resolve(strict=True)
            if digest(path)!=expected: raise ValueError(f'ImageGen input changed after planning: {item["name"]}')
        target=Path(item['output']).resolve()
        if not target.is_relative_to((project/'variants').resolve()): raise ValueError('variant output escapes project variants directory')
        if not target.is_file(): raise ValueError('a planned image variant is missing; render is blocked')
        accepted=Path(item.get('acceptance_receipt',''))
        if not accepted.is_absolute(): accepted=(project/accepted).resolve()
        if not accepted.is_file(): raise ValueError(f'image variant has no acceptance receipt: {item["name"]}')
        proof=json.loads(accepted.read_text(encoding='utf-8'))
        sprites=project/'built'/'sprites'/'sprites.json'
        if (not sprites.is_file() or proof.get('variant')!=item['name'] or proof.get('image_sha256')!=digest(target)
                or proof.get('mask_sha256')!=item.get('mask_sha256') or proof.get('mesh_build_sha256')!=digest(sprites)
                or proof.get('accepted_outside_mask') is not True or proof.get('reviewed_pose') is not True):
            raise ValueError('each assigned image variant needs build-sprites acceptance and pose review before rendering')
    packed=project/'work'/'avatar.mavatar'
    if not packed.is_file(): raise ValueError('project has no packed .mavatar; finish Mesh build/pack first')
    return packed

def inspect_video(args):
    source=args.video.resolve(strict=True)
    if not source.is_file(): raise ValueError('video must be a file')
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg: raise ValueError('ffmpeg must be available on PATH')
    duration=video_info(source); start=args.start; span=args.duration if args.duration is not None else min(20.0,duration-start)
    if not math.isfinite(start) or not math.isfinite(span) or start<0 or span<=0 or start+span>duration:
        raise ValueError('inspection interval must be inside the video duration')
    if not 3<=args.samples<=20: raise ValueError('samples must be from 3 to 20')
    out=args.out.resolve(); out.mkdir(parents=True,exist_ok=False)
    times=[start+(span*i/(args.samples-1)) for i in range(args.samples)]
    # The exact clip endpoint has no frame; sample the final instant just before EOF.
    if times[-1]>=duration: times[-1]=max(start,duration-max(1e-3,1/30))
    shots=[]
    for index,t in enumerate(times):
        frame=out/f'frame-{index:02d}.png'
        result=subprocess.run([ffmpeg,'-v','error','-i',str(source),'-ss',f'{t:.6f}','-frames:v','1','-vf','scale=640:-2','-y',str(frame)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
        if result.returncode or not frame.is_file(): raise RuntimeError(f'frame extraction failed at {t:.3f}s: {result.stderr[-1000:]}')
        shots.append((t,frame))
    cell_w,cell_h=320,220; sheet=Image.new('RGB',(cell_w*4,cell_h*math.ceil(len(shots)/4)),(30,30,30)); draw=ImageDraw.Draw(sheet)
    for i,(t,p) in enumerate(shots):
        image=Image.open(p).convert('RGB'); image.thumbnail((cell_w,cell_h-28))
        x=(i%4)*cell_w+(cell_w-image.width)//2; y=(i//4)*cell_h
        sheet.paste(image,(x,y)); draw.text(((i%4)*cell_w+8,(i//4)*cell_h+cell_h-22),f'{i:02d}  {t:.3f}s',fill='white')
    sheet_path=out/'contact-sheet.jpg'; sheet.save(sheet_path,quality=90)
    manifest={'video':str(source),'video_sha256':digest(source),'duration':duration,'interval':{'start':start,'duration':span},'frames':[{'index':i,'time':round(t,6),'path':str(p),'sha256':digest(p)} for i,(t,p) in enumerate(shots)],'contact_sheet':str(sheet_path),'contact_sheet_sha256':digest(sheet_path),'limitation':'Uniform samples identify candidate moments; inspect source at candidate timestamps before selecting action variants.'}
    (out/'video-inspection.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest

def read_keys(path, duration):
    keys = json.loads(path.read_text(encoding='utf-8-sig')) if path else {}
    if not isinstance(keys, dict) or set(keys) - set(RANGES):
        raise ValueError('keyframes must be an object of supported parameters: ' + ', '.join(RANGES))
    for name, points in keys.items():
        if not isinstance(points, list) or not points: raise ValueError(f'{name} requires nonempty keyframes')
        previous = -1
        for point in points:
            if not isinstance(point, dict) or set(point) != {'time','value'}: raise ValueError('keyframe needs time and value')
            t, value = point['time'], point['value']
            if any(isinstance(x, bool) or not isinstance(x, (int,float)) or not math.isfinite(x) for x in (t,value)):
                raise ValueError('keyframe time and value must be finite numbers')
            if t < 0 or t > duration or t <= previous or not RANGES[name][0] <= value <= RANGES[name][1]:
                raise ValueError(f'invalid {name} keyframe time or value')
            previous = t
    return keys

def render(args):
    studio = studio_path(args.studio); avatar = resolve_render_avatar(args)
    if not avatar.is_file() or avatar.suffix.lower() != '.mavatar': raise ValueError('avatar must be an existing .mavatar file')
    runtime = studio/'packages'/'runtime'/'dist'/'index.js'; playwright = studio/'node_modules'/'playwright'
    if not runtime.is_file() or not playwright.is_dir(): raise ValueError('studio requires built runtime and installed Playwright')
    browser = args.browser.resolve(strict=True)
    node = shutil.which('node'); ffmpeg = shutil.which('ffmpeg'); ffprobe = shutil.which('ffprobe')
    if not node or not ffmpeg or not ffprobe: raise ValueError('node, ffmpeg and ffprobe must be available on PATH')
    if not 1 <= args.frames <= 300: raise ValueError('frames must be from 1 to 300 at 30fps')
    duration = args.frames / 30
    keys = read_keys(args.keyframes, duration)
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    frames = out/'frames'; frames.mkdir()
    config = out/'motion.json'; config.write_text(json.dumps({'frames':args.frames,'keyframes':keys},ensure_ascii=False,indent=2),encoding='utf-8')
    driver = Path(__file__).with_name('mesh_avatar_render.cjs')
    result = subprocess.run([node,str(driver),str(studio),str(avatar),str(frames),str(config),str(browser)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=900)
    if result.returncode: raise RuntimeError(f'Mesh Avatar render failed: {result.stderr[-4000:]}')
    movie = out/'avatar.mov'
    encode = subprocess.run([ffmpeg,'-v','error','-framerate','30','-i',str(frames/'%05d.png'),'-frames:v',str(args.frames),'-c:v','png','-pix_fmt','rgba','-n',str(movie)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
    if encode.returncode: raise RuntimeError(f'FFmpeg encoding failed: {encode.stderr[-4000:]}')
    info = json.loads(subprocess.check_output([ffprobe,'-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames','-of','json',str(movie)],text=True,encoding='utf-8'))['streams'][0]
    if (info.get('codec_name'),info.get('pix_fmt'),info.get('width'),info.get('height'),info.get('avg_frame_rate'),int(info.get('nb_read_frames',0))) != ('png','rgba',1280,720,'30/1',args.frames):
        raise RuntimeError(f'encoded video does not meet contract: {info}')
    receipt={'status':'rendered','video':str(movie),'frames':args.frames,'fps':30,'duration':duration,'avatar_sha256':digest(avatar),'runtime_sha256':digest(runtime),'motion_sha256':digest(config),'video_sha256':digest(movie),'media':info,'limitations':['No speech or music analysis','Pixel-level repeatability is not guaranteed','Native playback and export require project verification']}
    (out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    return receipt

def attach(args):
    source=args.plan.resolve(strict=True); out=args.out.resolve(); video=args.video.resolve(strict=True)
    if out.parent != source.parent: raise ValueError('output plan must be next to the source plan to preserve relative paths')
    if out.exists(): raise FileExistsError('output plan already exists')
    plan=json.loads(source.read_text(encoding='utf-8-sig'))
    if plan.get('canvas') != {'width':1280,'height':720,'fps':30}: raise ValueError('only 1280x720 30fps plans are supported')
    order=plan.get('layer_order'); clips=plan.get('clips')
    if not isinstance(order,list) or not isinstance(clips,list) or args.track in order: raise ValueError('plan must have layer_order and a new track name')
    if not math.isfinite(args.start) or args.start < 0: raise ValueError('start must be nonnegative')
    receipt_path=video.parent/'receipt.json'
    receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
    if Path(receipt['video']).resolve()!=video or receipt['video_sha256']!=digest(video): raise ValueError('video does not match its render receipt')
    clips.append({'path':str(video),'track':args.track,'start':args.start,'duration':receipt['duration']})
    order.append(args.track)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.json',prefix='.mesh-plan-',dir=out.parent,delete=False) as f:
        temp=Path(f.name); json.dump(plan,f,ensure_ascii=False,indent=2)
    try:
        editor=Path(__file__).with_name('editor.py')
        check=subprocess.run([sys.executable,str(editor),'validate','--plan',str(temp)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
        if check.returncode: raise ValueError('plan validation failed: '+(check.stdout+check.stderr)[-4000:])
        # Exclusive create prevents overwriting any existing user plan.
        with out.open('x',encoding='utf-8') as f: json.dump(plan,f,ensure_ascii=False,indent=2)
    finally: temp.unlink(missing_ok=True)
    return {'status':'validated','plan':str(out),'video':str(video),'start':args.start,'duration':receipt['duration'],'track':args.track}

def prepare_character(args):
    studio=studio_path(args.studio)
    source=args.image.resolve(strict=True)
    if source.suffix.lower()!='.png': raise ValueError('Image-to-avatar currently requires a PNG source illustration')
    if not source.is_file(): raise ValueError('image must be a file')
    source_video=args.video.resolve(strict=True); duration=video_info(source_video); times=args.times
    if not times or len(times)>12 or any(not math.isfinite(t) or t<0 or t>=duration for t in times): raise ValueError('provide 1-12 selected video timestamps inside the source video')
    if any(b<=a for a,b in zip(times,times[1:])): raise ValueError('selected video timestamps must be strictly increasing')
    if not shutil.which('ffmpeg'): raise ValueError('ffmpeg must be available on PATH')
    name=args.name or re.sub(r'[^A-Za-z0-9_-]+','-',source.stem).strip('-_') or 'avatar'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',name): raise ValueError('name must use letters, digits, hyphens or underscores')
    project=studio/'projects'/name
    if project.exists(): raise FileExistsError(f'Mesh project already exists: {project}')
    new_project=studio/'tools'/'new-project.py'
    result=subprocess.run([sys.executable,str(new_project),str(source),name],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    if result.returncode: raise RuntimeError(f'Mesh project creation failed: {result.stderr[-3000:]}')
    context=project/'work'/'video-context'; context.mkdir(parents=True,exist_ok=False)
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg: raise ValueError('ffmpeg must be available on PATH')
    frames=[]
    for i,t in enumerate(times):
        target=context/f'context-{i:02d}-{t:.3f}s.png'
        result=subprocess.run([ffmpeg,'-v','error','-i',str(source_video),'-ss',f'{t:.6f}','-frames:v','1','-vf','scale=1280:-2','-y',str(target)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
        if result.returncode or not target.is_file(): raise RuntimeError(f'context frame extraction failed at {t:.3f}s: {result.stderr[-1000:]}')
        frames.append({'time':t,'path':str(target),'sha256':digest(target)})
    context_manifest={'project':str(project),'video':str(source_video),'video_sha256':digest(source_video),'selected_frames':frames,'source_image':str(project/'source.png'),'source_file_sha256':digest(project/'source.png'),'authorized_imagegen_inputs_policy':'per assignment source image + one selected built Mesh mask + only assigned video context frames'}
    (context/'context.json').write_text(json.dumps(context_manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'status':'prepared','studio':str(studio),'project':str(project),'source_image':str(project/'source.png'),
            'rig_draft':str(project/'rig.draft.json'),'video_context':str(context/'context.json'),'next':'Complete the Studio rig, build layers and visually review poses. Then run mesh-avatar prepare-variants to derive masks from the built rig and prepare per-variant image-edit requests.'}

def prepare_variants(args):
    studio=studio_path(args.studio); project=args.project.resolve(strict=True)
    if not project.is_relative_to((studio/'projects').resolve()): raise ValueError('project must be inside the configured Studio projects directory')
    required=[project/'source.png',project/'rig.json',project/'built'/'base.png',project/'built'/'layers.json',project/'work'/'video-context'/'context.json']
    missing=[str(path) for path in required if not path.is_file()]
    if missing: raise ValueError('complete and review Mesh rig/layer build before preparing variant masks: '+', '.join(missing))
    context_path=project/'work'/'video-context'/'context.json'
    context=json.loads(context_path.read_text(encoding='utf-8'))
    if context.get('project')!=str(project) or digest(project/'source.png')!=context.get('source_file_sha256'): raise ValueError('source illustration changed after project setup')
    result=subprocess.run([sys.executable,str(studio/'tools'/'variant-requests.py'),str(project)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
    if result.returncode: raise RuntimeError(f'Could not prepare Mesh variant masks: {result.stderr[-3000:]}')
    requests=project/'variant-requests'; variants_map={'eyes_closed':'blink','eyes_half':'half-lidded','eyes_smile':'smiling eyes','eyes_sad':'sad eyes','eyes_surprise':'surprised eyes','mouth_a':'open A','mouth_a_half':'half-open A','mouth_i':'narrow smile','mouth_o':'rounded mouth','mouth_angry':'angry closed mouth','mouth_sad':'sad closed mouth'}
    names=[name for name in variants_map if (requests/name/'mask.png').is_file() and (requests/name/'prompt.md').is_file()]
    if not names: raise ValueError('Mesh produced no valid local eye/mouth masks')
    base=project/'variants'/'base.png'; base.parent.mkdir(parents=True,exist_ok=True); source_hash=digest(project/'source.png')
    if not base.exists(): shutil.copyfile(project/'source.png',base)
    base_hash=digest(base)
    if base_hash!=source_hash: raise ValueError('identity base differs from original source image')
    identity=project/'work'/'identity-base.sha256'
    if identity.exists() and identity.read_text(encoding='utf-8').strip()!=source_hash: raise ValueError('identity base manifest conflicts with original source image')
    identity.write_text(source_hash+'\n',encoding='utf-8')
    data={'project':str(project),'source_image':str(project/'source.png'),'source_sha256':source_hash,'identity_base':str(base),'identity_base_sha256':base_hash,'identity_manifest_sha256':digest(identity),'video':context['video'],'video_sha256':context['video_sha256'],'policy':'Per ImageGen edit send original source.png, one variant mask and only assigned frame(s). Preserve all pixels outside the Mesh mask and do not copy scene background/full-body pose.'}
    data['variants']=[{'name':name,'expression':variants_map[name],'mask':str(requests/name/'mask.png'),'mask_sha256':digest(requests/name/'mask.png'),'prompt':str(requests/name/'prompt.md'),'output':str(project/'variants'/f'{name}.png'),'identity_sha256':base_hash,'context_frame_indices':[]} for name in names]
    output=requests/'video-context-requests.json'
    with output.open('x',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2)
    return {'status':'ready_for_imagegen','project':str(project),'requests':str(output),'variants':len(names),'per_call_inputs':'source.png + one mask.png + explicitly assigned frame(s)','note':'This operation does not call image generation; the active workbench Agent makes each selected ImageGen edit.'}

def plan_variants(args):
    studio=studio_path(args.studio); project=args.project.resolve(strict=True)
    if not project.is_relative_to((studio/'projects').resolve()): raise ValueError('project must be inside the configured Studio projects directory')
    context_path=project/'work'/'video-context'/'context.json'
    context=json.loads(context_path.read_text(encoding='utf-8'))
    request_path=project/'variant-requests'/'video-context-requests.json'
    bundle=json.loads(request_path.read_text(encoding='utf-8'))
    if Path(bundle.get('project','')).resolve()!=project: raise ValueError('variant request project identity does not match')
    if context.get('video')!=bundle.get('video') or context.get('video_sha256')!=bundle.get('video_sha256'):
        raise ValueError('source video identity differs between context and variant requests')
    identity_manifest=project/'work'/'identity-base.sha256'
    if not identity_manifest.is_file(): raise ValueError('identity base manifest is missing')
    if digest(identity_manifest)!=bundle.get('identity_manifest_sha256'):
        raise ValueError('identity base manifest changed after variant request preparation')
    if digest(project/'variants'/'base.png')!=bundle.get('identity_base_sha256'):
        raise ValueError('identity base changed after variant request preparation')
    if digest(project/'source.png')!=bundle.get('source_sha256') or identity_manifest.read_text(encoding='utf-8').strip()!=bundle['source_sha256']:
        raise ValueError('source illustration changed after variant request preparation')
    selection=json.loads(args.selection.read_text(encoding='utf-8-sig'))
    if not isinstance(selection,list) or not selection: raise ValueError('selection must be a nonempty array of variant assignments')
    source=project/'source.png'; identity_base=project/'variants'/'base.png'
    if Path(bundle['source_image']).resolve()!=source.resolve() or Path(bundle['identity_base']).resolve()!=identity_base.resolve(): raise ValueError('variant bundle source or identity base path mismatch')
    if context.get('project')!=str(project) or context.get('source_file_sha256')!=digest(source): raise ValueError('source illustration changed after video-context preparation')
    if digest(Path(context['video']).resolve(strict=True))!=context['video_sha256']:
        raise ValueError('source video changed after context preparation')
    by_name={item['name']:item for item in bundle['variants']}; planned=[]; seen=set()
    for item in selection:
        if not isinstance(item,dict) or set(item)!={'name','context_frame_indices','observed_expression'}: raise ValueError('each selection needs name, context_frame_indices and observed_expression')
        name=item['name']; indices=item['context_frame_indices']; observed=item['observed_expression']
        if name not in by_name or name in seen: raise ValueError('unknown or duplicate variant name')
        if not isinstance(observed,str) or not observed.strip() or len(observed)>240: raise ValueError('observed_expression must be concise nonempty text')
        if not isinstance(indices,list) or not indices or any(isinstance(i,bool) or not isinstance(i,int) or i<0 or i>=len(context['selected_frames']) for i in indices) or len(set(indices))!=len(indices): raise ValueError(f'{name} needs valid unique selected frame indices')
        seen.add(name); entry=by_name[name]
        if entry.get('identity_sha256')!=digest(source): raise ValueError('identity base changed after variant request preparation')
        frame_inputs=[context['selected_frames'][i] for i in indices]
        for frame in frame_inputs:
            frame_path=Path(frame['path']).resolve(strict=True)
            if not frame_path.is_relative_to((project/'work'/'video-context').resolve()): raise ValueError('selected context frame escapes the prepared project directory')
            if digest(frame_path)!=frame['sha256']: raise ValueError('selected context frame changed after preparation')
        mask_path=Path(entry['mask']).resolve(strict=True)
        if not mask_path.is_relative_to((project/'variant-requests').resolve()): raise ValueError('variant mask escapes the prepared request directory')
        if digest(mask_path)!=entry.get('mask_sha256'): raise ValueError('variant mask changed after request preparation')
        if digest(source)!=entry.get('identity_sha256'): raise ValueError('variant identity base changed after request preparation')
        if context.get('source_file_sha256') and digest(project/'source.png')!=context['source_file_sha256']: raise ValueError('Mesh source illustration changed after context preparation')
        planned.append({'name':name,'observed_expression':observed.strip(),'video_timestamps':[f['time'] for f in frame_inputs],
                        'inputs':[str(source),str(mask_path),*[str(Path(f['path']).resolve()) for f in frame_inputs]],
                        'input_sha256':[digest(source),digest(mask_path),*[f['sha256'] for f in frame_inputs]],
                        'mask':str(mask_path),'mask_sha256':digest(mask_path),'output':entry['output'],'prompt_file':entry['prompt'],
                        'acceptance_receipt':str(project/'work'/'acceptance'/f'{name}.json'),
                        'prompt':Path(entry['prompt']).read_text(encoding='utf-8')+'\nThe selected video frames are expression references only. Preserve identity, costume, hair, pose framing and style from source.png. Keep every pixel outside the supplied Mesh mask unchanged; do not copy the frame background or redraw the full image.'})
    result={'project':str(project),'source_sha256':digest(source),'identity_base_sha256':digest(identity_base),'identity_manifest_sha256':digest(identity_manifest),'video_sha256':context['video_sha256'],
        'authorization_scope':'Only this project source image, each assigned variant mask, and the explicitly assigned selected context frame(s). The original image is the identity anchor; the unedited base.png is retained as the rig source. This consent is for this prepared project only.',
            'variants':planned}
    out=project/'work'/'video-context'/'imagegen-assignments.json'
    with out.open('x',encoding='utf-8') as f: json.dump(result,f,ensure_ascii=False,indent=2)
    return {'status':'planned','assignments':str(out),'variants':len(planned),'imagegen_calls_required':len(planned),'inputs_per_call':'source image + one Mesh mask + assigned context frame(s)'}

def build_character(args):
    studio=studio_path(args.studio); project=args.project.resolve(strict=True)
    if not project.is_relative_to((studio/'projects').resolve()): raise ValueError('project must be inside the configured Studio projects directory')
    required=[project/'rig.json',project/'built'/'base.png',project/'built'/'layers.json']
    missing=[str(path) for path in required if not path.is_file()]
    if missing: raise ValueError('Mesh rig/layers are not ready; complete the guided rig and layer build first: '+', '.join(missing))
    result=subprocess.run([sys.executable,str(studio/'tools'/'build-sprites.py'),str(project)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
    if result.returncode: raise RuntimeError(f'Mesh sprite build failed: {result.stderr[-4000:]}')
    node=shutil.which('node')
    if not node: raise ValueError('node must be available on PATH')
    poses=subprocess.run([node,'--experimental-strip-types',str(studio/'tools'/'render-poses.mjs'),str(project)],cwd=studio,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
    if poses.returncode: raise RuntimeError(f'Mesh pose review failed: {poses.stderr[-4000:]}')
    return {'status':'built','project':str(project),'sprite_build':result.stdout[-2000:],'pose_review':poses.stdout[-2000:],
            'next':'Review pose sheets and avatar in Studio, correct rig/artifact errors, then pack and validate the .mavatar.'}

def pack_character(args):
    studio=studio_path(args.studio); project=args.project.resolve(strict=True)
    if not project.is_relative_to((studio/'projects').resolve()): raise ValueError('project must be inside the configured Studio projects directory')
    avatar=project/'avatar.json'; rig=project/'rig.json'; layers=project/'built'/'layers.json'; sprites=project/'built'/'sprites'/'sprites.json'
    if not all(p.is_file() for p in (avatar,rig,layers)): raise ValueError('Mesh project is incomplete; build rig and layers first')
    manifest=project/'work'/'video-context'/'imagegen-assignments.json'
    if args.require_sprites and not manifest.is_file(): raise ValueError('no per-variant manifest with Mesh acceptance and pose-review receipts')
    if args.require_sprites and (not sprites.is_file() or not json.loads(sprites.read_text(encoding='utf-8')).get('layers')):
        raise ValueError('no accepted expression sprites; generate and review variants before packing')
    if args.require_sprites:
        assignments=json.loads(manifest.read_text(encoding='utf-8'))
        if not assignments.get('variants'): raise ValueError('variant manifest is empty')
        if digest(project/'source.png')!=assignments.get('source_sha256') or digest(project/'variants'/'base.png')!=assignments.get('identity_base_sha256'):
            raise ValueError('source illustration or identity base changed after variant planning')
        if digest(Path(assignments['video']).resolve(strict=True))!=assignments.get('video_sha256'):
            raise ValueError('source video changed after variant planning')
        for item in assignments['variants']:
            output=Path(item['output']).resolve()
            if not output.is_relative_to((project/'variants').resolve()) or not output.is_file(): raise ValueError(f'planned image variant is missing or escapes project: {item["name"]}')
            inputs=item.get('inputs',[]); hashes=item.get('input_sha256',[])
            if len(inputs)<3 or len(inputs)!=len(hashes) or Path(inputs[0]).resolve()!=(project/'source.png').resolve() or Path(inputs[1]).resolve()!=Path(item['mask']).resolve():
                raise ValueError(f'ImageGen input manifest is invalid: {item["name"]}')
            for input_path,expected in zip(inputs,hashes):
                if digest(Path(input_path).resolve(strict=True))!=expected: raise ValueError(f'ImageGen input changed after planning: {item["name"]}')
            accepted=Path(item.get('acceptance_receipt',''))
            if not accepted.is_absolute(): accepted=(project/accepted).resolve()
            if not accepted.is_file(): raise ValueError(f'image variant has no acceptance receipt: {item["name"]}')
            proof=json.loads(accepted.read_text(encoding='utf-8'))
            if (proof.get('variant')!=item['name'] or proof.get('image_sha256')!=digest(output)
                    or proof.get('mask_sha256')!=item.get('mask_sha256')
                    or proof.get('mesh_build_sha256')!=digest(sprites)
                    or proof.get('accepted_outside_mask') is not True
                    or proof.get('reviewed_pose') is not True):
                raise ValueError(f'image variant lacks current Mesh mask acceptance or pose review: {item["name"]}')
    output=args.out.resolve(); output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise FileExistsError(f'avatar output already exists: {output}')
    result=subprocess.run(['npm','run','pack-avatar','--',str(project),'-o',str(output)],cwd=studio,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
    if result.returncode: raise RuntimeError(f'Mesh avatar pack failed: {result.stderr[-3000:]}')
    validate=subprocess.run(['npm','run','validate-avatar','--',str(output)],cwd=studio,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
    if validate.returncode: raise RuntimeError(f'packed avatar validation failed: {validate.stderr[-3000:]}')
    return {'status':'packed_and_validated','avatar':str(output),'sha256':digest(output),'validation':validate.stdout.strip(),
            'project':str(project),'sprite_manifest_sha256':digest(sprites) if sprites.is_file() else None}

def main():
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__); sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('configure'); p.add_argument('--studio',type=Path,required=True)
    p=sub.add_parser('inspect-video'); p.add_argument('--video',type=Path,required=True); p.add_argument('--out',type=Path,required=True); p.add_argument('--start',type=float,default=0); p.add_argument('--duration',type=float); p.add_argument('--samples',type=int,default=12)
    p=sub.add_parser('prepare-character'); p.add_argument('--studio',type=Path); p.add_argument('--image',type=Path,required=True); p.add_argument('--video',type=Path,required=True); p.add_argument('--times',type=float,nargs='+',required=True); p.add_argument('--name')
    p=sub.add_parser('prepare-variants'); p.add_argument('--studio',type=Path); p.add_argument('--project',type=Path,required=True)
    p=sub.add_parser('plan-variants'); p.add_argument('--studio',type=Path); p.add_argument('--project',type=Path,required=True); p.add_argument('--selection',type=Path,required=True)
    p=sub.add_parser('build-character'); p.add_argument('--studio',type=Path); p.add_argument('--project',type=Path,required=True)
    p=sub.add_parser('pack-character'); p.add_argument('--studio',type=Path); p.add_argument('--project',type=Path,required=True); p.add_argument('--out',type=Path,required=True); p.add_argument('--require-sprites',action='store_true')
    p=sub.add_parser('render'); p.add_argument('--studio',type=Path); p.add_argument('--avatar',type=Path,required=True,help='Validated .mavatar or completed Studio project; project render requires all selected variants accepted and pose-reviewed'); p.add_argument('--out',type=Path,required=True)
    p.add_argument('--frames',type=int,required=True); p.add_argument('--keyframes',type=Path); p.add_argument('--browser',type=Path,default=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe')
    p=sub.add_parser('attach'); p.add_argument('--plan',type=Path,required=True);p.add_argument('--video',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--start',type=float,required=True);p.add_argument('--track',default='avatar')
    args=parser.parse_args()
    try:
        if args.command=='prepare-character': result=prepare_character(args)
        elif args.command=='prepare-variants': result=prepare_variants(args)
        elif args.command=='plan-variants': result=plan_variants(args)
        elif args.command=='build-character': result=build_character(args)
        elif args.command=='pack-character': result=pack_character(args)
        elif args.command=='inspect-video': result=inspect_video(args)
        elif args.command=='configure':
            studio=studio_path(args.studio); path=config_path(); path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as f: json.dump({'studio':str(studio)},f,ensure_ascii=False,indent=2)
            result={'status':'configured','studio':str(studio),'config':str(path)}
        else: result=render(args) if args.command=='render' else attach(args)
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (ValueError,OSError,RuntimeError,KeyError,subprocess.SubprocessError) as e: print(json.dumps({'status':'failed','error':str(e)},ensure_ascii=False),file=sys.stderr); return 1
if __name__=='__main__': sys.exit(main())
