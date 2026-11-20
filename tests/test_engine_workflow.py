"""Real headless MCP pipeline: reverse -> denoise-audio -> captions -> tracking-apply -> FilmCraft build -> artifacts."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters
from PIL import Image
from openedit_engine import api, artifacts


class EngineWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_pipeline_to_filmcraft_fcproj_and_artifact_discovery(self):
        with tempfile.TemporaryDirectory(prefix='openedit-workflow-') as folder:
            root = Path(folder)
            work = root / 'work'
            source_dir = work / 'incoming'
            source_dir.mkdir(parents=True)
            scripts_dir = str(api.RUNTIME / 'scripts')
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
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

            environment = {name: os.environ[name] for name in
                           ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA', 'USERPROFILE', 'TEMP', 'TMP') if name in os.environ}
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            environment['PYTHONIOENCODING'] = 'utf-8'
            params = StdioServerParameters(
                command=sys.executable,
                args=['-m', 'openedit_engine.mcp_server', '--state-dir', str(root / 'jobs')],
                env=environment,
            )
            async with Client(params) as client:
                async def run_job(op, args):
                    started = (await client.call_tool('start_job', {'operation': op, 'arguments': args})).structured_content
                    self.assertTrue(started.get('job_id'), started)
                    async with asyncio.timeout(120):
                        while True:
                            st = (await client.call_tool('job_status', {'job_id': started['job_id']})).structured_content
                            if st['state'] not in ('running', 'starting'):
                                break
                            await asyncio.sleep(0.05)
                    self.assertEqual((st['state'], st['returncode']), ('succeeded', 0), st)
                    return json.loads((await client.call_tool('read_job_output', {'job_id': started['job_id']})).structured_content['text'])

                rev = await run_job('reverse', ['--input', str(video), '--start', '0', '--duration', '1',
                                                '--audio', 'reverse', '--out', str(work / 'output/reverse')])
                self.assertEqual(rev['status'], 'passed')
                reversed_media = Path(rev['output_media'])
                self.assertTrue(reversed_media.is_file())

                denoise = await run_job('denoise-audio', ['--input', str(reversed_media), '--start', '0',
                                                          '--duration', '1', '--noise-floor', '-40', '--reduction', '12',
                                                          '--out', str(work / 'output/denoise')])
                self.assertEqual(denoise['status'], 'passed')
                cleaned = Path(denoise['output_media'])

                marker = source_dir / 'marker.png'
                Image.new('RGB', (32, 32), '#FF4400').save(marker)
                base_plan = work / 'output/plan-base.json'
                base_plan.write_text(json.dumps({
                    'schema_version': 2,
                    'name': 'workflow-demo',
                    'canvas': {'width': 320, 'height': 240, 'fps': 30},
                    'clips': [{'path': str(reversed_media), 'start': 0.0, 'duration': 1.0, 'volume': 0.0}],
                    'audio': [{'path': str(cleaned), 'track': 'voice', 'start': 0.0, 'duration': 1.0, 'volume': 1.0}],
                    'captions': [{'text': '跟随标签', 'start': 0.0, 'duration': 1.0, 'x': 0, 'y': 0,
                                  'border': False, 'track': 'follow-label', 'style': {'auto_wrapping': False}}],
                }, ensure_ascii=False, indent=2), encoding='utf-8')

                cap_plan = work / 'output/plan-captions.json'
                cap_res = await run_job('captions', [str(base_plan), '--srt', str(subtitle), '--timebase', 'timeline',
                                                     '--index', '0', '--out', str(cap_plan)])
                self.assertEqual(cap_res['imported_captions'], 2)

                tracking_json = work / 'output/tracking.json'
                tracking_json.write_text(json.dumps({
                    'schema_version': 1, 'status': 'tracked', 'method': 'opencv_csrt',
                    'source': {'path': str(reversed_media.resolve()), 'bytes': reversed_media.stat().st_size,
                               'sha256': hashlib.sha256(reversed_media.read_bytes()).hexdigest(),
                               'width': 320, 'height': 240},
                    'coverage_start': 0.0, 'coverage_end': 1.0, 'lost_at': None, 'boundaries': [],
                    'samples': [{'time': 0.0, 'bbox': [70, 80, 20, 20]}, {'time': 0.5, 'bbox': [150, 110, 20, 20]}],
                }), encoding='utf-8')
                final_plan = work / 'output/plan-final.json'
                trk_res = await run_job('tracking-apply', [str(cap_plan), '--tracking', str(tracking_json),
                                                           '--source-index', '0', '--caption-index', '0',
                                                           '--out', str(final_plan)])
                self.assertEqual(trk_res['status'], 'passed')

                built = await run_job('build', ['--plan', str(final_plan), '--out', str(work / 'output/project')])
                self.assertEqual(built['status'], 'passed')
                self.assertTrue((work / 'output/project/workflow-demo.fcproj').is_file())
                self.assertTrue((work / 'output/project/剪辑交付.md').is_file())
                self.assertTrue((work / 'output/project/素材来源.json').is_file())

                (work / 'workbench-delivery.json').write_text(json.dumps({
                    'schema_version': 1,
                    'items': [
                        {'path': 'output/project/workflow-demo.fcproj', 'title': 'FilmCraft可编辑工程'},
                        {'path': 'output/project/剪辑交付.md', 'title': '交付说明'},
                    ],
                }, ensure_ascii=False), encoding='utf-8')
                discovered = artifacts.discover(work)
                deliverables = [d for d in discovered if d['category'] == 'deliverable']
                self.assertEqual(len(deliverables), 2)
                self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), original_video)
                self.assertEqual(subtitle.read_bytes(), original_subtitle)


if __name__ == '__main__':
    unittest.main()
