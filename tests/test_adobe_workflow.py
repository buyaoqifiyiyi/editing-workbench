"""Regression guards for Adobe handoff; all host/media processes are mocked."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import adobe_compile
import adobe_workflow as workflow
from jianying_engine.workbench import Workbench


@unittest.skipUnless(os.name == 'nt', 'AE host locking is Windows-only')
class AdobeHostLockTests(unittest.TestCase):
    def test_real_process_contention_and_exception_release(self):
        with tempfile.TemporaryDirectory(prefix='adobe-host-lock-') as temporary:
            config = Path(temporary) / 'adobe.json'
            child = (
                'import sys; from pathlib import Path; '
                f'sys.path.insert(0, {str(SCRIPTS)!r}); '
                'import adobe_workflow as w; '
                f'w.config_path=lambda: Path({str(config)!r})\n'
                'try:\n'
                ' with w.ae_host_lock(): print("acquired", flush=True)\n'
                'except ValueError as error:\n'
                ' print(str(error), flush=True); sys.exit(3)\n'
            )

            def contender():
                return subprocess.run([sys.executable, '-c', child], capture_output=True,
                                      text=True, timeout=20, check=False)

            with patch.object(workflow, 'config_path', return_value=config):
                with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                    with workflow.ae_host_lock():
                        blocked = contender()
                        self.assertEqual(blocked.returncode, 3, blocked.stderr)
                        self.assertIn('busy in another worker', blocked.stdout)
                        raise RuntimeError('fixture failure')
                available = contender()
                self.assertEqual(available.returncode, 0, available.stderr)
                self.assertIn('acquired', available.stdout)
                self.assertTrue((Path(temporary) / 'ae-host.lock').is_file())


class AdobeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adobe-workflow-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(workflow, 'config_path', return_value=self.root/'adobe.json'))
        (self.root / 'renders').mkdir()
        self.render = self.root / 'renders/shot-001.avi'
        self.render.write_bytes(b'mocked render; no host or decoder started')
        self.plan = {'canvas': {'width': 320, 'height': 180, 'fps': 30},
                     'clips': [{'path': str(self.root/'source.mov'), 'source_in': .5,
                                'duration': 2, 'volume': .5}]}
        self.streams = [
            {'codec_type': 'video', 'codec_name': 'rawvideo', 'nb_read_frames': '60',
             'width': 320, 'height': 180, 'avg_frame_rate': '30/1'},
            {'codec_type': 'audio', 'codec_name': 'pcm_s16le', 'sample_rate': '48000', 'channels': 2}]
        self.audio = np.full((96000, 2), .05, dtype='float32')
        self.run = self.enterContext(patch.object(workflow.editor, 'run', side_effect=self.fake_run))
        self.enterContext(patch.object(workflow.editor, 'probe', return_value={'has_audio': True}))
        self.enterContext(patch.object(workflow.editor, 'tool', side_effect=lambda name: name))
        self.decode = self.enterContext(patch.object(workflow, 'decoded_audio', side_effect=lambda *args: self.audio.copy()))
        # A test accidentally reaching a real subprocess must fail immediately.
        self.enterContext(patch.object(workflow.subprocess, 'run', side_effect=AssertionError('Host/subprocess execution forbidden in this suite')))
        self.popen = self.enterContext(patch.object(workflow.subprocess, 'Popen', side_effect=AssertionError('Host launch forbidden in this suite')))

    def fake_run(self, command):
        return SimpleNamespace(stdout=json.dumps({'streams': self.streams}) if command[0] == 'ffprobe' else '')

    def test_valid_media_checks_selected_source_and_gain_without_certifying_alignment(self):
        result = workflow.verify_media(self.root, self.plan)
        self.assertEqual(result['technical_media_verification'], 'passed')
        self.assertEqual(result['human_viewing_listening'], 'not_performed')
        checks = result['outputs'][0]['audio_check']
        self.assertEqual(checks['samples'], 96000)
        self.assertIn('not_certified', checks['content_alignment'])
        self.assertEqual(self.decode.call_args_list[1].args,
                         (self.plan['clips'][0]['path'], .5, 2, .5))

    def test_native_text_parameters_are_required_and_compared_after_reopen(self):
        expected = {'shots': [{'name': 'shot-001', 'text_layers': [
            {'text': '动态文字', 'keys': [{'time': .5, 'value': [100, 200]}]}]}]}
        plan = {'captions': [{'text': '动态文字'}]}
        path = self.root / 'ae-text-readback.json'
        with patch.object(adobe_compile, 'caption_layer_specs', return_value=expected):
            with self.assertRaisesRegex(ValueError, 'Missing regular'):
                workflow.verify_text_readback(self.root, plan)
            path.write_text(json.dumps({'before_save': expected, 'after_reopen': expected}), encoding='utf-8')
            result = workflow.verify_text_readback(self.root, plan)
            self.assertEqual(result['status'], 'native_parameters_save_reopen_verified')
            changed = copy.deepcopy(expected)
            changed['shots'][0]['text_layers'][0]['keys'][0]['value'][1] = 201
            path.write_text(json.dumps({'before_save': expected, 'after_reopen': changed}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'after_reopen.*native value differs'):
                workflow.verify_text_readback(self.root, plan)
            changed = copy.deepcopy(expected)
            changed['shots'][0]['text_layers'][0]['text'] = 'wrong text'
            path.write_text(json.dumps({'before_save': expected, 'after_reopen': changed}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'native content differs'):
                workflow.verify_text_readback(self.root, plan)

    def test_native_text_float_tolerance_rejects_nan_boolean_and_changed_keys(self):
        workflow._same_native_text({'time': [1 + 1e-7]}, {'time': [1]})
        for actual in ({'time': [float('nan')]}, {'time': [True]}, {'time': [1.01]}, {'other': [1]}):
            with self.subTest(actual=actual):
                with self.assertRaises(ValueError):
                    workflow._same_native_text(actual, {'time': [1]})

    def test_native_foreground_readback_requires_mask_layer_order_and_muted_audio(self):
        plan = {'clips': [{'foreground_mask': {'vertices': [[0, 0], [10, 0], [0, 10]]}}]}
        expected = {'shots': [{'name': 'shot-001', 'layer_order': ['foreground', 'text', 'source'],
                              'foregrounds': [{'audio_enabled': False,
                                               'mask': {'vertices': [[0, 0], [10, 0], [0, 10]],
                                                        'feather': [0, 0], 'inverted': False}}]}]}
        path = self.root / 'ae-layer-readback.json'
        with patch.object(adobe_compile, 'foreground_layer_specs', return_value=expected):
            with self.assertRaisesRegex(ValueError, 'Missing regular'):
                workflow.verify_foreground_readback(self.root, plan)
            path.write_text(json.dumps({'before_save': expected, 'after_reopen': expected}), encoding='utf-8')
            self.assertEqual(workflow.verify_foreground_readback(self.root, plan)['status'],
                             'native_layer_order_mask_and_mute_save_reopen_verified')
            for field, value in [('audio_enabled', True), ('mask', {'vertices': [], 'feather': [0, 0], 'inverted': False})]:
                changed = copy.deepcopy(expected)
                changed['shots'][0]['foregrounds'][0][field] = value
                path.write_text(json.dumps({'before_save': expected, 'after_reopen': changed}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    workflow.verify_foreground_readback(self.root, plan)
            changed = copy.deepcopy(expected)
            changed['shots'][0]['layer_order'] = ['text', 'foreground', 'source']
            path.write_text(json.dumps({'before_save': expected, 'after_reopen': changed}), encoding='utf-8')
            with self.assertRaises(ValueError):
                workflow.verify_foreground_readback(self.root, plan)

    def test_short_audio_render_or_reference_selection_is_rejected(self):
        for actual, reference in [(self.audio[:480], self.audio), (self.audio, self.audio[:480])]:
            with self.subTest(actual_samples=len(actual), reference_samples=len(reference)):
                self.decode.side_effect = [actual, reference]
                with self.assertRaisesRegex(ValueError, 'audio sample count'):
                    workflow.verify_media(self.root, self.plan)

    def test_lossy_video_or_audio_codec_cannot_pass(self):
        for stream_index, codec in [(0, 'h264'), (1, 'aac')]:
            with self.subTest(codec=codec):
                original = self.streams[stream_index]['codec_name']
                self.streams[stream_index]['codec_name'] = codec
                self.decode.reset_mock()
                with self.assertRaises(ValueError):
                    workflow.verify_media(self.root, self.plan)
                self.decode.assert_not_called()
                self.streams[stream_index]['codec_name'] = original

    def test_wrong_gain_is_rejected_per_channel(self):
        wrong = self.audio.copy(); wrong[:, 1] *= 2
        self.decode.side_effect = [wrong, self.audio]
        with self.assertRaisesRegex(ValueError, 'gain RMS'):
            workflow.verify_media(self.root, self.plan)

    def test_muted_policy_accepts_no_audio_or_silence_and_rejects_signal(self):
        self.plan['clips'][0]['volume'] = 0
        self.decode.side_effect = None
        self.decode.return_value = np.zeros((96000, 2), dtype='float32')
        checks = workflow.verify_media(self.root, self.plan)['outputs'][0]['audio_check']
        self.assertEqual(checks, {'policy': 'muted', 'verified': True})
        self.decode.return_value = self.audio
        with self.assertRaisesRegex(ValueError, 'Muted shot unexpectedly'):
            workflow.verify_media(self.root, self.plan)
        self.streams = self.streams[:1]
        self.decode.reset_mock()
        self.assertEqual(workflow.verify_media(self.root, self.plan)['technical_media_verification'], 'passed')
        self.decode.assert_not_called()

    def test_existing_ae_prevents_any_invocation_or_receipt_mutation(self):
        args = SimpleNamespace(bundle=self.root, ae_root=self.root, timeout=10)
        with patch.object(workflow, 'load_bundle', return_value=(self.root, self.plan)), \
             patch.object(workflow, 'applications', return_value={'ae_root': str(self.root)}), \
             patch.object(workflow, 'active_ae', return_value=['AfterFX.exe']), \
             patch.object(workflow, 'invoke') as invoke, patch.object(workflow, 'record') as record:
            with self.assertRaisesRegex(ValueError, 'already running'):
                workflow.run_ae(args)
            invoke.assert_not_called(); record.assert_not_called()
            self.popen.assert_not_called()

    def test_active_ae_detects_console_dispatcher_as_busy(self):
        def tasklist(command, **kwargs):
            is_console = command[2] == 'IMAGENAME eq AfterFX.com'
            return SimpleNamespace(stdout=b'"AfterFX.com","1234","Console","1","500 K"\r\n' if is_console else b'INFO: No tasks found\r\n')

        with patch.object(workflow.os, 'name', 'nt'), \
             patch.object(workflow.subprocess, 'run', side_effect=tasklist) as run:
            self.assertEqual(workflow.active_ae(), ['AfterFX.com'])
            filters = {c.args[0][2] for c in run.call_args_list}
            self.assertEqual(filters, {'IMAGENAME eq AfterFX.exe', 'IMAGENAME eq AfterFX.com', 'IMAGENAME eq aerender.exe'})
            self.popen.assert_not_called()

    def test_post_render_identity_failure_never_publishes_success(self):
        args = SimpleNamespace(bundle=self.root, ae_root=self.root, timeout=10)

        def invoke(command, root, label, timeout):
            if label == 'ae-dispatch':
                (root/'ae-project.aep').touch(); (root/'ae-reopened.aep').touch()

        with patch.object(workflow, 'load_bundle', side_effect=[(self.root, self.plan), (self.root, self.plan), ValueError('Source media changed')]) as load, \
             patch.object(workflow, 'applications', return_value={'ae_root': str(self.root)}), \
             patch.object(workflow, 'active_ae', side_effect=[[], [], []]), \
             patch.object(workflow.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: None)) as launch, \
             patch.object(workflow, '_wait_for_ae_ready') as ready, patch.object(workflow, 'invoke', side_effect=invoke) as call, \
             patch.object(workflow, 'record') as record, patch.object(workflow, 'verify_media') as verify:
            with self.assertRaisesRegex(ValueError, 'Source media changed'):
                workflow.run_ae(args)
            self.assertEqual(load.call_count, 3)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(Path(launch.call_args.args[0][0]).name, 'AfterFX.exe')
            self.assertFalse(launch.call_args.kwargs['shell'])
            ready.assert_called_once()
            verify.assert_not_called()
            self.assertEqual(record.call_args.args[2]['status'], 'failed')
            self.assertNotIn('ae_rendered_pr_import_pending', [c.args[2]['status'] for c in record.call_args_list])

    def test_one_ready_probe_waits_for_delayed_receipt_before_single_create_dispatch(self):
        args = SimpleNamespace(bundle=self.root, ae_root=self.root, timeout=10)
        labels = []
        token = SimpleNamespace(hex='fixture-ready-token')

        def invoke(command, root, label, timeout):
            labels.append(label)
            if label == 'ae-dispatch':
                (root/'ae-reopened.aep').write_bytes(b'fixture AEP')

        def delayed_receipt(_):
            self.assertEqual(labels, ['ae-ready-001'])
            (self.root/'ae-ready.txt').write_text(token.hex + ':ready', encoding='utf-8')

        with patch.object(workflow, 'load_bundle', return_value=(self.root, self.plan)), \
             patch.object(workflow, 'applications', return_value={'ae_root': str(self.root)}), \
             patch.object(workflow, 'active_ae', return_value=[]), \
             patch.object(workflow.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: None)), \
             patch.object(workflow.uuid, 'uuid4', return_value=token), \
             patch.object(workflow.time, 'sleep', side_effect=delayed_receipt), \
             patch.object(workflow, 'invoke', side_effect=invoke), \
             patch.object(workflow, 'record'), patch.object(workflow, 'verify_media', return_value={}):
            self.assertEqual(workflow.run_ae(args)['status'], 'ae_rendered_pr_import_pending')
        self.assertEqual(labels, ['ae-ready-001', 'ae-dispatch', 'aerender'])
        probe = (self.root/'ae-ready.jsx').read_text(encoding='utf-8')
        for forbidden in ('app.newProject', 'app.project.close', 'app.project.save', 'app.quit'):
            self.assertNotIn(forbidden, probe)
        self.assertIn('app.project.numItems===0', probe)

    def test_probe_dispatch_errors_wait_without_redispatch_or_creation(self):
        errors = [ValueError('Dispatcher is not ready yet'),
                  workflow.subprocess.TimeoutExpired('AfterFX.com', 10)]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                directory = self.root/f'dispatch-error-{index}'; directory.mkdir()
                warm = unittest.mock.Mock(); warm.poll.return_value = None
                with patch.object(workflow.time, 'monotonic', side_effect=[0, 0, 1, 30, 60, 90, 121]), \
                     patch.object(workflow.time, 'sleep'), \
                     patch.object(workflow, 'invoke', side_effect=error) as call:
                    with self.assertRaisesRegex(ValueError, 'after one probe; startup dialog.*creation was not dispatched.*no forced process termination'):
                        workflow._wait_for_ae_ready(self.root, directory, warm)
                self.assertEqual([c.args[2] for c in call.call_args_list], ['ae-ready-001'])
                warm.kill.assert_not_called(); warm.terminate.assert_not_called()

    def test_probe_occupied_project_or_timeout_never_creates_or_kills_host(self):
        for state in ('occupied', 'timeout'):
            with self.subTest(state=state):
                directory = self.root/state; directory.mkdir()
                warm = unittest.mock.Mock(); warm.poll.return_value = None

                def invoke(command, root, label, timeout):
                    if state == 'occupied':
                        (root/'ae-ready.txt').write_text('token:occupied', encoding='utf-8')

                with patch.object(workflow.uuid, 'uuid4', return_value=SimpleNamespace(hex='token')), \
                     patch.object(workflow.time, 'monotonic', side_effect=[0, 0, 1, 30, 60, 90, 121]), \
                     patch.object(workflow.time, 'sleep'), patch.object(workflow, 'invoke', side_effect=invoke) as call:
                    with self.assertRaisesRegex(ValueError, 'left untouched' if state == 'occupied' else 'timed out'):
                        workflow._wait_for_ae_ready(self.root, directory, warm)
                self.assertEqual([c.args[2] for c in call.call_args_list], ['ae-ready-001'])
                warm.kill.assert_not_called(); warm.terminate.assert_not_called()

    def test_identity_failure_after_ready_does_not_dispatch_creation(self):
        args = SimpleNamespace(bundle=self.root, ae_root=self.root, timeout=10)
        with patch.object(workflow, 'load_bundle', side_effect=[(self.root, self.plan), ValueError('Plan or compiler changed')]), \
             patch.object(workflow, 'applications', return_value={'ae_root': str(self.root)}), \
             patch.object(workflow, 'active_ae', return_value=[]), \
             patch.object(workflow.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: None)), \
             patch.object(workflow, '_wait_for_ae_ready'), patch.object(workflow, 'invoke') as invoke, \
             patch.object(workflow, 'record'):
            with self.assertRaisesRegex(ValueError, 'Plan or compiler changed'):
                workflow.run_ae(args)
            invoke.assert_not_called()

    def test_exited_warm_host_does_not_dispatch_console_script(self):
        args = SimpleNamespace(bundle=self.root, ae_root=self.root, timeout=10)
        with patch.object(workflow, 'load_bundle', return_value=(self.root, self.plan)), \
             patch.object(workflow, 'applications', return_value={'ae_root': str(self.root)}), \
             patch.object(workflow, 'active_ae', return_value=[]), \
             patch.object(workflow.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: 1)), \
             patch.object(workflow.time, 'sleep'), patch.object(workflow, 'invoke') as invoke, \
             patch.object(workflow, 'record') as record:
            with self.assertRaisesRegex(ValueError, 'Owned AE startup exited'):
                workflow.run_ae(args)
            invoke.assert_not_called()
            self.assertEqual(record.call_args.args[2]['status'], 'failed')

    def test_workbench_uses_selected_backends_without_forcing_an_editor(self):
        instructions = Workbench.instructions(SimpleNamespace(feedback_instructions=lambda: ''))
        for expected in ['以当轮配置块为准', '单独调用仅处理选中软件',
                         '保留原路径和旧版', '不能强退', '不得静默忽略']:
            self.assertIn(expected, instructions)
        self.assertNotIn('新项目默认达芬奇', instructions)
        self.assertIn('不能称全自动已完成', instructions)

    def test_ae_execution_does_not_require_uninstalled_premiere(self):
        ae = self.root / 'ae'
        ae.mkdir()
        for name in ('AfterFX.exe', 'AfterFX.com', 'aerender.exe'):
            (ae / name).write_bytes(b'fixture')
        (self.root / 'adobe.json').write_text(json.dumps({'ae_root': str(ae), 'pr_exe': str(self.root / 'missing.exe')}))
        self.assertEqual(workflow.applications(require_pr=False), {'ae_root': str(ae.resolve())})
        with self.assertRaises(FileNotFoundError):
            workflow.applications()

    def test_xml_explicit_stereo_outputs_and_bgm_gain(self):
        plan = {'name': 'stereo witness', 'canvas': self.plan['canvas'], 'duration_frames': 60,
                'clips': [dict(self.plan['clips'][0], start_frames=0, duration_frames=60,
                               info={'has_audio': True})],
                'audio': [{'path': str(self.root/'stereo.wav'), 'start_frames': 0,
                           'source_in_frames': 15, 'duration_frames': 60, 'volume': .25,
                           'info': {'has_audio': True, 'channels': 2, 'sample_rate': 48000, 'duration': 5}}]}
        root = ET.fromstring(adobe_compile.compile_xmeml(plan, self.root))
        audio = root.find('sequence/media/audio')
        self.assertEqual([n.text for n in audio.findall('outputs/group/numchannels')], ['1', '1'])
        self.assertEqual([n.text for n in audio.findall('outputs/group/channel/index')], ['1', '2'])
        tracks = audio.findall('track')
        self.assertEqual([t.get('currentExplodedTrackIndex') for t in tracks], ['0', '1', '0', '1'])
        self.assertTrue(all(t.get('totalExplodedTrackCount') == '2' for t in tracks))
        self.assertTrue(all(t.find('clipitem').get('premiereChannelType') == 'stereo' for t in tracks))
        self.assertEqual([t.findtext('outputchannelindex') for t in tracks], ['1', '2', '1', '2'])
        self.assertEqual([t.findtext('clipitem/sourcetrack/trackindex') for t in tracks], ['1', '2', '1', '2'])
        for track in tracks[-2:]:
            self.assertEqual(track.findtext('clipitem/filter/effect/parameter/value'), '0.25')
            self.assertEqual(track.findtext('clipitem/in'), '15')
        # Gain already baked by AE must not be applied a second time to source shots.
        for track in tracks[:2]:
            self.assertIsNone(track.find('clipitem/filter'))

    def test_mono_bgm_is_rejected_before_handoff(self):
        source = self.root/'source.mov'; source.touch()
        music = self.root/'mono.wav'; music.touch()
        plan = {'schema_version': 1, 'backend': 'pr-ae', 'name': 'mono not admitted',
                'canvas': self.plan['canvas'],
                'clips': [{'path': str(source), 'start': 0, 'source_in': 0, 'duration': 2}],
                'audio': [{'path': str(music), 'start': 0, 'source_in': 0, 'duration': 2}]}
        path = self.root/'plan.json'; path.write_text(json.dumps(plan), encoding='utf-8')

        def probe(media):
            if Path(media) == music:
                return {'kind': 'audio', 'duration': 4, 'has_audio': True, 'sample_rate': 48000, 'channels': 1}
            return {'kind': 'video', 'duration': 4, 'has_audio': True, 'width': 320, 'height': 180}

        with self.assertRaisesRegex(ValueError, 'stereo'):
            adobe_compile.validate_plan(path, probe)


if __name__ == '__main__':
    unittest.main()
