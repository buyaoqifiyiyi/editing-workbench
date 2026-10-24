"""UI settings persistence, credential non-disclosure and real Windows DPAPI/child-process readback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from jianying_engine.runtime.scripts import omni_config
from jianying_engine.workbench import Workbench, create_app
from test_workbench import available_backends


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class OmniSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='omni-settings-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'omni.json'
        self.environment = patch.dict(os.environ, {'JIANYING_OMNI_CONFIG': str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = Workbench(self.root / 'state', self.root)
        self.app = create_app(self.service, 'settings-token', 8765)

    async def asyncTearDown(self):
        await self.service.close()

    def client(self, authenticated=True):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url='http://127.0.0.1:8765',
                                  headers={'X-Workbench-Token': 'settings-token'} if authenticated else {})

    async def test_configuration_requires_page_auth_and_does_not_start_models(self):
        async with self.client(False) as client:
            self.assertEqual((await client.get('/api/omni/status')).status_code, 401)
            self.assertEqual((await client.post('/api/omni/configure', json={})).status_code, 401)
        async with self.client() as client:
            initial = (await client.get('/api/omni/status')).json()
            self.assertFalse(initial['ready'])
            self.assertFalse(initial['network_called'])
            saved = await client.post('/api/omni/configure', json={'base_url': 'https://example.invalid/v1', 'enabled': False,
                                                                 'protocol': 'openai-compatible', 'model': 'fixture-av-model'})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertFalse(saved.json()['omni']['enabled'])
            self.assertEqual(self.service.sessions, [])
            self.assertIsNone(self.service.bridge)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    async def test_real_dpapi_save_no_echo_reopen_keep_clear_and_cli_child(self):
        test_key = 'sk-omni-fixture-not-a-real-account'
        async with self.client() as client:
            saved = await client.post('/api/omni/configure', json={'base_url': 'https://example.invalid/v1',
                                       'enabled': True, 'api_key': test_key, 'protocol': 'openai-compatible', 'model': 'fixture-av-model'})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertNotIn(test_key, saved.text)
            value = saved.json()['omni']
            self.assertTrue(value['ready'])
            self.assertEqual(value['credential_source'], 'saved')
            self.assertEqual(omni_config.get_key(omni_config.load_config()), test_key)
            self.assertNotIn(test_key.encode(), self.config.read_bytes())
            self.assertNotIn(test_key.encode(), omni_config.key_path().read_bytes())
            self.assertNotIn(test_key, json.dumps(self.service.state()))
            # A fresh engine process reads the saved key directly, with no plaintext key in its environment.
            done = await __import__('asyncio').to_thread(subprocess.run,
                [sys.executable, '-I', '-m', 'jianying_engine', 'music-edit', 'doctor'],
                capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(json.loads(done.stdout)['ready'])
            restored = Workbench(self.root / 'reopened', self.root)
            try:
                self.assertEqual(restored.state()['omni']['credential_source'], 'saved')
            finally:
                await restored.close()
            await client.post('/api/omni/configure', json={'base_url': 'https://other.invalid/v1', 'enabled': True, 'api_key': ''})
            self.assertEqual(omni_config.get_key(omni_config.load_config()), test_key)
            cleared = await client.post('/api/omni/configure', json={'base_url': 'https://other.invalid/v1',
                                                                 'enabled': False, 'clear_key': True})
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertFalse(omni_config.key_path().exists())
            self.assertFalse(cleared.json()['omni']['enabled'])

    async def test_invalid_request_and_busy_do_not_modify_settings(self):
        async with self.client() as client:
            for body in ({'base_url': 'http://example.invalid/v1', 'enabled': True},
                         {'base_url': 'https://example.invalid/v1?key=private', 'enabled': True},
                         {'base_url': 'https://example.invalid/v1', 'enabled': 'true'},
                         {'base_url': 'https://example.invalid/v1', 'enabled': True, 'api_key': 'private\nkey'}):
                response = await client.post('/api/omni/configure', json=body)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn('private', response.text)
                self.assertFalse(self.config.exists())
            session = self.service.new_session()
            self.service.active_id = session['id']
            session['status'] = 'running'
            result = await client.post('/api/omni/configure', json={'base_url': 'https://example.invalid/v1', 'enabled': True})
            self.assertEqual(result.status_code, 400)
            self.assertFalse(self.config.exists())
            session['status'] = 'idle'

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    async def test_config_write_failure_restores_previous_key(self):
        omni_config.save('https://example.invalid/v1', True, 'fixture-first', protocol='openai-compatible', model='fixture-av-model')
        before_config = self.config.read_bytes()
        before_key = omni_config.key_path().read_bytes()
        original = omni_config._write_atomic
        def fail_config(path, data):
            if path == self.config:
                raise OSError('fixture disk failure')
            original(path, data)
        with patch.object(omni_config, '_write_atomic', side_effect=fail_config):
            with self.assertRaises(OSError):
                omni_config.save('https://other.invalid/v1', True, 'fixture-second')
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(omni_config.key_path().read_bytes(), before_key)
        self.assertEqual(omni_config.get_key(omni_config.load_config()), 'fixture-first')

    async def test_environment_credentials_remain_supported(self):
        omni_config.save('https://example.invalid/v1', True, protocol='openai-compatible', model='fixture-av-model')
        with patch.dict(os.environ, {'MULTIMODAL_API_KEY': 'fixture-env'}):
            value = omni_config.status()
            self.assertTrue(value['ready'])
            self.assertEqual(value['credential_source'], 'environment')
            self.assertNotIn('fixture-env', json.dumps(value))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    async def test_legacy_config_and_key_preserved_when_switching_protocol_and_model(self):
        # Existing three-field config retains its DPAPI key, but no model is guessed.
        self.config.write_text(json.dumps({'enabled': True, 'base_url': 'https://example.invalid/v1',
                                          'api_key_env': 'MULTIMODAL_API_KEY'}), encoding='utf-8')
        omni_config.key_path().write_bytes(__import__('base64').b64encode(omni_config._crypt(b'fixture-existing-key')))
        before_secret = omni_config.key_path().read_bytes()
        initial = omni_config.status()
        self.assertEqual(initial['model'], '')
        self.assertEqual(initial['protocol'], 'openai-compatible')
        self.assertEqual(initial['request_options'], {})
        self.assertFalse(initial['ready'])
        self.assertIn('旧配置', initial['message'])
        async with self.client() as client:
            saved = await client.post('/api/omni/configure', json={'base_url': 'https://example.invalid/v1beta',
                'enabled': True, 'protocol': 'gemini', 'model': 'gemini-custom-fixture', 'request_options': {}})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()['omni']['model'], 'gemini-custom-fixture')
            self.assertEqual(saved.json()['omni']['protocol'], 'gemini')
            self.assertNotIn('fixture-existing-key', saved.text)
        self.assertEqual(omni_config.key_path().read_bytes(), before_secret)
        self.assertEqual(omni_config.get_key(omni_config.load_config()), 'fixture-existing-key')
        done = await __import__('asyncio').to_thread(subprocess.run,
            [sys.executable, '-I', '-m', 'jianying_engine', 'music-edit', 'doctor'],
            capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(json.loads(done.stdout)['model'], 'gemini-custom-fixture')

    async def test_new_settings_require_model_and_protocol_without_vendor_default(self):
        self.assertEqual(omni_config.status()['model'], '')
        async with self.client() as client:
            for extra in ({}, {'protocol': 'unknown', 'model': 'fixture'},
                          {'protocol': 'gemini', 'model': ''},
                          {'protocol': 'gemini', 'model': 'models/../../secret'}):
                response = await client.post('/api/omni/configure', json={'base_url': 'https://example.invalid/v1beta',
                    'enabled': True, **extra})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.config.exists())
