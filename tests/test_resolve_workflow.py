"""Resolve safety contracts; no Resolve host is started by this suite."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import resolve_native as native
import resolve_workflow as workflow


class ResolveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='resolve-workflow-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'synthetic identity, not playable media')
        self.plan_path = self.root / 'plan.json'
        self.plan_path.write_text(json.dumps({'schema_version': 1, 'backend': 'resolve', 'name': 'Test',
            'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': str(self.source), 'start': 0, 'source_in': 0, 'duration': 1}]}), encoding='utf-8')
        facts = {'kind': 'video', 'duration': 2, 'width': 320, 'height': 180, 'fps': 30,
                 'has_audio': False}
        self.enterContext(patch.object(workflow.editor, 'probe', return_value=facts))
        self.enterContext(patch.object(workflow, 'verify_cfr'))
        self.enterContext(patch.object(workflow, 'config_path', return_value=self.root / 'config/resolve.json'))
        self.bundle = self.root / 'bundle'
        self.prepare()

    def prepare(self):
        return workflow.prepare(SimpleNamespace(plan=self.plan_path, out=self.bundle))

    def test_prepare_is_offline_and_generated_plan_cannot_be_jointly_rehashed(self):
        root, plan, receipt = workflow.load_bundle(self.bundle)
        self.assertEqual(plan['duration_frames'], 30)
        self.assertEqual(receipt['status'], 'prepared_not_executed')
        self.assertFalse((root / 'project.drp').exists())
        normalized = root / 'normalized-plan.json'
        value = json.loads(normalized.read_text(encoding='utf-8'))
        value['clips'][0]['duration_frames'] = 29
        normalized.write_text(workflow.encoded(value), encoding='utf-8')
        receipt['normalized_sha256'] = workflow.sha(normalized)
        (root / 'handoff.json').write_text(workflow.encoded(receipt), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'altered'):
            workflow.load_bundle(root)

    def test_drift_in_plan_media_code_and_existing_bundle_rejected(self):
        with self.assertRaises(ValueError):
            self.prepare()
        for path in (self.plan_path, self.source):
            saved = path.read_bytes()
            path.write_bytes(saved + b' ')
            with self.assertRaises(ValueError):
                workflow.load_bundle(self.bundle)
            path.write_bytes(saved)
        identity = workflow.code_identity()
        for key in ('native', 'compiler', 'workflow'):
            changed = dict(identity, **{key: '0' * 64})
            with patch.object(workflow, 'code_identity', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    workflow.load_bundle(self.bundle)

    def test_new_bundle_has_distinct_project_name_even_with_same_plan(self):
        first = workflow.load_bundle(self.bundle)[2]
        second = workflow.prepare(SimpleNamespace(plan=self.plan_path, out=self.root / 'new-version'))
        self.assertNotEqual(first['project_name'], second['project_name'])

    def test_doctor_does_not_connect_or_launch_host(self):
        with patch.object(workflow.subprocess, 'run', side_effect=AssertionError('offline doctor')):
            result = workflow.doctor(SimpleNamespace(resolve_root=self.root / 'missing'))
        self.assertEqual(result['status'], 'needs_configuration')
        self.assertFalse(result['host_execution'])

    @unittest.skipUnless(os.name == 'nt', 'Windows host lock')
    def test_failure_receipt_is_retained_and_retry_refused(self):
        files = {}
        for name in ('executable', 'python', 'script_library'):
            path = self.root / name
            path.write_bytes(b'executable identity fixture')
            files[name] = str(path)
        apps = {'available': True, 'files': files, 'resolve_root': str(self.root)}
        with patch.object(workflow, 'applications', return_value=apps), patch.object(
                workflow.subprocess, 'run', side_effect=subprocess.TimeoutExpired('helper', 1)):
            args = SimpleNamespace(bundle=self.bundle, resolve_root=None, timeout=1)
            with self.assertRaises(subprocess.TimeoutExpired):
                workflow.run(args)
            result = json.loads((self.bundle / 'execution.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['host_terminated'])
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                workflow.run(args)

    @unittest.skipUnless(os.name == 'nt', 'Windows host lock')
    def test_existing_output_prevents_host_call(self):
        (self.bundle / 'output.mp4').write_bytes(b'never overwrite')
        with patch.object(workflow.subprocess, 'run', side_effect=AssertionError('no host call')):
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                workflow.run(SimpleNamespace(bundle=self.bundle, resolve_root=None, timeout=1))

    def test_native_refuses_any_saved_or_nonempty_user_project(self):
        manager = Mock()
        manager.GetProjectListInCurrentFolder.return_value = ['Saved']
        project = Mock()
        project.GetName.return_value = 'Saved'
        manager.GetCurrentProject.return_value = project
        resolve = Mock()
        resolve.GetProjectManager.return_value = manager
        resolve.GetVersion.return_value = [21, 1, 1, 10, '']
        _, plan, _ = workflow.load_bundle(self.bundle)
        with self.assertRaisesRegex(ValueError, 'Current user project'):
            native.execute(resolve, self.bundle, plan, 'new', 1)
        manager.CreateProject.assert_not_called()
        project.GetName.return_value = 'Untitled Project'
        manager.GetProjectListInCurrentFolder.return_value = []
        project.GetTimelineCount.return_value = 0
        folder = project.GetMediaPool.return_value.GetRootFolder.return_value
        folder.GetClipList.return_value = []
        child = Mock()
        child.GetClipList.return_value = [object()]
        folder.GetSubFolderList.return_value = [child]
        self.assertFalse(native.default_empty_project(project, manager))
        folder.GetSubFolderList.return_value = []
        self.assertTrue(native.default_empty_project(project, manager))
        manager.GetProjectListInCurrentFolder.return_value = ['Untitled Project']
        self.assertFalse(native.default_empty_project(project, manager))

    def test_native_property_mapping_uses_canvas_coordinates_and_decibels(self):
        _, plan, _ = workflow.load_bundle(self.bundle)
        clip = copy.deepcopy(plan['clips'][0])
        clip['transform'].update(scale=.5, x=200, y=30, rotation=90, opacity=.5)
        values = native.video_properties(clip, plan)
        self.assertEqual({k: values[k] for k in ('ZoomX', 'ZoomY', 'Pan', 'Tilt', 'RotationAngle', 'Opacity')},
                         {'ZoomX': .5, 'ZoomY': .5, 'Pan': 40, 'Tilt': 60, 'RotationAngle': -90, 'Opacity': 50})
        self.assertAlmostEqual(native.audio_properties({'volume': .5})['AudioVolume'], -6.020599913279624)
        with self.assertRaisesRegex(ValueError, 'Native property differs'):
            native.property_readback({'Pan': 1}, {'Pan': 0})

    def test_native_source_full_data_level_is_set_and_exactly_read_back(self):
        media = Mock()
        media.SetClipProperty.return_value = True
        media.GetClipProperty.return_value = 'Full'
        native.set_data_level(media, 'full')
        media.SetClipProperty.assert_called_once_with('Data Level', 'Full')
        media.GetClipProperty.return_value = 'Auto'
        with self.assertRaisesRegex(ValueError, 'data level readback differs'):
            native.set_data_level(media, 'full')

    def test_unadmitted_version_fails_before_creating_a_project(self):
        _, plan, _ = workflow.load_bundle(self.bundle)
        resolve = Mock()
        resolve.GetVersion.return_value = [21, 2, 0]
        with self.assertRaisesRegex(ValueError, 'version is not admitted'):
            native.execute(resolve, self.bundle, plan, 'new', 1)
        resolve.GetProjectManager.assert_not_called()

    def test_native_ownership_rejects_same_name_different_project_or_timeline(self):
        manager = Mock()
        project = manager.GetCurrentProject.return_value
        project.GetName.return_value = 'Owned'
        project.GetUniqueId.return_value = 'other-project'
        with self.assertRaisesRegex(ValueError, 'project identity'):
            native.ensure_owned(manager, 'Owned', 'owned-project', 'owned-timeline')
        project.GetUniqueId.return_value = 'owned-project'
        timeline = project.GetCurrentTimeline.return_value
        timeline.GetName.return_value = 'Main'
        project.GetTimelineCount.return_value = 1
        timeline.GetUniqueId.return_value = 'other-timeline'
        with self.assertRaisesRegex(ValueError, 'timeline identity'):
            native.ensure_owned(manager, 'Owned', 'owned-project', 'owned-timeline')
        timeline.GetUniqueId.return_value = 'owned-timeline'
        self.assertIs(native.ensure_owned(manager, 'Owned', 'owned-project', 'owned-timeline'), project)

    def test_owned_finish_preserves_changed_project_without_saving_or_closing(self):
        manager = Mock()
        with patch.object(native, 'ensure_owned', return_value=Mock()), patch.object(
                native, 'snapshot', return_value={'changed': True}):
            with self.assertRaisesRegex(ValueError, 'changed during rendering'):
                native.finish_owned_project(manager, 'Owned', 'project-id', 'timeline-id', {'original': True})
        manager.SaveProject.assert_not_called()
        manager.CloseProject.assert_not_called()

    def test_owned_finish_saves_then_normally_closes_only_unchanged_owned_project(self):
        manager, project = Mock(), Mock()
        manager.SaveProject.return_value = True
        manager.CloseProject.return_value = True
        manager.GetCurrentProject.return_value = None
        expected = {'original': True}
        with patch.object(native, 'ensure_owned', return_value=project) as guard, patch.object(
                native, 'snapshot', return_value=expected) as readback:
            self.assertTrue(native.finish_owned_project(manager, 'Owned', 'project-id', 'timeline-id', expected))
        self.assertEqual(readback.call_count, 2)
        self.assertEqual(guard.call_count, 4)
        manager.SaveProject.assert_called_once_with()
        manager.CloseProject.assert_called_once_with(project)

    def test_owned_finish_rejects_identity_switch_before_save(self):
        manager = Mock()
        with patch.object(native, 'ensure_owned', side_effect=ValueError('User switched project identity')):
            with self.assertRaisesRegex(ValueError, 'identity'):
                native.finish_owned_project(manager, 'Owned', 'project-id', 'timeline-id', {})
        manager.SaveProject.assert_not_called()
        manager.CloseProject.assert_not_called()

    def test_cfr_gate_rejects_vfr_missing_timestamps_and_insufficient_frames(self):
        good = [index / 30 for index in range(60)]
        workflow.validate_cfr_timestamps(good, 60)
        vfr = list(good)
        vfr[30] += .002
        for timestamps, required in ((vfr, 60), (good, 61), ([float('nan')], 1), ([], 1)):
            with self.subTest(required=required):
                with self.assertRaises(ValueError):
                    workflow.validate_cfr_timestamps(timestamps, required)

    def test_native_success_cannot_be_inferred_from_media_alone(self):
        for filename in ('output.mp4', 'project.drp'):
            (self.bundle / filename).write_bytes(b'not a native success')
        (self.bundle / 'native-result.json').write_text('{"status":"failed"}')
        _, plan, _ = workflow.load_bundle(self.bundle)
        with patch.object(workflow.editor, 'run', side_effect=AssertionError('no decoder until native proof')):
            with self.assertRaisesRegex(ValueError, 'native render receipt'):
                workflow.verify_media(self.bundle, plan)

    def test_generated_caption_file_is_recomputed_before_execution(self):
        raw = json.loads(self.plan_path.read_text(encoding='utf-8'))
        raw['captions'] = [{'text': '字幕测试\nSECOND', 'start': 1 / 30, 'duration': 20 / 30}]
        self.plan_path.write_text(json.dumps(raw), encoding='utf-8')
        bundle = self.root / 'caption-bundle'
        workflow.prepare(SimpleNamespace(plan=self.plan_path, out=bundle))
        _, normalized, _ = workflow.load_bundle(bundle)
        self.assertEqual((bundle / 'captions.srt').read_text(encoding='utf-8'),
                         workflow.resolve_compile.compile_srt(normalized))
        (bundle / 'captions.srt').write_text('altered SRT', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'subtitles altered'):
            workflow.load_bundle(bundle)

    def test_native_subtitle_readback_rejects_changed_text_time_or_disabled_track(self):
        item = Mock()
        item.GetName.return_value = '第一行\u2028第二行'
        self.assertEqual(native.subtitle_text(item), '第一行\n第二行')
        expected = [{'text': '中文字幕', 'start_frames': 1, 'duration_frames': 22}]
        track = {'enabled': True, 'items': [{'text': '中文字幕', 'start': 108001,
                                           'end': 108023, 'duration': 22}]}
        native.validate_caption_readback([track], expected, 108000)
        for changed in (dict(track, enabled=False),
                        dict(track, items=[dict(track['items'][0], text='wrong')]),
                        dict(track, items=[dict(track['items'][0], start=108002)])):
            with self.assertRaises(ValueError):
                native.validate_caption_readback([changed], expected, 108000)
        with self.assertRaises(ValueError):
            native.validate_caption_readback([track], [], 108000)

    def test_native_helper_strictly_revalidates_hash_matching_normalized_document(self):
        normalized = self.bundle / 'normalized-plan.json'
        value = json.loads(normalized.read_text(encoding='utf-8'))
        value['clips'][0]['duration_frames'] = 29
        normalized.write_text(json.dumps(value), encoding='utf-8')
        with patch('builtins.print'), patch.dict(sys.modules, {'DaVinciResolveScript': Mock()}), patch.object(
                native, 'execute', side_effect=AssertionError('Invalid normalized document reached host')) as execute:
            with self.assertRaises(SystemExit) as stopped:
                native.main(['run', '--bundle', str(self.bundle), '--normalized-sha256', workflow.sha(normalized),
                             '--project-name', 'Test'])
            self.assertEqual(stopped.exception.code, 2)
            execute.assert_not_called()
        receipt = json.loads((self.bundle / 'native-result.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('normalized plan', receipt['error'])


@unittest.skipUnless(os.name == 'nt', 'Windows host lock')
class ResolveLockTests(unittest.TestCase):
    def test_process_contention_and_release_on_exception(self):
        with tempfile.TemporaryDirectory(prefix='resolve-lock-') as temporary:
            config = Path(temporary) / 'resolve.json'
            child = ('import sys; from pathlib import Path; '
                     f'sys.path.insert(0,{str(SCRIPTS)!r}); import resolve_workflow as w; '
                     f'w.config_path=lambda:Path({str(config)!r})\n'
                     'try:\n with w.resolve_host_lock(): print("acquired")\n'
                     'except ValueError as e:\n print(e); sys.exit(3)\n')
            def contender():
                return subprocess.run([sys.executable, '-c', child], capture_output=True, text=True, timeout=20)
            with patch.object(workflow, 'config_path', return_value=config):
                with self.assertRaisesRegex(RuntimeError, 'fixture'):
                    with workflow.resolve_host_lock():
                        self.assertEqual(contender().returncode, 3)
                        raise RuntimeError('fixture')
                self.assertEqual(contender().returncode, 0)


if __name__ == '__main__':
    unittest.main()
