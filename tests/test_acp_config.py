"""Session-discovered ACP configuration, with no external model calls."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from jianying_engine.acp_bridge import AcpBridge, _RpcError
from test_acp_bridge import SERVER


OPTIONS = [
    {'id': 'provider-model', 'category': 'model', 'type': 'select', 'currentValue': 'a', 'options': [
        {'group': 'provider', 'options': [{'value': 'a', 'name': 'Model A'}, {'value': 'b', 'name': 'Model B'}]}]},
    {'id': 'reasoning_effort', 'category': 'thought_level', 'type': 'select', 'currentValue': 'high',
     'options': [{'value': 'low', 'name': 'Low'}, {'value': 'high', 'name': 'High'}]},
    {'id': 'mode', 'category': 'mode', 'type': 'select', 'currentValue': 'build',
     'options': [{'value': 'build', 'name': 'Build'}, {'value': 'plan', 'name': 'Plan', 'description': 'Read only'}]},
]

CONFIG_SERVER = SERVER.replace("calls=[]", 'calls=[]\nconfigs=' + repr(OPTIONS) + '\nfail_config=False')
CONFIG_SERVER = CONFIG_SERVER.replace("'agentCapabilities':{'loadSession':'--no-load' not in sys.argv}",
    "'agentCapabilities':({'sessionCapabilities':{'resume':{}}} if '--resume' in sys.argv else {'loadSession':True})")
CONFIG_SERVER = CONFIG_SERVER.replace("('session/new','session/load')", "('session/new','session/load','session/resume')")
CONFIG_SERVER = CONFIG_SERVER.replace("result(req,{'sessionId':'session-1'})", "result(req,{'sessionId':'session-1','configOptions':configs})")
CONFIG_SERVER = CONFIG_SERVER.replace("elif method=='session/prompt':", """elif method=='session/set_config_option':
    if fail_config:
      send({'id':req['id'],'error':{'code':-32002,'message':'Model unavailable api_key=fake-private-123 Bearer fake-bearer-456','data':{'private':'do-not-render'}}});continue
    option=next(o for o in configs if o['id']==p['configId'])
    option['currentValue']=p['value']
    result(req,{'configOptions':configs})
 elif method=='test/fail-config':fail_config=True;result(req,{})
 elif method=='test/config-update':
    configs[1]['currentValue']='low'
    update({'sessionUpdate':'config_option_update','configOptions':configs});result(req,{})
 elif method=='session/prompt':""")


class AcpConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.script = self.root / 'agent.py'
        self.script.write_text(CONFIG_SERVER, encoding='utf8')
        self.events = []
        self.bridge = AcpBridge(self.events.append, [sys.executable, '-u', str(self.script)])
        await self.bridge.connect()
        self.identity = await self.bridge.start_thread(self.root, 'preserve sources')

    async def asyncTearDown(self):
        await self.bridge.close()
        self.directory.cleanup()

    async def test_grouped_options_and_real_setters_before_prompt(self):
        info = self.bridge.session_info(self.identity)
        self.assertEqual([o['id'] for o in info['models']], ['a', 'b'])
        self.assertEqual(info['currentModel'], 'a')
        self.assertEqual([e['reasoningEffort'] for e in info['models'][0]['supportedReasoningEfforts']], ['low', 'high'])
        info = await self.bridge.set_session_options(self.identity, model='b', effort='low', mode='plan')
        self.assertEqual((info['currentModel'], info['currentEffort'], info['currentMode']), ('b', 'low', 'plan'))
        await self.bridge.start_turn(self.identity, 'hello', model='a', effort='high')
        async with asyncio.timeout(3):
            while not any(e.get('method') == 'turn/completed' for e in self.events):
                await asyncio.sleep(.01)
        calls = await self.bridge._rpc('test/calls', {})
        self.assertEqual(calls['calls'].count('session/set_config_option'), 5)
        self.assertLess(calls['calls'].index('session/set_config_option'), calls['calls'].index('session/prompt'))

    async def test_invalid_values_have_no_partial_set_and_none_preserves_current(self):
        for options in ({'model': 'unknown'}, {'effort': 'max'}, {'mode': 'invented'}, {'model': 'b', 'effort': 'max'}):
            with self.assertRaises(ValueError):
                await self.bridge.set_session_options(self.identity, **options)
        await self.bridge.set_session_options(self.identity)
        calls = await self.bridge._rpc('test/calls', {})
        self.assertNotIn('session/set_config_option', calls['calls'])
        self.assertNotIn('session/prompt', calls['calls'])

    async def test_failed_configuration_never_sends_prompt_and_redacts_errors(self):
        await self.bridge._rpc('test/fail-config', {})
        with self.assertRaises(_RpcError) as error:
            await self.bridge.start_turn(self.identity, 'hello', model='b')
        self.assertIn('Model unavailable', str(error.exception))
        for private in ('fake-private-123', 'fake-bearer-456', 'do-not-render'):
            self.assertNotIn(private, str(error.exception))
        calls = await self.bridge._rpc('test/calls', {})
        self.assertNotIn('session/prompt', calls['calls'])
        self.assertIsNone(self.bridge._active_turn)

    async def test_config_notifications_update_idle_session(self):
        await self.bridge._rpc('test/config-update', {})
        self.assertEqual(self.bridge.session_info(self.identity)['currentEffort'], 'low')
        self.assertFalse(any(e.get('method') == 'item/agentMessage/delta' for e in self.events))

    async def test_modern_resume_and_legacy_load_select_only_declared_method(self):
        await self.bridge.resume_thread(self.identity, self.root)
        self.assertIn('session/load', (await self.bridge._rpc('test/calls', {}))['calls'])
        other = AcpBridge(lambda e: None, [sys.executable, '-u', str(self.script), '--resume'])
        try:
            info = await other.connect()
            self.assertTrue(info['capabilities']['loadSession'])
            self.assertEqual(info['capabilities']['resumeMethod'], 'session/resume')
            await other.resume_thread(self.identity, self.root)
            calls = await other._rpc('test/calls', {})
            self.assertIn('session/resume', calls['calls'])
            self.assertNotIn('session/load', calls['calls'])
            self.assertEqual(other.session_info(self.identity)['currentModel'], 'a')
        finally:
            await other.close()

    async def test_legacy_model_mode_contract(self):
        self.bridge._session_configs[self.identity] = {
            'models': {'currentModelId': 'old-a', 'availableModels': [{'modelId': 'old-a', 'name': 'Old A'}]},
            'modes': {'currentModeId': 'build', 'availableModes': [{'id': 'build'}, {'id': 'plan'}]}}
        sent = []
        async def rpc(method, params, timeout):
            sent.append((method, params))
            return {}
        self.bridge._rpc = rpc
        info = await self.bridge.set_session_options(self.identity, model='old-a', mode='plan')
        self.assertEqual([method for method, _ in sent], ['session/set_model', 'session/set_mode'])
        self.assertEqual(info['currentMode'], 'plan')
        with self.assertRaises(ValueError):
            await self.bridge.set_session_options(self.identity, effort='high')


if __name__ == '__main__':
    unittest.main()
