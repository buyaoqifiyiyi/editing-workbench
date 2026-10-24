import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from jianying_engine import api
from jianying_engine.workbench import Workbench, create_app
from test_workbench import FakeBridge, available_backends


class ConfigBridge(FakeBridge):
    def __init__(self, callback, **kwargs):
        super().__init__(callback)
        self.settings = {'models': [{'id': key, 'model': key, 'displayName': key,
                         'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]}
                         for key in ('a', 'b')], 'currentModel': 'a', 'currentEffort': 'high',
                         'modes': [{'id': 'build', 'name': 'Build'}, {'id': 'plan', 'name': 'Plan'}],
                         'currentMode': 'build', 'configOptions': []}

    async def connect(self):
        self.calls.append('connect')
        return {'models': [], 'capabilities': {'loadSession': True}, 'version': 'fake'}

    def session_info(self, identity):
        return copy.deepcopy(self.settings)

    def set_instructions(self, identity, text):
        self.calls.append(('instructions', text))

    async def set_session_options(self, identity, model=None, effort=None, mode=None):
        if model not in (None, 'a', 'b') or effort not in (None, 'low', 'high') or mode not in (None, 'build', 'plan'):
            raise ValueError('Unsupported option')
        for key, value in (('currentModel', model), ('currentEffort', effort), ('currentMode', mode)):
            if value is not None:
                self.settings[key] = value
        return self.session_info(identity)


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class WorkbenchFeatures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.service = Workbench(self.root / 'state', self.workspace, FakeBridge)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_session_settings_prepare_configure_and_resume_empty_context(self):
        with patch('jianying_engine.acp_bridge.AcpBridge', ConfigBridge):
            await self.service.connect(sys.executable, 'custom-acp', [])
            await self.service.prepare_session()
            session = self.service.current()
            self.assertEqual(session['messages'], [])
            self.assertEqual(self.service.agent['currentModel'], 'a')
            self.assertEqual(self.service.bridge.calls, ['connect', 'thread'])
            await self.service.configure_session('b', 'low', 'plan')
            self.assertEqual(session['settings'], {'currentModel': 'b', 'currentEffort': 'low', 'currentMode': 'plan'})
            with self.assertRaises(ValueError):
                await self.service.configure_session('unknown')
            self.assertEqual(self.service.agent['currentModel'], 'b')
            await self.service.close()
            restored = Workbench(self.root / 'state', self.workspace, FakeBridge)
            try:
                await restored.connect(sys.executable, 'custom-acp', [])
                await restored.prepare_session()
                self.assertIn('resume', restored.bridge.calls)
                self.assertTrue(any(isinstance(call, tuple) and call[0] == 'instructions' for call in restored.bridge.calls))
                self.assertEqual(restored.current()['messages'], [])
                self.assertEqual(restored.current()['settings'], {'currentModel': 'b', 'currentEffort': 'low', 'currentMode': 'plan'})
                self.assertEqual(restored.agent['currentEffort'], 'low')
                self.assertFalse(any(isinstance(call, tuple) and call[0] == 'turn' for call in restored.bridge.calls))
                restored.current()['settings']['currentModel'] = 'removed-model'
                restored.current()['_restore_settings'] = True
                with self.assertRaisesRegex(ValueError, '恢复原会话设置失败'):
                    await restored.prepare_session()
                self.assertEqual(restored.current()['settings']['currentModel'], 'removed-model')
                self.assertEqual(restored.agent['currentModel'], 'b')
                await restored.configure_session('a', 'high', 'build')
                self.assertNotIn('_restore_settings', restored.current())
                self.assertEqual(restored.current()['settings']['currentModel'], 'a')
            finally:
                await restored.close()

    async def test_completion_discovers_deep_outputs_and_preview_is_authenticated(self):
        await self.service.connect()
        await self.service.send('fake only')
        output = self.workspace / 'project/version/output/draft'
        output.mkdir(parents=True)
        (output / 'draft_content.json').write_text('{"tracks": []}', encoding='utf-8')
        subtitle = output.parent / 'captions.srt'
        subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n<script>literal text</script>', encoding='utf-8')
        inputs = self.workspace / 'raw-input'
        inputs.mkdir()
        (inputs / 'source.mp4').write_bytes(b'source')
        self.service.media = [{'path': str(inputs), 'kind': 'folder'}]
        await self.service.on_event({'kind': 'notification', 'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}})
        self.assertEqual({item['name'] for item in self.service.artifacts}, {'draft_content.json', 'captions.srt'})
        restored = Workbench(self.root / 'state', self.workspace, FakeBridge)
        self.assertEqual(restored.artifacts, self.service.artifacts)
        entry = next(item for item in self.service.artifacts if item['name'] == 'captions.srt')
        app = create_app(self.service, 'token', 8765)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as client:
            self.assertEqual((await client.get(entry['url'])).status_code, 401)
            preview = await client.get(entry['url'], headers={'X-Workbench-Token': 'token'})
            self.assertEqual(preview.json()['text'], subtitle.read_bytes().decode('utf-8'))
            outside = self.root / 'outside.md'
            outside.write_text('private')
            self.service.artifacts.append({'id': 'outside', 'path': str(outside)})
            denied = await client.get('/api/artifacts/outside', headers={'X-Workbench-Token': 'token'})
            self.assertEqual(denied.status_code, 400)
            self.assertNotIn('private', denied.text)

    async def test_desktop_cli_fallback_uses_console_python(self):
        python = self.root / 'python.exe'
        python.write_bytes(b'not executed')
        with patch.object(api.sys, 'executable', str(self.root / 'pythonw.exe')):
            self.assertEqual(api.paths()['python'], str(python))
            self.assertEqual(api.command_for('doctor', [])[0], str(python))


if __name__ == '__main__':
    unittest.main()
