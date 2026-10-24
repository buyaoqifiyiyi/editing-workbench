"""AE return contract with real plan/bundle validators and mocked media probes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import adobe_workflow
import resolve_ae
import resolve_compile


class ResolveAEReturnTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='resolve-ae-return-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.mov'; self.source.write_bytes(b'source fixture')
        self.music = self.root/'music.wav'; self.music.write_bytes(b'music fixture')
        self.canvas = {'width': 320, 'height': 180, 'fps': 30}
        self.plan = {'schema_version': 1, 'backend': 'resolve', 'name': 'non-first return',
                     'canvas': self.canvas,
                     'clips': [{'path': str(self.source), 'source_in': 0, 'duration': 1, 'start': 0, 'volume': .25},
                               {'path': str(self.source), 'source_in': 1, 'duration': 2, 'start': 1, 'volume': .5,
                                'transform': {'scale': 1.2, 'x': 150}}],
                     'audio': [{'path': str(self.music), 'source_in': 0, 'duration': 3, 'start': 0, 'volume': .25}]}
        self.original = self.root/'resolve.json'
        self.original.write_text(json.dumps(self.plan), encoding='utf-8')
        self.ae_plan = {'schema_version': 1, 'backend': 'pr-ae', 'name': 'one AE shot',
                        'canvas': self.canvas, 'clips': [dict(self.plan['clips'][1], start=0)],
                        'captions': [{'text': 'AE native text', 'start': 0, 'duration': 2, 'font_size_px': 24}]}
        self.ae_original = self.root/'ae.json'
        self.ae_original.write_text(json.dumps(self.ae_plan), encoding='utf-8')
        self.bundle = self.root/'bundle'
        self.args = SimpleNamespace(plan=self.original, bundle=self.bundle, clip_index=2,
                                    shot_index=1, out=self.root/'returned.json')
        self.enterContext(patch.object(resolve_ae.editor, 'probe', side_effect=self.probe))
        self.enterContext(patch.object(adobe_workflow.subprocess, 'Popen', side_effect=AssertionError('No host launch')))
        self.enterContext(patch.object(adobe_workflow.subprocess, 'run', side_effect=AssertionError('No real process')))
        self.verify = self.enterContext(patch.object(adobe_workflow, 'verify_media', side_effect=self.verified))
        self.prepare_bundle()

    def probe(self, path):
        audio = Path(path).suffix == '.wav'
        return {'kind': 'audio' if audio else 'video', 'has_audio': True, 'duration': 10,
                'fps': 30, 'width': 320, 'height': 180, 'sample_rate': 48000, 'channels': 2}

    def verified(self, root, plan):
        return {'outputs': [{'path': str(root/'renders/shot-001.avi'),
                             'sha256': adobe_workflow.sha(root/'renders/shot-001.avi')}],
                'technical_media_verification': 'passed'}

    def prepare_bundle(self):
        adobe_workflow.prepare(SimpleNamespace(plan=self.ae_original, out=self.bundle))
        (self.bundle/'ae-reopened.aep').write_bytes(b'editable saved/reopened fixture AEP')
        (self.bundle/'renders/shot-001.avi').write_bytes(b'AVI fixture')
        adobe_workflow.record(self.bundle, 'execution.json', dict(self.verified(self.bundle, {}),
                                                               status='ae_rendered_pr_import_pending'))

    def test_second_clip_returns_local_shot_without_double_gain_or_transform(self):
        original_bytes = self.original.read_bytes()
        result = resolve_ae.return_shot(self.args)
        output = json.loads(self.args.out.read_text(encoding='utf-8'))
        clip = output['clips'][1]
        self.assertEqual(output['clips'][0], self.plan['clips'][0])
        self.assertEqual(output['audio'], self.plan['audio'])
        self.assertEqual((clip['start'], clip['duration'], clip['source_in'], clip['volume']), (1, 2, 0, 1))
        self.assertEqual(clip['data_level'], 'full')
        self.assertEqual(clip['transform'], {'scale': 1, 'x': 160, 'y': 90, 'rotation': 0, 'opacity': 1})
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertEqual(result['output_plan_sha256'], adobe_workflow.sha(self.args.out))
        self.assertEqual(result['ae_project_sha256'], adobe_workflow.sha(self.bundle/'ae-reopened.aep'))
        self.assertEqual(result['status'], 'ae_returned_resolve_execution_pending')
        self.assertIn('AEP', result['effect_editability'])
        resolve_compile.validate_plan(self.args.out, self.probe)
        self.verify.assert_called_once()

    def test_main_captions_are_preserved_and_validated_by_resolve_compiler(self):
        self.plan['captions'] = [{'text': 'main caption', 'start': 1, 'duration': 1}]
        self.original.write_text(json.dumps(self.plan), encoding='utf-8')
        resolve_ae.return_shot(self.args)
        self.assertEqual(json.loads(self.args.out.read_text(encoding='utf-8'))['captions'], self.plan['captions'])
        normalized = resolve_compile.validate_plan(self.args.out, self.probe)
        self.assertEqual(normalized['captions'], [dict(self.plan['captions'][0], start_frames=30, duration_frames=30)])
        self.assertEqual(resolve_compile.validate_normalized(normalized), normalized)

    def test_relative_paths_keep_their_media_when_saved_elsewhere(self):
        self.plan['clips'][0]['path'] = self.source.name
        self.plan['clips'][1]['path'] = self.source.name
        self.plan['audio'][0]['path'] = self.music.name
        self.original.write_text(json.dumps(self.plan), encoding='utf-8')
        folder = self.root/'other'; folder.mkdir(); self.args.out = folder/'returned.json'
        resolve_ae.return_shot(self.args)
        output = json.loads(self.args.out.read_text(encoding='utf-8'))
        self.assertEqual(output['clips'][0]['path'], str(self.source))
        self.assertEqual(output['audio'][0]['path'], str(self.music))

    def test_incomplete_execution_missing_project_and_modified_avi_are_rejected(self):
        execution = self.bundle/'execution.json'
        saved = execution.read_bytes()
        for status in ('creating_native_projects', 'failed'):
            execution.write_text(json.dumps({'status': status}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'not completed'):
                resolve_ae.return_shot(self.args)
        execution.write_bytes(saved)
        project = self.bundle/'ae-reopened.aep'; project.unlink()
        with self.assertRaisesRegex(ValueError, 'editable AE project'):
            resolve_ae.return_shot(self.args)
        project.write_bytes(b'project')
        (self.bundle/'renders/shot-001.avi').write_bytes(b'changed render')
        with self.assertRaisesRegex(ValueError, 'differs from the successful'):
            resolve_ae.return_shot(self.args)
        self.assertFalse(self.args.out.exists())

    def test_tampered_source_or_generated_script_fails_bundle_identity(self):
        for path in (self.source, self.bundle/'create-ae.jsx'):
            saved = path.read_bytes(); path.write_bytes(saved + b' changed')
            with self.assertRaisesRegex(ValueError, 'changed|altered'):
                resolve_ae.return_shot(self.args)
            path.write_bytes(saved)
        self.assertFalse(self.args.out.exists())

    def test_source_selection_canvas_gain_and_indices_must_match(self):
        other = self.root/'other.mov'; other.write_bytes(b'other source')
        for key, value in (('path', str(other)), ('source_in', 0), ('duration', 1), ('volume', .75)):
            changed = copy.deepcopy(self.plan); changed['clips'][1][key] = value
            if key == 'duration':
                changed['audio'][0]['duration'] = 2
            self.original.write_text(json.dumps(changed), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'source selection/duration/gain'):
                resolve_ae.return_shot(self.args)
        changed = copy.deepcopy(self.plan); changed['canvas']['width'] = 640
        self.original.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'canvas'):
            resolve_ae.return_shot(self.args)
        self.original.write_text(json.dumps(self.plan), encoding='utf-8')
        for field, value in (('clip_index', 0), ('clip_index', 3), ('shot_index', 2), ('shot_index', True)):
            args = copy.copy(self.args); setattr(args, field, value)
            with self.assertRaisesRegex(ValueError, '1-based index'):
                resolve_ae.return_shot(args)
        self.assertFalse(self.args.out.exists())

    def test_ae_independent_music_is_explicitly_rejected(self):
        self.ae_plan['audio'] = [dict(self.plan['audio'][0], duration=2)]
        self.ae_original.write_text(json.dumps(self.ae_plan), encoding='utf-8')
        other = self.root/'bgm-bundle'
        adobe_workflow.prepare(SimpleNamespace(plan=self.ae_original, out=other))
        self.args.bundle = other
        with self.assertRaisesRegex(ValueError, 'mix it on the main Resolve timeline'):
            resolve_ae.return_shot(self.args)

    def test_existing_plan_or_receipt_is_never_overwritten(self):
        for path in (self.args.out, self.args.out.with_suffix('.json.return.json')):
            path.write_bytes(b'existing')
            with self.assertRaisesRegex(ValueError, 'new output'):
                resolve_ae.return_shot(self.args)
            self.assertEqual(path.read_bytes(), b'existing')
            path.unlink()
        self.args.out = self.original
        with self.assertRaisesRegex(ValueError, 'new output'):
            resolve_ae.return_shot(self.args)


if __name__ == '__main__':
    unittest.main()
