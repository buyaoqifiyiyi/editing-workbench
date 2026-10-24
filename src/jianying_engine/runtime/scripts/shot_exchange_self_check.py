"""Frame/identity/no-overwrite contract checks; optional real background Blender render."""
import argparse
import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
import sys
from PIL import Image

import editor
import shot_exchange as exchange


def check(root, blender=None):
    root.mkdir(parents=True, exist_ok=False)
    source = root / 'source.mp4'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                "nullsrc=s=320x180:r=30:d=4,geq=lum='16+mod(N*37,220)':cb=128:cr=128",
                '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', source])
    base = {'schema_version': 1, 'name': '开源镜头协作', 'mode': 'script',
            'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': 'source.mp4', 'start': 0, 'source_in': 1, 'duration': 1},
                      {'path': 'source.mp4', 'start': 1, 'source_in': 2, 'duration': 1}]}
    plan_path = root / 'source-plan.json'
    editor.write_json(plan_path, base)
    original = plan_path.read_bytes()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = []

    def prepare(name, plan=None, **changes):
        path = plan_path
        if plan is not None:
            path = root / (name + '-plan.json')
            editor.write_json(path, plan)
        values = dict(plan=path, index=0, head_frames=3, tail_frames=3, end_scale=1, out=root / name)
        values.update(changes)
        return exchange.prepare(Args(**values))

    handoff = root / 'handoff'
    result = prepare('handoff')
    assert result['status'] == 'handoff_prepared' and result['native_project'] == 'not_created'
    shot = result['shot']
    assert shot['source_first_frame'] == 27 and shot['content_frames'] == 30 and shot['total_frames'] == 36
    assert shot['timeline_start_frame'] == 0 and shot['head_frames'] == 3 and shot['tail_frames'] == 3
    compile((handoff / 'create_blender.py').read_text(encoding='utf-8'), 'generated', 'exec')
    checks.append('source/timeline/handles are integer frames; prepare does not pretend to create a native project')

    def refuses(label, action, output=None):
        try:
            action()
        except (ValueError, OSError, KeyError):
            if output:
                assert not output.exists(), label
            checks.append(label)
        else:
            raise AssertionError('Expected refusal: ' + label)

    refuses('prepare never overwrites', lambda: prepare('handoff'))
    for name, options in (('index', {'index': -1}), ('handles', {'head_frames': 121}),
                          ('negative', {'tail_frames': -1}), ('scale', {'end_scale': float('nan')})):
        refuses(name, lambda options=options, name=name: prepare(name, **options), root / name)
    for name, field, value in (('speed', 'speed', 2), ('sound', 'volume', 1),
                               ('transform', 'transform', {'rotation': 3}),
                               ('subframe', 'source_in', 1.01)):
        plan = copy.deepcopy(base)
        plan['clips'][0][field] = value
        refuses(name, lambda name=name, plan=plan: prepare(name, plan), root / name)
    plan = copy.deepcopy(base)
    plan['clips'][0]['source_in'] = 0
    refuses('missing head handle', lambda: prepare('head', plan), root / 'head')
    plan_path.write_bytes(original + b'\n')
    refuses('plan byte changes invalidate old handoff', lambda: exchange.load_handoff(handoff))
    plan_path.write_bytes(original)
    snapshot = (handoff / 'source-plan.json').read_bytes()
    corrupted = editor.read_json(handoff / 'source-plan.json')
    corrupted['clips'][0]['source_in'] = 0
    editor.write_json(handoff / 'source-plan.json', corrupted)
    refuses('snapshot edits cannot shift source timing', lambda: exchange.load_handoff(handoff))
    (handoff / 'source-plan.json').write_bytes(snapshot)
    hbytes = (handoff / 'handoff.json').read_bytes()
    corrupted = editor.read_json(handoff / 'handoff.json')
    corrupted['shot']['index'] = 1
    editor.write_json(handoff / 'handoff.json', corrupted)
    refuses('selection edits invalidate handoff', lambda: exchange.load_handoff(handoff))
    (handoff / 'handoff.json').write_bytes(hbytes)
    script = handoff / 'create_blender.py'
    trusted = script.read_bytes()
    script.write_text('print("untrusted script must never run")\n', encoding='utf-8')
    forged = editor.read_json(handoff / 'handoff.json')
    forged['script_sha256'] = hashlib.sha256(script.read_bytes()).hexdigest()
    editor.write_json(handoff / 'handoff.json', forged)
    fake_executable = root / 'blender.exe'
    fake_executable.write_bytes(b'not an executable')
    with patch.object(exchange.editor, 'run', side_effect=AssertionError('Untrusted script launched')):
        # Source probing also uses editor.run; validate identity before installing the launch sentinel.
        with patch.object(exchange, 'load_handoff', return_value=(handoff, forged, {}, [])):
            try:
                exchange.build(Args(handoff=handoff, blender=fake_executable, render=False))
            except ValueError as exc:
                assert 'script changed' in str(exc)
            else:
                raise AssertionError('Forged script/hash was accepted')
    script.write_bytes(trusted)
    (handoff / 'handoff.json').write_bytes(hbytes)
    checks.append('script plus manifest hash tampering cannot turn the whitelisted operation into arbitrary Python execution')

    # A surrogate exercises return validation only; it is never reported as Blender output.
    render = root / 'surrogate.mp4'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', '0.9', '-i', source,
                '-t', '1.2', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', render])
    project = root / 'attachment-only.blend'
    project.write_bytes(b'BLENDER' + bytes(64))
    returned = root / 'return'
    report = exchange.apply(Args(out=returned, handoff=handoff, render_file=render, project=project))
    assert report['project_validation'] == 'attachment_header_only' and report['producer'] == 'caller_supplied'
    normalized, _, _ = editor.validate_plan(returned / 'edit-plan.json')
    assert normalized['clips'][0]['source_in'] == .1 and normalized['clips'][0]['duration'] == 1
    assert normalized['clips'][1]['source_in'] == 2 and normalized['clips'][1]['start'] == 1
    assert plan_path.read_bytes() == original and hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    checks.append('surrogate return preserves timeline/other clips; strips handles using the returned source offset')
    refuses('apply never overwrites', lambda: exchange.apply(Args(out=returned, handoff=handoff, render_file=render, project=project)))
    refuses('original media is not a returned render', lambda: exchange.verify_return(source, shot))
    for label, options in (('wrong-length', ['-t', '0.5']), ('wrong-fps', ['-r', '25']),
                           ('wrong-size', ['-vf', 'scale=160:90'])):
        invalid = root / (label + '.mp4')
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', render, *options,
                    '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', invalid])
        refuses(label, lambda invalid=invalid: exchange.verify_return(invalid, shot))
    audible = root / 'audible.mp4'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', render, '-f', 'lavfi', '-i',
                'anullsrc=r=48000:cl=stereo', '-t', '1.2', '-c:v', 'copy', '-c:a', 'aac', audible])
    refuses('returned audio cannot double the original mix', lambda: exchange.verify_return(audible, shot))
    rendered = None
    if blender:
        native = root / 'native'
        prepare('native', end_scale=1.1)
        rendered = exchange.build(Args(handoff=native, blender=blender, render=True))
        assert rendered['status'] == 'render_verified'
        assert rendered['receipt']['strip_start'] == 1 and rendered['receipt']['strip_end_exclusive'] == 37
        expression = ("import bpy,json; s=bpy.context.scene; strip=s.sequence_editor.strips[0]; "
                      "s.frame_set(4); first=strip.transform.scale_x; s.frame_set(33); last=strip.transform.scale_x; "
                      "print('SHOT_REOPEN '+json.dumps({'first':first,'last':last,'source':bpy.path.abspath(strip.filepath),"
                      "'start':strip.frame_start,'left':strip.frame_final_start,'right':strip.frame_final_end}))")
        reopened = editor.run([blender, '--disable-autoexec', '--background', native / 'shot.blend',
                               '--python-expr', expression])
        import json
        data = json.loads(next(line.split('SHOT_REOPEN ', 1)[1] for line in reopened.stdout.splitlines()
                               if line.startswith('SHOT_REOPEN ')))
        assert abs(data['first'] - 1) < .00001 and abs(data['last'] - 1.1) < .00001
        assert data['start'] == -26 and data['left'] == 1 and data['right'] == 37
        assert Path(data['source']).resolve() == source.resolve()
        editor.write_json(native / 'background-reopen.json', data)
        checks.append('separate background reopen preserves native source trim and editable zoom keyframe endpoints')
        result = exchange.apply(Args(out=root / 'native-return', handoff=native,
                                    render_file=native / 'processed.mp4', project=None))
        assert result['status'] == 'return_plan_created'
        checks.append('real background Blender creates editable .blend and renders 36 verified frames before return')
        for returned_frame in (0, 3, 17, 32, 35):
            pixels = []
            for label, media, frame in (('original', source, 27 + returned_frame),
                                        ('blender', native / 'processed.mp4', returned_frame)):
                image = root / f'{label}-{returned_frame}.png'
                editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', media,
                            '-vf', f'select=eq(n\\,{frame})', '-frames:v', '1', image])
                with Image.open(image) as picture:
                    pixels.append(picture.convert('RGB').getpixel((160, 90)))
            assert max(abs(a-b) for a,b in zip(*pixels)) <= 3, (returned_frame, pixels)
        checks.append('five native frame identities including handles/content edges match independent source samples within 3 RGB levels')
    else:
        checks.append('native Blender build/render not run; surrogate tests do not prove native output')
    try:
        import opentimelineio as otio
    except ImportError:
        otio = None
    if otio:
        path = root / 'timeline.otio'
        t = exchange.timeline(Args(plan=returned / 'edit-plan.json', out=path))
        parsed = otio.adapters.read_from_file(str(path))
        assert t['frames'] == 60 and parsed.tracks[0][0].source_range.start_time.value == 3
        assert parsed.tracks[0][1].source_range.start_time.value == 60
        checks.append('OTIO library reads 60-frame timeline with the correct replacement handle offset')
        rejects = copy.deepcopy(base)
        rejects['captions'] = [{'text': 'keep', 'start': 0, 'duration': 1}]
        editor.write_json(root / 'captions.json', rejects)
        refuses('OTIO never silently drops captions', lambda: exchange.timeline(Args(plan=root / 'captions.json', out=root / 'rejected.otio')),
                root / 'rejected.otio')
    summary = {'status': 'passed', 'checks': checks, 'count': len(checks),
               'native_blender': rendered if rendered else {'status': 'not_run'},
               'otio_library': 'passed' if otio else 'not_installed',
               'ui_save_reopen': 'unverified', 'final_export': 'user_owned'}
    editor.write_json(root / 'checks.json', summary)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--blender', type=Path, default=os.environ.get('JIANYING_BLENDER'))
    args = p.parse_args()
    editor.emit(check(args.out.resolve(), args.blender))


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    main()
