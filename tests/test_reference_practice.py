"""Real local media checks for bounded reference extraction and practice comparison."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters
import numpy as np
from PIL import Image


def invoke(*args, env=None):
    return subprocess.run([sys.executable, '-I', '-m', 'openedit_engine', *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, env=env)


def synthetic(path):
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-n',
               '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=2',
               '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=2',
               '-f', 'lavfi', '-i', 'color=c=white:s=320x180:r=30:d=2',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
               '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
               '-map', '[v]', '-map', '3:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=60)


class ReferenceAndPracticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oe-reference-practice-')
        self.root = Path(self.temp.name)
        self.video = self.root / '来源-三镜头.mp4'
        synthetic(self.video)
        self.original = hashlib.sha256(self.video.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_reference_extraction_uses_bounded_window_and_preserves_source(self):
        out = self.root / 'ref'
        result = invoke('reference', '--input', self.video, '--out', out,
                        '--start', '1', '--duration', '4', '--max-frames', '4', '--threshold', '0.2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((out / 'reference.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'passed')
        self.assertEqual((report['start'], report['window']), (1.0, 4.0))
        self.assertTrue(report['frames'])
        for frame in report['frames']:
            self.assertTrue(Path(frame).is_file())
            self.assertTrue(Path(frame).resolve().is_relative_to(out.resolve()))
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), self.original)

    def test_reference_rejects_bad_threshold_before_creating_output(self):
        out = self.root / 'invalid'
        result = invoke('reference', '--input', self.video, '--out', out, '--threshold', 'nan')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_real_comparison_preserves_side_assignment_audio_and_inputs(self):
        out = self.root / 'comparison'
        args = ('practice-compare', '--left', self.video, '--right', self.video,
                '--left-start', '0', '--right-start', '2', '--duration', '1',
                '--audio', 'right', '--goal', '颜色对照', '--change', '两段不同颜色', '--out', out)
        result = invoke(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((out / 'comparison.json').read_text(encoding='utf-8'))
        self.assertEqual((report['frames'], report['audio']), (15, 'right'))
        self.assertEqual(report['human_watching'], 'not_performed')
        self.assertEqual(report['same_source_or_music'], 'not_verified')
        self.assertEqual(report['left']['sha256'], self.original)
        frame = self.root / 'comparison.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(out / 'comparison.mp4'),
                        '-frames:v', '1', str(frame)], check=True, capture_output=True)
        image = Image.open(frame).convert('RGB')
        l, r = image.getpixel((160, 90)), image.getpixel((480, 90))
        self.assertGreater(l[0], l[2] + 150)
        self.assertGreater(r[2], r[0] + 150)
        before = (out / 'comparison.mp4').read_bytes()
        self.assertNotEqual(invoke(*args).returncode, 0)
        self.assertEqual((out / 'comparison.mp4').read_bytes(), before)
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), self.original)

    def test_rejects_out_of_range_before_creating_output(self):
        out = self.root / 'bad-range'
        result = invoke('practice-compare', '--left', self.video, '--right', self.video,
                        '--left-start', '5.5', '--duration', '2', '--audio', 'none',
                        '--goal', 'test', '--change', 'test', '--out', out)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_audio_side_is_selected_not_mixed(self):
        other = self.root / '880hz.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(self.video),
                        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=6',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
                        '-shortest', str(other)], check=True, capture_output=True)
        for side, expected in [('left', 440), ('right', 880)]:
            out = self.root / ('audio-' + side)
            result = invoke('practice-compare', '--left', self.video, '--right', other,
                            '--duration', '1', '--audio', side, '--goal', '音轨选择',
                            '--change', '不同音频', '--out', out)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(out / 'comparison.mp4'),
                                      '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-'],
                                     check=True, capture_output=True).stdout
            samples = np.frombuffer(decoded, dtype='<i2').astype(float)
            spectrum = np.abs(np.fft.rfft(samples))
            frequency = np.fft.rfftfreq(len(samples), d=1 / 16000)[spectrum.argmax()]
            self.assertLess(abs(frequency - expected), 3, (side, frequency))


class PracticeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_routes_comparison_and_reference_jobs(self):
        with tempfile.TemporaryDirectory(prefix='oe-practice-mcp-') as tmp:
            root = Path(tmp)
            video = root / 'source.mp4'
            synthetic(video)
            environment = {name: os.environ[name] for name in
                           ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA', 'USERPROFILE', 'TEMP', 'TMP')
                           if name in os.environ}
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'openedit_engine.mcp_server', '--state-dir', str(root / 'state')],
                env=environment)
            async with Client(params) as client:
                discovered = (await client.call_tool('describe_operations', {})).structured_content
                self.assertIn('practice-compare', discovered['operations'])
                started = (await client.call_tool('start_job', {'operation': 'practice-compare',
                    'arguments': ['--left', str(video), '--right', str(video), '--duration', '1',
                                  '--audio', 'none', '--goal', '对照', '--change', '相同素材',
                                  '--out', str(root / 'out')], 'timeout_seconds': 90})).structured_content
                job_id = started['job_id']
                for _ in range(300):
                    status = (await client.call_tool('job_status', {'job_id': job_id})).structured_content
                    if status['finished'] is not None:
                        break
                    await asyncio.sleep(.1)
                self.assertEqual((status['state'], status['returncode']), ('succeeded', 0), status)
                self.assertTrue((root / 'out' / 'comparison.mp4').is_file())
                report = json.loads((root / 'out' / 'comparison.json').read_text(encoding='utf-8'))
                self.assertEqual(report['human_watching'], 'not_performed')


if __name__ == '__main__':
    unittest.main()
