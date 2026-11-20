"""Open audio beat-mark mapping uses synthetic media and never launches an editor or model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as Args
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beat_apply
import beat_apply_self_check


class BeatApplyTests(unittest.TestCase):
    def test_real_loader_and_validation_on_all_three_scenes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'marks'
            checks = beat_apply_self_check.check_marks(root)
            self.assertGreater(len(checks), 45)
            bootstrap = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                         'import beat_apply; sys.exit(beat_apply.main())')
            case = root / '0'
            target = case / 'cli.json'
            result = subprocess.run([sys.executable, '-c', bootstrap, str(SCRIPTS),
                '--plan', str(case / 'plan.json'), '--beats', str(case / 'candidates'),
                '--out', str(target), '--every', '4', '--mark-audio'],
                capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['mode'], 'mark_audio')
            self.assertIn('beat_marks', json.loads(target.read_text(encoding='utf-8'))['audio'][0])

    def test_microsecond_collision_is_refused_even_before_every_n(self):
        plan = {'canvas': dict(width=320, height=180, fps=30),
                'audio': [dict(path=str(ROOT / 'audio.wav'), source_in=0, duration=4)],
                'notes': []}
        result = {'source': {'path': plan['audio'][0]['path']},
                  'selection': dict(start_us=0, sample_rate=10_000_000, sample_count=40_000_000),
                  'beat_samples': [1, 2, 10, 20]}
        with patch('beats.load_result', return_value=result), self.assertRaisesRegex(ValueError, 'duplicate'):
            beat_apply.mark_audio(Args(beats=ROOT), plan, b'', ROOT / 'input', ROOT / 'output', 2)

    def test_rational_rounding_cannot_escape_selected_source(self):
        plan = {'canvas': dict(width=320, height=180, fps=30),
                'audio': [dict(path=str(ROOT / 'audio.wav'), source_in=.5, duration=2.5)],
                'notes': []}
        result = {'source': {'path': plan['audio'][0]['path']},
                  'selection': dict(start_us=2988390, sample_rate=22050, sample_count=44100),
                  'beat_samples': [256]}
        with patch('beats.load_result', return_value=result), self.assertRaisesRegex(ValueError, 'outside'):
            beat_apply.mark_audio(Args(beats=ROOT), plan, b'', ROOT / 'input', ROOT / 'output', 1)

    def test_output_race_and_source_change_preserve_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'in.json', Path(tmp) / 'out.json'
            source.write_bytes(b'original')
            def validate(path):
                target.write_bytes(b'concurrent owner')
                return {}, {}, []
            with patch('editor.validate_plan', side_effect=validate), self.assertRaises(FileExistsError):
                beat_apply.write_plan({}, target, source, b'original')
            self.assertEqual(target.read_bytes(), b'concurrent owner')
            target.unlink()
            source.write_bytes(b'changed')
            with patch('editor.validate_plan', return_value=({}, {}, [])), self.assertRaisesRegex(ValueError, 'Input plan changed'):
                beat_apply.write_plan({}, target, source, b'original')
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), b'changed')

    def test_real_whitelist_cli_help_and_output_conflict(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONIOENCODING='utf-8')
        command = [sys.executable, '-m', 'openedit_engine', 'beat-apply']
        help_result = subprocess.run(command + ['--help'], cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--mark-audio', help_result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing.json'
            target.write_bytes(b'preserve')
            result = subprocess.run(command + ['--plan', str(target), '--beats', tmp,
                '--out', str(target), '--mark-audio'], cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'failed')
            self.assertEqual(target.read_bytes(), b'preserve')


if __name__ == '__main__':
    unittest.main()
