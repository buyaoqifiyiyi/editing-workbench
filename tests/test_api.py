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


if __name__ == '__main__':
    unittest.main()
