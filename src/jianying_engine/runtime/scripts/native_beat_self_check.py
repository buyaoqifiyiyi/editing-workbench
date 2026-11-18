"""Offline synthetic fixtures only: no Jianying launch, user data or admissions.

Uses an explicitly mocked installation and a fixture contract to exercise hash
guards without claiming real host compatibility. The shipped contract is NOT
overwritten. --out must be a new isolated directory.
"""
import argparse
import copy
from contextlib import ExitStack
import hashlib
from pathlib import Path
import shutil
from types import SimpleNamespace as Args
from unittest.mock import patch

import editor
import migrate
import native_beat


def refused(action):
    try:
        action()
    except (ValueError, TypeError, KeyError):
        return
    raise AssertionError('Expected strict refusal')


def check(out):
    root = Path(out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    install = root / '11.7.0.14652'
    install.mkdir()
    runtime = install / 'fixture.bin'
    runtime.write_bytes(b'NOT A JIANYING RUNTIME: synthetic identity fixture')
    spec = root / 'fixture-contract.json'
    editor.write_json(spec, dict(runtime_version=install.name,
        runtime_sha256={'fixture.bin': hashlib.sha256(runtime.read_bytes()).hexdigest()}))
    movie, sound = root / 'synthetic.mp4', root / 'synthetic.wav'
    for target, source, extra in (
            (movie, 'color=c=blue:s=320x180:r=30:d=4', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
            (sound, 'sine=frequency=330:duration=4', ['-c:a', 'pcm_s16le'])):
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', source, *extra, target])
    base = dict(schema_version=1, name='native-beat-fixture', mode='script',
        canvas=dict(width=320, height=180, fps=30),
        clips=[dict(path=str(movie), start=0, duration=4, volume=0)],
        audio=[dict(path=str(sound), start=0, source_in=0, duration=4, track='music',
                    beat_marks=[.5, 1.25, 2.5])])
    large_movie, large_sound = root / 'large.mp4', root / 'large.wav'
    for target, source, extra in (
            (large_movie, 'color=c=blue:s=640x360:r=30:d=10', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
            (large_sound, 'sine=frequency=330:duration=12', ['-c:a', 'pcm_s16le'])):
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', source, *extra, target])
    large = copy.deepcopy(base)
    large['canvas'].update(width=640, height=360)
    large['clips'][0].update(path=str(large_movie), duration=10)
    large['audio'][0].update(path=str(large_sound), source_in=2, start=1, duration=8)
    moved = root / 'moved-media'
    moved.mkdir()
    for path in (movie, sound, large_movie, large_sound):
        shutil.copy2(path, moved / path.name)
    plan_path = root / 'plan.json'
    checks = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(native_beat, 'CONTRACT', spec))
        stack.enter_context(patch.object(editor, 'jianying_install', return_value=install))
        stack.enter_context(patch.object(editor, 'configured_user_data', return_value=None))
        stack.enter_context(patch.object(editor, 'default_text_font', return_value=None))
        stack.enter_context(patch.object(editor, 'draft_location', return_value={
            'active_draft_root': None, 'homepage_index': str(root / 'never-created-index.json')}))
        cases = [('zero', base, (0, 0, 4), [.5, 1.25, 2.5]),
                 ('offset', base, (.5, 1, 2.5), [.5, 1.25, 2.5]),
                 ('large-16', large, (2, 1, 8), [2 + i * .5 for i in range(16)]),
                 ('large-1', large, (2, 1, 8), [4.75]),
                 ('large-2', large, (2, 1, 8), [2.3, 9.2])]
        for label, template, values, expected_marks in cases:
            plan = copy.deepcopy(template)
            plan['audio'][0]['beat_marks'] = expected_marks
            plan['name'] += '-' + label
            plan['audio'][0].update(zip(('source_in', 'start', 'duration'), values))
            editor.write_json(plan_path, plan)
            normalized, _, _ = editor.validate_plan(plan_path)
            built = Path(editor.build_draft(Args(plan=plan_path, out=root / ('draft-' + label)))['draft'])
            content = editor.read_json(built / 'draft_content.json')
            editor.verify_structure(content, normalized)
            marks = content['materials']['beats'][0]['user_beats']
            assert marks == [native_beat.micros(v) for v in expected_marks]
            archive = root / ('archive-' + label)
            archive.mkdir()
            published = Path(editor.publish_draft(Args(draft=built, draft_root=archive, offline_copy=True))['draft'])
            assert editor.read_json(published / 'draft_content.json')['materials']['beats'] == content['materials']['beats']
            checks.append(label + ': validate/build/verify/offline publish preserve source clock and IDs')
            migrated, changes, missing = migrate.remap_plan(built / 'edit-plan.json',
                migrate.mappings([str(root) + '=' + str(moved)]))
            assert not missing and migrated['audio'][0]['beat_marks'] == plan['audio'][0]['beat_marks']
            assert len(changes) == 2
            migration_path = root / ('migrated-' + label + '.json')
            editor.write_json(migration_path, migrated)
            migrated_normalized, _, _ = editor.validate_plan(migration_path)
            migrated_build = Path(editor.build_draft(Args(plan=migration_path, out=root / ('rebuilt-' + label)))['draft'])
            editor.verify_structure(editor.read_json(migrated_build / 'draft_content.json'), migrated_normalized)
            checks.append(label + ': existing migration and rebuild preserve beat values')
        # Build legacy plans, with no runtime requirement and no beats injection.
        legacy = copy.deepcopy(base)
        del legacy['audio'][0]['beat_marks']
        editor.write_json(plan_path, legacy)
        with patch.object(editor, 'jianying_install', return_value=None):
            old = Path(editor.build_draft(Args(plan=plan_path, out=root / 'legacy'))['draft'])
            assert not editor.read_json(old / 'draft_content.json')['materials'].get('beats')
        checks.append('legacy plan works without the beat runtime')
        for marks in (None, [], (), [True], ['1'], [float('inf')], [float('nan')],
                      [-1], [0.0000001], [1, 1.00000001], [1, 1], [2, 1], [4],
                      [0, 1, 2, 3], {}, [False]):
            bad = copy.deepcopy(base); bad['audio'][0]['beat_marks'] = marks
            refused(lambda: native_beat.validate(bad))
        for key, value in (('source_in', .0000001), ('start', .0000001), ('duration', 3.9999999),
                           ('track', 'voice'), ('speed', 2), ('speed', True), ('volume', .9),
                           ('fade_in', .1), ('fade_out', .1), ('keyframes', [{'time': 0, 'value': 1}]),
                           ('denoise', {'mode': 'x'}), ('loudness', {})):
            bad = copy.deepcopy(base); bad['audio'][0][key] = value
            editor.write_json(plan_path, bad)
            refused(lambda: editor.validate_plan(plan_path))
        offset = copy.deepcopy(base)
        offset['audio'][0].update(source_in=.5, start=1, duration=2.5)
        for marks in ([.499999], [3], [3.000001]):
            offset['audio'][0]['beat_marks'] = marks
            refused(lambda: native_beat.validate(offset))
        for marks in ([0], [.000001], [3.999999], [.5, 1.25]):
            good = copy.deepcopy(base); good['audio'][0]['beat_marks'] = marks
            native_beat.validate(good)
        checks.append('types/count/order/half-open boundaries/strict microsecond quantization')
        # Every mutator must be rejected by the shared structure path.
        editor.write_json(plan_path, base)
        normalized, probes, _ = editor.validate_plan(plan_path)
        initial = editor.read_json(root / 'draft-zero/draft_content.json')
        beat_id = initial['materials']['beats'][0]['id']
        # Preview has its own install argument and must not trust a draft without
        # its corresponding validated plan, even when the material is nested.
        preview_root = root / 'draft-zero'
        native_beat.preview_check(initial, preview_root, install)
        refused(lambda: native_beat.preview_check(initial, preview_root, root / '11.6.0.14552'))
        refused(lambda: native_beat.preview_check(initial, root / 'no-plan', install))
        changed = copy.deepcopy(initial)
        changed['materials']['beats'][0]['user_beats'][0] += 1
        refused(lambda: native_beat.preview_check(changed, preview_root, install))
        refused(lambda: native_beat.preview_check(initial, root / 'legacy', install))
        with patch.object(native_beat, 'runtime', side_effect=AssertionError('legacy preview runtime check')):
            native_beat.preview_check(editor.read_json(root / 'legacy/draft_content.json'), root / 'legacy', install)
        checks.append('preview validates its actual runtime, matching plan and beat structure; legacy remains available')
        def audio(c):
            return next(t for t in c['tracks'] if t['name'] == 'music')['segments'][0]
        def video(c):
            return next(t for t in c['tracks'] if t['name'] == 'video')['segments'][0]
        mutations = [
            lambda c: c['materials']['beats'][0].update(user_beats=[500001,1250000,2500000]),
            lambda c: c['materials']['beats'][0].update(gear=True),
            lambda c: c['materials']['beats'][0].update(enable_ai_beats=0),
            lambda c: c['materials']['beats'][0].update(extra=1),
            lambda c: c['materials']['beats'].append(copy.deepcopy(c['materials']['beats'][0])),
            lambda c: c['materials']['effects'].append(copy.deepcopy(c['materials']['beats'][0])),
            lambda c: c['materials']['audios'][0].update(id=beat_id),
            lambda c: audio(c)['extra_material_refs'].remove(beat_id),
            lambda c: audio(c)['extra_material_refs'].append(beat_id),
            lambda c: video(c)['extra_material_refs'].append(beat_id),
            lambda c: audio(c).update(volume=.5),
            lambda c: audio(c)['source_timerange'].update(start=1),
            lambda c: audio(c)['target_timerange'].update(start=1),
            lambda c: c.update(fps=24),
            lambda c: audio(c).update(common_keyframes=[{'property_type':'x'}]),
            lambda c: audio(c).update(reverse=True),
            lambda c: audio(c).update(is_tone_modify=True),
            lambda c: c['materials']['audios'][0].update(duration=5000000),
            lambda c: c['materials']['effects'].append({'id':'orphan','type':'audio_effect'}),
            lambda c: audio(c)['extra_material_refs'].append(video(c)['extra_material_refs'][0]),
            lambda c: c['materials']['drafts'].append({'content': {'materials': {'beats': [dict(native_beat.MATERIAL, id='nested')]}}}),
            lambda c: audio(c).update(id=beat_id),
            lambda c: audio(c)['source_timerange'].update(start=0.0),
            lambda c: audio(c).update(speed=True),
            lambda c: c['materials']['audios'][0].update(effect_id='tampered-effect'),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(initial); mutate(changed)
            refused(lambda: editor.verify_structure(changed, normalized))
        refused(lambda: editor.verify_structure(initial, editor.validate_plan(root / 'legacy/edit-plan.json')[0]))
        for key, value in (('duration', 5), ('kind', 'video'), ('sample_rate', 48000), ('channels', 2), ('audio_codec', 'aac')):
            bad_probes = copy.deepcopy(probes)
            bad_probes[normalized['audio'][0]['path']][key] = value
            refused(lambda: native_beat.validate_media(normalized, bad_probes))
        checks.append(str(len(mutations)) + ' structure tamper cases, unplanned beats and media refusal')
        # Even after a manifest hash is recomputed, publish must reject semantic tampering.
        draft = root / 'draft-zero'
        changed = copy.deepcopy(initial); audio(changed).update(reverse=True)
        editor.write_json(draft / 'draft_content.json', changed)
        manifest = editor.read_json(draft / '_skill_manifest.json')
        manifest['draft_content_sha256'] = hashlib.sha256((draft / 'draft_content.json').read_bytes()).hexdigest()
        editor.write_json(draft / '_skill_manifest.json', manifest)
        refused(lambda: editor.publish_draft(Args(draft=draft, draft_root=root / 'archive-zero', offline_copy=True)))
        checks.append('offline publish rejects semantic tamper even with recomputed manifest hash')
        # The additional scene is an exact tuple, never a Cartesian expansion.
        for marks in ([2], [9.999999], [2, 9.999999]):
            good = copy.deepcopy(large); good['audio'][0]['beat_marks'] = marks
            native_beat.validate(good)
        for marks in ([], [1.999999], [10], [10.000001], [9.2, 2.3], [2.3, 2.3],
                      [2.0000001], [True], ['4.75'], [2 + i * .4 for i in range(17)]):
            bad = copy.deepcopy(large); bad['audio'][0]['beat_marks'] = marks
            refused(lambda: native_beat.validate(bad))
        for target, key, value in (('canvas', 'width', 320), ('canvas', 'fps', 60),
                ('audio', 'source_in', 0), ('audio', 'duration', 7), ('audio', 'start', 0),
                ('clips', 'duration', 4), ('audio', 'speed', 2), ('audio', 'volume', .9)):
            bad = copy.deepcopy(large)
            bad['audio'][0]['beat_marks'] = [4.75]
            (bad[target] if target == 'canvas' else bad[target][0])[key] = value
            refused(lambda: native_beat.validate(bad))
        checks.append('large scene: 1..16 count, [2,10) boundaries, exact microseconds and mixed-scene refusal')
        large_draft = root / 'draft-large-16'
        large_plan, large_probes, _ = editor.validate_plan(large_draft / 'edit-plan.json')
        large_content = editor.read_json(large_draft / 'draft_content.json')
        large_id = large_content['materials']['beats'][0]['id']
        large_mutations = [
            lambda c: c['materials']['beats'][0]['user_beats'].__setitem__(-1, 10000000),
            lambda c: c['materials']['beats'][0]['user_beats'].append(9999999),
            lambda c: c['materials']['beats'][0]['user_beats'].__setitem__(0, 2000000.0),
            lambda c: audio(c)['source_timerange'].update(start=0),
            lambda c: audio(c)['target_timerange'].update(start=0),
            lambda c: c['materials']['audios'][0].update(duration=4000000),
            lambda c: c['materials']['videos'][0].update(width=320),
            lambda c: audio(c)['extra_material_refs'].remove(large_id),
            lambda c: video(c)['extra_material_refs'].append(large_id),
            lambda c: c['materials']['beats'].append(copy.deepcopy(c['materials']['beats'][0])),
        ]
        for mutate in large_mutations:
            changed = copy.deepcopy(large_content); mutate(changed)
            refused(lambda: editor.verify_structure(changed, large_plan))
        for group, key, value in (('audio', 'duration', 4), ('clips', 'duration', 4),
                                  ('clips', 'width', 320), ('clips', 'has_audio', True),
                                  ('clips', 'fps', 24), ('audio', 'sample_rate', 48000), ('audio', 'channels', 2),
                                  ('audio', 'audio_codec', 'aac'), ('clips', 'codec', 'hevc')):
            bad = copy.deepcopy(large_probes)
            bad[large_plan[group][0]['path']][key] = value
            refused(lambda: native_beat.validate_media(large_plan, bad))
        changed = copy.deepcopy(large_content); audio(changed)['source_timerange'].update(start=0)
        editor.write_json(large_draft / 'draft_content.json', changed)
        manifest = editor.read_json(large_draft / '_skill_manifest.json')
        manifest['draft_content_sha256'] = hashlib.sha256((large_draft / 'draft_content.json').read_bytes()).hexdigest()
        editor.write_json(large_draft / '_skill_manifest.json', manifest)
        refused(lambda: editor.publish_draft(Args(draft=large_draft, draft_root=root / 'archive-large-16', offline_copy=True)))
        checks.append('large scene: 10 structure tamper cases, 9 media refusals and rehashed publish refusal')
        runtime.write_bytes(b'tampered')
        refused(lambda: editor.validate_plan(plan_path))
        with patch.object(editor, 'jianying_install', return_value=root / '11.6.0.14552'):
            refused(lambda: editor.validate_plan(plan_path))
        checks.append('changed runtime hash and unmeasured version refused')
    result = dict(status='passed', checks=checks, evidence='synthetic fixtures only',
                  native_ui='not_run', admission='not_written', production_integration='main_agent_required')
    editor.write_json(root / 'native-beat-check.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out))
