import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine.agent_catalog import AgentCatalog
from jianying_engine.workbench import Workbench
from test_workbench import FakeBridge, available_backends


class CatalogTests(unittest.TestCase):
    def test_npm_native_binary_is_not_passed_to_node(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / 'JianyingEngine/agents/node_modules/opencode-ai'
            package.mkdir(parents=True)
            (package / 'agent.exe').write_bytes(b'test-only')
            (package / 'package.json').write_text(json.dumps({'bin': {'opencode': 'agent.exe'}}))
            with patch.dict('os.environ', {'LOCALAPPDATA': folder}), patch('jianying_engine.agent_catalog.shutil.which', return_value=None):
                catalog = AgentCatalog(Path(folder) / 'state')
                self.assertEqual(catalog.discover('opencode'), [str(package / 'agent.exe'), 'acp'])

    def test_save_rescan_and_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = AgentCatalog(folder)
            entry = catalog.resolve('custom-acp', sys.executable, ['agent.py', '--stdio'])
            catalog.remember(entry)
            restored = AgentCatalog(folder)
            self.assertEqual(restored.selected, 'custom-acp')
            self.assertEqual(restored.resolve()['command'], entry['command'])
            self.assertNotEqual(entry['fingerprint'], catalog.resolve('custom-acp', sys.executable, ['other.py'])['fingerprint'])
            for args in ('--stdio', [None], ['x'] * 33, ['a\0b']):
                with self.assertRaises(ValueError):
                    catalog.resolve('custom-acp', sys.executable, args)
            with self.assertRaises(ValueError):
                catalog.resolve('codex', sys.executable, ['--dangerously-bypass-approvals-and-sandbox'])

    def test_corrupt_preferences_remain_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'agents.json'
            file.write_text('{broken', encoding='utf-8')
            catalog = AgentCatalog(folder)
            self.assertTrue(catalog.error)
            with self.assertRaises(ValueError):
                catalog.resolve('codex')
            self.assertEqual(file.read_text(), '{broken')


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class MultiAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_does_not_reuse_another_agents_session(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Workbench(Path(folder) / 'state', folder, FakeBridge)
            await service.connect()
            await service.send('Codex-only history')
            first = service.current()['id']
            await service.on_event({'kind': 'notification', 'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}})
            with patch('jianying_engine.acp_bridge.AcpBridge', side_effect=lambda callback, **kw: FakeBridge(callback)):
                await service.connect(sys.executable, 'custom-acp', ['agent.py'])
                self.assertIsNone(service.current())
                service.select(first)
                with self.assertRaisesRegex(ValueError, '另一个 Agent'):
                    await service.send('must not send')
                self.assertEqual(service.bridge.calls, ['connect'])
                second = service.new_session()
                self.assertEqual(second['agent_id'], 'custom-acp')
                await service.send('ACP-only text')
                self.assertNotIn('Codex-only history', str(service.bridge.calls))
            await service.close()
            restored = Workbench(Path(folder) / 'state', folder, FakeBridge)
            self.assertEqual(restored.agent['id'], 'custom-acp')
            self.assertEqual(restored.sessions[0]['agent_id'], 'codex')
            await restored.close()

    async def test_no_catalog_claims_authentication_and_acp_requests_are_unattended(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Workbench(Path(folder) / 'state', folder, FakeBridge)
            self.assertTrue(all('authenticated' not in e for e in service.state()['agents']))
            with patch('jianying_engine.acp_bridge.AcpBridge', side_effect=lambda callback, **kw: FakeBridge(callback)):
                await service.connect(sys.executable, 'custom-acp', ['agent.py'])
                await service.send('test')
                await service.on_event({'kind': 'request', 'id': 7, 'method': 'acp/requestPermission',
                                        'params': {'options': [{'optionId': 'once', 'kind': 'allow_once'}], 'toolCall': {'title': 'write'}}})
                self.assertEqual(service.state()['approvals'], [])
                self.assertEqual(service.bridge.responses, [(7, {'decision': 'accept'})])
            await service.close()


if __name__ == '__main__':
    unittest.main()
