import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters
from jianying_engine.mcp_server import create_server


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_lifespan_reaps_active_job(self):
        with tempfile.TemporaryDirectory(prefix='jianying-engine-lifespan-test-') as state:
            server = create_server(state)
            async with Client(server) as client:
                with patch('jianying_engine.jobs.command_for',
                           return_value=[sys.executable, '-c', 'import time;time.sleep(60)']):
                    started = await client.call_tool('start_job', {'operation': 'doctor', 'arguments': []})
                job_id = started.structured_content['job_id']
                self.assertTrue(job_id)
            result = json.loads((Path(state) / job_id / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['state'], 'cancelled')
            self.assertIsNotNone(result['returncode'])

    async def test_real_stdio_discovery_doctor_and_rejection(self):
        with tempfile.TemporaryDirectory(prefix='jianying-engine-stdio-test-') as state:
            # Forward only runtime paths, not API credentials or the host's entire environment.
            environment = {name: os.environ[name] for name in
                           ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA', 'USERPROFILE', 'TEMP', 'TMP') if name in os.environ}
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            parameters = StdioServerParameters(command=sys.executable,
                                              args=['-m', 'jianying_engine.mcp_server', '--state-dir', state],
                                              env=environment)
            async with Client(parameters) as client:
                self.assertIsNotNone(client.server_info)
                self.assertTrue(client.protocol_version)
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools},
                                 {'describe_operations', 'read_guide', 'start_job', 'job_status', 'read_job_output', 'cancel_job'})
                discovered = await client.call_tool('describe_operations', {})
                self.assertFalse(discovered.is_error)
                self.assertIn('doctor', str(discovered.structured_content['operations']))
                self.assertIn('admit-matting', discovered.structured_content['operations'])
                guide = await client.call_tool('read_guide', {'name': 'editing'})
                self.assertTrue(guide.structured_content['text'])
                self.assertFalse(guide.structured_content['truncated'])
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
                # The new operation reaches the existing external-review gate, never invents approval.
                from PIL import Image
                source = Path(state) / 'source.png'
                Image.new('RGB', (1280, 720), 'green').save(source)
                review = Path(state) / 'review.json'
                review.write_text('{"status":"failed"}', encoding='utf-8')
                output = Path(state) / 'must-not-exist.json'
                admission = (await client.call_tool('start_job', {'operation': 'admit-matting',
                    'arguments': ['--source', str(source), '--cache', state, '--custom-id',
                                  '00000000-0000-0000-0000-000000000000', '--review', str(review),
                                  '--out', str(output)]})).structured_content
                for _ in range(150):
                    rejected_review = (await client.call_tool('job_status', {'job_id': admission['job_id']})).structured_content
                    if rejected_review['finished'] is not None:
                        break
                    await asyncio.sleep(.1)
                self.assertEqual((rejected_review['state'], rejected_review['returncode']), ('failed', 1), rejected_review)
                self.assertIn('external UI quality review', rejected_review['stdout_tail'])
                self.assertFalse(output.exists())
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


if __name__ == '__main__':
    unittest.main()
