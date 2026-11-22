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
import overlay_recipes
import runtime_core as core


class _FakeEffectCraft:
    def __init__(self, calls):
        self.calls = calls
        self.size = (320, 180)

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
            cmd = params.get('command')
            p = params.get('params', {})
            if cmd == 'comp.new':
                self.size = (int(p.get('width', 320)), int(p.get('height', 180)))
            elif cmd == 'file.import':
                return {'items': ['item_1'], 'errors': []}
            elif cmd in ('layer.newText', 'layer.newSolid', 'layer.newShape', 'layer.newCamera', 'layer.addItem'):
                return {'layer': f'L_{len(self.calls)}'}
            elif cmd == 'layer.addMask':
                return {'mask': 1}
            return {'ok': True}
        if name == 'save_project':
            Path(params['path']).write_text('{"comp": "Title"}', encoding='utf-8')
            return {'saved': True}
        if name == 'render_frame':
            img = Image.new('RGBA', self.size, (0, 0, 0, 0))
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
            elif cid == 'clip.speedDuration':
                factor = float(p.get('speed', 100.0)) / 100.0
                targets = set(p.get('clips') or [])
                for sec in ('video', 'audio'):
                    for track in self.seq.get(sec, []):
                        for item in track.get('items', []):
                            if item.get('clip') in targets:
                                item['speed'] = factor
                                item['duration'] = round(int(item['duration']) / factor)
            elif cid == 'timeline.trim':
                if p.get('edge') == 'out':
                    target = p.get('clip')
                    delta = int(p.get('delta', 0))
                    for sec in ('video', 'audio'):
                        for track in self.seq.get(sec, []):
                            for item in track.get('items', []):
                                if item.get('clip') == target:
                                    item['duration'] = int(item['duration']) + delta
            elif cid == 'graphics.newText':
                return {'clip': f'gx_{len(self.calls)}'}
            elif cid == 'masks.add':
                return {'clip': p.get('clip'), 'effect': p.get('effect', 'opacity'), 'mask': 0, 'name': 'Mask (1)'}
            elif cid == 'clip.nest':
                return {'clips': [f'nest_{len(self.calls)}'], 'sequence': 99}
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
                {'path': str(self.v2), 'start': 1.0, 'duration': 1.0,
                 'transition': {'name': 'whip', 'duration': 0.2, 'align': 'center', 'reverse': True, 'params': {'direction': 1, 'blur': 85.0}}},
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

        # Refusals: boolean field coercion, NaN, same-track overlap, mixed track type, remote path, bad layer_order, bad transition fields.
        bad_cases = [
            lambda p: p['canvas'].update(fps=True),
            lambda p: p['clips'][0].update(duration=float('nan')),
            lambda p: p['clips'][1].update(start=0.9),
            lambda p: p['audio'][0].update(track='video'),
            lambda p: p['clips'][0].update(path='https://example.invalid/v.mp4'),
            lambda p: p.update(layer_order=['video']),
            lambda p: p['clips'][1]['transition'].update(align='bogus'),
            lambda p: p['clips'][1]['transition'].update(reverse='yes'),
            lambda p: p['clips'][1]['transition'].update(params='bad'),
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
                 'transition': {'name': 'whip', 'duration': 0.2, 'align': 'center', 'reverse': True, 'params': {'direction': 1, 'blur': 85.0}},
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
        self.assertTrue(any(
            cid == 'sequence.applyVideoTransition' and p.get('effect') == 'whip' and p.get('align') == 'center'
            and p.get('reverse') is True and p.get('params') == {'direction': 1, 'blur': 85.0}
            for cid, p in cmd_calls
        ))

        # Tampered provenance rejects cleanly before creating target dir.
        core.write_json(sidecar, {'local_path': str(self.v1), 'license_verification': 'agent_review_recorded', 'review_note': 'bad', 'sha256': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            filmcraft_backend.build_project(normalized, self.root / 'tampered_out', probes=probes)
        self.assertFalse((self.root / 'tampered_out').exists())

    def test_craft_rpc_helpers_and_cli_exit_codes(self):
        self.assertIsNone(craft_rpc.extract_png_bytes([{'type': 'text', 'text': 'hi'}]))
        self.assertEqual(open_engine.main(['capabilities']), 0)
        self.assertEqual(open_engine.main(['validate', '--plan', str(self.root / 'missing.json')]), 1)

    def test_direction_a_validation_and_compilation_contracts(self):
        # 640x480 foreground clip to exercise fit scale composition alongside 1920x1080 canvas.
        fg = self.root / 'fg640.mp4'
        core.run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                  '-f', 'lavfi', '-i', 'color=c=0x00C832:s=640x480:r=30:d=2',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(fg)])
        plan = {
            'schema_version': 2,
            'name': 'dir-a-full',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [
                {
                    'path': str(self.v1),
                    'start': 0.0,
                    'duration': 2.0,
                    'track': 'video',
                    'loudness': {'target_lufs': -16.0, 'type': 'dialogue'},
                    'filters': [{'name': 'gaussian_blur', 'params': {'blurriness': 2.0}}],
                    'grading': {
                        'saturation': 112,
                        'creative_sat': 105,
                        'sharpen': 25,
                        'faded_film': 12,
                        'look_intensity': 80,
                        'vignette': -1.5,
                        'vignette_midpoint': 45,
                        'hue_vs_sat': [[0.0, 0.5], [0.5, 0.7], [1.0, 0.5]],
                        'wheels': {'midtones': [0.1, -0.05], 'highlights_l': 8.0},
                        'red_hsl': {'saturation': 20, 'lightness': 15, 'hue_shift': -5},
                        'local_circle': {'center_x': 0.0, 'center_y': 0.0, 'radius': 240.0, 'feather': 25.0},
                    },
                },
                {
                    'path': str(fg),
                    'start': 0.2,
                    'duration': 1.5,
                    'track': 'pip',
                    'fit': 'contain',
                    'transform': {'scale_x': 0.5, 'scale_y': 0.5, 'flip_horizontal': True, 'flip_vertical': True},
                    'keyframes': [{'property': 'uniform_scale', 'time': 0.5, 'value': 0.8}],
                    'chroma': {'method': 'ultra_key', 'key_color': '#00C832', 'tolerance': 35, 'choke': 10, 'soften': 15},
                    'mask': {
                        'shape': 'rectangle',
                        'center': [960, 540],
                        'size': [800, 600],
                        'feather': 0.0,
                        'opacity': 0.85,
                        'inverted': False,
                        'keyframes': [
                            {'time': 0.0, 'feather': 0.0},
                            {'time': 0.8, 'center': [1000, 540], 'size': [600, 450], 'feather': 12.0},
                        ],
                    },
                },
            ],
            'audio': [
                {
                    'path': str(self.audio),
                    'start': 0.0,
                    'duration': 2.0,
                    'track': 'bgm',
                    'fade_in': 0.25,
                    'fade_out': 0.3,
                    'loudness': -18.0,
                    'beat_marks': [0.25, 0.75, 1.5],
                },
            ],
            'composites': [
                {
                    'name': 'PipGroup',
                    'tracks': ['pip'],
                    'start': 0.0,
                    'duration': 2.0,
                    'transform': {'rotation': 8.0, 'alpha': 0.9},
                    'grading': {'contrast': 10},
                },
            ],
        }
        p_path = self.root / 'dir_a.json'
        core.write_json(p_path, plan)
        normalized, probes, warnings = core.validate_plan(p_path)
        self.assertEqual(warnings, [])

        calls = []
        with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeFilmCraft(calls)):
            report = filmcraft_backend.build_project(normalized, self.root / 'dir_a_out', probes=probes)
        self.assertEqual(report['status'], 'passed')
        cmds = [(prm['id'], prm.get('params', {})) for n, prm in calls if n == 'command_run']

        # Fit + flip + composed keyframe scale (640x480 in 1920x1080 contain fit ratio = 2.25)
        self.assertTrue(any(cid == 'clip.fitToFrame' for cid, _ in cmds))
        self.assertTrue(any(cid == 'effects.apply' and p.get('effect') == 'horizontal_flip' for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.apply' and p.get('effect') == 'vertical_flip' for cid, p in cmds))
        self.assertTrue(any(
            cid == 'effects.setParam' and p.get('effect') == 'motion' and p.get('param') == 'scale'
            and p.get('keyframe') and abs(p.get('value', 0.0) - 0.8 * 2.25 * 100.0) < 1e-3
            for cid, p in cmds
        ))

        # Chroma + static & animated rectangle mask with explicit feather=0 override & 4-vertex keyframe path
        self.assertTrue(any(cid == 'effects.apply' and p.get('effect') == 'ultra_key' for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'ultra_key' and p.get('param') == 'key_color' for cid, p in cmds))
        self.assertTrue(any(cid == 'masks.add' and p.get('shape') == 'rectangle' for cid, p in cmds))
        self.assertTrue(any(cid == 'masks.set' and p.get('feather') == 0.0 and p.get('opacity') == 85.0 for cid, p in cmds))
        self.assertTrue(any(
            cid == 'effects.setParam' and p.get('mask') == 0 and p.get('keyframe')
            and p.get('param') == 'path' and p.get('time') == filmcraft_backend._ticks(1.0)
            and p.get('value') == [[700.0, 315.0], [1300.0, 315.0], [1300.0, 765.0], [700.0, 765.0]]
            for cid, p in cmds
        ))
        self.assertTrue(any(
            cid == 'effects.setParam' and p.get('mask') == 0 and p.get('keyframe')
            and p.get('param') == 'feather' and p.get('time') == filmcraft_backend._ticks(1.0)
            and p.get('value') == 12.0
            for cid, p in cmds
        ))

        # Lumetri saturation vs creative_sat, sharpen, wheels, red_hsl->hsl_sat/hue_vs_luma, local_circle
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'saturation' and p.get('value') == 112 for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'sharpen' and p.get('value') == 25 for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'vignette_amount' and p.get('value') == -1.5 for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'wheel_midtones' and p.get('value') == [0.1, -0.05] for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'hsl_sat' and p.get('value') == 120.0 for cid, p in cmds))
        self.assertTrue(any(cid == 'effects.setParam' and p.get('effect') == 'lumetri' and p.get('param') == 'hue_vs_luma' for cid, p in cmds))
        self.assertTrue(any(cid == 'masks.add' and p.get('effect') == 'lumetri' and p.get('shape') == 'ellipse' for cid, p in cmds))

        # Filters + audio transitions + essentialSound setType->autoMatch ordering + beat markers + composite nest
        self.assertTrue(any(cid == 'effects.apply' and p.get('effect') == 'gaussian_blur' for cid, p in cmds))
        self.assertTrue(any(cid == 'sequence.applyAudioTransition' and p.get('edge') == 'in' for cid, p in cmds))
        self.assertTrue(any(cid == 'sequence.applyAudioTransition' and p.get('edge') == 'out' for cid, p in cmds))
        es_seq = [cid for cid, _ in cmds if cid.startswith('essentialSound.')]
        self.assertEqual(es_seq, ['essentialSound.setType', 'essentialSound.autoMatch', 'essentialSound.setType', 'essentialSound.autoMatch'])
        self.assertEqual(sum(1 for cid, _ in cmds if cid == 'markers.add'), 3)
        self.assertTrue(any(cid == 'clip.nest' and p.get('name') == 'PipGroup' for cid, p in cmds))

        # Silent/no-audio clip with loudness produces a clear warning
        warn_plan = json.loads(json.dumps(plan))
        warn_plan['clips'][0].update(path=str(self.v2), volume=0.0)
        wp = self.root / 'warn_loudness.json'
        core.write_json(wp, warn_plan)
        _, _, w_loud = core.validate_plan(wp)
        self.assertTrue(any('loudness' in w and 'no active audio' in w for w in w_loud))

        # Validation error cases across all 6 new groups
        invalid_cases = [
            lambda d: d['clips'][1]['mask'].update(shape='polygon', size=[10, 10]),  # missing path for polygon
            lambda d: d['clips'][1]['chroma'].update(method='invalid_key'),
            lambda d: d['audio'][0].update(fade_in=1.5, fade_out=1.0),
            lambda d: d['audio'][0].update(beat_marks=[1.0, 0.5]),
            lambda d: d['composites'][0].update(tracks=['missing_track']),
            lambda d: d['clips'][0]['grading'].update(primary_wheel={'shadows': [0.0, 0.0]}),
            lambda d: d['clips'][0]['grading'].update(green_hsl={'saturation': 10}),
            lambda d: d['clips'][0]['grading'].update(hue_vs_luma=[[0.0, 0.5], [1.0, 0.5]]),
            lambda d: d['clips'][0]['grading'].update(vignette=-18.0),
            lambda d: d['clips'][0]['grading']['local_circle'].update(bogus=1),
        ]
        for idx, mutate in enumerate(invalid_cases):
            broken = json.loads(json.dumps(plan))
            mutate(broken)
            bp = self.root / f'bad_dir_a_{idx}.json'
            core.write_json(bp, broken)
            with self.assertRaises(ValueError, msg=f'dir-a invalid case {idx}'):
                core.validate_plan(bp)

    def test_overlay_recipes_validation_expansion_and_mock_build(self):
        recipes_meta = overlay_recipes.list_recipes()
        self.assertEqual(
            set(recipes_meta),
            {
                'title_card', 'lower_third', 'keyword_callout', 'beat_flash',
                'cinematic_bars', 'progress_banner', 'chapter_opener',
                'pip_card', 'split_screen_frame', 'wipe_transition',
            },
        )
        sample_specs = [
            ('title_card', (1920, 1080), {
                'name': 'r_title', 'duration': 2.0, 'recipe': 'title_card',
                'params': {'title': '开源引擎标题', 'subtitle': '副标题验证', 'highlights': [{'start': 0, 'end': 4, 'color': '#FACC15'}]},
            }),
            ('lower_third', (1080, 1920), {
                'name': 'r_lt', 'duration': 2.0, 'recipe': 'lower_third',
                'params': {'title': '主讲人', 'subtitle': '竖屏下三分之一', 'align': 'right'},
            }),
            ('keyword_callout', (1920, 1080), {
                'name': 'r_kw', 'duration': 1.2, 'recipe': 'keyword_callout',
                'params': {'text': '核心突破点', 'highlights': [{'start': 0, 'end': 2, 'color': '#38BDF8'}]},
            }),
            ('beat_flash', (1920, 1080), {
                'name': 'r_flash', 'duration': 0.5, 'recipe': 'beat_flash',
                'params': {'peak_time': 0.15, 'peak_opacity': 90, 'blend_mode': 'Screen'},
            }),
            ('cinematic_bars', (1080, 1920), {
                'name': 'r_bars', 'duration': 1.8, 'recipe': 'cinematic_bars',
                'params': {'bar_ratio': 0.12, 'animate_in': 0.3, 'animate_out': 0.3},
            }),
            ('progress_banner', (1920, 1080), {
                'name': 'r_prog', 'duration': 2.0, 'recipe': 'progress_banner',
                'params': {'label': '第一章 架构总览', 'progress_start': 0.1, 'progress_end': 0.9, 'position': 'top'},
            }),
            ('chapter_opener', (1920, 1080), {
                'name': 'r_chap', 'duration': 2.2, 'recipe': 'chapter_opener',
                'params': {'badge': '02', 'title': '核心特性深度拆解', 'subtitle': '10-bit 保护与批量制备', 'highlights': [{'start': 0, 'end': 4, 'color': '#FACC15'}]},
            }),
            ('pip_card', (1080, 1920), {
                'name': 'r_pip', 'duration': 2.0, 'recipe': 'pip_card',
                'params': {'title': '实机演示', 'corner': 'top_right', 'width_ratio': 0.42},
            }),
            ('split_screen_frame', (1920, 1080), {
                'name': 'r_split', 'duration': 2.0, 'recipe': 'split_screen_frame',
                'params': {'layout': 'vertical_3', 'labels': ['原片', '抠像', '成片']},
            }),
            ('wipe_transition', (1920, 1080), {
                'name': 'r_wipe', 'duration': 1.0, 'recipe': 'wipe_transition',
                'params': {'direction': 'left', 'angle_offset': 10.0, 'peak_hold': 0.15},
            }),
        ]
        all_calls = []
        for idx, (rname, (w, h), spec) in enumerate(sample_specs):
            ec_calls = []
            with patch('effectcraft_backend.find_cli', return_value='effectcraft-cli'), \
                    patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeEffectCraft(ec_calls)):
                res = effectcraft_backend.build_overlay(spec, self.root / f'recipe_{idx}_{rname}', width=w, height=h, fps=30, render_video=False)
            self.assertEqual(res['status'], 'passed')
            self.assertEqual(res.get('recipe'), rname)
            self.assertEqual((res['width'], res['height']), (w, h))
            all_calls.extend(ec_calls)

            # Verify add_effect precedes any add_keyframe targeting effects/#...
            add_eff_indices = [i for i, (tn, _) in enumerate(ec_calls) if tn == 'add_effect']
            eff_kf_indices = [i for i, (tn, pr) in enumerate(ec_calls) if tn == 'add_keyframe' and str(pr.get('path', '')).startswith('effects/')]
            if eff_kf_indices:
                self.assertTrue(add_eff_indices and min(add_eff_indices) < min(eff_kf_indices), f'{rname}: effect must precede effect keyframe')

        exec_cmds = [(pr.get('command'), pr.get('params', {})) for tn, pr in all_calls if tn == 'execute_command']
        self.assertTrue(any(c == 'layer.newShape' and p.get('kind') == 'rounded' for c, p in exec_cmds))
        self.assertTrue(any(c == 'layer.newText' and 'position' in p and 'font' in p for c, p in exec_cmds))
        self.assertTrue(any(c == 'layer.setText' and p.get('range') == [0, 4] for c, p in exec_cmds))
        self.assertTrue(any(c == 'layer.applyTextPreset' and p.get('preset') == 'fadeUpCharacters' for c, p in exec_cmds))
        self.assertTrue(any(c == 'layer.style.dropShadow' for c, _ in exec_cmds))
        self.assertTrue(any(c == 'layer.setBlendMode' and p.get('mode') == 'Screen' for c, p in exec_cmds))

        # Validate plan accepts overlays[].recipe and rejects invalid recipe payloads
        valid_plan = {
            'schema_version': 2,
            'name': 'recipe-plan',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': str(self.v2), 'start': 0.0, 'duration': 2.0}],
            'overlays': [{'name': 'hero', 'start': 0.0, 'duration': 1.5, 'recipe': 'title_card', 'params': {'title': '配方测试'}}],
        }
        vp = self.root / 'recipe_plan.json'
        core.write_json(vp, valid_plan)
        norm_p, _, _ = core.validate_plan(vp)
        self.assertEqual(norm_p['overlays'][0]['recipe'], 'title_card')

        invalid_overlay_cases = [
            lambda d: d['overlays'][0].update(recipe='nonexistent_recipe'),
            lambda d: d['overlays'][0]['params'].pop('title'),
            lambda d: d['overlays'][0]['params'].update(bg_opacity=150),
            lambda d: d['overlays'][0].update(layers=[{'type': 'text', 'text': 'conflict'}]),
            lambda d: d['overlays'][0].update(recipe='pip_card', params={'corner': 'center'}),
            lambda d: d['overlays'][0].update(recipe='split_screen_frame', params={'layout': 'vertical_2', 'labels': ['A', 'B', 'C']}),
            lambda d: d['overlays'][0].update(recipe='wipe_transition', params={'angle_offset': 60}),
        ]
        for idx, mutate in enumerate(invalid_overlay_cases):
            broken = json.loads(json.dumps(valid_plan))
            mutate(broken)
            bp = self.root / f'bad_recipe_{idx}.json'
            core.write_json(bp, broken)
            with self.assertRaises(ValueError, msg=f'recipe invalid case {idx}'):
                core.validate_plan(bp)

    def test_frame_snapped_keyframe_and_nested_interpolation_timing(self):
        snapped_start = 1545264000000  # frame 365 @ 60fps for start=6.083s
        snapped_nest_start = 1545264000000

        class _SnappedFilmCraft(_FakeFilmCraft):
            def call_tool(self, name, params=None):
                res = super().call_tool(name, params)
                if name == 'command_run' and (params or {}).get('id') == 'timeline.place':
                    p = params.get('params', {})
                    if p.get('video') is not False:
                        self.seq['video'][-1]['items'][-1].update({
                            'start': snapped_start, 'sourceIn': 25401600000, 'speed': 1.25,
                        })
                elif name == 'command_run' and (params or {}).get('id') == 'clip.nest':
                    nid = res['clips'][0]
                    self.seq['video'][0]['items'].append({
                        'clip': nid, 'start': snapped_nest_start, 'sourceIn': 0, 'speed': 1.0,
                    })
                return res

        plan = {
            'schema_version': 2,
            'name': 'snap-timing',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 60},
            'clips': [{
                'path': str(self.v2), 'start': 6.083, 'duration': 1.0, 'source_in': 0.1, 'speed': 1.25, 'track': 'split_top',
                'keyframes': [{'property': 'uniform_scale', 'time': 0.2, 'value': 1.1, 'interpolation': 'easeOut'}],
                'mask': {'shape': 'rectangle', 'center': [960, 540], 'size': [400, 400], 'keyframes': [{'time': 0.0, 'feather': 0.0}]},
            }],
            'composites': [{
                'name': 'SnapNest', 'tracks': ['split_top'], 'start': 6.083, 'duration': 1.0,
                'keyframes': [{'property': 'uniform_scale', 'time': 0.0, 'value': 1.0, 'interpolation': 'easeOut'}],
            }],
        }
        p_path = self.root / 'snap.json'
        core.write_json(p_path, plan)
        norm, probes, _ = core.validate_plan(p_path)
        calls = []
        with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _SnappedFilmCraft(calls)):
            report = filmcraft_backend.build_project(norm, self.root / 'snap_out', probes=probes)
        self.assertEqual(report['status'], 'passed')
        cmds = [(prm['id'], prm.get('params', {})) for n, prm in calls if n == 'command_run']
        # Nested composite keyframe at t_local=0 must use snapped_nest_start for setParam AND mediaTime=0 for setInterpolation.
        self.assertTrue(any(
            cid == 'effects.setParam' and str(p.get('clip', '')).startswith('nest_')
            and p.get('param') == 'scale' and p.get('time') == snapped_nest_start
            for cid, p in cmds
        ))
        self.assertTrue(any(
            cid == 'effects.setInterpolation' and str(p.get('clip', '')).startswith('nest_')
            and p.get('mediaTime') == 0 and p.get('interpolation') == 'easeOut'
            for cid, p in cmds
        ))
        # Speed-scaled clip keyframe & mask keyframe at snapped_start must stay aligned with sourceIn + local*speed.
        expected_media = round(25401600000 + filmcraft_backend._ticks(0.2) * 1.25)
        self.assertTrue(any(
            cid == 'effects.setInterpolation' and not str(p.get('clip', '')).startswith('nest_')
            and p.get('mediaTime') == expected_media
            for cid, p in cmds
        ))
        self.assertTrue(any(
            cid == 'effects.setParam' and p.get('mask') == 0 and p.get('time') == snapped_start
            for cid, p in cmds
        ))

    def test_hi10p_h264_detection_warning_and_build_refusal(self):
        self.assertTrue(core.is_filmcraft_hi10p({'kind': 'video', 'video_codec': 'h264', 'pix_fmt': 'yuv420p10le'}))
        self.assertFalse(core.is_filmcraft_hi10p({'kind': 'video', 'video_codec': 'h264', 'pix_fmt': 'yuv420p'}))
        self.assertFalse(core.is_filmcraft_hi10p({'kind': 'video', 'video_codec': 'prores', 'pix_fmt': 'yuva444p12le'}))

        hi10p = self.root / 'anime_hi10p.mp4'
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=s=64x64:r=24:d=1',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le', str(hi10p),
        ])
        insp = media_prep.inspect_media(hi10p, self.root / 'inspect_hi10p', frames=1)
        self.assertTrue(insp['items'][0]['needs_normalization'])
        self.assertIn('h264_10bit_hi10p_unsupported_by_filmcraft', insp['items'][0]['warnings'])

        plan_path = self.root / 'hi10p_plan.json'
        core.write_json(plan_path, {
            'schema_version': 2,
            'name': 'hi10p-guard',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': str(hi10p), 'start': 0.0, 'duration': 0.8}],
        })
        norm, probes, warnings = core.validate_plan(plan_path)
        self.assertTrue(any('10-bit H.264' in w and 'normalize-media' in w for w in warnings))
        with self.assertRaisesRegex(ValueError, 'normalize-media'):
            filmcraft_backend.build_project(norm, self.root / 'hi10p_out', probes=probes)
        self.assertFalse((self.root / 'hi10p_out').exists())

    def test_batch_normalize_media_skips_compliant_and_converts_hi10p(self):
        batch_src = self.root / 'batch_src'
        nested = batch_src / 'sub'
        nested.mkdir(parents=True)
        good_clip = batch_src / 'ok_cfr.mp4'
        bad_clip = nested / 'hi10p.mp4'
        core.run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                  '-f', 'lavfi', '-i', 'color=c=blue:s=128x96:r=30:d=0.5',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(good_clip)])
        core.run([core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                  '-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=24:d=0.5',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le', str(bad_clip)])

        report = media_prep.normalize_media(batch_src, self.root / 'batch_out', fps=30)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['mode'], 'batch')
        self.assertEqual(report['summary'], {'scanned': 2, 'normalized': 1, 'skipped_compliant': 1})
        self.assertEqual(len(report['path_map']), 1)
        out_clip = Path(report['path_map'][str(bad_clip.resolve())])
        self.assertEqual(core.probe(out_clip)['pix_fmt'], 'yuv420p')
        self.assertTrue((self.root / 'batch_out' / 'prepare-report.json').is_file())

        forced = media_prep.normalize_media(batch_src, self.root / 'batch_forced', fps=30, force=True)
        self.assertEqual(forced['summary'], {'scanned': 2, 'normalized': 2, 'skipped_compliant': 0})

    def test_speed_duration_preserves_timeline_length(self):
        plan = {
            'schema_version': 2,
            'name': 'speed-trim-check',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 60},
            'clips': [
                {'path': str(self.v1), 'start': 0.0, 'duration': 0.25, 'source_in': 0.1, 'speed': 1.4},
                {'path': str(self.v2), 'start': 0.25, 'duration': 1.0, 'source_in': 0.2, 'speed': 0.5},
            ],
        }
        p_path = self.root / 'speed_trim.json'
        core.write_json(p_path, plan)
        norm, probes, _ = core.validate_plan(p_path)
        calls = []
        holder = {}

        class _CaptureFake(_FakeFilmCraft):
            def __enter__(self):
                holder['seq'] = self.seq
                return self

        with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _CaptureFake(calls)):
            report = filmcraft_backend.build_project(norm, self.root / 'speed_trim_out', probes=probes)
        self.assertEqual(report['status'], 'passed')
        cmds = [(prm['id'], prm.get('params', {})) for n, prm in calls if n == 'command_run']
        self.assertTrue(any(cid == 'timeline.trim' and p.get('edge') == 'out' for cid, p in cmds))
        placed_items = holder['seq']['video'][0]['items']
        self.assertEqual(len(placed_items), 2)
        self.assertLessEqual(abs(placed_items[0]['duration'] - filmcraft_backend._ticks(0.25)), 1000)
        self.assertLessEqual(abs(placed_items[1]['duration'] - filmcraft_backend._ticks(1.0)), 1000)

    def test_effectcraft_3d_camera_footage_timing_and_time_remap(self):
        spec = {
            'name': 'scene3d_test',
            'duration': 1.2,
            'width': 1920,
            'height': 1080,
            'fps': 60,
            'motion_blur': True,
            'layers': [
                {
                    'type': 'footage',
                    'name': 'hero',
                    'path': str(self.v1),
                    'time': 0.1,
                    'in': 0.05,
                    'out': 0.95,
                    'duration': 0.9,
                    'three_d': True,
                    'motion_blur': True,
                    'position': [960, 540, 240],
                    'time_remap': True,
                    'keyframes': [
                        {'path': 'timeRemap', 'keys': [{'time': 0.0, 'value': 0.0}, {'time': 0.8, 'value': 0.9}], 'interpolation': 'bezier'},
                    ],
                },
                {
                    'type': 'camera',
                    'name': 'cam1',
                    'camera_type': 'twoNode',
                    'preset': '35mm',
                    'position': [960, 540, -1400],
                    'poi': [960, 540, 0],
                    'keyframes': [
                        {'path': 'transform/position', 'keys': [{'time': 0.0, 'value': [960, 540, -1400]}, {'time': 1.0, 'value': [1020, 520, -1180]}]},
                        {'path': 'cameraOptions/zoom', 'keys': [{'time': 0.0, 'value': 1860.0}, {'time': 1.0, 'value': 2100.0}]},
                    ],
                },
            ],
        }
        calls = []
        with patch('effectcraft_backend.find_cli', return_value='effectcraft-cli'), \
                patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeEffectCraft(calls)):
            rep = effectcraft_backend.build_overlay(spec, self.root / 'ec_3d_out', render_video=False, use_cache=False)
        self.assertEqual(rep['status'], 'passed')

        cmds = [(n, p.get('command'), p.get('params', {}), p) for n, p in calls]
        self.assertTrue(any(n == 'execute_command' and c == 'comp.setSwitch' and cp == {'switch': 'motionBlur', 'value': True} for n, c, cp, _ in cmds))
        add_idx = next(i for i, (n, c, cp, _) in enumerate(cmds) if n == 'execute_command' and c == 'layer.addItem')
        self.assertEqual(cmds[add_idx][2]['time'], 0.1)
        self.assertEqual(cmds[add_idx][2]['out'], 0.95)
        self.assertNotIn('position', cmds[add_idx][2])

        sw3d_idx = next(i for i, (n, c, cp, _) in enumerate(cmds) if n == 'execute_command' and c == 'layer.setSwitch' and cp.get('switch') == 'threeD')
        pos_idx = next(i for i, (n, _, _, raw) in enumerate(cmds) if n == 'set_property' and raw.get('path') == 'transform/position')
        self.assertLess(sw3d_idx, pos_idx)
        self.assertEqual(cmds[pos_idx][3]['value'], [960, 540, 240])

        tr_en_idx = next(i for i, (n, c, cp, _) in enumerate(cmds) if n == 'execute_command' and c == 'layer.enableTimeRemap' and cp.get('value') is True)
        tr_kf_idx = next(i for i, (n, _, _, raw) in enumerate(cmds) if n == 'add_keyframe' and raw.get('path') == 'timeRemap')
        self.assertLess(tr_en_idx, tr_kf_idx)
        self.assertTrue(any(n == 'execute_command' and c == 'layer.newCamera' and cp.get('type') == 'twoNode' and cp.get('poi') == [960, 540, 0] for n, c, cp, _ in cmds))
        self.assertTrue(any(n == 'add_keyframe' and raw.get('path') == 'cameraOptions/zoom' for n, _, _, raw in cmds))

        k_on = effectcraft_backend._overlay_cache_key(__file__, spec, width=1920, height=1080, fps=60, duration=1.2, render_video=True, preview_time=0.6, gpu=False)
        k_off = effectcraft_backend._overlay_cache_key(__file__, {**spec, 'motion_blur': False}, width=1920, height=1080, fps=60, duration=1.2, render_video=True, preview_time=0.6, gpu=False)
        self.assertNotEqual(k_on, k_off)


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
