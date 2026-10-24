"""Fresh stdio MCP -> native media preparation/analysis fixture -> both backend plans -> real draft."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters
from jianying_engine import api
from jianying_engine.workbench import Workbench
import test_music_edit as fixture


class MusicEditFlow(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_mcp_music_pipeline_and_workbench_route(self):
        sys.path.insert(0, str(api.RUNTIME / 'scripts'))
        import music_edit
        await asyncio.to_thread(fixture.MusicEditTests.setUpClass)
        self.addCleanup(fixture.MusicEditTests.tearDownClass)
        with tempfile.TemporaryDirectory(prefix='music-edit-mcp-') as temporary:
            root = Path(temporary)
            request = root / 'request.json'
            request.write_text(json.dumps(fixture.MusicEditTests.request), encoding='utf-8')
            environment = {name: os.environ[name] for name in
                           ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA', 'USERPROFILE', 'TEMP', 'TMP') if name in os.environ}
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            environment['PYTHONIOENCODING'] = 'utf-8'
            parameters = StdioServerParameters(command=sys.executable,
                                              args=['-m', 'jianying_engine.mcp_server', '--state-dir', str(root / 'jobs')],
                                              env=environment)
            async with Client(parameters) as client:
                found = await client.call_tool('describe_operations', {})
                self.assertIn('music-edit', found.structured_content['operations'])
                guide = await client.call_tool('read_guide', {'name': 'reference/music-edit'})
                self.assertIn('OpenAI', guide.structured_content['text'])
                self.assertIn('Gemini', guide.structured_content['text'])

                async def job(operation, args):
                    start = (await client.call_tool('start_job', {'operation': operation, 'arguments': args})).structured_content
                    self.assertTrue(start.get('job_id'), start)
                    async with asyncio.timeout(120):
                        while True:
                            status = (await client.call_tool('job_status', {'job_id': start['job_id']})).structured_content
                            if status['state'] not in ('running', 'starting'):
                                break
                            await asyncio.sleep(.05)
                    self.assertEqual(status['state'], 'succeeded', status)
                    output = await client.call_tool('read_job_output', {'job_id': start['job_id']})
                    return json.loads(output.structured_content['text'])

                ready = await job('music-edit', ['--config', str(root / 'absent.json'), 'doctor'])
                self.assertFalse(ready['ready'])
                self.assertFalse(ready['network_called'])
                prepared = await job('music-edit', ['prepare', '--request', str(request), '--out', str(root / 'bundle')])
                self.assertFalse(prepared['network_called'])
                config = root / 'omni.json'
                config.write_text(json.dumps({'enabled': True, 'base_url': 'https://example.invalid/v1',
                                               'api_key_env': 'MUSIC_EDIT_TEST_KEY', 'protocol': 'openai-compatible',
                                               'model': 'fixture/av-model', 'request_options': {}}), encoding='utf-8')
                response = {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(fixture.MusicEditTests.analysis)}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
                # Only the provider reply is a fixture; codecs, MCP jobs, validators and draft builder are real.
                with patch.dict(os.environ, {'MUSIC_EDIT_TEST_KEY': 'fixture-only-key'}), \
                        patch.object(music_edit, 'send_payload', return_value=response):
                    await asyncio.to_thread(music_edit.analyze, root / 'bundle', config)
                result = await job('beats', ['--input', str(root / 'bundle/media/music.wav'), '--duration', '4', '--out', str(root / 'beats')])
                self.assertEqual(result['status'], 'succeeded')
                for backend in ('resolve', 'jianying'):
                    result = await job('music-edit', ['compile', '--bundle', str(root / 'bundle'), '--backend', backend,
                                                      '--out', str(root / backend), '--beats', str(root / 'beats'),
                                                      '--width', '320', '--height', '180'])
                    self.assertEqual(result['status'], 'plan_validated')
                built = await job('build', ['--plan', str(root / 'jianying/edit-plan.json'), '--out', str(root / 'draft')])
                self.assertTrue((root / 'draft/draft_content.json').is_file())
                self.assertIsInstance(built, dict)
            workbench = Workbench(root / 'state', root)
            instructions = workbench.instructions()
            self.assertIn('music-edit', instructions)
            self.assertIn('reference/music-edit', workbench.feedback_instructions())
            await workbench.close()


if __name__ == '__main__':
    unittest.main()
