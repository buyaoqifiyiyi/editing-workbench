"""Pure open-source engine self-check covering runtime_core, media_prep, EffectCraft, FilmCraft, and open_engine CLI."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from PIL import Image

import craft_rpc
import effectcraft_backend
import file_hash
import filmcraft_backend
import media_prep
import open_engine
import runtime_core as core


class _FakeEffectCraft:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call_tool(self, name, params=None):
        params = params or {}
        self.calls.append((name, params))
        if name == 'list_fonts':
            return {'families': [{'family': 'Noto Sans SC'}]}
        if name == 'execute_command':
            if params.get('command') in ('layer.newText', 'layer.newSolid', 'layer.newShape'):
                return {'layer': f'L_{len(self.calls)}'}
            return {'ok': True}
        if name == 'save_project':
            Path(params['path']).write_text('{"comp": "Title"}', encoding='utf-8')
            return {'saved': True}
        if name == 'render_frame':
            img = Image.new('RGBA', (320, 180), (0, 0, 0, 0))
            img.save(params['path'])
            return {'path': params['path']}
        return {}


class _FakeFilmCraft:
    def __init__(self, calls, *, duplicate_mode=False):
        self.calls = calls
        self.duplicate_mode = duplicate_mode
        self.imported = []
        self.seq = {'video': [{'items': []}], 'audio': [{'items': []}], 'durationFrames': 60}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call_tool(self, name, params=None):
        params = params or {}
        self.calls.append((name, params))
        if name == 'media_import':
            for p in params['paths']:
                idx = len(self.imported) + 1
                self.imported.append((f'item_{idx}', Path(p).name))
            return {'imported': len(params['paths'])}
        if name == 'project_inspect':
            return {'root': {'children': [{'item': iid, 'name': n} for iid, n in self.imported]}}
        if name == 'sequence_inspect':
            return self.seq
        if name == 'render_frame':
            buf = io.BytesIO()
            Image.new('RGB', (320, 180), (20, 40, 80)).save(buf, format='PNG')
            return [{'type': 'image', 'data': base64.b64encode(buf.getvalue()).decode('ascii')}]
        if name == 'command_run':
            cid, p = params['id'], params.get('params', {})
            if cid == 'timeline.place':
                is_audio_only = p.get('video') is False
                vtrack = p.get('track', 'V1')
                if not is_audio_only:
                    idx = int(vtrack[1:]) - 1
                    while len(self.seq['video']) <= idx:
                        self.seq['video'].append({'items': []})
                    self.seq['video'][idx]['items'].append({
                        'clip': f'vc_{len(self.calls)}',
                        'start': round(p['seconds'] * core.TICKS_PER_SEC),
                        'duration': p['duration'],
                    })
                    if p.get('audio'):
                        a_idx = int(p.get('audioTrack', 'A1')[1:]) - 1
                        while len(self.seq['audio']) <= a_idx:
                            self.seq['audio'].append({'items': []})
                        self.seq['audio'][a_idx]['items'].append({
                            'clip': f'ac_linked_{len(self.calls)}',
                            'start': round(p['seconds'] * core.TICKS_PER_SEC),
                            'duration': p['duration'],
                        })
                else:
                    idx = int(vtrack[1:]) - 1
                    while len(self.seq['audio']) <= idx:
                        self.seq['audio'].append({'items': []})
                    self.seq['audio'][idx]['items'].append({
                        'clip': f'ac_{len(self.calls)}',
                        'start': round(p['seconds'] * core.TICKS_PER_SEC),
                        'duration': p['duration'],
                    })
            elif cid == 'graphics.newText':
                return {'clip': f'gx_{len(self.calls)}'}
            elif cid in ('file.saveAs', 'file.exportOtio', 'file.exportFcpxml'):
                Path(p['path']).write_text('{}', encoding='utf-8')
        return {}


class OpenEngineTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix='openedit-engine-check-')
        self.addCleanup(self.holder.cleanup)
        self.root = Path(self.holder.name)
        self.v1 = self.root / 'a' / 'clip.mp4'
        self.v2 = self.root / 'b' / 'clip.mp4'
        self.v1.parent.mkdir()
        self.v2.parent.mkdir()
        core.run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                  '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=2',
                  '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(self.v1)])
        core.run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                  '-f', 'lavfi', '-i', 'color=c=navy:s=320x180:r=30:d=2',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-an', str(self.v2)])
        self.audio = self.root / 'bgm.wav'
        with wave.open(str(self.audio), 'wb') as handle:
            handle.setparams((1, 2, 16000, 32000, 'NONE', 'not compressed'))
            handle.writeframes(b'\0\0' * 32000)

    def test_validate_plan_supports_4k60_and_enforces_safety(self):
        plan = {
            'schema_version': 2,
            'name': 'uhd-test',
            'canvas': {'width': 3840, 'height': 2160, 'fps': 60},
            'clips': [
                {'path': str(self.v1), 'start': 0, 'duration': 1.0, 'volume': 0.5,
                 'grading': {'exposure': 0.2, 'saturation': 115, 'luma_curve': [[0, 0], [0.5, 0.6], [1, 1]]}},
                {'path': str(self.v2), 'start': 1.0, 'duration': 1.0, 'transition': {'name': 'Cross Dissolve', 'duration': 0.25}},
            ],
            'audio': [{'path': str(self.audio), 'start': 0, 'duration': 2.0, 'track': 'bgm'}],
            'captions': [{'text': '高亮中文测试', 'start': 0.2, 'duration': 1.2, 'highlights': [{'start': 0, 'end': 2, 'color': '#FFD54F'}]}],
            'motion_graphics': [{'name': 'card', 'start': 0.1, 'duration': 1.0, 'layers': [{'type': 'text', 'text': '动效'}]}],
            'layer_order': ['bgm', 'video', 'overlay', 'captions'],
        }
        plan_path = self.root / 'plan.json'
        core.write_json(plan_path, plan)
        normalized, probes, warnings = core.validate_plan(plan_path)
        self.assertEqual(normalized['canvas'], {'width': 3840, 'height': 2160, 'fps': 60})
        self.assertEqual(len(normalized['overlays']), 1)
        self.assertIn(str(self.v1.resolve()), probes)
        self.assertEqual(warnings, [])

        # Refusals: boolean field coercion, NaN, same-track overlap, mixed track type, remote path, bad layer_order.
        bad_cases = [
            lambda p: p['canvas'].update(fps=True),
            lambda p: p['clips'][0].update(duration=float('nan')),
            lambda p: p['clips'][1].update(start=0.9),
            lambda p: p['audio'][0].update(track='video'),
            lambda p: p['clips'][0].update(path='https://example.invalid/v.mp4'),
            lambda p: p.update(layer_order=['video']),
        ]
        for idx, mutate in enumerate(bad_cases):
            broken = json.loads(json.dumps(plan))
            mutate(broken)
            bad_path = self.root / f'bad-{idx}.json'
            with bad_path.open('w', encoding='utf-8') as f:
                json.dump(broken, f, allow_nan=True)
            with self.assertRaises(ValueError, msg=f'case {idx}'):
                core.validate_plan(bad_path)

    def test_media_prep_inspect_reference_and_normalize(self):
        insp = media_prep.inspect_media(self.v1, self.root / 'inspect', frames=2)
        self.assertEqual(insp['count'], 1)
        self.assertEqual(len(insp['items'][0]['sample_frames']), 2)
        ref = media_prep.extract_reference(self.v1, self.root / 'ref', start=0.0, duration=1.5, max_frames=4)
        self.assertTrue(len(ref['frames']) >= 1)
        norm = media_prep.normalize_media(self.v1, self.root / 'norm.mp4', fps=30)
        self.assertEqual(norm['status'], 'passed')
        self.assertEqual(norm['probe']['width'], 320)

    def test_effectcraft_alpha_verification_and_non_alpha_refusal(self):
        ec_calls = []
        real_run = core.run

        def fake_ec_render(cmd, timeout=120):
            if Path(str(cmd[0])).name.startswith('effectcraft-cli'):
                out_mov = Path(cmd[cmd.index('--out') + 1])
                real_run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                          '-f', 'lavfi', '-i', 'color=c=red@0.5:s=320x180:r=30:d=0.5,format=rgba',
                          '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p12le', str(out_mov)])
                return type('Res', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()
            return real_run(cmd, timeout=timeout)

        spec = {'name': 'badge', 'duration': 0.5, 'width': 320, 'height': 180, 'fps': 30,
                'layers': [{'type': 'text', 'text': '中文叠层'}]}
        with patch('effectcraft_backend.find_cli', return_value='effectcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeEffectCraft(ec_calls)), \
                patch('runtime_core.run', side_effect=fake_ec_render):
            res = effectcraft_backend.build_overlay(spec, self.root / 'ov_ok', render_video=True)
        self.assertEqual(res['overlay_video']['pix_fmt'], 'yuva444p12le')

        def fake_no_alpha(cmd, timeout=120):
            if Path(str(cmd[0])).name.startswith('effectcraft-cli'):
                out_mov = Path(cmd[cmd.index('--out') + 1])
                real_run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                          '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=30:d=0.5',
                          '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(out_mov)])
                return type('Res', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()
            return real_run(cmd, timeout=timeout)

        with patch('effectcraft_backend.find_cli', return_value='effectcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeEffectCraft([])), \
                patch('runtime_core.run', side_effect=fake_no_alpha):
            with self.assertRaisesRegex(RuntimeError, 'lacks alpha'):
                effectcraft_backend.build_overlay(spec, self.root / 'ov_bad', render_video=True)

    def test_filmcraft_build_handles_duplicate_basenames_linked_gain_and_provenance(self):
        # Create valid provenance sidecar for v1.
        sidecar = self.v1.with_name(self.v1.name + '.provenance.json')
        core.write_json(sidecar, {
            'local_path': str(self.v1.resolve()),
            'license_verification': 'agent_review_recorded',
            'review_note': 'Synthetic fixture',
            'sha256': file_hash.sha256(self.v1),
        })
        plan = {
            'schema_version': 1,
            'name': 'fc-full',
            'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [
                {'path': str(self.v1), 'start': 0.0, 'duration': 1.0, 'volume': 0.5,
                 'transform': {'transform_x': 0.5, 'transform_y': -0.5}},
                {'path': str(self.v2), 'start': 1.0, 'source_in': 0.2, 'duration': 1.0,
                 'transition': {'name': 'Cross Dissolve', 'duration': 0.2},
                 'keyframes': [{'property': 'uniform_scale', 'time': 0.5, 'value': 1.2, 'interpolation': 'smoothstep'}]},
            ],
            'audio': [
                {'path': str(self.audio), 'start': 1.0, 'source_in': 0.25, 'duration': 1.0, 'track': 'bgm',
                 'keyframes': [{'time': 0.0, 'value': 1.0}, {'time': 0.5, 'value': 0.5}]},
            ],
            'captions': [{'text': '重点高亮', 'start': 0.2, 'duration': 0.8,
                          'highlights': [{'start': 0, 'end': 2, 'color': '#FFCC00'}],
                          'keyframes': [{'property': 'position_x', 'time': 0.0, 'value': -0.5},
                                        {'property': 'position_y', 'time': 0.0, 'value': 0.5},
                                        {'property': 'alpha', 'time': 0.4, 'value': 0.5}]}],
            'layer_order': ['bgm', 'video', 'captions'],
        }
        plan_path = self.root / 'fc_plan.json'
        core.write_json(plan_path, plan)
        normalized, probes, _ = core.validate_plan(plan_path)
        calls = []
        with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeFilmCraft(calls, duplicate_mode=True)):
            report = filmcraft_backend.build_project(
                normalized,
                self.root / 'out_proj',
                probes=probes,
                preview_times=[0.5],
                export_otio=self.root / 'out.otio',
                export_fcpxml=self.root / 'out.fcpxml',
            )
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(len(report['previews']), 1)
        self.assertTrue((self.root / 'out_proj' / '素材来源.json').is_file())
        # Verify duplicate basenames imported sequentially with distinct IDs -> item_1 and item_2 (plus item_3 audio).
        placed_items = [p['item'] for _, p in ((n, prm.get('params', {})) for n, prm in calls if n == 'command_run' and prm.get('id') == 'timeline.place')]
        self.assertEqual(placed_items, ['item_1', 'item_2', 'item_3'])
        # Verify linked A1 gain (~ -6.02 dB), timeline-clock setParam vs media-clock setInterpolation, & graphic keyframes.
        cmd_calls = [(prm['id'], prm.get('params', {})) for n, prm in calls if n == 'command_run']
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'volume' and abs(p.get('value', 0) + 6.02) < 0.05 for cid, p in cmd_calls))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'motion' and p.get('param') == 'scale'
                            and p.get('time') == filmcraft_backend._ticks(1.5) for cid, p in cmd_calls))
        self.assertTrue(any(cid == 'effects.setInterpolation' and p.get('effect') == 'motion' and p.get('param') == 'scale'
                            and p.get('mediaTime') == filmcraft_backend._ticks(0.7) for cid, p in cmd_calls))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'volume' and p.get('keyframe')
                            and p.get('time') == filmcraft_backend._ticks(1.5) for cid, p in cmd_calls))
        self.assertTrue(any(cid == 'graphics.set' and p.get('keyframe') and p['props'].get('position') == [80.0, 45.0] for cid, p in cmd_calls))

        # Tampered provenance rejects cleanly before creating target dir.
        core.write_json(sidecar, {'local_path': str(self.v1), 'license_verification': 'agent_review_recorded', 'review_note': 'bad', 'sha256': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            filmcraft_backend.build_project(normalized, self.root / 'tampered_out', probes=probes)
        self.assertFalse((self.root / 'tampered_out').exists())

    def test_craft_rpc_helpers_and_cli_exit_codes(self):
        self.assertIsNone(craft_rpc.extract_png_bytes([{'type': 'text', 'text': 'hi'}]))
        self.assertEqual(open_engine.main(['capabilities']), 0)
        self.assertEqual(open_engine.main(['validate', '--plan', str(self.root / 'missing.json')]), 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OpenEngineTests)
    res = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {
        'status': 'passed' if res.wasSuccessful() else 'failed',
        'tests': res.testsRun,
        'failures': len(res.failures),
        'errors': len(res.errors),
        'log': stream.getvalue(),
    }
    core.write_json(args.out / 'open-engine-check.json', report)
    core.emit({'status': report['status'], 'tests': report['tests'], 'failures': report['failures'], 'errors': report['errors']})
    return 0 if res.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
