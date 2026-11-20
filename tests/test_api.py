import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jianying_engine import api


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
        self.assertTrue((api.RUNTIME / 'scripts/native_preview.cjs').is_file())
        for name in api.paths()['guides']:
            self.assertTrue(api.read_guide(name).strip())
        self.assertIn('reference/styles/anime-mix', api.paths()['guides'])
        self.assertIn('reference/native-effects', api.paths()['guides'])
        for path in (api.RUNTIME / 'assets').glob('native-preview-*.json'):
            self.assertNotIn('install', json.loads(path.read_text(encoding='utf-8')))

    def test_cli_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            good = subprocess.run([sys.executable, '-m', 'jianying_engine', 'doctor'], cwd=directory,
                                  text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            result = json.loads(good.stdout)
            self.assertEqual(Path(result['skill_root']), api.RUNTIME)
            self.assertIn('backend_importable', result)
            bad = subprocess.run([sys.executable, '-m', 'jianying_engine', '../editor.py'], cwd=directory,
                                 text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(json.loads(bad.stderr)['status'], 'failed')

    def test_operation_schemas_and_arguments_from_params(self):
        schemas = api.operation_schemas()
        self.assertTrue(set(schemas).issubset(api.operations()))
        for name in ('validate', 'build', 'publish', 'captions', 'music-edit', 'resolve-workflow', 'ducking'):
            self.assertIn(name, schemas)
        self.assertEqual(api.operation_schemas('validate'), schemas['validate'])
        self.assertIsNone(api.operation_schemas('templates'))
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
            'out': 'o.json', 'backend': 'resolve', 'width': 1920,
        })
        self.assertEqual(me_tokens, ['--config', 'cfg.json', 'compile', '--bundle', 'b',
                                     '--out', 'o.json', '--backend', 'resolve', '--width', '1920'])

        rv_tokens = api.arguments_from_params('resolve-workflow', {
            'subcommand': 'return-ae', 'bundle': 'b', 'plan': 'p.json', 'clip_index': 1, 'out': 'o.json',
        })
        self.assertEqual(rv_tokens[0], 'return-ae')
        self.assertIn('--clip-index', rv_tokens)

        bad_cases = [
            ('unknown-op', {}),
            ('templates', {}),
            ('validate', 'not-a-dict'),
            ('validate', {}),
            ('validate', {'plan': 'p.json', 'extra': '1'}),
            ('validate', {'plan': 'p\x00.json'}),
            ('capabilities', {'limit': True}),
            ('inspect', {'input': 'a', 'out': 'b', 'frames': 1.5}),
            ('reference', {'input': 'a', 'out': 'b', 'start': float('nan')}),
            ('reference', {'input': 'a', 'out': 'b', 'detector': 'invalid'}),
            ('publish', {'draft': 'd', 'offline_copy': 'yes'}),
            ('native-preview', {'install': 'i', 'draft': 'd', 'out': 'o', 'times': []}),
            ('music-edit', {'subcommand': 'configure'}),
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
                         {'resolve', 'jianying', 'after-effects', 'premiere'})
        with tempfile.TemporaryDirectory() as directory:
            run = subprocess.run([sys.executable, '-m', 'jianying_engine', 'backends', '--no-detect'],
                                 cwd=directory, text=True, encoding='utf-8', capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual({b['id'] for b in payload['backends']},
                             {'resolve', 'jianying', 'after-effects', 'premiere'})


if __name__ == '__main__':
    unittest.main()
