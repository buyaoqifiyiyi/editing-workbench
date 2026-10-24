"""Software selection travels through real HTTP/session/prompt boundaries; no model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2

from jianying_engine.workbench import Workbench, create_app
from test_workbench import FakeBridge, available_backends


def workflow():
    return {'mode': 'workflow', 'roles': {'editing': ['resolve'], 'effects': ['after-effects'],
                                        'color': [], 'audio': []},
            'single_backend': None}


def single(backend='after-effects'):
    return {'mode': 'single', 'roles': {'editing': [], 'effects': [], 'color': [], 'audio': []},
            'single_backend': backend}


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class BackendSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backend-session-')
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        self.service = Workbench(self.root / 'state', self.workspace, FakeBridge)
        self.app = create_app(self.service, 'test-token', 8765)
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                       base_url='http://127.0.0.1:8765',
                                       headers={'X-Workbench-Token': 'test-token'})

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.temp.cleanup()

    async def configure(self, value):
        response = await self.client.post('/api/backends/configure', json={'selection': value})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def complete(self):
        await self.service.on_event({'kind': 'notification', 'method': 'turn/completed',
                                     'params': {'threadId': 'test-thread',
                                                'turn': {'id': 'test-turn', 'status': 'completed'}}})

    async def test_configuration_does_not_connect_or_start_a_task(self):
        value = workflow()
        state = await self.configure(value)
        self.assertEqual(state['backends']['selection'], value)
        self.assertEqual(state['backends']['default_selection'], value)
        self.assertIsNone(self.service.bridge)
        self.assertEqual(self.service.sessions, [])
        persisted = json.loads(self.service.file.read_text(encoding='utf-8'))
        self.assertEqual(persisted['backend_default'], value)

    async def test_current_and_new_session_choices_survive_switch_and_reopen(self):
        await self.configure(workflow())
        first = self.service.new_session()['id']
        second = self.service.new_session()['id']
        await self.configure(single('resolve'))
        self.service.select(first)
        self.assertEqual(self.service.state()['backends']['selection'], workflow())
        restored = Workbench(self.service.directory, self.workspace, FakeBridge)
        try:
            self.assertEqual(restored.current_id, first)
            self.assertEqual(restored.state()['backends']['selection'], workflow())
            restored.select(second)
            self.assertEqual(restored.state()['backends']['selection'], single('resolve'))
            third = restored.new_session()
            self.assertEqual(third['backend_selection'], single('resolve'))
        finally:
            await restored.close()

    async def test_single_ae_reaches_first_and_continuing_turn_without_default_editor(self):
        await self.configure(single())
        await self.client.post('/api/connect', json={})
        response = await self.client.post('/api/send', json={'text': '只做一个AE可编辑文字镜头'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('以当轮配置块为准', self.service.bridge.instructions)
        self.assertNotIn('adobe-workflow', self.service.bridge.instructions)
        self.assertNotIn('resolve-workflow', self.service.bridge.instructions)
        stable_instructions = self.service.bridge.instructions
        first_prompt = next(call[1] for call in self.service.bridge.calls
                            if isinstance(call, tuple) and call[0] == 'turn')
        self.assertIn('adobe-workflow', first_prompt)
        self.assertNotIn('resolve-workflow', first_prompt)
        await self.complete()
        await self.configure(single('jianying'))
        response = await self.client.post('/api/send', json={'text': '只分析这段音频的节拍候选'})
        self.assertEqual(response.status_code, 200, response.text)
        latest = [call[1] for call in self.service.bridge.calls
                  if isinstance(call, tuple) and call[0] == 'turn'][-1]
        self.assertIn('beats', latest)
        self.assertNotIn('adobe-workflow', latest)
        self.assertNotIn('resolve-workflow', latest)
        self.assertEqual(self.service.bridge.calls.count('thread'), 1)
        self.assertEqual(self.service.bridge.instructions, stable_instructions)

    async def test_legacy_session_keeps_project_context_until_explicit_choice(self):
        self.service.new_session()
        self.service.current().pop('backend_selection')
        self.service.current()['messages'] = [{'role': 'assistant', 'text': '已有剪映草稿，请保留'}]
        self.service.save()
        restored = Workbench(self.service.directory, self.workspace, FakeBridge)
        try:
            self.assertIsNone(restored.state()['backends']['selection'])
            prompt = restored.backend_prompt(restored.current())
            self.assertIn('沿用此会话已有工程', prompt)
            self.assertNotIn('resolve-workflow', prompt)
            restored.configure_backends(single())
            self.assertEqual(restored.state()['backends']['selection'], single())
            self.assertEqual(restored.current()['messages'][0]['text'], '已有剪映草稿，请保留')
        finally:
            await restored.close()

    async def test_invalid_or_busy_configuration_cannot_mutate_saved_choices(self):
        await self.configure(workflow())
        before = self.service.file.read_bytes()
        bad = [{'mode': 'workflow', 'roles': {'color': ['resolve']}}, None,
               {'mode': 'single', 'single_backend': 'premiere'},
               {'mode': 'workflow', 'roles': {'editing': ['resolve', 'jianying']}}]
        for value in bad:
            response = await self.client.post('/api/backends/configure', json={'selection': value})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.service.file.read_bytes(), before)
        self.service.new_session()
        self.service.current()['status'] = 'running'
        self.service.active_id = self.service.current_id
        self.service.save()
        before = self.service.file.read_bytes()
        response = await self.client.post('/api/backends/configure', json={'selection': single()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.file.read_bytes(), before)
        self.service.current()['status'] = 'idle'
        self.service.active_id = None

    async def test_uninstalled_software_blocks_save_and_send_without_mutating_history(self):
        await self.service.connect()
        await self.configure(single('resolve'))
        self.service.new_session()
        self.service.current()['messages'] = [{'role': 'user', 'text': '保留工程与历史'}]
        self.service.save()
        before, calls = self.service.file.read_bytes(), list(self.service.bridge.calls)
        availability = available_backends()
        availability['resolve'].update(installed=False, compatible=False, selectable=False,
                                      state='missing', label='未安装', reason='已卸载')
        with patch('jianying_engine.workbench.detect_backends', return_value=availability):
            response = await self.client.post('/api/backends/refresh', json={})
            state = response.json()['backends']
            resolve = next(item for item in state['catalog'] if item['id'] == 'resolve')
            self.assertFalse(resolve['selectable'])
            self.assertIn('已卸载', state['error'])
            self.assertEqual(state['selection'], single('resolve'))
            for endpoint, payload in [('backends/configure', {'selection': single('resolve')}),
                                      ('send', {'text': '现在运行达芬奇'})]:
                response = await self.client.post('/api/' + endpoint, json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('已卸载', response.json()['error'])
                self.assertEqual(self.service.file.read_bytes(), before)
                self.assertEqual(self.service.bridge.calls, calls)
            await self.configure(single('jianying'))
            self.assertEqual(self.service.current()['messages'][0]['text'], '保留工程与历史')

    async def test_detection_refresh_and_expiry_update_catalog_without_saving(self):
        availability = available_backends()
        self.service.backend_detection = None
        with patch('jianying_engine.workbench.detect_backends', return_value=availability) as detect:
            self.service.backend_state()
            self.service.backend_state()
            self.assertEqual(detect.call_count, 1)
            self.service.backend_checked_at -= 11
            self.service.backend_state()
            self.assertEqual(detect.call_count, 2)
            availability['resolve'].update(selectable=False, compatible=False, reason='版本不兼容')
            response = await self.client.post('/api/backends/refresh', json={})
            self.assertEqual(detect.call_count, 3)
            self.assertIn('版本不兼容', response.json()['backends']['error'])
            self.assertFalse(self.service.file.exists())
            self.assertIsNone(self.service.bridge)

    async def test_stale_selection_preserves_history_and_blocks_execution_until_reselected(self):
        self.service.new_session()
        self.service.current()['messages'] = [{'role': 'user', 'text': '保留历史要求'}]
        self.service.current()['backend_selection'] = {'mode': 'single', 'single_backend': 'removed-software'}
        self.service.save()
        restored = Workbench(self.service.directory, self.workspace, FakeBridge)
        try:
            self.assertEqual(restored.current()['messages'][0]['text'], '保留历史要求')
            self.assertTrue(restored.state()['backends']['error'])
            self.assertIsNone(restored.load_error)
            self.assertEqual(list(restored.directory.glob('sessions-invalid-*')), [])
            self.assertEqual(restored.current()['backend_selection']['single_backend'], 'removed-software')
            restored.configure_backends(single())
            self.assertIsNone(restored.state()['backends']['error'])
        finally:
            await restored.close()

    async def test_old_jianying_color_choice_is_preserved_but_cannot_be_executed(self):
        await self.service.connect()
        self.service.new_session()
        legacy = {'mode': 'workflow', 'roles': {'editing': ['jianying'], 'color': ['jianying']},
                  'single_backend': None}
        self.service.current()['backend_selection'] = legacy
        self.service.current()['messages'] = [{'role': 'user', 'text': '原调色要求和工程保留'}]
        self.service.save()
        before = self.service.file.read_bytes()
        state = self.service.backend_state()
        self.assertIn('尚未准入 color', state['error'])
        self.assertEqual(self.service.current()['backend_selection'], legacy)
        response = await self.client.post('/api/send', json={'text': '继续旧配置'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.service.file.read_bytes(), before)
        self.assertEqual(self.service.bridge.calls, ['connect'])
        await self.configure(workflow())
        self.assertEqual(self.service.current()['messages'][0]['text'], '原调色要求和工程保留')

    async def test_catalog_shows_unadmitted_product_roles_but_api_rejects_selection(self):
        response = await self.client.get('/api/state')
        catalog = {item['id']: item for item in response.json()['backends']['catalog']}
        self.assertIn('color', catalog['resolve']['roles'])
        self.assertFalse(catalog['resolve']['role_availability']['color']['selectable'])
        self.assertIn('editing', catalog['premiere']['roles'])
        self.assertFalse(catalog['premiere']['role_availability']['editing']['selectable'])
        self.assertNotIn('color', catalog['jianying']['roles'])
        for value in ({'mode': 'workflow', 'roles': {'color': ['resolve']}},
                      {'mode': 'workflow', 'roles': {'effects': ['resolve']}},
                      {'mode': 'workflow', 'roles': {'audio': ['premiere']}}):
            response = await self.client.post('/api/backends/configure', json={'selection': value})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertFalse(self.service.file.exists())


if __name__ == '__main__':
    unittest.main()
