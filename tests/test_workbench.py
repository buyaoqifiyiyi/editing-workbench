import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2

from jianying_engine.workbench import Workbench, create_app
from jianying_engine import software_backends


def available_backends():
    return {entry['id']: {'installed': True, 'compatible': entry['single_supported'],
                         'selectable': entry['single_supported'], 'state': 'available',
                         'label': '本机可用', 'reason': '测试安装', 'path': None, 'version': None}
            for entry in software_backends.catalog()}


class FakeBridge:
    def __init__(self, on_event, codex_path=None):
        self.callback, self.calls, self.responses = on_event, [], []
        self.closed, self.block_start = False, False
        self.gate = asyncio.Event()

    async def connect(self):
        self.calls.append('connect')
        return {'state': 'ready', 'version': 'test-only', 'cli_path': 'mock',
                'models': [{'id': 'test-model', 'model': 'test-model', 'displayName': 'Test model',
                            'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}], 'defaultReasoningEffort': 'medium'}]}

    async def start_thread(self, cwd, developer_instructions):
        self.calls.append('thread')
        self.instructions = developer_instructions
        if self.block_start:
            await self.gate.wait()
            if self.closed:
                raise ConnectionError('closed during startup')
        return 'test-thread'

    async def resume_thread(self, thread_id, cwd):
        self.calls.append('resume')
        return {}

    async def start_turn(self, thread_id, text, model=None, effort=None, images=None):
        self.calls.append(('turn', text, model, effort))
        return {'id': 'test-turn'}

    async def interrupt(self, thread_id, turn_id):
        self.calls.append('interrupt')

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))

    async def close(self):
        self.closed = True
        self.gate.set()


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.service = Workbench(self.root / 'state', self.workspace, FakeBridge)
        self.app = create_app(self.service, 'test-token', 8765)
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                       base_url='http://127.0.0.1:8765', headers={'X-Workbench-Token': 'test-token'})

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.temp.cleanup()

    async def post(self, endpoint, payload=None):
        return await self.client.post('/api/' + endpoint, json=payload or {})

    async def notify(self, method, params):
        await self.service.on_event({'kind': 'notification', 'method': method, 'params': params})

    async def test_readonly_connect_never_starts_a_turn(self):
        home = await self.client.get('/')
        self.assertIn('test-token', home.text)
        self.assertNotIn('__WORKBENCH_TOKEN__', home.text)
        response = await self.post('connect')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.bridge.calls, ['connect'])
        self.assertEqual(response.json()['agent']['state'], 'connected')
        self.assertEqual(self.service.sessions, [])

    async def test_creative_guide_reaches_new_and_continuing_turns(self):
        await self.post('connect')
        response = await self.post('send', {'text': '自主剪动漫情绪MV，仅模拟入口'})
        self.assertEqual(response.status_code, 200, response.text)
        instructions = self.service.bridge.instructions
        self.assertIn('planning', instructions)
        self.assertIn('reference/styles/anime-mix', instructions)
        self.assertNotIn('backend=resolve', instructions)
        for text in ('字幕、关键帧、调色和复杂特效不得静默忽略', '不修改引擎'):
            self.assertIn(text, instructions)
        first_turn = next(call for call in self.service.bridge.calls if isinstance(call, tuple) and call[0] == 'turn')
        self.assertIn('planning', first_turn[1])
        self.assertIn('backend=resolve', first_turn[1])
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'id': 'test-turn', 'status': 'completed'}})
        response = await self.post('send', {'text': '只修改已认可结尾的字幕'})
        self.assertEqual(response.status_code, 200, response.text)
        turns = [call for call in self.service.bridge.calls if isinstance(call, tuple) and call[0] == 'turn']
        self.assertEqual(len(turns), 2)
        self.assertEqual(self.service.bridge.calls.count('thread'), 1)
        self.assertIn('planning', turns[-1][1])
        self.assertIn('局部修改只复查受影响部分', turns[-1][1])
        self.assertIn('只修改已认可结尾的字幕', turns[-1][1])

    async def test_auth_origin_host_and_file_boundaries(self):
        for headers, status in [({'X-Workbench-Token': 'wrong'}, 401),
                                ({'Origin': 'https://attacker.example'}, 403),
                                ({'Host': 'attacker.example:8765'}, 403)]:
            response = await self.client.get('/api/state', headers=headers)
            self.assertEqual(response.status_code, status)
        self.assertEqual((await self.client.get('/config.env')).status_code, 404)
        secret = self.workspace / 'config.env'
        secret.write_text('secret fixture', encoding='utf-8')
        response = await self.post('media', {'paths': [str(secret)]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.media, [])
        response = await self.client.post('/api/send', content=b'x' * 262145)
        self.assertEqual(response.status_code, 400)

    async def test_media_preview_and_no_source_changes(self):
        image = self.workspace / '素材.png'
        from PIL import Image
        Image.new('RGB', (8, 8), '#346538').save(image)
        original = image.read_bytes()
        self.assertEqual((await self.post('media', {'paths': [str(image), str(image)]})).status_code, 200)
        self.assertEqual(len(self.service.media), 1)
        entry = self.service.media[0]
        response = await self.client.get(entry['url'] + '?token=test-token', headers={'X-Workbench-Token': ''})
        self.assertEqual(response.content, original)
        self.assertEqual((await self.client.get(entry['url'], headers={'X-Workbench-Token': ''})).status_code, 401)
        self.assertEqual((await self.client.delete(entry['url'])).status_code, 200)
        self.assertEqual(image.read_bytes(), original)
        self.assertEqual((await self.client.get(entry['url'])).status_code, 404)

    async def test_stream_completion_stop_and_duplicate_send(self):
        await self.post('connect')
        response = await self.post('send', {'text': '剪辑测试，仅模拟', 'model': 'test-model', 'effort': 'medium'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.current()['status'], 'running')
        self.assertEqual((await self.post('send', {'text': 'duplicate'})).status_code, 400)
        await self.notify('item/agentMessage/delta', {'threadId': 'test-thread', 'itemId': 'answer', 'delta': '<script>测试</script>'})
        self.assertEqual(self.service.current()['messages'][-1]['text'], '<script>测试</script>')
        await self.post('interrupt')
        self.assertEqual(self.service.current()['status'], 'stopping')
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'id': 'test-turn', 'status': 'interrupted'}})
        self.assertEqual(self.service.current()['status'], 'idle')
        self.assertIsNone(self.service.turn_id)

    async def test_unattended_command_and_input_preserve_request_id(self):
        await self.post('connect')
        await self.post('send', {'text': '模拟审批'})
        await self.service.on_event({'kind': 'request', 'id': 42, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'echo test'}})
        self.assertEqual(self.service.state()['approvals'], [])
        self.assertEqual(self.service.bridge.responses[-1], (42, {'decision': 'accept'}))
        await self.service.on_event({'kind': 'request', 'id': 'q', 'method': 'item/tool/requestUserInput',
                                    'params': {'questions': [{'id': 'style', 'question': '风格？', 'options':[{'label':'清爽'}]}]}})
        answers = {'style': {'answers': ['清爽']}}
        self.assertEqual(self.service.bridge.responses[-1], ('q', {'answers': answers}))
        self.assertEqual(self.service.current()['status'], 'running')

    async def test_stop_during_startup(self):
        await self.post('connect')
        self.service.bridge.block_start = True
        task = asyncio.create_task(self.service.send('startup'))
        for _ in range(100):
            if self.service.active():
                break
            await asyncio.sleep(.001)
        await self.service.interrupt()
        await asyncio.wait_for(task, 2)
        self.assertTrue(self.service.bridge.closed)
        self.assertEqual(self.service.current()['status'], 'idle')
        self.assertEqual(self.service.agent['state'], 'disconnected')

    async def test_file_approval_resolves_in_background_and_ignores_other_thread(self):
        await self.post('connect')
        await self.post('send', {'text': '剪辑'})
        await self.notify('item/started', {'threadId': 'test-thread', 'item': {
            'id': 'edit', 'type': 'fileChange', 'changes': [
                {'path': str(self.workspace / 'plan.json'), 'kind': {'type': 'update'}, 'diff': '+ new plan'}]}})
        await self.service.on_event({'kind': 'request', 'id': 'file-request',
                                    'method': 'item/fileChange/requestApproval',
                                    'params': {'threadId': 'test-thread', 'turnId': 'test-turn',
                                               'itemId': 'edit', 'reason': None}})
        self.assertEqual(self.service.state()['approvals'], [])
        self.assertEqual(self.service.bridge.responses[-1], ('file-request', {'decision': 'accept'}))
        self.assertEqual(self.service.current()['status'],'running')
        await self.service.on_event({'kind': 'request', 'id': 'old',
                                    'method': 'item/fileChange/requestApproval', 'params': {'threadId': 'other'}})
        self.assertEqual(self.service.state()['approvals'], [])
        self.assertEqual(len(self.service.bridge.responses),1)

    async def test_unattended_requests_use_once_only_responses(self):
        await self.post('connect')
        await self.post('send', {'text': '剪辑'})
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'acp/requestPermission'):
            await self.service.on_event({'kind':'request', 'id':method, 'method':method, 'params':{}})
            self.assertEqual(self.service.state()['approvals'],[])
            self.assertEqual(self.service.bridge.responses[-1],(method,{'decision':'accept'}))
        response=await self.post('approval',{'id':'obsolete','decision':'acceptForSession'})
        self.assertEqual(response.status_code,400)

    async def test_real_plan_and_terminal_state_without_fake_progress(self):
        await self.post('connect')
        await self.post('send', {'text': '剪辑'})
        plan = [{'step': '查看素材', 'status': 'completed'}, {'step': '生成草稿', 'status': 'inProgress'}]
        await self.notify('turn/plan/updated', {'threadId': 'test-thread', 'turnId': 'test-turn', 'plan': plan})
        self.assertEqual(self.service.current()['plan'], plan)
        await self.notify('item/completed', {'threadId': 'test-thread', 'item': {
            'id': 'declined', 'type': 'fileChange', 'status': 'declined'}})
        self.assertEqual(self.service.current()['events'][-1]['status'], 'error')
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'status': 'interrupted'}})
        self.assertEqual(self.service.current()['turn_status'], 'interrupted')
        await self.post('send', {'text': '继续'})
        self.assertEqual(self.service.current()['plan'], [])
        self.assertEqual(self.service.current()['turn_status'], 'inProgress')
        sent = next(c[1] for c in self.service.bridge.calls if isinstance(c, tuple) and c[0] == 'turn')
        self.assertIn('只负责本轮剪辑、特效、调色或声音制作任务', sent)
        self.assertIn('Obsidian', sent)

    async def test_stale_connection_cannot_change_new_connection(self):
        await self.post('connect')
        old = self.service.bridge
        await self.post('connect')
        await old.callback({'kind': 'connection_closed', 'error': 'old process'})
        self.assertEqual(self.service.agent['state'], 'connected')

    async def test_history_survives_and_resumes_only_owned_thread(self):
        await self.post('connect')
        await self.post('send', {'text': 'history'})
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'status': 'completed'}})
        restored = Workbench(self.root / 'state', self.workspace, FakeBridge)
        self.assertEqual(restored.current()['messages'][0]['text'], 'history')
        await restored.connect()
        await restored.send('continue')
        self.assertIn('resume', restored.bridge.calls)
        self.assertNotIn('thread', restored.bridge.calls)
        await restored.close()

    async def test_resume_failure_does_not_append_unsent_message(self):
        await self.post('connect')
        await self.post('send', {'text':'first'})
        await self.notify('turn/completed', {'threadId':'test-thread','turn':{'status':'completed'}})
        session=self.service.current()
        before=list(session['messages'])
        await self.post('connect')
        async def rejected(*args):
            raise RuntimeError('fixture resume rejected')
        self.service.bridge.resume_thread=rejected
        result=await self.post('send', {'text':'never submitted'})
        self.assertEqual(result.status_code,400)
        self.assertEqual(session['messages'],before,'Unsent messages must not be presented as submitted')

    async def test_unattended_requests_never_publish_confirmation_cards(self):
        await self.post('connect')
        await self.post('send',{'text':'剪辑，全程无需确认'})
        expected=[
            ('item/commandExecution/requestApproval',{}, {'decision':'accept'}),
            ('item/fileChange/requestApproval',{}, {'decision':'accept'}),
            ('execCommandApproval',{}, {'decision':'approved'}),
            ('applyPatchApproval',{}, {'decision':'approved'}),
            ('item/permissions/requestApproval',{'permissions':{'network':{'enabled':True}}}, {'permissions':{'network':{'enabled':True}},'scope':'turn'}),
            ('acp/requestPermission',{}, {'decision':'accept'}),
            ('item/tool/requestUserInput',{'questions':[{'id':'style','options':[{'label':'自主选择'}]}]}, {'answers':{'style':{'answers':['自主选择']}}}),
        ]
        for index,(method,params,result) in enumerate(expected):
            await self.service.on_event({'kind':'request','id':index,'method':method,'params':params})
            self.assertEqual(self.service.state()['approvals'],[])
            self.assertEqual(self.service.current()['status'],'running')
            self.assertEqual(self.service.bridge.responses[-1],(index,result))

    async def test_unattended_response_has_no_transient_popup_while_transport_waits(self):
        await self.post('connect')
        await self.post('send',{'text':'剪辑'})
        entered,release=asyncio.Event(),asyncio.Event()
        async def respond(identity,result):
            entered.set()
            await release.wait()
        self.service.bridge.respond=respond
        task=asyncio.create_task(self.service.on_event({'kind':'request','id':'slow','method':'item/commandExecution/requestApproval','params':{}}))
        await entered.wait()
        try:
            self.assertEqual(self.service.state()['approvals'],[])
            self.assertEqual(self.service.current()['status'],'running')
        finally:
            release.set()
            await task
        audit=(self.service.directory/'approval-audit.jsonl').read_text(encoding='utf-8')
        self.assertIn('slow',audit)

    async def test_active_writer_recovers_in_same_workbench_session_without_takeover(self):
        from jianying_engine.codex_bridge import CodexRpcError
        await self.post('connect')
        await self.post('send', {'text':'first'})
        await self.notify('turn/completed', {'threadId':'test-thread','turn':{'status':'completed'}})
        identity=self.service.current_id
        await self.post('connect')
        async def conflict(*args):
            raise CodexRpcError(-32600,'thread test-thread already has an active writer')
        async def fork(thread_id,cwd):
            self.assertEqual(thread_id,'test-thread')
            self.service.bridge.calls.append('fork')
            return {'thread':{'id':'continuation-thread'}}
        self.service.bridge.resume_thread=conflict
        self.service.bridge.fork_thread=fork
        response=await self.post('send',{'text':'continue editing'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.service.current_id,identity)
        self.assertEqual(len(self.service.sessions),1)
        self.assertEqual(self.service.current()['thread_id'],'continuation-thread')
        self.assertEqual(self.service.current()['continued_from'],'test-thread')
        self.assertEqual(len(self.service.current()['messages']),2)
        self.assertEqual(self.service.bridge.calls.count('fork'),1)

    async def test_bad_model_and_artifact_boundaries(self):
        await self.post('connect')
        response = await self.post('send', {'text': 'x', 'model': 'invented-model'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.bridge.calls, ['connect'])
        draft = self.workspace / 'draft'
        draft.mkdir()
        (draft / 'draft_content.json').write_text('{}')
        self.service.refresh_artifacts()
        self.assertEqual(len(self.service.artifacts), 1)
        with patch('jianying_engine.workbench.subprocess.Popen') as launched:
            response = await self.post('reveal', {'path': str(self.root / 'not-registered')})
            self.assertEqual(response.status_code, 400)
            launched.assert_not_called()

    async def test_obsidian_handoff_runs_in_background_without_agent_approval(self):
        inbox = self.root / 'vault' / '01-收件箱'
        inbox.mkdir(parents=True)
        (self.service.directory / 'handoff.json').write_text(json.dumps({'obsidian_vault': str(inbox.parent)}), encoding='utf-8')
        await self.post('connect')
        await self.post('send', {'text': '剪辑'})
        await self.notify('item/completed', {'threadId': 'test-thread', 'item': {
            'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer', 'text': '草稿已生成；尚未导出。'}})
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'status': 'completed'}})
        self.assertEqual(self.service.current()['status'], 'idle')
        if self.service.background_tasks:
            await asyncio.gather(*self.service.background_tasks)
        notes = list(inbox.glob('*.md'))
        self.assertEqual(len(notes), 1)
        note = notes[0].read_text(encoding='utf-8')
        self.assertIn('草稿已生成；尚未导出。', note)
        self.assertIn('待核实', note)
        self.assertEqual(self.service.state()['approvals'], [])
        self.assertEqual(self.service.bridge.responses, [])
        self.assertEqual(len([c for c in self.service.bridge.calls if isinstance(c, tuple) and c[0] == 'turn']), 1)
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'status': 'completed'}})
        self.assertEqual(len(list(inbox.glob('*.md'))), 1)

    async def test_completion_snapshot_survives_next_turn_during_artifact_scan(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        captured=[]
        def scan(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture scan was not released')
            return []
        async def handoff(snapshot):
            captured.append(snapshot)
        await self.post('connect')
        await self.post('send', {'text':'first'})
        await self.notify('item/completed', {'threadId':'test-thread','item':{'id':'first-final','type':'agentMessage','text':'first result'}})
        with patch('jianying_engine.workbench.artifacts.discover', side_effect=scan), patch.object(self.service,'_handoff',side_effect=handoff):
            completed=asyncio.create_task(self.notify('turn/completed', {'threadId':'test-thread','turn':{'status':'completed'}}))
            try:
                await asyncio.to_thread(entered.wait,2)
                self.assertTrue(entered.is_set())
                response=await self.post('send', {'text':'second'})
                self.assertEqual(response.status_code,200)
                await self.notify('item/completed', {'threadId':'test-thread','item':{'id':'second-final','type':'agentMessage','text':'second result'}})
            finally:
                release.set()
                await completed
            if self.service.background_tasks:
                await asyncio.gather(*self.service.background_tasks)
        self.assertEqual(captured[0]['summary'],'first result')
        self.assertEqual(self.service.current()['status'],'running')

    async def test_corrupt_history_backed_up_and_session_limit_preserves_history(self):
        directory = self.root / 'broken'
        directory.mkdir()
        (directory / 'sessions.json').write_text('{broken')
        broken = Workbench(directory, self.workspace, FakeBridge)
        self.assertTrue(broken.load_error)
        self.assertEqual(len(list(directory.glob('sessions-invalid-*'))), 1)
        self.service.sessions = [{'id': str(i)} for i in range(100)]
        with self.assertRaises(ValueError):
            self.service.new_session()
        self.assertEqual(len(self.service.sessions), 100)


if __name__ == '__main__':
    unittest.main()
