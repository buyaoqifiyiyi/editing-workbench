import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from openedit_engine import api
from jianying_engine import api as legacy_api


class EngineInterfaceTests(unittest.TestCase):
    def test_rejects_non_operations_and_invalid_tokens_and_keeps_alias(self):
        self.assertIs(legacy_api, api)
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
            alias = subprocess.run([sys.executable, '-m', 'jianying_engine', 'doctor'], cwd=directory,
                                   text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(alias.returncode, 0, alias.stderr)
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
                     'separate', 'tracking', 'tracking-apply', 'auto-cut', 'matting', 'stock', 'mesh-avatar'):
            self.assertIn(name, schemas)
        self.assertEqual(set(schemas), set(api.operations()))
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
            ('auto-cut', {'input': 'talk.mp4', 'out': 'ac_out', 'detector': 'silero', 'threshold': 0.5, 'min_silence': 0.4},
             ['--input', 'talk.mp4', '--out', 'ac_out', '--detector', 'silero', '--threshold', '0.5', '--min-silence', '0.4']),
            ('matting', {'input': 'fg.mp4', 'out': 'm_out', 'backend': 'birefnet', 'alpha_feather': 0.5, 'alpha_erode': -1, 'keep_audio': True},
             ['--input', 'fg.mp4', '--out', 'm_out', '--backend', 'birefnet', '--alpha-feather', '0.5', '--alpha-erode=-1', '--keep-audio']),
            ('checks', {'only': ['auto_cut', 'matting'], 'no_auto_models': True},
             ['--only', 'auto_cut', 'matting', '--no-auto-models']),
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
