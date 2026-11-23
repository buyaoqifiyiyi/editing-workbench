"""Actual omni transport shape, safe HTTP error redaction, and single-attempt probes."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import omni_config as omni  # noqa: E402


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'omni.json'
        env = patch.dict(os.environ, {'OPENEDIT_OMNI_CONFIG': str(self.path), 'MULTIMODAL_API_KEY': 'fixture-private-key'})
        env.start()
        self.addCleanup(env.stop)
        omni.save('https://example.invalid/v1', True, protocol='openai-compatible', model='fixture-model')

    @staticmethod
    def response(request, timeout):
        body = json.loads(request.data)
        assert timeout == 45
        assert request.get_header('Authorization') == 'Bearer fixture-private-key'
        assert request.full_url == 'https://example.invalid/v1/chat/completions'
        assert body['stream'] is False
        assert body['max_tokens'] == 256
        answer = body['messages'][0]['content'].split(': ')[-1]
        return io.BytesIO(json.dumps({
            'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}],
            'usage': {'total_tokens': 12, 'untrusted': 'fixture-private-key'},
        }).encode())

    def test_probe_uses_saved_config_and_status_never_dispatches(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=self.response) as opened:
            self.assertIsNone(omni.status()['connection_test'])
            opened.assert_not_called()
            result = omni.test_connection()
            self.assertTrue(result['success'], result)
            self.assertEqual(result['http_status'], 200)
            self.assertEqual(result['usage'], {'total_tokens': 12})
            self.assertNotIn('fixture-private-key', json.dumps(result))
            current = omni.status()
            self.assertEqual(current['connection_test'], result)
            self.assertFalse(current['network_called'])
            opened.assert_called_once()
            omni.save('https://example.invalid/v1', True, model='another-model')
            self.assertIsNone(omni.status()['connection_test'])

    def test_disabled_config_refuses_without_network(self):
        with patch.object(omni, 'send_request') as send:
            omni.save('https://example.invalid/v1', False)
            result = omni.test_connection()
            self.assertFalse(result['network_called'])
            self.assertFalse(result['success'])
            send.assert_not_called()

    def test_safe_http_errors_and_single_attempt(self):
        for code in (400, 401, 402, 403, 404, 429, 503):
            with self.subTest(code=code), patch(
                'urllib.request.OpenerDirector.open',
                side_effect=urllib.error.HTTPError('https://example.invalid', code, 'fixture-private-key', {}, io.BytesIO(b'private body')),
            ) as opened:
                result = omni.test_connection()
                self.assertFalse(result['success'])
                self.assertEqual(result['http_status'], code)
                self.assertNotIn('private', json.dumps(result))
                opened.assert_called_once()

    def test_malformed_truncated_redirect_and_timeout_are_not_success(self):
        cases = [b'not-json', b'{}', json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]}).encode()]
        for raw in cases:
            with patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(raw)) as opened:
                result = omni.test_connection()
                self.assertTrue(result['network_called'])
                self.assertFalse(result['success'])
                opened.assert_called_once()
        with patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError('private')) as opened:
            result = omni.test_connection()
            self.assertFalse(result['success'])
            self.assertNotIn('private', json.dumps(result))
            opened.assert_called_once()

    def test_gemini_protocol_uses_native_endpoint(self):
        omni.save('https://example.invalid/v1beta', True, protocol='gemini', model='fixture-gemini')

        def respond(request, timeout):
            self.assertEqual(request.full_url, 'https://example.invalid/v1beta/models/fixture-gemini:generateContent')
            self.assertEqual(request.get_header('X-goog-api-key'), 'fixture-private-key')
            body = json.loads(request.data)
            answer = body['contents'][0]['parts'][0]['text'].split(': ')[-1]
            return io.BytesIO(json.dumps({
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': answer}]}}]
            }).encode())

        with patch('urllib.request.OpenerDirector.open', side_effect=respond):
            self.assertTrue(omni.test_connection()['success'])

    def test_concurrent_connection_probe_is_rejected_safely(self):
        self.assertTrue(omni._test_lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(ValueError, '正在测试'):
                omni.test_connection()
        finally:
            omni._test_lock.release()


if __name__ == '__main__':
    unittest.main()
