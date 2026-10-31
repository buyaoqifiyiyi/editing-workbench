"""Actual transport shape, safe failure reporting and nonblocking connection checks."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

import httpx2
from jianying_engine.runtime.scripts import omni_config as omni
from jianying_engine.workbench import Workbench, create_app
from test_workbench import available_backends


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'omni.json'
        env = patch.dict(os.environ, {'JIANYING_OMNI_CONFIG': str(self.path), 'MULTIMODAL_API_KEY': 'fixture-private-key'})
        env.start()
        self.addCleanup(env.stop)
        omni.save('https://example.invalid/v1', True, protocol='openai-compatible', model='fixture-model')
        self.service = Workbench(self.root / 'state', self.root)
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=create_app(self.service, 'token', 8765)),
            base_url='http://127.0.0.1:8765', headers={'X-Workbench-Token': 'token'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()

    @staticmethod
    def response(request, timeout):
        body = json.loads(request.data)
        assert timeout == 45
        assert request.get_header('Authorization') == 'Bearer fixture-private-key'
        assert request.full_url == 'https://example.invalid/v1/chat/completions'
        assert body['stream'] is False
        assert body['max_tokens'] == 256
        answer = body['messages'][0]['content'].split(': ')[-1]
        return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}],
            'usage': {'total_tokens': 12, 'untrusted': 'fixture-private-key'}}).encode())

    async def test_probe_uses_saved_config_and_status_never_dispatches(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=self.response) as opened:
            self.assertIsNone((await self.client.get('/api/omni/status')).json()['connection_test'])
            opened.assert_not_called()
            result = (await self.client.post('/api/omni/test', json={})).json()
            self.assertTrue(result['success'], result)
            self.assertEqual(result['http_status'], 200)
            self.assertEqual(result['usage'], {'total_tokens': 12})
            self.assertNotIn('fixture-private-key', json.dumps(result))
            current = (await self.client.get('/api/omni/status')).json()
            self.assertEqual(current['connection_test'], result)
            self.assertFalse(current['network_called'])
            opened.assert_called_once()
            # A model or credential change invalidates success, even without restarting.
            omni.save('https://example.invalid/v1', True, model='another-model')
            self.assertIsNone(omni.status()['connection_test'])

    async def test_requires_auth_and_refuses_unsaved_fields(self):
        with patch.object(omni, 'send_request') as send:
            for body in ({'api_key': 'fixture-private-key'}, {'base_url': 'https://other.invalid'}):
                self.assertEqual((await self.client.post('/api/omni/test', json=body)).status_code, 400)
            self.assertEqual((await self.client.post('/api/omni/test', json={}, headers={'X-Workbench-Token': ''})).status_code, 401)
            omni.save('https://example.invalid/v1', False)
            result = (await self.client.post('/api/omni/test', json={})).json()
            self.assertFalse(result['network_called'])
            send.assert_not_called()

    async def test_safe_http_errors_and_single_attempt(self):
        for code in (400, 401, 403, 404, 429, 503):
            with self.subTest(code=code), patch('urllib.request.OpenerDirector.open',
                    side_effect=urllib.error.HTTPError('https://example.invalid', code, 'fixture-private-key', {}, io.BytesIO(b'private body'))) as opened:
                result = (await self.client.post('/api/omni/test', json={})).json()
                self.assertFalse(result['success'])
                self.assertEqual(result['http_status'], code)
                self.assertNotIn('private', json.dumps(result))
                opened.assert_called_once()

    async def test_malformed_truncated_and_timeout_are_not_success(self):
        cases = [b'not-json', b'{}', json.dumps({'choices':[{'finish_reason':'length','message':{'content':''}}]}).encode()]
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

    async def test_gemini_protocol_uses_native_endpoint(self):
        omni.save('https://example.invalid/v1beta', True, protocol='gemini', model='fixture-gemini')
        def respond(request, timeout):
            self.assertEqual(request.full_url, 'https://example.invalid/v1beta/models/fixture-gemini:generateContent')
            self.assertEqual(request.get_header('X-goog-api-key'), 'fixture-private-key')
            body = json.loads(request.data)
            answer = body['contents'][0]['parts'][0]['text'].split(': ')[-1]
            return io.BytesIO(json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':answer}]}}]}).encode())
        with patch('urllib.request.OpenerDirector.open', side_effect=respond):
            self.assertTrue(omni.test_connection()['success'])

    async def test_running_check_does_not_block_state_or_allow_duplicate_save(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(5)
            return {'success': True, 'message': 'fixture'}
        with patch.object(omni, 'test_connection', side_effect=slow) as probe:
            task = asyncio.create_task(self.client.post('/api/omni/test', json={}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                state = await asyncio.wait_for(self.client.get('/api/state'), 1)
                self.assertEqual(state.status_code, 200)
                self.assertEqual((await self.client.post('/api/omni/test', json={})).status_code, 400)
                self.assertEqual((await self.client.post('/api/omni/configure', json={'base_url':'https://other.invalid','enabled':True})).status_code, 400)
                self.assertEqual(omni.load_config()['base_url'], 'https://example.invalid/v1')
            finally:
                release.set()
                await task
            probe.assert_called_once()
