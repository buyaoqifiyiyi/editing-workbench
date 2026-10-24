"""UI API -> owned ACP peer -> real MCP engine -> editable draft -> artifact preview."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from PIL import Image
from jianying_engine.workbench import Workbench, create_app
from jianying_engine import api
from test_workbench import available_backends


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class EngineWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_actual_engine_from_agent_protocol_to_readable_deliverables(self):
        with tempfile.TemporaryDirectory(prefix='engine-flow-') as folder:
            root = Path(folder)
            work = root / 'work'
            source_dir = work / 'incoming'
            source_dir.mkdir(parents=True)
            source = source_dir / 'shot.png'
            Image.new('RGB', (320, 240), '#639b46').save(source)
            sys.path.insert(0, str(api.RUNTIME / 'scripts'))
            import editor
            video = source_dir / 'reverse-source.mov'
            editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                        '-f', 'lavfi', '-i', "nullsrc=s=320x240:r=30:d=2,geq=lum='20+N*3':cb=128:cr=128",
                        '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000:duration=2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', video])
            original_video = hashlib.sha256(video.read_bytes()).hexdigest()
            subtitle = source_dir / 'corrected.srt'
            subtitle.write_text('1\n00:00:00,100 --> 00:00:00,400\n人工校正字幕\n第二行\n\n2\n00:00:00,600 --> 00:00:00,900\n第二个镜头\n', encoding='utf-8-sig')
            original_subtitle = subtitle.read_bytes()
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            service = Workbench(root / 'state', work)
            app = create_app(service, 'token', 8765)
            fixture = Path(__file__).with_name('fixtures') / 'acp_engine_fixture.py'
            try:
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://127.0.0.1:8765',
                                             headers={'X-Workbench-Token': 'token'}) as client:
                    for action, body in [('connect', {'agent_id': 'custom-acp', 'cli_path': sys.executable, 'args': ['-u', str(fixture)]}),
                                         ('session/prepare', {}), ('media', {'paths': [str(source_dir)]}),
                                         ('send', {'text': '通过本机引擎创建1秒倒放草稿及中文多行字幕；只用于集成测试。'})]:
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
                    self.assertTrue(any('build' in event['label'] and event['status'] == 'completed' for event in state['current_session']['events']))
                    self.assertTrue(any('reverse' in event['label'] and event['status'] == 'completed' for event in state['current_session']['events']))
                    self.assertTrue(any('denoise-audio' in event['label'] and event['status'] == 'completed' for event in state['current_session']['events']))
                    self.assertTrue(any('tracking-apply' in event['label'] and event['status'] == 'completed' for event in state['current_session']['events']))
                    self.assertTrue(any(item['name'] == 'draft_content.json' for item in state['artifacts']))
                    self.assertEqual(len([item for item in state['artifacts'] if item['category']=='deliverable']),2)
                    report = next(item for item in state['artifacts'] if item['name'] == '交付说明.md')
                    preview = (await client.get(report['url'])).json()['text']
                    self.assertIn('未做剪映UI', preview)
                    content = json.loads((work / 'output/draft/draft_content.json').read_text(encoding='utf-8'))
                    segments = [segment for track in content['tracks'] for segment in track['segments']]
                    video_segments = next(track for track in content['tracks'] if track['type'] == 'video')['segments']
                    self.assertEqual(sum(segment['target_timerange']['duration'] for segment in video_segments), 1_000_000)
                    captions = next(track for track in content['tracks'] if track.get('name') == 'captions')
                    self.assertEqual([segment['target_timerange'] for segment in captions['segments']],
                                     [{'start': 100000, 'duration': 300000}, {'start': 600000, 'duration': 300000}])
                    text = next(item for item in content['materials']['texts']
                                if item['id'] == captions['segments'][0]['material_id'])
                    self.assertEqual(json.loads(text['content'])['text'], '人工校正字幕\n第二行')
                    words = next(track for track in content['tracks'] if track.get('name') == 'words')
                    texts = {item['id']: json.loads(item['content'])['text'] for item in content['materials']['texts']}
                    self.assertEqual([(segment['target_timerange']['start'], segment['target_timerange']['duration'],
                                       texts[segment['material_id']]) for segment in words['segments']],
                                     [(100000, 100000, '合成'), (200000, 200000, '词测试'),
                                      (600000, 100000, '第二'), (700000, 200000, '镜头')])
                    self.assertEqual(subtitle.read_bytes(), original_subtitle)
                    label = next(track for track in content['tracks'] if track.get('name') == 'follow-label')['segments'][0]
                    self.assertEqual(texts[label['material_id']], '跟随标签')
                    self.assertEqual(label['target_timerange'], {'start': 0, 'duration': 500000})
                    keys = {group['property_type']: group['keyframe_list'] for group in label['common_keyframes']}
                    self.assertEqual([key['time_offset'] for key in keys['KFTypePositionX']], [0, 250000, 499999])
                    self.assertEqual(keys['KFTypePositionX'][0]['values'], [-.5])
                    self.assertEqual(keys['KFTypePositionY'][0]['values'], [.25])
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
                    reversed_media = work / 'output/reverse/reversed.mov'
                    self.assertTrue(reversed_media.is_file())
                    self.assertTrue(all(Path(item['path']) == reversed_media for item in content['materials']['videos']))
                    self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), original_video)
                    cleaned = work / 'output/denoise/denoised.wav'
                    audio_segment = next(track for track in content['tracks'] if track['type'] == 'audio')['segments'][0]
                    material = next(item for item in content['materials']['audios'] if item['id'] == audio_segment['material_id'])
                    self.assertEqual(Path(material['path']), cleaned)
                    self.assertEqual(audio_segment['source_timerange'], {'start': 0, 'duration': 1000000})
                    self.assertEqual(audio_segment['target_timerange'], {'start': 0, 'duration': 1000000})
                    self.assertTrue(all(segment['volume'] == 0 for segment in video_segments))
                    self.assertFalse(content['materials'].get('realtime_denoises'))
                    denoise_report = json.loads((work / 'output/denoise/denoise-report.json').read_text(encoding='utf-8'))
                    self.assertEqual(denoise_report['status'], 'passed')
                    self.assertEqual(denoise_report['denoised']['frames'], 48000)
                    import native_mask_feather
                    mask = content['materials']['common_mask'][0]
                    self.assertEqual(Path(mask['path']), work / 'output/draft' / native_mask_feather.RELATIVE)
                    self.assertEqual(mask['config']['feather'], .2)
                    native_mask_feather.verify_directory(work / 'output/draft')
            finally:
                await service.close()


if __name__ == '__main__':
    unittest.main()
