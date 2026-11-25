import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from openedit_engine import api


class EngineInterfaceTests(unittest.TestCase):
    def test_rejects_non_operations_and_invalid_tokens(self):
        for operation in ('python', '../editor.py', 'doctor; echo unsafe', []):
            with self.assertRaises(ValueError):
                api.command_for(operation, [])
        for tokens in ('--help', [None], ['x\0y']):
            with self.assertRaises(ValueError):
                api.command_for('doctor', tokens)
        with self.assertRaises(ValueError):
            api.read_guide('../../config.env')

    def test_local_resources_are_complete(self):
        for name in api.operations():
            command = api.command_for(name, ['--help'])
            self.assertEqual(command[0], sys.executable)
            self.assertTrue(Path(command[1]).is_file(), name)
        for name in ('reference/styles/anime-mix', 'reference/music-edit'):
            self.assertIn(name, api.paths()['guides'])
            self.assertTrue(api.read_guide(name).strip(), name)

    def test_cli_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            good = subprocess.run([sys.executable, '-m', 'openedit_engine', 'doctor'], cwd=directory,
                                  text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            result = json.loads(good.stdout)
            self.assertEqual(Path(result['runtime_root']), api.RUNTIME)
            self.assertEqual(set(result['backends']), {'filmcraft', 'effectcraft', 'ffmpeg'})
            self.assertIsInstance(result.get('mesh_avatar'), dict)
            bad = subprocess.run([sys.executable, '-m', 'openedit_engine', '../editor.py'], cwd=directory,
                                 text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(json.loads(bad.stderr)['status'], 'failed')

    def test_operation_schemas_and_arguments_from_params(self):
        schemas = api.operation_schemas()
        self.assertTrue(set(schemas).issubset(api.operations()))
        for name in ('validate', 'build', 'preview', 'export', 'render-overlay',
                     'filmcraft-exec', 'effectcraft-exec', 'captions', 'music-edit', 'ducking',
                     'layout-tracks', 'denoise-audio', 'reverse', 'migrate', 'transcribe',
                     'separate', 'tracking', 'tracking-apply', 'auto-cut', 'matting',
                     'auto-reframe', 'rough-cut', 'audio-master', 'sfx', 'sfx-search',
                     'music-structure', 'motion-analyze', 'interpolate', 'depth',
                     'shot-search', 'shader-fx', 'stock', 'mesh-avatar', 'audit', 'bootstrap'):
            self.assertIn(name, schemas)
        self.assertEqual(set(schemas), set(api.operations()))
        self.assertEqual(len(schemas), 47)
        self.assertEqual(api.operation_schemas('validate'), schemas['validate'])
        with self.assertRaises(ValueError):
            api.operation_schemas('nonexistent-op')

        cap_tokens = api.arguments_from_params('captions', {
            'plan': 'p.json', 'srt': 's.srt', 'timebase': 'timeline', 'index': [0, 1], 'out': 'out.json',
        })
        self.assertEqual(cap_tokens[0], 'p.json')
        self.assertEqual(cap_tokens[cap_tokens.index('--index') + 1:cap_tokens.index('--index') + 3], ['0', '1'])
        self.assertTrue(api.command_for('captions', cap_tokens))

        duck_tokens = api.arguments_from_params('ducking', {
            'plan': 'p.json', 'vocals': 'v.wav', 'out': 'd.json', 'duck_db': -12,
        })
        self.assertEqual(duck_tokens[0], 'p.json')
        self.assertIn('--duck-db=-12', duck_tokens)

        me_tokens = api.arguments_from_params('music-edit', {
            'config': 'cfg.json', 'subcommand': 'compile', 'bundle': 'b',
            'out': 'o.json', 'backend': 'filmcraft', 'width': 1920,
        })
        self.assertEqual(me_tokens, ['--config', 'cfg.json', 'compile', '--bundle', 'b',
                                     '--out', 'o.json', '--backend', 'filmcraft', '--width', '1920'])

        valid_cases = [
            ('layout-tracks', {'plan': 'p.json', 'out': 'out.json'},
             ['--plan', 'p.json', '--out', 'out.json']),
            ('denoise-audio', {'input': 'in.wav', 'duration': 2.5, 'noise_floor': -45, 'reduction': 12, 'out': 'o'},
             ['--input', 'in.wav', '--duration', '2.5', '--noise-floor=-45', '--reduction', '12', '--out', 'o']),
            ('reverse', {'input': 'a.mp4', 'duration': 1.0, 'audio': 'mute', 'out': 'o'},
             ['--input', 'a.mp4', '--duration', '1.0', '--audio', 'mute', '--out', 'o']),
            ('migrate', {'plan': 'p.json', 'map': ['D:/a=E:/a', 'D:/b=E:/b'], 'out': 'm.json'},
             ['--plan', 'p.json', '--map', 'D:/a=E:/a', '--map', 'D:/b=E:/b', '--out', 'm.json']),
            ('transcribe', {'input': 'a.wav', 'out': 'o', 'compute_type': 'int8', 'word_timestamps': True},
             ['--input', 'a.wav', '--out', 'o', '--compute-type', 'int8', '--word-timestamps']),
            ('separate', {'input': 'a.wav', 'out': 'o', 'stems': 'pair', 'overlap': 0.25},
             ['--input', 'a.wav', '--out', 'o', '--stems', 'pair', '--overlap', '0.25']),
            ('tracking', {'input': 'a.mp4', 'bbox': [10, 20, 100, 120], 'duration': 2.0, 'out': 'o'},
             ['--input', 'a.mp4', '--bbox', '10', '20', '100', '120', '--duration', '2.0', '--out', 'o']),
            ('tracking-apply', {'plan': 'p.json', 'tracking': 't.json', 'source_index': 0,
                                'caption_index': 0, 'offset_x': -12.5, 'out': 'o.json'},
             ['p.json', '--tracking', 't.json', '--source-index', '0', '--caption-index', '0', '--offset-x=-12.5', '--out', 'o.json']),
            ('stock', {'subcommand': 'search', 'provider': 'openverse', 'kind': 'audio', 'query': 'rain', 'limit': 5, 'out': 's.json'},
             ['search', '--provider', 'openverse', '--kind', 'audio', '--query', 'rain', '--limit', '5', '--out', 's.json']),
            ('mesh-avatar', {'subcommand': 'attach', 'plan': 'p.json', 'video': 'v.mov', 'out': 'o.json', 'start': 1.25},
             ['attach', '--plan', 'p.json', '--video', 'v.mov', '--out', 'o.json', '--start', '1.25']),
            ('reference', {'input': 'r.mp4', 'out': 'o', 'detector': 'pyscenedetect', 'content_threshold': 27, 'min_scene_seconds': 0.3},
             ['--input', 'r.mp4', '--out', 'o', '--detector', 'pyscenedetect', '--content-threshold', '27', '--min-scene-seconds', '0.3']),
            ('build', {'plan': 'p.json', 'out': 'o', 'no_overlay_cache': True},
             ['--plan', 'p.json', '--out', 'o', '--no-overlay-cache']),
            ('render-overlay', {'spec': 's.json', 'out': 'o', 'no_cache': True},
             ['--spec', 's.json', '--out', 'o', '--no-cache']),
            ('normalize-media', {'input': 'clips_dir', 'out': 'prep_out', 'force': True, 'max_files': 50},
             ['--input', 'clips_dir', '--out', 'prep_out', '--force', '--max-files', '50']),
            ('auto-cut', {'input': 'talk.mp4', 'out': 'ac_out', 'detector': 'silero', 'threshold': 0.5, 'min_silence': 0.4, 'gpu': True},
             ['--input', 'talk.mp4', '--out', 'ac_out', '--detector', 'silero', '--threshold', '0.5', '--min-silence', '0.4', '--gpu']),
            ('matting', {'input': 'fg.mp4', 'out': 'm_out', 'backend': 'birefnet', 'alpha_feather': 0.5, 'alpha_erode': -1, 'keep_audio': True, 'device': 'cuda'},
             ['--input', 'fg.mp4', '--out', 'm_out', '--backend', 'birefnet', '--alpha-feather', '0.5', '--alpha-erode=-1', '--keep-audio', '--device', 'cuda']),
            ('matting', {'input': 'fg.mp4', 'out': 'm2_out', 'backend': 'matanyone2', 'mask': 'seed.png', 'max_size': 720, 'warmup': 6, 'decontaminate': True, 'gpu': True},
             ['--input', 'fg.mp4', '--out', 'm2_out', '--backend', 'matanyone2', '--mask', 'seed.png', '--max-size', '720', '--warmup', '6', '--decontaminate', '--gpu']),
            ('auto-reframe', {'input': 'w.mp4', 'out': 'rf_out', 'aspect': '9:16', 'detector': 'yunet', 'smoothing': 0.6},
             ['--input', 'w.mp4', '--out', 'rf_out', '--aspect', '9:16', '--detector', 'yunet', '--smoothing', '0.6']),
            ('rough-cut', {'input': 't.mp4', 'out': 'rc_out', 'language': 'zh', 'gpu': True, 'karaoke_mode': 'active'},
             ['--input', 't.mp4', '--out', 'rc_out', '--language', 'zh', '--gpu', '--karaoke-mode', 'active']),
            ('audio-master', {'input': 'a.wav', 'out': 'am_out', 'preset': 'podcast', 'sample_rate': 48000, 'reverb_ir': 'plate', 'reverb_wet': 0.25, 'sidechain': 'vox.wav', 'sidechain_ratio': 8.0},
             ['--input', 'a.wav', '--out', 'am_out', '--preset', 'podcast', '--sample-rate', '48000', '--reverb-ir', 'plate', '--reverb-wet', '0.25', '--sidechain', 'vox.wav', '--sidechain-ratio', '8.0']),
            ('music-structure', {'input': 'bgm.mp3', 'out': 'ms_out', 'backend': 'beat-this', 'start': 10.0, 'duration': 30.0, 'gpu': True},
             ['--input', 'bgm.mp3', '--out', 'ms_out', '--backend', 'beat-this', '--start', '10.0', '--duration', '30.0', '--gpu']),
            ('sfx-search', {'library': 'sfx_dir', 'query': 'cinematic whoosh', 'out': 'ss_out', 'top_k': 3, 'backend': 'clap', 'gpu': True},
             ['--library', 'sfx_dir', '--query', 'cinematic whoosh', '--out', 'ss_out', '--top-k', '3', '--backend', 'clap', '--gpu']),
            ('sfx', {'cues': 'cues.json', 'duration': 32.2, 'out': 'sfx_out', 'sample_rate': 48000, 'duck_out': 'duck.json'},
             ['--cues', 'cues.json', '--duration', '32.2', '--out', 'sfx_out', '--sample-rate', '48000', '--duck-out', 'duck.json']),
            ('sfx', {'from_structure': 'struct_dir', 'out': 'sfx_st_out'},
             ['--from-structure', 'struct_dir', '--out', 'sfx_st_out']),
            ('beat-apply', {'plan': 'p.json', 'beats': 'struct_dir', 'every': 1, 'use_downbeats': True, 'out': 'ba_out.json'},
             ['--plan', 'p.json', '--beats', 'struct_dir', '--every', '1', '--use-downbeats', '--out', 'ba_out.json']),
            ('motion-analyze', {'out': 'lut_out', 'make_lut': 'agx_punchy', 'lut_size': 33},
             ['--out', 'lut_out', '--make-lut', 'agx_punchy', '--lut-size', '33']),
            ('motion-analyze', {'input': 'clips_dir', 'out': 'mf_out', 'backend': 'neuflow', 'gpu': True, 'make_lut': 'steel_violet'},
             ['--input', 'clips_dir', '--out', 'mf_out', '--backend', 'neuflow', '--gpu', '--make-lut', 'steel_violet']),
            ('interpolate', {'input': 'a.mp4', 'out': 'in_out', 'target_fps': 60, 'slow_factor': 0.5, 'model': 'rife-v4.6', 'backend': 'rife', 'deband': True, 'gpu_id': 0},
             ['--input', 'a.mp4', '--out', 'in_out', '--target-fps', '60', '--slow-factor', '0.5', '--model', 'rife-v4.6', '--backend', 'rife', '--deband', '--gpu-id', '0']),
            ('depth', {'input': 'a.mp4', 'out': 'dp_out', 'backend': 'video-depth-anything', 'dof_blur': 8.0, 'parallax_layers': True, 'gpu': True},
             ['--input', 'a.mp4', '--out', 'dp_out', '--backend', 'video-depth-anything', '--dof-blur', '8.0', '--parallax-layers', '--gpu']),
            ('shot-search', {'input': 'clips_dir', 'query': '蓝色大海', 'out': 'sh_out', 'top_k': 5, 'backend': 'siglip2', 'gpu': True, 'with_motion': True},
             ['--input', 'clips_dir', '--query', '蓝色大海', '--out', 'sh_out', '--top-k', '5', '--backend', 'siglip2', '--gpu', '--with-motion']),
            ('audit', {'plan': 'p.json', 'out': 'au_out', 'export': 'final.mp4', 'build_dir': 'b_dir', 'beats': 'ms_dir', 'preset': 'streaming', 'contact_sheet': True, 'sheet_frames': 12},
             ['--plan', 'p.json', '--out', 'au_out', '--export', 'final.mp4', '--build-dir', 'b_dir', '--beats', 'ms_dir', '--preset', 'streaming', '--contact-sheet', '--sheet-frames', '12']),
            ('shader-fx', {'subcommand': 'transition', 'from': 'a.mp4', 'to': 'b.mp4', 'out': 'tr_out', 'preset': 'crosszoom', 'duration': 0.5, 'easing': 'ease_in_out'},
             ['transition', '--from', 'a.mp4', '--to', 'b.mp4', '--out', 'tr_out', '--preset', 'crosszoom', '--duration', '0.5', '--easing', 'ease_in_out']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'a.mp4', 'out': 'fx_out', 'preset': 'halation_bloom', 'alpha': True},
             ['effect', '--input', 'a.mp4', '--out', 'fx_out', '--preset', 'halation_bloom', '--alpha']),
            ('stock', {'subcommand': 'search', 'provider': 'freesound', 'kind': 'audio', 'query': 'whoosh', 'out': 'fs.json'},
             ['search', '--provider', 'freesound', '--kind', 'audio', '--query', 'whoosh', '--out', 'fs.json']),
            ('build', {'plan': 'p.json', 'out': 'bo', 'draft': True, 'export_scale': 0.5},
             ['--plan', 'p.json', '--out', 'bo', '--export-scale', '0.5', '--draft']),
            ('export', {'project': 'p.fcproj', 'out': 'e.mp4', 'draft': True, 'scale': 0.5, 'gpu_rendering': 'off'},
             ['--project', 'p.fcproj', '--out', 'e.mp4', '--gpu-rendering', 'off', '--scale', '0.5', '--draft']),
            ('checks', {'only': ['auto_cut', 'matting', 'auto_reframe', 'rough_cut', 'audio_master'], 'no_auto_models': True},
             ['--only', 'auto_cut', 'matting', 'auto_reframe', 'rough_cut', 'audio_master', '--no-auto-models']),
            ('inspect', {'input': 'clips_dir', 'out': 'insp_out', 'frames': 4, 'contact_sheet': True, 'analyze_framing': True},
             ['--input', 'clips_dir', ' --out'.strip(), 'insp_out', '--frames', '4', '--contact-sheet', '--analyze-framing']),
            ('matting', {'input': 'fg.mp4', 'out': 'm3_out', 'target_fps': 30, 'defringe': 1, 'clean_color_holes': 'blue_sky', 'bottom_feather': 0.12},
             ['--input', 'fg.mp4', '--out', 'm3_out', '--target-fps', '30', '--defringe', '1', '--clean-color-holes', 'blue_sky', '--bottom-feather', '0.12']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'a.mp4', 'out': 'fx2_out', 'preset': 'echo_chromatic_snap', 'alpha': True},
             ['effect', '--input', 'a.mp4', '--out', 'fx2_out', '--preset', 'echo_chromatic_snap', '--alpha']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'a.mp4', 'out': 'fx3_out', 'preset': 'deep_glow', 'alpha': True},
             ['effect', '--input', 'a.mp4', '--out', 'fx3_out', '--preset', 'deep_glow', '--alpha']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'a.mp4', 'out': 'fx4_out', 'preset': 'shutter_motion_blur'},
             ['effect', '--input', 'a.mp4', '--out', 'fx4_out', '--preset', 'shutter_motion_blur']),
            ('mesh-avatar', {'subcommand': 'status'}, ['status']),
            ('mesh-avatar', {'subcommand': 'render', 'avatar': 'char.mavatar', 'out': 'ma_out', 'frames': 150, 'width': 1920, 'height': 1080, 'fps': 30, 'codec': 'prores_4444', 'fit': 'contain'},
             ['render', '--avatar', 'char.mavatar', '--out', 'ma_out', '--frames', '150', '--width', '1920', '--height', '1080', '--fps', '30', '--codec', 'prores_4444', '--fit', 'contain']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'fg.mov', 'out': 'fx6_out', 'preset': 'light_wrap', 'bg': 'bg.mp4', 'alpha': True},
             ['effect', '--input', 'fg.mov', '--out', 'fx6_out', '--preset', 'light_wrap', '--bg', 'bg.mp4', '--alpha']),
            ('shader-fx', {'subcommand': 'effect', 'input': 'fg.mov', 'out': 'fx7_out', 'preset': 'layer_motion_blur', 'alpha': True},
             ['effect', '--input', 'fg.mov', '--out', 'fx7_out', '--preset', 'layer_motion_blur', '--alpha']),
            ('motion-analyze', {'out': 'lut2_out', 'make_lut': 'opendrt_filmic', 'lut_size': 33},
             ['--out', 'lut2_out', '--make-lut', 'opendrt_filmic', '--lut-size', '33']),
            ('motion-analyze', {'out': 'lut3_out', 'make_lut': 'match_reference', 'match_reference': 'ref.png', 'match_strength': 0.8},
             ['--out', 'lut3_out', '--make-lut', 'match_reference', '--match-reference', 'ref.png', '--match-strength', '0.8']),
            ('audit', {'plan': 'p.json', 'out': 'au2_out', 'export': 'final.mp4', 'visual_qc': True, 'motion_review': True, 'font_check': 'simhei.ttf'},
             ['--plan', 'p.json', '--out', 'au2_out', '--export', 'final.mp4', '--visual-qc', '--motion-review', '--font-check', 'simhei.ttf']),
        ]
        for op, params, expected_tokens in valid_cases:
            with self.subTest(op=op):
                tokens = api.arguments_from_params(op, params)
                self.assertEqual(tokens, expected_tokens)
                self.assertTrue(api.command_for(op, tokens))

        bad_cases = [
            ('unknown-op', {}),
            ('reverse', {}),
            ('tracking', {'input': 'a.mp4', 'bbox': [1, 2, 3], 'duration': 1.0, 'out': 'o'}),
            ('stock', {'subcommand': 'search'}),
            ('mesh-avatar', {'subcommand': 'unknown'}),
            ('validate', 'not-a-dict'),
            ('validate', {}),
            ('validate', {'plan': 'p.json', 'extra': '1'}),
            ('validate', {'plan': 'p\x00.json'}),
            ('inspect', {'input': 'a', 'out': 'b', 'frames': 1.5}),
            ('reference', {'input': 'a', 'out': 'b', 'start': float('nan')}),
            ('preview', {'project': 'p.fcproj', 'out': 'o', 'times': []}),
            ('music-edit', {'subcommand': 'configure'}),
            ('auto-cut', {'input': 'talk.mp4', 'out': 'o', 'detector': 'bogus'}),
            ('matting', {'input': 'fg.mp4', 'out': 'o', 'backend': 'bogus'}),
            ('auto-reframe', {'input': 'w.mp4', 'out': 'o', 'detector': 'bogus'}),
            ('audio-master', {'input': 'a.wav', 'out': 'o', 'preset': 'bogus'}),
            ('music-structure', {'input': 'a.mp3', 'out': 'o', 'backend': 'bogus'}),
            ('sfx-search', {'library': 'lib', 'query': 'hit', 'out': 'o', 'backend': 'bogus'}),
            ('interpolate', {'input': 'a.mp4', 'out': 'o', 'backend': 'bogus'}),
            ('depth', {'input': 'a.mp4', 'out': 'o', 'backend': 'bogus'}),
            ('shot-search', {'input': 'a.mp4', 'query': 'sky', 'out': 'o', 'backend': 'bogus'}),
            ('shader-fx', {'subcommand': 'bogus'}),
            ('shader-fx', {'subcommand': 'transition', 'from': 'a.mp4', 'to': 'b.mp4', 'out': 'o', 'preset': 'bogus'}),
            ('shader-fx', {'subcommand': 'effect', 'input': 'fg.mov', 'out': 'o', 'preset': 'character_live25d'}),
            ('audit', {'plan': 'p.json', 'out': 'o', 'preset': 'bogus'}),
        ]
        for op, params in bad_cases:
            with self.subTest(op=op, params=params):
                with self.assertRaises(ValueError):
                    api.arguments_from_params(op, params)

    def test_backends_catalog_and_cli_from_unrelated_directory(self):
        static = api.backends(detect=False)
        self.assertFalse(static['detected'])
        self.assertTrue(static['read_only'])
        self.assertTrue(static['roles'])
        self.assertEqual({b['id'] for b in static['backends']},
                         {'filmcraft', 'effectcraft', 'ffmpeg'})
        with tempfile.TemporaryDirectory() as directory:
            run = subprocess.run([sys.executable, '-m', 'openedit_engine', 'backends', '--no-detect'],
                                 cwd=directory, text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual({b['id'] for b in payload['backends']},
                             {'filmcraft', 'effectcraft', 'ffmpeg'})


if __name__ == '__main__':
    unittest.main()
