"""HTTP -> persisted standalone choice -> owned ACP process -> MCP -> audio deliverable."""
from array import array
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import httpx2

from jianying_engine.workbench import Workbench, create_app
from test_workbench import available_backends


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class StandaloneBackendFlow(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_local_audio_produces_only_audio_and_note_deliverables(self):
        with tempfile.TemporaryDirectory(prefix='standalone-audio-') as folder:
            root = Path(folder)
            work = root / 'work'
            source = work / 'incoming/source.wav'
            source.parent.mkdir(parents=True)
            samples = array('h')
            for index in range(48000):
                samples.extend((round(6000 * math.sin(2 * math.pi * 330 * index / 48000)),
                                round(4000 * math.sin(2 * math.pi * 660 * index / 48000))))
            if sys.byteorder != 'little':
                samples.byteswap()
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
                audio.writeframes(samples.tobytes())
            original = source.read_bytes()
            selection = {'mode': 'single', 'roles': {'editing': [], 'effects': [], 'color': [], 'audio': []},
                         'single_backend': 'jianying'}
            service = Workbench(root / 'state', work)
            fixture = Path(__file__).with_name('fixtures') / 'acp_audio_backend_fixture.py'
            try:
                app = create_app(service, 'token', 8765)
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                             base_url='http://127.0.0.1:8765',
                                             headers={'X-Workbench-Token': 'token'}) as client:
                    for action, body in [
                        ('backends/configure', {'selection': selection}),
                        ('connect', {'agent_id': 'custom-acp', 'cli_path': sys.executable, 'args': ['-u', str(fixture)]}),
                        ('session/prepare', {}), ('media', {'paths': [str(source)]}),
                        ('send', {'text': '仅处理这段1秒音频：noise-floor -40、reduction 12；单独生成降噪音频，不创建视频工程。'})]:
                        response = await client.post('/api/' + action, json=body)
                        self.assertEqual(response.status_code, 200, response.text)
                    async with asyncio.timeout(45):
                        while True:
                            state = (await client.get('/api/state')).json()
                            if state['current_session']['status'] == 'error':
                                self.fail(state['current_session']['error'])
                            if state['current_session']['status'] == 'idle' and state['artifacts']:
                                break
                            await asyncio.sleep(.02)
                    self.assertEqual(state['backends']['selection'], selection)
                    self.assertEqual(state['current_session']['backend_selection'], selection)
                    events = state['current_session']['events']
                    self.assertTrue(any('denoise-audio' in item['label'] and item['status'] == 'completed' for item in events))
                    self.assertFalse(any('build' in item['label'] for item in events))
                    delivered = [item for item in state['artifacts'] if item['category'] == 'deliverable']
                    self.assertEqual({item['name'] for item in delivered}, {'denoised.wav', '交付说明.md'})
                    audio_entry = next(item for item in delivered if item['name'] == 'denoised.wav')
                    self.assertEqual((audio_entry['type'], audio_entry['preview_kind']), ('audio', 'audio'))
                    audio_response = await client.get(audio_entry['url'])
                    self.assertEqual(audio_response.status_code, 200)
                    self.assertEqual(audio_response.content, (work / 'output/denoise/denoised.wav').read_bytes())
                    note_entry = next(item for item in delivered if item['name'] == '交付说明.md')
                    note = (await client.get(note_entry['url'])).json()['text']
                    self.assertIn('未做试听或音质验收', note)
                    self.assertFalse(any(item['type'] in ('video', 'draft') for item in state['artifacts']))
                self.assertEqual(source.read_bytes(), original)
                report = json.loads((work / 'output/denoise/denoise-report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['source_sha256'], hashlib.sha256(original).hexdigest())
                self.assertFalse(report['source_modified'])
                self.assertEqual((report['source_in'], report['duration'], report['first_sample'], report['sample_count']), (0, 1, 0, 48000))
                self.assertEqual((report['params']['noise_floor_db'], report['params']['reduction_db']), (-40, 12))
                for key, filename, hash_key in [('dry', 'dry.wav', 'comparison_sha256'),
                                                ('denoised', 'denoised.wav', 'output_sha256')]:
                    output = work / 'output/denoise' / filename
                    self.assertEqual(report[hash_key], hashlib.sha256(output.read_bytes()).hexdigest())
                    self.assertEqual((report[key]['sample_rate'], report[key]['channels'], report[key]['sample_width'], report[key]['frames']), (48000, 2, 2, 48000))
                    with wave.open(str(output), 'rb') as audio:
                        self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()), (48000, 2, 2, 48000))
                        if key == 'dry':
                            self.assertEqual(audio.readframes(48000), samples.tobytes())
                proof = json.loads((work / 'output/protocol-proof.json').read_text(encoding='utf-8'))
                self.assertEqual(proof['operations_started'], ['denoise-audio'])
                self.assertEqual((proof['job']['state'], proof['job']['returncode']), ('succeeded', 0))
                self.assertIn('本次为单独调用：剪映专业版', proof['prompt'])
                self.assertIn('工作台内部工具', proof['prompt'])
                self.assertNotIn('resolve-workflow', proof['prompt'])
                self.assertNotIn('adobe-workflow', proof['prompt'])
                self.assertFalse(any(path.suffix.lower() in ('.mp4', '.mov', '.drp', '.aep', '.blend', '.otio', '.prproj', '.kdenlive')
                                     or path.name in ('edit-plan.json', 'draft_content.json') for path in work.rglob('*')))
                persisted = json.loads(service.file.read_text(encoding='utf-8'))
                self.assertEqual(persisted['backend_default'], selection)
                await service.close()
                restored = Workbench(root / 'state', work)
                try:
                    self.assertEqual(restored.state()['backends']['selection'], selection)
                    self.assertEqual(restored.current()['backend_selection'], selection)
                    self.assertEqual({item['name'] for item in restored.artifacts if item['category'] == 'deliverable'},
                                     {'denoised.wav', '交付说明.md'})
                finally:
                    await restored.close()
            finally:
                await service.close()


if __name__ == '__main__':
    unittest.main()
