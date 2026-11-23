import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters
from openedit_engine.mcp_server import create_server


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_lifespan_reaps_active_job(self):
        with tempfile.TemporaryDirectory(prefix='openedit-engine-lifespan-test-') as state:
            server = create_server(state)
            async with Client(server) as client:
                with patch('openedit_engine.jobs.command_for',
                           return_value=[sys.executable, '-c', 'import time;time.sleep(60)']):
                    started = await client.call_tool('start_job', {'operation': 'doctor', 'arguments': []})
                job_id = started.structured_content['job_id']
                self.assertTrue(job_id)
            result = json.loads((Path(state) / job_id / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['state'], 'cancelled')
            self.assertIsNotNone(result['returncode'])

    async def test_real_stdio_discovery_doctor_and_rejection(self):
        for module_name in ('openedit_engine.mcp_server',):
            with self.subTest(module_name=module_name), tempfile.TemporaryDirectory(prefix='openedit-mcp-') as state:
                environment = {name: os.environ[name] for name in
                               ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA', 'USERPROFILE', 'TEMP', 'TMP') if name in os.environ}
                environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
                parameters = StdioServerParameters(command=sys.executable,
                                                  args=['-m', module_name, '--state-dir', state],
                                                  env=environment)
                async with Client(parameters) as client:
                    self.assertIsNotNone(client.server_info)
                    self.assertTrue(client.protocol_version)
                    tools = await client.list_tools()
                    self.assertEqual({t.name for t in tools.tools},
                                     {'describe_operations', 'read_guide', 'search_knowledge',
                                      'start_job', 'job_status', 'read_job_output', 'cancel_job'})
                    discovered = await client.call_tool('describe_operations', {})
                    self.assertFalse(discovered.is_error)
                    for op in ('doctor', 'validate', 'build', 'render-overlay', 'filmcraft-exec', 'effectcraft-exec'):
                        self.assertIn(op, discovered.structured_content['operations'])
                    invalid_guide = await client.call_tool('read_guide', {'name': '../../secrets'})
                    self.assertIn('error', invalid_guide.structured_content)
                    denied = await client.call_tool('start_job', {'operation': 'powershell', 'arguments': ['-Command', 'whoami']})
                    self.assertEqual(denied.structured_content['state'], 'rejected')
                    started = await client.call_tool('start_job', {'operation': 'doctor', 'arguments': [], 'timeout_seconds': 30})
                    job_id = started.structured_content['job_id']
                    self.assertTrue(job_id)
                    for _ in range(150):
                        status = (await client.call_tool('job_status', {'job_id': job_id})).structured_content
                        if status['finished'] is not None:
                            break
                        await asyncio.sleep(.1)
                    self.assertEqual((status['state'], status['returncode']), ('succeeded', 0), status)
                    self.assertTrue(Path(status['stdout_log']).is_file())
                    self.assertIn('ready', status['stdout_tail'])

                    missing_plan = Path(state) / 'absent.json'
                    rejected = (await client.call_tool('start_job', {'operation': 'validate',
                        'arguments': ['--plan', str(missing_plan)]})).structured_content
                    for _ in range(150):
                        failed = (await client.call_tool('job_status', {'job_id': rejected['job_id']})).structured_content
                        if failed['finished'] is not None:
                            break
                        await asyncio.sleep(.1)
                    self.assertEqual((failed['state'], failed['returncode']), ('failed', 1), failed)

                    pages, offset = [], 0
                    while True:
                        page = (await client.call_tool('read_job_output',
                                {'job_id': job_id, 'offset': offset, 'limit': 113})).structured_content
                        self.assertFalse(page['truncated'])
                        pages.append(page['text'])
                        if page['next_offset'] is None:
                            break
                        offset = page['next_offset']
                    self.assertIn('ready', json.loads(''.join(pages)))
                    bad_stream = (await client.call_tool('read_job_output',
                                  {'job_id': job_id, 'stream': '../stdout.log'})).structured_content
                    self.assertEqual(bad_stream['state'], 'rejected')
                    bad_offset = await client.call_tool('read_job_output', {'job_id': job_id, 'offset': True})
                    self.assertTrue(bad_offset.is_error)
                    stderr_page = (await client.call_tool('read_job_output',
                                   {'job_id': job_id, 'stream': 'stderr'})).structured_content
                    self.assertEqual(stderr_page['stream'], 'stderr')

    async def test_structured_params_and_schema_discovery(self):
        with tempfile.TemporaryDirectory(prefix='openedit-params-test-') as state:
            async with Client(create_server(state)) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    {t.name for t in tools.tools},
                    {'describe_operations', 'read_guide', 'search_knowledge',
                     'start_job', 'job_status', 'read_job_output', 'cancel_job'},
                )
                described = (await client.call_tool('describe_operations', {})).structured_content
                self.assertIn('validate', described['operation_schemas'])
                self.assertEqual(described['job_limits'], {
                    'concurrent_limit': 2,
                    'retained_terminal_limit': 128,
                    'eviction': 'lru_terminal_only',
                    'logs_persisted_on_disk': True,
                })
                self.assertNotIn('backends', described)
                with_backends = (await client.call_tool('describe_operations', {'include_backends': True})).structured_content
                self.assertEqual({b['id'] for b in with_backends['backends']['backends']},
                                 {'filmcraft', 'effectcraft', 'ffmpeg'})
                self.assertTrue(with_backends['backends']['roles'])
                focused = (await client.call_tool('describe_operations', {'operation': 'validate'})).structured_content
                self.assertTrue(focused['schema']['params']['plan']['required'])
                bad_op = (await client.call_tool('describe_operations', {'operation': '../nope'})).structured_content
                self.assertIn('error', bad_op)

                both = (await client.call_tool('start_job', {
                    'operation': 'doctor', 'arguments': [], 'params': {},
                })).structured_content
                self.assertEqual(both['state'], 'rejected')

                bad_params = (await client.call_tool('start_job', {
                    'operation': 'validate', 'params': {'unknown': 'x'},
                })).structured_content
                self.assertEqual(bad_params['state'], 'rejected')

                started = (await client.call_tool('start_job', {
                    'operation': 'doctor', 'params': {}, 'timeout_seconds': 30,
                })).structured_content
                self.assertTrue(started['job_id'])
                for _ in range(150):
                    status = (await client.call_tool('job_status', {'job_id': started['job_id']})).structured_content
                    if status['finished'] is not None:
                        break
                    await asyncio.sleep(.1)
                self.assertEqual((status['state'], status['returncode']), ('succeeded', 0), status)


if __name__ == '__main__':
    unittest.main()
