import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2

from jianying_engine.workbench import Workbench, classify_task, create_app
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

    async def test_view_other_session_during_run_preserves_active_events_and_stop_target(self):
        await self.post('connect')
        active=self.service.new_session()
        other=self.service.new_session()
        self.service.select(active['id'])
        await self.post('send', {'text':'制作'})
        result=await self.post('session/select', {'id':other['id']})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['active_session_id'],active['id'])
        self.assertEqual(result.json()['active_status'],'running')
        self.assertEqual(result.json()['current_session']['id'],other['id'])
        denied=await self.post('interrupt')
        self.assertEqual(denied.status_code,400)
        self.assertNotIn('interrupt',self.service.bridge.calls)
        await self.notify('item/agentMessage/delta', {'threadId':'test-thread','itemId':'active-msg','delta':'制作中'})
        self.assertEqual(active['messages'][-1]['text'],'制作中')
        self.assertFalse(any(m.get('text')=='制作中' for m in other['messages']))
        await self.post('session/select', {'id':active['id']})
        await self.post('interrupt')
        self.assertIn('interrupt',self.service.bridge.calls)

    async def test_completion_after_switch_updates_original_session(self):
        await self.post('connect')
        active=self.service.new_session()
        other=self.service.new_session()
        self.service.select(active['id'])
        await self.post('send', {'text':'制作'})
        await self.post('session/select', {'id':other['id']})
        await self.notify('turn/completed', {'threadId':'test-thread','turn':{'id':'test-turn','status':'completed'}})
        self.assertEqual(active['turn_status'],'completed')
        self.assertEqual(active['status'],'idle')
        self.assertEqual(self.service.current()['id'],other['id'])
        self.assertIsNone(self.service.active())

    async def test_delete_history_backs_up_and_refuses_active(self):
        await self.post('connect')
        active=self.service.new_session()
        other=self.service.new_session()
        other['messages'].append({'id':'m','role':'user','text':'保留备份'})
        self.service.select(active['id'])
        await self.post('send', {'text':'制作'})
        result=await self.post('session/delete', {'id':active['id']})
        self.assertEqual(result.status_code,400)
        result=await self.post('session/delete', {'id':other['id']})
        self.assertEqual(result.status_code,200)
        saved=list((self.service.directory/'deleted-sessions').glob('*.json'))
        self.assertEqual(len(saved),1)
        self.assertEqual(json.loads(saved[0].read_text(encoding='utf-8'))['messages'][0]['text'],'保留备份')
        self.assertEqual(self.service.active()['id'],active['id'])

    async def test_readonly_connect_never_starts_a_turn(self):
        home = await self.client.get('/')
        self.assertIn('test-token', home.text)
        self.assertNotIn('__WORKBENCH_TOKEN__', home.text)
        response = await self.post('connect')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.bridge.calls, ['connect'])
        self.assertEqual(response.json()['agent']['state'], 'connected')
        self.assertEqual(self.service.sessions, [])

    async def test_theme_script_is_served_as_javascript(self):
        response = await self.client.get('/theme.js')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('javascript', response.headers['content-type'])
        source = Path(__file__).parents[1] / 'src/jianying_engine/web/theme.js'
        self.assertEqual(response.content, source.read_bytes())
        self.assertIsNone(self.service.bridge)

    async def test_codex_selection_defaults_switches_persists_and_reaches_turn(self):
        await self.post('connect')
        models = [
            {'id': 'quick', 'model': 'quick', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}], 'defaultReasoningEffort': 'low'},
            {'id': 'recommended', 'model': 'recommended', 'isDefault': True, 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}], 'defaultReasoningEffort': 'medium'},
            {'id': 'hidden', 'model': 'hidden', 'hidden': True, 'isDefault': True}]
        self.service.agent['models'] = models
        self.assertEqual(self.service.state()['agent']['currentModel'], 'recommended')
        self.assertEqual(self.service.state()['agent']['currentEffort'], 'medium')
        chosen = await self.post('session/configure', {'model': 'recommended', 'effort': 'high'})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        first = self.service.current()['id']
        self.assertEqual(self.service.bridge.calls, ['connect'], 'Selecting settings must not create a thread or paid turn')
        self.assertEqual(self.service.state()['agent']['currentEffort'], 'high')
        second = self.service.new_session()['id']
        await self.post('session/configure', {'model': 'quick'})
        self.assertEqual(self.service.state()['agent']['currentEffort'], 'low')
        self.service.select(first)
        self.assertEqual(self.service.state()['agent']['currentEffort'], 'high')
        saved = json.loads(self.service.file.read_text(encoding='utf-8'))
        self.assertEqual(next(s for s in saved['sessions'] if s['id'] == first)['settings']['currentModel'], 'recommended')
        for invalid in ({'model': 'missing'}, {'model': 'hidden'}, {'model': 'quick', 'effort': 'high'}):
            result = await self.post('session/configure', invalid)
            self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.service.state()['agent']['currentEffort'], 'high')
        sent = await self.post('send', {'text': 'test selected model'})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.service.bridge.calls[-1][2:], ('recommended', 'high'))
        self.assertNotEqual(first, second)

    async def test_software_logo_assets_are_local_images(self):
        for name in ('software-resolve.png', 'software-jianying.png', 'software-after-effects.png', 'software-premiere.svg'):
            response = await self.client.get('/' + name)
            self.assertEqual(response.status_code, 200, name)
            self.assertTrue(response.headers['content-type'].startswith('image/'), name)
            self.assertGreater(len(response.content), 100)
        self.assertEqual((await self.client.get('/software-unknown.svg')).status_code, 404)

    async def test_theme_survives_rebuilding_workbench_and_renders_in_home(self):
        self.assertEqual(self.service.ui_theme, 'dark')
        backend_default = self.service.backend_default
        session = self.service.new_session()
        session['status'] = 'running'
        self.service.active_id = session['id']
        for theme in ('light', 'dark'):
            with patch.object(self.service, 'require_idle', side_effect=AssertionError('must not require idle')):
                response = await self.post('theme', {'theme': theme})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['ui_theme'], theme)
            self.assertEqual(session['status'], 'running')
            self.assertIsNone(self.service.bridge)
            self.assertEqual(session['messages'], [])
            rebuilt = Workbench(self.root / 'state', self.workspace, FakeBridge)
            try:
                self.assertEqual(rebuilt.ui_theme, theme)
                self.assertEqual(rebuilt.backend_default, backend_default)
                rebuilt.save()
                self.assertEqual(json.loads(rebuilt.file.read_text(encoding='utf-8'))['ui_theme'], theme)
                app = create_app(rebuilt, 'new-token', 9876)
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                             base_url='http://127.0.0.1:9876') as client:
                    home = await client.get('/')
                self.assertEqual(home.status_code, 200)
                self.assertIn(f'data-theme="{theme}"', home.text)
                self.assertNotIn('__WORKBENCH_THEME__', home.text)
            finally:
                await rebuilt.close()

    async def test_theme_rejects_invalid_input_without_changing_saved_selection(self):
        await self.post('theme', {'theme': 'light'})
        saved = self.service.file.read_bytes()
        for payload in ({}, {'theme': 'auto'}, {'theme': 'LIGHT'}, {'theme': None},
                        {'theme': []}, {'theme': {}}, {'theme': True},
                        {'theme': 'dark', 'extra': True}, [], 'dark'):
            response = await self.client.post('/api/theme', json=payload)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.service.ui_theme, 'light')
            self.assertEqual(self.service.file.read_bytes(), saved)
        response = await self.client.post('/api/theme', json={'theme': 'dark'},
                                          headers={'X-Workbench-Token': 'wrong'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.service.file.read_bytes(), saved)

    async def test_theme_load_defaults_invalid_or_missing_values_without_discarding_history(self):
        session = self.service.new_session()
        data = json.loads(self.service.file.read_text(encoding='utf-8'))
        for value in (None, 'auto', [], {}, True):
            data['ui_theme'] = value
            self.service.file.write_text(json.dumps(data), encoding='utf-8')
            rebuilt = Workbench(self.root / 'state', self.workspace, FakeBridge)
            try:
                self.assertEqual(rebuilt.ui_theme, 'dark')
                self.assertIsNone(rebuilt.load_error)
                self.assertEqual(rebuilt.sessions[0]['id'], session['id'])
            finally:
                await rebuilt.close()
        del data['ui_theme']
        self.service.file.write_text(json.dumps(data), encoding='utf-8')
        rebuilt = Workbench(self.root / 'state', self.workspace, FakeBridge)
        try:
            self.assertEqual(rebuilt.ui_theme, 'dark')
            self.assertIsNone(rebuilt.load_error)
        finally:
            await rebuilt.close()

    async def test_creative_guide_reaches_new_and_continuing_turns(self):
        await self.post('connect')
        response = await self.post('send', {'text': '自主剪动漫情绪MV，仅模拟入口'})
        self.assertEqual(response.status_code, 200, response.text)
        instructions = self.service.bridge.instructions
        self.assertIn('planning', instructions)
        self.assertIn('reference/styles/anime-mix', instructions)
        self.assertNotIn('不要请求权限确认或调用用户提问工具', instructions)
        self.assertIn('未授权的素材外发或新增付费', instructions)
        self.assertNotIn('backend=resolve', instructions)
        for text in ('字幕、关键帧、调色和复杂特效不得静默忽略', '不修改引擎'):
            self.assertIn(text, instructions)
        first_turn = next(call for call in self.service.bridge.calls if isinstance(call, tuple) and call[0] == 'turn')
        self.assertIn('planning', first_turn[1])
        self.assertIn('未授权的素材外发或新增付费', first_turn[1])
        self.assertIn('backend=resolve', first_turn[1])
        self.assertIn('## 每版同时设计剧情与剪辑', first_turn[1])
        self.assertIn('本地知识检索线索', first_turn[1])
        self.assertIn('第一版可以粗糙', first_turn[1])
        await self.notify('turn/completed', {'threadId': 'test-thread', 'turn': {'id': 'test-turn', 'status': 'completed'}})
        response = await self.post('send', {'text': '只修改已认可结尾的字幕'})
        self.assertEqual(response.status_code, 200, response.text)
        turns = [call for call in self.service.bridge.calls if isinstance(call, tuple) and call[0] == 'turn']
        self.assertEqual(len(turns), 2)
        self.assertEqual(self.service.bridge.calls.count('thread'), 1)
        self.assertIn('planning', turns[-1][1])
        self.assertIn('未授权的素材外发或新增付费', turns[-1][1])
        self.assertIn('局部修改只复查受影响部分', turns[-1][1])
        self.assertIn('只修改已认可结尾的字幕', turns[-1][1])
        self.assertIn('## 每版同时设计剧情与剪辑', turns[-1][1])
        self.assertIn('用户认可剧情但否定剪辑时', turns[-1][1])

    async def test_decision_context_uses_current_rules_and_real_local_sources(self):
        from jianying_engine.api import read_guide
        text = '只读分析，剧情认可但剪辑像拼接'
        context = self.service.decision_context({'messages': []}, text)
        self.assertIn('只读分析和方向建议仍以分析或建议为终点', context)
        self.assertIn('已发现关键表达缺失时先修改', context)
        self.assertIn('剪辑前先看同款作品', context)
        self.assertIn('只看到标题、封面、文字或字幕时', context)
        self.assertIn('参考查找不授权上传用户素材', context)
        self.assertIn('## 主动展开候选', context)
        self.assertIn('动作接剪', context)
        self.assertIn('局部复合', context)
        self.assertIn('不能因“克制”', context)
        self.assertIn('实际准入', context)
        self.assertNotIn('### 1. 从连续素材确认事件', context)
        leads = [line for line in context.splitlines() if line.startswith('- ')]
        self.assertTrue(leads)
        self.assertEqual(len(leads), len(set(leads)))
        for lead in leads:
            location, detail = lead[2:].split(' [', 1)
            name, line = location.rsplit(':', 1)
            excerpt = detail.split('] ', 1)[1]
            self.assertTrue(read_guide(name).splitlines()[int(line) - 1].strip().startswith(excerpt))

    async def test_continue_search_uses_previous_user_request_and_latest_guide(self):
        from unittest.mock import patch
        session = {'messages': [{'role': 'user', 'text': '剧情认可但剪辑像拼接'},
                                {'role': 'assistant', 'text': '不作为检索指令'}]}
        rule = '# 决策\n## 每版同时设计剧情与剪辑\n当前规则修订\n## 后续步骤\n其他内容'
        technique_rule = '# 方法\n## 主动展开候选\n当前方法\n## 后续内容\n其他技巧'
        with patch('jianying_engine.workbench.read_guide',
                   side_effect=lambda name: rule if name == 'planning' else technique_rule), patch(
                'jianying_engine.workbench.search_knowledge', return_value={'results': []}) as search:
            context = self.service.decision_context(session, '继续')
        self.assertEqual(search.call_args_list[0].args[0], '继续\n剧情认可但剪辑像拼接')
        self.assertIn('当前规则修订', context)
        self.assertIn('当前方法', context)
        self.assertNotIn('其他内容', context)
        self.assertNotIn('其他技巧', context)
        self.assertIn('本轮未命中', context)
        for call in search.call_args_list:
            self.assertLessEqual(len(call.args[0]), 256)

    async def test_task_tags_are_local_metadata_and_original_prompt_is_unchanged(self):
        original = '只读分析一下当前剪辑项目的叙事问题'
        self.assertEqual(classify_task(original), ['剪辑工作台', '只读核查'])
        self.assertEqual(classify_task('给我看看当前项目框架'), ['只读核查'])
        self.assertEqual(classify_task('此前测试成片叙事与剪辑太平，作为 OVA 看不出剧情'), ['剪辑工作台', '创作反馈'])
        self.assertEqual(classify_task('运行工作台单元测试并修复失败用例'), ['引擎维护', '故障排查'])
        self.assertEqual(classify_task('此次只授权只读核查，不自动开始重剪或修改项目'), ['剪辑工作台', '只读核查'])
        self.assertEqual(classify_task(
            '请接手此前的剪辑工作台与创作知识库工作，先阅读并核实交接，说明已掌握状态并等待下一步；'
            '此次只授权交接与只读核查，不自动开始重剪、修改项目或发送素材。'),
            ['剪辑工作台', '只读核查'])
        await self.post('connect')
        result = await self.post('send', {'text': original})
        self.assertEqual(result.status_code, 200, result.text)
        message = self.service.current()['messages'][0]
        self.assertEqual(message['text'], original)
        self.assertEqual(message['task_tags'], ['剪辑工作台', '只读核查'])
        turn = next(call for call in self.service.bridge.calls if isinstance(call, tuple) and call[0] == 'turn')
        prompt = turn[1].split('用户要求：\n', 1)[1]
        self.assertTrue(prompt.startswith(original))
        self.assertNotIn('task_tags', turn[1])
        self.assertEqual([call if isinstance(call, str) else call[0]
                          for call in self.service.bridge.calls], ['connect', 'thread', 'turn'])

        rebuilt = Workbench(self.root / 'state', self.workspace, FakeBridge)
        try:
            self.assertEqual(rebuilt.current()['messages'][0]['task_tags'], ['剪辑工作台', '只读核查'])
            self.assertEqual(rebuilt.current()['messages'][0]['text'], original)
        finally:
            await rebuilt.close()

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
