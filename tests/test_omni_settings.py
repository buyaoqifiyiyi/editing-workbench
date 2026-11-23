"""Omni credential persistence, non-disclosure, validation, and real DPAPI/child-process readback."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import omni_config  # noqa: E402


class OmniSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='omni-settings-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'omni.json'
        self.environment = patch.dict(os.environ, {'OPENEDIT_OMNI_CONFIG': str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    def test_real_dpapi_save_no_echo_clear_and_cli_child(self):
        test_key = 'sk-omni-fixture-not-a-real-account'
        value = omni_config.save(
            'https://example.invalid/v1', True,
            api_key=test_key, protocol='openai-compatible', model='fixture-av-model',
        )
        self.assertTrue(value['ready'])
        self.assertEqual(value['credential_source'], 'saved')
        self.assertEqual(omni_config.get_key(omni_config.load_config()), test_key)
        self.assertNotIn(test_key.encode(), self.config.read_bytes())
        self.assertNotIn(test_key.encode(), omni_config.key_path().read_bytes())
        self.assertNotIn(test_key, json.dumps(value))

        # Fresh engine process reads DPAPI key with no plaintext key in env.
        done = subprocess.run(
            [sys.executable, '-I', '-m', 'openedit_engine', 'music-edit', 'doctor'],
            capture_output=True, text=True, encoding='utf-8',
            env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)['ready'])

        # Updating URL with empty api_key keeps saved key.
        omni_config.save('https://other.invalid/v1', True, api_key='')
        self.assertEqual(omni_config.get_key(omni_config.load_config()), test_key)

        cleared = omni_config.save('https://other.invalid/v1', False, clear_key=True)
        self.assertFalse(omni_config.key_path().exists())
        self.assertFalse(cleared['enabled'])

    def test_invalid_inputs_do_not_create_or_leak_config(self):
        for kwargs in (
            {'base_url': 'http://example.invalid/v1', 'enabled': True, 'protocol': 'openai-compatible', 'model': 'm'},
            {'base_url': 'https://example.invalid/v1?key=private', 'enabled': True, 'protocol': 'openai-compatible', 'model': 'm'},
            {'base_url': 'https://example.invalid/v1', 'enabled': 'true', 'protocol': 'openai-compatible', 'model': 'm'},
            {'base_url': 'https://example.invalid/v1', 'enabled': True, 'api_key': 'private\nkey', 'protocol': 'openai-compatible', 'model': 'm'},
            {'base_url': 'https://example.invalid/v1', 'enabled': True, 'protocol': 'unknown', 'model': 'm'},
            {'base_url': 'https://example.invalid/v1', 'enabled': True, 'protocol': 'gemini', 'model': ''},
            {'base_url': 'https://example.invalid/v1', 'enabled': True, 'protocol': 'gemini', 'model': 'models/../../secret'},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError) as caught:
                    omni_config.save(**kwargs)
                self.assertNotIn('private', str(caught.exception))
                self.assertFalse(self.config.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    def test_config_write_failure_restores_previous_key(self):
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

    def test_environment_credentials_remain_supported(self):
        omni_config.save('https://example.invalid/v1', True, protocol='openai-compatible', model='fixture-av-model')
        with patch.dict(os.environ, {'MULTIMODAL_API_KEY': 'fixture-env'}):
            value = omni_config.status()
            self.assertTrue(value['ready'])
            self.assertEqual(value['credential_source'], 'environment')
            self.assertNotIn('fixture-env', json.dumps(value))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    def test_legacy_config_and_key_preserved_when_switching_protocol_and_model(self):
        self.config.write_text(json.dumps({
            'enabled': True, 'base_url': 'https://example.invalid/v1', 'api_key_env': 'MULTIMODAL_API_KEY',
        }), encoding='utf-8')
        omni_config.key_path().write_bytes(base64.b64encode(omni_config._crypt(b'fixture-existing-key')))
        before_secret = omni_config.key_path().read_bytes()
        initial = omni_config.status()
        self.assertEqual(initial['model'], '')
        self.assertEqual(initial['protocol'], 'openai-compatible')
        self.assertEqual(initial['request_options'], {})
        self.assertFalse(initial['ready'])
        self.assertIn('旧配置', initial['message'])
        saved = omni_config.save(
            'https://example.invalid/v1beta', True, protocol='gemini', model='gemini-custom-fixture', request_options={},
        )
        self.assertEqual(saved['model'], 'gemini-custom-fixture')
        self.assertEqual(saved['protocol'], 'gemini')
        self.assertNotIn('fixture-existing-key', json.dumps(saved))
        self.assertEqual(omni_config.key_path().read_bytes(), before_secret)
        self.assertEqual(omni_config.get_key(omni_config.load_config()), 'fixture-existing-key')


if __name__ == '__main__':
    unittest.main()
