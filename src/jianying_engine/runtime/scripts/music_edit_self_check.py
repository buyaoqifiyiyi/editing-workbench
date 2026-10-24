"""Native-media payload, action-anchor and backend regression; never calls a cloud provider."""
import argparse
import base64
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from types import SimpleNamespace

import editor
import file_hash
import music_edit


class MusicEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder = tempfile.TemporaryDirectory(prefix='music-edit-tests-')
        cls.base = Path(cls.holder.name)
        cls.video = cls.base / 'video.mp4'
        cls.music = cls.base / 'music.wav'
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
                    '-i', 'testsrc2=s=320x180:r=30:d=8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.video)])
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
                    '-i', 'sine=frequency=440:duration=10', '-ar', '48000', '-ac', '2', str(cls.music)])
        cls.request = {'schema_version': 1, 'goal': 'BGM先铺垫后释放，命中对音乐重音',
                       'music': {'path': str(cls.music), 'start': 1, 'duration': 4},
                       'sources': [{'id': 'a', 'path': str(cls.video), 'start': 1, 'duration': 6}]}
        cls.request_file = cls.base / 'request.json'
        editor.write_json(cls.request_file, cls.request)
        music_edit.prepare(cls.request_file, cls.base / 'baseline')
        cls.analysis = {'music_sections': [{'start': 0, 'end': 2, 'description': '铺垫'},
                                           {'start': 2, 'end': 4, 'description': '释放'}],
                        'shots': [{'source_id': 'a', 'source_in': .5, 'music_end': 2, 'reason': '蓄势动作'},
                                  {'source_id': 'a', 'source_in': 2, 'music_end': 4, 'reason': '命中响应音乐',
                                   'anchor': {'source_time': 2.8, 'music_time': 2.8}}]}

    @classmethod
    def tearDownClass(cls):
        cls.holder.cleanup()

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix='music-edit-case-')
        self.addCleanup(self.holder.cleanup)
        self.root = Path(self.holder.name)
        self.bundle = self.root / 'bundle'
        shutil.copytree(self.base / 'baseline', self.bundle)
        self.config = self.root / 'config.json'
        editor.write_json(self.config, {'enabled': True, 'base_url': 'https://example.invalid/compatible-mode/v1',
                                       'api_key_env': 'MUSIC_EDIT_TEST_KEY', 'protocol': 'openai-compatible',
                                       'model': 'fixture/av-model', 'request_options': {'reasoning_effort': 'none'}})

    def response(self, value=None):
        return {'model': 'fixture-av-model', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.analysis if value is None else value, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}

    def receive(self, value=None):
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch.object(music_edit, 'send_payload', return_value=self.response(value)) as send:
            value = music_edit.analyze(self.bundle, self.config)
        self.assertEqual(send.call_count, 1)
        return value

    def test_native_audio_and_video_payload_has_real_bytes_and_no_path_text(self):
        root, manifest = music_edit.load_bundle(self.bundle)
        payload = music_edit.payload(root, manifest, music_edit.load_config(self.config))
        items = payload['messages'][0]['content']
        audio = next(item['input_audio']['data'] for item in items if item['type'] == 'input_audio')
        video = next(item['video_url']['url'] for item in items if item['type'] == 'video_url')
        self.assertEqual(base64.b64decode(audio.split(',', 1)[1]), (root / 'media/music.mp3').read_bytes())
        self.assertEqual(base64.b64decode(video.split(',', 1)[1]), (root / 'media/a.mp4').read_bytes())
        self.assertNotIn(str(self.base), json.dumps(payload))
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['reasoning_effort'], 'none')

    def test_prepare_preserves_original_and_refuses_existing(self):
        _, manifest = music_edit.load_bundle(self.bundle)
        self.assertFalse(manifest['network_called'])
        music_edit.check_sources(manifest['request'])
        self.assertAlmostEqual(editor.probe(self.bundle / 'media/music.wav')['duration'], 4, places=4)
        with self.assertRaises(FileExistsError):
            music_edit.prepare(self.request_file, self.bundle)

    def test_missing_key_and_disabled_config_do_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(music_edit, 'send_payload') as send:
            self.assertFalse(music_edit.doctor(self.config)['ready'])
            with self.assertRaises(ValueError):
                music_edit.analyze(self.bundle, self.config)
            send.assert_not_called()
        cfg = music_edit.read_json(self.config)
        cfg['enabled'] = False
        editor.write_json(self.config, cfg)
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}):
            self.assertFalse(music_edit.doctor(self.config)['ready'])

    def test_analysis_records_usage_and_reuses_without_second_call(self):
        value = self.receive()
        self.assertEqual(value['usage']['prompt_tokens'], 100)
        self.assertEqual(value['timing'], 'model_estimates')
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch.object(music_edit, 'send_payload') as send:
            with self.assertRaises(ValueError):
                music_edit.analyze(self.bundle, self.config)
            send.assert_not_called()

    def test_compile_both_backends_preserves_selected_offset_and_action(self):
        self.receive()
        for backend in ('jianying', 'resolve'):
            result = music_edit.compile_edit(self.bundle, self.root / backend, backend, 320, 180)
            self.assertEqual(result['duration_frames'], 120)
            plan = music_edit.read_json(result['plan'])
            self.assertEqual([clip['start'] for clip in plan['clips']], [0, 2])
            self.assertEqual(plan['clips'][1]['source_in'], 3)
            # The action at original source 3.8 s occurs on timeline 2.8 s.
            self.assertAlmostEqual(plan['clips'][1]['start'] + 3.8 - plan['clips'][1]['source_in'], 2.8)
            self.assertEqual(plan['audio'][0]['source_in'], 0)
            self.assertEqual(Path(plan['audio'][0]['path']), self.bundle / 'media/music.wav')
            with self.assertRaises(FileExistsError):
                music_edit.compile_edit(self.bundle, self.root / backend, backend, 320, 180)

    def test_beat_snap_moves_action_source_in_not_just_cut(self):
        value = copy.deepcopy(self.analysis)
        value['shots'][1]['anchor'] = {'source_time': 2.78, 'music_time': 2.78}
        self.receive(value)
        detected = {'source': {'path': str(self.bundle / 'media/music.wav')},
                    'selection': {'start_us': 0, 'sample_rate': 30000}, 'beat_samples': [60000, 84000, 120000]}
        with patch.object(music_edit.beats, 'load_result', return_value=detected):
            result = music_edit.compile_edit(self.bundle, self.root / 'snap', 'resolve', 320, 180, 'fixture')
        details = music_edit.read_json(result['alignment'])
        self.assertTrue(details['beat_candidates_used'])
        self.assertEqual(details['decisions'][1]['anchor']['music_frame'], 84)
        self.assertLess(details['decisions'][1]['source_in_frame'], 90)
        self.assertLessEqual(abs(details['decisions'][1]['anchor']['source_quantization_error_ms']), 1000 / 60)

    def test_source_or_analysis_or_proxy_change_rejected(self):
        self.receive()
        with (self.bundle / 'media/a.mp4').open('ab') as handle:
            handle.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            music_edit.compile_edit(self.bundle, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())
        shutil.copyfile(self.base / 'baseline/media/a.mp4', self.bundle / 'media/a.mp4')
        editor.write_json(self.bundle / 'analysis.json', {})
        with self.assertRaisesRegex(ValueError, 'changed'):
            music_edit.compile_edit(self.bundle, self.root / 'bad')

    def test_unknown_sources_and_invalid_times_are_not_plan_fields(self):
        request = music_edit.read_json(self.bundle / 'manifest.json')['request']
        cases = []
        value = copy.deepcopy(self.analysis); value['shots'][0]['source_id'] = '../../other'; cases.append(value)
        value = copy.deepcopy(self.analysis); value['shots'][0]['source_in'] = 5; cases.append(value)
        value = copy.deepcopy(self.analysis); value['shots'][0]['music_end'] = float('nan'); cases.append(value)
        value = copy.deepcopy(self.analysis); value['shots'][0]['duration'] = 2; cases.append(value)
        value = copy.deepcopy(self.analysis); value['music_sections'][1]['start'] = 2.5; cases.append(value)
        value = copy.deepcopy(self.analysis); value['shots'][1]['anchor']['music_time'] = .1; cases.append(value)
        value = copy.deepcopy(self.analysis); value['shots'][-1]['music_end'] = 3.9; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                music_edit.validate_analysis(value, request)

    def test_truncated_or_invalid_model_response_not_accepted(self):
        response = self.response(); response['choices'][0]['finish_reason'] = 'length'
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch.object(music_edit, 'send_payload', return_value=response):
            with self.assertRaises(ValueError):
                music_edit.analyze(self.bundle, self.config)
        self.assertFalse((self.bundle / 'analysis.json').exists())

    def test_reject_bad_endpoint_key_and_budget_before_call(self):
        for url in ('http://example.invalid/v1', 'https://user:secret@example.invalid/v1', 'https://example.invalid/v1?key=secret'):
            editor.write_json(self.config, {'enabled': True, 'base_url': url, 'api_key_env': 'MUSIC_EDIT_TEST_KEY',
                                            'protocol': 'openai-compatible', 'model': 'fixture/av-model',
                                            'request_options': {}})
            with self.assertRaises(ValueError):
                music_edit.load_config(self.config)
        for mutate in (lambda value: value['sources'].append(value['sources'][0]),
                       lambda value: value['music'].update(duration=180),
                       lambda value: value['sources'][0].update(path='https://example.invalid/video.mp4')):
            value = copy.deepcopy(self.request); mutate(value)
            path = self.root / 'invalid.json'; editor.write_json(path, value)
            with self.assertRaises(ValueError):
                music_edit.prepare(path, self.root / 'invalid')
            self.assertFalse((self.root / 'invalid').exists())

    def test_transport_redacts_provider_body_and_does_not_retry(self):
        cfg = music_edit.load_config(self.config)
        error = urllib.error.HTTPError(cfg['base_url'], 429, 'fixture-key', {}, io.BytesIO(b'private-body'))
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch('urllib.request.OpenerDirector.open', side_effect=error) as opened:
            with self.assertRaisesRegex(ValueError, '^Multimodal API HTTP 429;') as caught:
                music_edit.send_payload(cfg, {'model': cfg['model']}, self.config)
            self.assertNotIn('private-body', str(caught.exception))
            self.assertEqual(opened.call_count, 1)

    def test_transport_posts_native_payload_and_accepts_json_response(self):
        cfg = music_edit.load_config(self.config)
        root, bundle = music_edit.load_bundle(self.bundle)
        body = music_edit.payload(root, bundle, cfg)
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(self.response()).encode())) as opened:
            response = music_edit.send_payload(cfg, body, self.config)
        sent = opened.call_args.args[0]
        self.assertEqual(sent.get_method(), 'POST')
        self.assertTrue(sent.full_url.endswith('/chat/completions'))
        self.assertEqual(sent.get_header('Authorization'), 'Bearer fixture-only-key')
        self.assertEqual(json.loads(sent.data)['model'], cfg['model'])
        self.assertEqual(response['usage']['prompt_tokens'], 100)

    def test_failed_attempt_cannot_bill_again_in_same_bundle(self):
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch.object(music_edit, 'send_payload', side_effect=ValueError('network unknown')) as send:
            with self.assertRaisesRegex(ValueError, 'network unknown'):
                music_edit.analyze(self.bundle, self.config)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                music_edit.analyze(self.bundle, self.config)
            self.assertEqual(send.call_count, 1)
        self.assertTrue((self.bundle / 'analysis-attempt.json').exists())

    def test_compatible_model_id_and_options_are_from_config_not_a_fixed_model(self):
        cfg = {'enabled': True, 'base_url': 'https://example.invalid/custom-api', 'api_key_env': 'MUSIC_EDIT_TEST_KEY',
               'protocol': 'openai-compatible', 'model': 'vendor/another-av-model', 'request_options': {'temperature': .2}}
        editor.write_json(self.config, cfg)
        root, manifest = music_edit.load_bundle(self.bundle)
        body = music_edit.payload(root, manifest, cfg)
        self.assertEqual(body['model'], 'vendor/another-av-model')
        self.assertEqual(body['temperature'], .2)
        self.assertNotIn('reasoning_effort', body)
        result = self.receive()
        report = music_edit.read_json(self.bundle / 'analysis-report.json')
        self.assertEqual(result['model'], 'vendor/another-av-model')
        self.assertEqual(report['protocol'], 'openai-compatible')

    def test_gemini_native_media_auth_url_and_response(self):
        cfg = {'enabled': True, 'base_url': 'https://example.invalid/v1beta', 'api_key_env': 'MUSIC_EDIT_TEST_KEY',
               'protocol': 'gemini', 'model': 'models/gemini-fixture', 'request_options': {'temperature': .3}}
        editor.write_json(self.config, cfg)
        cfg = music_edit.load_config(self.config)
        root, manifest = music_edit.load_bundle(self.bundle)
        body = music_edit.payload(root, manifest, cfg)
        self.assertNotIn('messages', body)
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(body['generationConfig']['temperature'], .3)
        parts = body['contents'][0]['parts']
        media = [part['inlineData'] for part in parts if 'inlineData' in part]
        self.assertEqual([item['mimeType'] for item in media], ['audio/mpeg', 'video/mp4'])
        self.assertEqual(base64.b64decode(media[0]['data']), (root / 'media/music.mp3').read_bytes())
        self.assertEqual(base64.b64decode(media[1]['data']), (root / 'media/a.mp4').read_bytes())
        encoded = json.dumps(self.analysis)
        response = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': 'fixture thinking ignored', 'thought': True}, {'text': encoded[:20]}, {'text': encoded[20:]}]}}],
                    'usageMetadata': {'promptTokenCount': 123, 'candidatesTokenCount': 45}}
        with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            result = music_edit.analyze(self.bundle, self.config)
        sent = opened.call_args.args[0]
        self.assertTrue(sent.full_url.endswith('/v1beta/models/gemini-fixture:generateContent'))
        self.assertEqual(sent.get_header('X-goog-api-key'), 'fixture-only-key')
        self.assertIsNone(sent.get_header('Authorization'))
        self.assertNotIn('fixture-only-key', sent.full_url)
        self.assertEqual(result['model'], 'gemini-fixture')
        self.assertEqual(result['usage']['promptTokenCount'], 123)
        self.assertEqual(music_edit.read_json(root / 'analysis.json'), self.analysis)
        compiled = music_edit.compile_edit(root, self.root / 'gemini-plan', 'resolve', 320, 180)
        self.assertEqual(compiled['status'], 'plan_validated')

    def test_gemini_blocked_or_partial_response_is_not_accepted(self):
        for response in ({'candidates': []}, {'candidates': [{'finishReason': 'MAX_TOKENS'}]},
                         {'candidates': [{'finishReason': 'SAFETY'}]}):
            with self.subTest(response=response), self.assertRaises((ValueError, IndexError)):
                music_edit.read_response(response, 'gemini')

    def test_advanced_options_cannot_override_media_auth_or_model(self):
        cfg = music_edit.load_config(self.config)
        for field in ('model', 'messages', 'contents', 'authorization', 'api_key', 'tools', 'responseMimeType'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                music_edit.omni_config.validate({**cfg, 'request_options': {field: 'fixture'}})

    def test_compiled_plan_builds_editable_jianying_draft(self):
        self.receive()
        result = music_edit.compile_edit(self.bundle, self.root / 'plan', 'jianying', 320, 180)
        out = self.root / 'draft'
        editor.build_draft(SimpleNamespace(plan=result['plan'], out=str(out)))
        draft = music_edit.read_json(out / 'draft_content.json')
        video = next(track for track in draft['tracks'] if track['type'] == 'video')['segments']
        self.assertEqual([segment['target_timerange'] for segment in video],
                         [{'start': 0, 'duration': 2000000}, {'start': 2000000, 'duration': 2000000}])
        self.assertEqual(video[1]['source_timerange']['start'], 3000000)
        self.assertTrue(all(segment['volume'] == 0 for segment in video))

    def test_backend_incompatibility_does_not_create_deliverable(self):
        self.receive()
        with self.assertRaises(ValueError):
            music_edit.compile_edit(self.bundle, self.root / 'bad', 'resolve', 4000, 2000)
        self.assertFalse((self.root / 'bad').exists())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MusicEditTests)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {'status': 'passed' if result.wasSuccessful() else 'failed', 'tests': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors), 'network_called': False,
              'real_model': 'unverified', 'log': stream.getvalue()}
    editor.write_json(args.out / 'music-edit-check.json', report)
    editor.emit(report)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
