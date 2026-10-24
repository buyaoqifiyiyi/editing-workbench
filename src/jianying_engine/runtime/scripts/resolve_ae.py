"""Return one verified AE render to a new Resolve plan; no host is launched."""
import copy
import json
from pathlib import Path

import adobe_workflow
import editor
import resolve_compile


def return_shot(args):
    original = Path(args.plan).resolve(strict=True)
    output = Path(args.out).absolute()
    receipt_path = output.with_suffix(output.suffix + '.return.json')
    if (not output.parent.is_dir() or any(p.exists() or p.is_symlink()
                                         for p in (output, receipt_path))):
        raise ValueError('Choose a new output plan and return receipt with an existing parent')
    original_hash = adobe_workflow.sha(original)
    target = resolve_compile.validate_plan(original, editor.probe)
    root, ae = adobe_workflow.load_bundle(Path(args.bundle))
    if ae.get('audio'):
        raise ValueError('AE independent audio/BGM is not baked into shots; mix it on the main Resolve timeline')
    execution_path = root / 'execution.json'
    if execution_path.is_symlink():
        raise ValueError('Expected regular AE execution receipt')
    execution = json.loads(execution_path.read_text(encoding='utf-8'))
    if execution.get('status') != 'ae_rendered_pr_import_pending':
        raise ValueError('AE execution has not completed successfully')
    project = root / 'ae-reopened.aep'
    if project.is_symlink() or not project.is_file() or project.stat().st_size == 0:
        raise ValueError('Expected the saved and reopened editable AE project')
    clip_index = args.clip_index
    shot_index = getattr(args, 'shot_index', 1)
    for index, clips, label in ((clip_index, target['clips'], 'clip_index'),
                                (shot_index, ae['clips'], 'shot_index')):
        if type(index) is not int or not 1 <= index <= len(clips):
            raise ValueError(f'{label}: expected a 1-based index within the plan')
    clip, shot = target['clips'][clip_index - 1], ae['clips'][shot_index - 1]
    if target['canvas'] != ae['canvas']:
        raise ValueError('AE and Resolve canvas size/fps differ')
    if any(clip[key] != shot[key] for key in ('path', 'source_in_frames', 'duration_frames', 'volume')):
        raise ValueError('AE source selection/duration/gain differs from the selected Resolve clip')
    verified = adobe_workflow.verify_media(root, ae)
    recorded = execution.get('outputs', [])
    if (len(recorded) != len(verified['outputs']) or
            any(recorded_item.get('path') != current['path'] or recorded_item.get('sha256') != current['sha256']
                for recorded_item, current in zip(recorded, verified['outputs']))):
        raise ValueError('AE render differs from the successful execution receipt')
    rendered = verified['outputs'][shot_index - 1]
    # Preserve the original document, including main-timeline audio and captions.
    raw = json.loads(original.read_text(encoding='utf-8-sig'), object_pairs_hook=resolve_compile._pairs,
                     parse_constant=resolve_compile._constant)
    updated = copy.deepcopy(raw)
    # Rebase relative paths when the new plan is saved in a different directory.
    for group in ('clips', 'audio'):
        for item, normalized in zip(updated.get(group, []), target[group]):
            if not Path(item['path']).is_absolute():
                item['path'] = normalized['path']
    replacement = updated['clips'][clip_index - 1]
    replacement.update(path=rendered['path'], source_in=0, volume=1, data_level='full',
                       transform={'scale': 1, 'x': target['canvas']['width'] / 2,
                                  'y': target['canvas']['height'] / 2, 'rotation': 0, 'opacity': 1})
    resolve_compile._normalize(updated, output.parent, editor.probe)
    if adobe_workflow.sha(original) != original_hash:
        raise ValueError('Original Resolve plan changed during return; choose a new attempt')
    adobe_workflow.load_bundle(root)  # Media decoding may take time; recheck AE inputs before publishing.
    handoff = json.loads((root / 'handoff.json').read_text(encoding='utf-8'))
    receipt = {'schema_version': 1, 'status': 'ae_returned_resolve_execution_pending',
               'original_plan': str(original), 'original_plan_sha256': original_hash,
               'ae_bundle': str(root), 'ae_plan_sha256': handoff['plan_sha256'],
               'ae_handoff_sha256': adobe_workflow.sha(root / 'handoff.json'),
               'ae_execution_sha256': adobe_workflow.sha(execution_path),
               'ae_project': str(project), 'ae_project_sha256': adobe_workflow.sha(project),
               'clip_index': clip_index, 'shot_index': shot_index,
               'render': rendered['path'], 'render_sha256': rendered['sha256'],
               'output_plan': str(output), 'return_receipt': str(receipt_path),
               'effect_editability': 'AE layers remain editable in AEP; Resolve receives rendered video',
               'resolve_execution': 'not_performed', 'human_viewing_listening': 'not_performed'}
    adobe_workflow.write_new(output, json.dumps(updated, ensure_ascii=False, indent=2))
    receipt['output_plan_sha256'] = adobe_workflow.sha(output)
    adobe_workflow.write_new(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt
