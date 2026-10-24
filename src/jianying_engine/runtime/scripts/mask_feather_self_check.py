"""Formal rectangle feather build, owned resources, migration/publication and refusal checks; no UI."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor
import migrate
import native_advanced
import native_mask_feather as masks
import native_preview


def check(root):
    root.mkdir(exist_ok=False, parents=True)
    source = masks.source_path()
    original = masks.tree(source)
    inputs = root / 'inputs'
    inputs.mkdir()
    foreground, background = inputs / 'foreground.png', inputs / 'background.png'
    Image.new('RGB',(1280,720),(240,240,240)).save(foreground)
    Image.new('RGB',(1280,720),(20,40,90)).save(background)
    base = {'schema_version':1,'name':'formal-feather-check','mode':'script',
            'canvas':{'width':1280,'height':720,'fps':24},
            'clips':[{'path':str(background),'start':0,'duration':2},
                     {'path':str(foreground),'start':0,'duration':2,'track':'panel',
                      'mask':{'name':'矩形','experimental':True,'size':.6,'rect_width':.3375,
                              'feather':20,'rendering':masks.MODE}}], 'layer_order':['video','panel']}
    path=root/'plan.json'
    editor.write_json(path,base)
    before=path.read_bytes()
    built=editor.build_draft(Args(plan=str(path),out=str(root/'draft')))
    assert built['verification']['native_effects']['rectangle_red_coverage']==1
    assert path.read_bytes()==before and masks.tree(source)==original
    masks.verify_directory(root/'draft')
    content=editor.read_json(root/'draft/draft_content.json')
    native=content['materials']['common_mask'][0]
    assert Path(native['path'])==root/'draft'/masks.RELATIVE and native['config']['feather']==.2
    assert masks.tree(root/'draft'/masks.RELATIVE)==masks.derived_files()
    checks=['formal validate/build binds exact owned resource with standard editable feather; source and plan unchanged']
    # Publication must not reach the original cache when the draft already owns an authenticated copy.
    archive=root/'archive';archive.mkdir()
    with patch.object(native_advanced,'cached_material',side_effect=ValueError('original cache deliberately unavailable')):
        published=editor.publish_draft(Args(draft=str(root/'draft'),draft_root=str(archive),offline_copy=True))
    destination=Path(published['draft'])
    plan=editor.read_json(destination/'edit-plan.json')
    exported=editor.read_json(destination/'draft_content.json')
    assert Path(exported['materials']['common_mask'][0]['path'])==destination/masks.RELATIVE
    editor.verify_structure(exported,plan,destination,destination)
    checks.append('offline publication uses owned copy without source-cache access, rewrites resource path and verifies the new root')
    dynamic=copy.deepcopy(base)
    dynamic['name']='feather-keys'
    dynamic['clips'][1]['mask'].update(feather=0,keyframes=[{'property':'feather','time':0,'value':5},
                                                        {'property':'feather','time':1.5,'value':20}])
    editor.write_json(root/'dynamic.json',dynamic)
    editor.build_draft(Args(plan=str(root/'dynamic.json'),out=str(root/'dynamic')))
    data=editor.read_json(root/'dynamic/draft_content.json')
    keys=next(g for t in data['tracks'] for s in t['segments'] for g in s.get('common_keyframes',[])
              if g['property_type']=='KFTypeCommonMaskFeather')
    assert [(k['time_offset'],k['values']) for k in keys['keyframe_list']]==[(0,[.05]),(1500000,[.2])]
    checks.append('formal nonzero feather keys retain native source timing and material reference')
    moved=root/'moved-media';shutil.copytree(inputs,moved)
    remapped,changes,missing=migrate.remap_plan(path,[(migrate.PureWindowsPath(str(inputs)),moved)])
    assert not missing and remapped['clips'][1]['mask']['rendering']==masks.MODE
    remapped['name']='migrated-feather'
    editor.write_json(root/'migrated.json',remapped)
    editor.build_draft(Args(plan=str(root/'migrated.json'),out=str(root/'migrated-draft')))
    masks.verify_directory(root/'migrated-draft')
    checks.append('explicit media-root plan migration retains mode and rebuilds a new draft-owned resource')

    def reject(label,mutate):
        value=copy.deepcopy(base);mutate(value)
        target=root/('reject-'+str(len(checks))+'.json');editor.write_json(target,value)
        frozen=target.read_bytes()
        try:
            editor.build_draft(Args(plan=str(target),out=str(root/('rejected-'+str(len(checks))))))
        except (ValueError,OSError):
            pass
        else:
            raise AssertionError(label+' accepted')
        assert not (root/('rejected-'+str(len(checks)))).exists() and target.read_bytes()==frozen
        checks.append('refused: '+label)
    for label,mutate in (
        ('default nonzero remains refused',lambda p:p['clips'][1]['mask'].pop('rendering')),
        ('unknown mode',lambda p:p['clips'][1]['mask'].update(rendering='other')),
        ('circle variant',lambda p:p['clips'][1]['mask'].update(name='圆形')),
        ('over admitted feather',lambda p:p['clips'][1]['mask'].update(feather=21)),
        ('round corners',lambda p:p['clips'][1]['mask'].update(round_corner=10)),
        ('clip transform',lambda p:p['clips'][1].update(transform={'scale_x':1.2})),
        ('cover fit',lambda p:p['clips'][1].update(fit='cover')),
        ('transition combination',lambda p:p['clips'][1].update(transition={'name':'叠化','duration':.1})),
        ('other mask key property',lambda p:p['clips'][1]['mask'].update(keyframes=[{'property':'width','time':0,'value':400}])),
        ('large feather key',lambda p:p['clips'][1]['mask'].update(keyframes=[{'property':'feather','time':0,'value':30}])),
        ('composite',lambda p:p.update(composites=[{'name':'g','tracks':['video','panel']}]))):
        reject(label,mutate)
    with patch.object(masks,'source_path',side_effect=ValueError('source cache missing')):
        reject('missing source cache',lambda p:None)
    with patch.object(masks,'check_runtime',side_effect=ValueError('runtime changed')):
        reject('runtime identity changed',lambda p:None)
    # Every file is mandatory, authenticated independently from its adjacent identity document.
    for relative in masks.derived_files():
        file=destination/masks.RELATIVE/relative
        frozen=file.read_bytes()
        file.write_bytes(frozen+b'changed')
        try:
            masks.verify_directory(destination)
        except ValueError:
            pass
        else:
            raise AssertionError('Changed companion accepted: '+relative)
        file.write_bytes(frozen)
        checks.append('refused changed owned file: '+relative)
    extra=destination/masks.RELATIVE/'extra.lua';extra.write_text('return {}',encoding='utf-8')
    try:
        masks.verify_directory(destination)
    except ValueError:
        pass
    else:
        raise AssertionError('Unlisted script accepted')
    extra.unlink();checks.append('unlisted resource script refused')
    file=destination/masks.RELATIVE/masks.contract()['fragment'];frozen=file.read_bytes();file.unlink()
    try:
        native_preview.preview(editor.jianying_install(),destination/'draft_content.json',root/'must-not-start-preview',[.1])
    except ValueError:
        pass
    else:
        raise AssertionError('Missing resource reached preview')
    assert not (root/'must-not-start-preview').exists()
    file.write_bytes(frozen);checks.append('missing shader refused before native preview starts')
    metadata=destination/masks.IDENTITY;frozen=metadata.read_bytes();metadata.write_text('{}',encoding='utf-8')
    try:
        editor.publish_draft(Args(draft=str(destination),draft_root=str(archive),offline_copy=True))
    except ValueError:
        pass
    else:
        raise AssertionError('Tampered identity accepted')
    metadata.write_bytes(frozen);checks.append('tampered identity refused by formal publication')
    bad=copy.deepcopy(exported);bad['materials']['common_mask'][0]['path']=str(root/'draft'/masks.RELATIVE)
    try:
        editor.verify_structure(bad,plan,destination,destination)
    except ValueError:
        pass
    else:
        raise AssertionError('Stale published path accepted')
    checks.append('old source-draft resource path refused at published-root verification')
    resources=destination/'resources';hold=destination/'resources-retained'
    assert resources.resolve().is_relative_to(root.resolve()) and hold.parent.resolve()==destination.resolve()
    resources.rename(hold)
    try:
        command='& { New-Item -ItemType Junction -Path $args[0] -Target $args[1] -ErrorAction Stop | Out-Null }'
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command,str(resources),str(hold)],check=True,capture_output=True)
        try:
            masks.verify_directory(destination)
        except ValueError:
            pass
        else:
            raise AssertionError('Junction resource parent accepted')
    finally:
        if os.path.isjunction(resources):
            assert resources.resolve()==hold.resolve()
            os.rmdir(resources)
        hold.rename(resources)
    checks.append('linked resource parent refused without following or modifying its target')
    for key,value in (('expansion',.8),('feather',True),('invert',0)):
        bad=copy.deepcopy(exported);bad['materials']['common_mask'][0]['config'][key]=value
        try:
            editor.verify_structure(bad,plan,destination,destination)
        except ValueError:
            pass
        else:
            raise AssertionError('Undeclared config/type accepted: '+key)
        checks.append('refused derived config/type: '+key)
    assert masks.tree(source)==original and path.read_bytes()==before
    report={'status':'passed','checks_count':len(checks),'checks':checks,'shared_resource_unchanged':True,
            'scope':'formal build/verify/offline publish/migration and refusal boundaries; no UI/save-reopen/export',
            'paid_calls':0,'UI_operations':0}
    editor.write_json(root/'mask-feather-check.json',report)
    return report

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    editor.emit(check(p.parse_args().out.resolve()))
