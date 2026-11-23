"""Isolated read-only detection checks for FilmCraft, EffectCraft, FFmpeg, and GPU."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openedit_engine import software_detection as detection


class SoftwareDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env_patch = patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.root),
            'OPENEDIT_FILMCRAFT_CLI': '',
            'OPENEDIT_FILMCRAFT_ROOT': '',
            'OPENEDIT_EFFECTCRAFT_CLI': '',
            'OPENEDIT_EFFECTCRAFT_ROOT': '',
            'OPENEDIT_FFMPEG': '',
            'OPENEDIT_FFPROBE': '',
            'ProgramFiles': str(self.root / 'pf'),
            'ProgramFiles(x86)': str(self.root / 'pf86'),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def make_file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
        return path

    def test_detects_openedittools_and_reflects_removal(self):
        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        ec_exe = 'effectcraft-cli.exe' if os.name == 'nt' else 'effectcraft-cli'
        fc_cli = self.make_file(f'OpenEditTools/filmcraft-0.20.1/{exe}')
        ec_cli = self.make_file(f'OpenEditTools/effectcraft-0.24.1/{ec_exe}')
        with patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_query_cli_version', side_effect=['0.20.1', '0.24.1']), \
                patch.object(detection, '_tool_path', return_value=None):
            res = detection.detect_backends()
        self.assertTrue(res['filmcraft']['selectable'])
        self.assertEqual(res['filmcraft']['path'], str(fc_cli.resolve()))
        self.assertEqual(res['filmcraft']['version'], '0.20.1')
        self.assertTrue(res['effectcraft']['selectable'])
        self.assertEqual(res['effectcraft']['path'], str(ec_cli.resolve()))

        fc_cli.unlink()
        with patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_tool_path', return_value=None):
            res2 = detection.detect_backends()
        self.assertFalse(res2['filmcraft']['installed'])
        self.assertEqual(res2['filmcraft']['state'], 'missing')

    def test_explicit_cli_override_does_not_fallback_when_missing(self):
        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        self.make_file(f'OpenEditTools/filmcraft-0.20.1/{exe}')
        with patch.dict(os.environ, {'OPENEDIT_FILMCRAFT_CLI': str(self.root / 'missing-cli.exe')}), \
                patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_tool_path', return_value=None):
            fc = detection.detect_backends()['filmcraft']
        self.assertFalse(fc['installed'])
        self.assertFalse(fc['selectable'])

        explicit = self.make_file(f'custom/{exe}')
        with patch.dict(os.environ, {'OPENEDIT_FILMCRAFT_CLI': str(explicit)}), \
                patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_query_cli_version', return_value='0.99.0'), \
                patch.object(detection, '_tool_path', return_value=None):
            fc_ok = detection.detect_backends()['filmcraft']
        self.assertTrue(fc_ok['selectable'])
        self.assertEqual(fc_ok['path'], str(explicit.resolve()))

    def test_ffmpeg_requires_both_ffmpeg_and_ffprobe(self):
        ff = self.make_file('bin/ffmpeg.exe')
        fp = self.make_file('bin/ffprobe.exe')
        with patch.dict(os.environ, {'OPENEDIT_FFMPEG': str(ff), 'OPENEDIT_FFPROBE': str(fp)}):
            res = detection._detect_ffmpeg()
        self.assertTrue(res['installed'])
        fp.unlink()
        with patch.dict(os.environ, {'OPENEDIT_FFMPEG': str(ff), 'OPENEDIT_FFPROBE': str(fp)}):
            res2 = detection._detect_ffmpeg()
        self.assertFalse(res2['installed'])
        self.assertEqual(res2['state'], 'missing')

    def test_gpu_detection_without_nvidia_smi(self):
        with patch.object(detection.shutil, 'which', return_value=None):
            self.assertEqual(detection.detect_gpu(), {'nvidia': False, 'gpus': []})

    def test_relative_override_paths_are_rejected(self):
        self.assertIsNone(detection._path('relative/filmcraft-cli.exe'))
        self.assertEqual(set(detection.detect_backends()), {'filmcraft', 'effectcraft', 'ffmpeg'})

    def test_openedittools_prefers_highest_semver_over_lexicographic_order(self):
        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        self.make_file(f'OpenEditTools/filmcraft-0.9.0/{exe}')
        self.make_file(f'OpenEditTools/filmcraft-0.20.1/{exe}')
        cli_100 = self.make_file(f'OpenEditTools/filmcraft-0.100.0/{exe}')
        self.make_file(f'OpenEditTools/filmcraft-0.100.0-rc.1/{exe}')
        with patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_query_cli_version', return_value='0.100.0'):
            fc = detection.detect_backends()['filmcraft']
        self.assertEqual(fc['path'], str(cli_100.resolve()))
        self.assertGreater(detection.parse_semver('0.20.1'), detection.parse_semver('0.20.1-rc.1'))
        self.assertGreater(detection.parse_semver('0.100.0'), detection.parse_semver('0.20.1'))
        self.assertGreater(detection.parse_semver('0.20.1'), detection.parse_semver('0.9.0'))

    def test_version_baseline_warnings_cover_verified_older_newer_and_unknown(self):
        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        self.make_file(f'OpenEditTools/filmcraft-0.20.1/{exe}')
        for ver, expected_status, expect_warn in (
            ('0.20.1', 'verified', False),
            ('0.21.0', 'unverified_newer', True),
            ('0.19.9', 'older_than_baseline', True),
            ('unknown', 'unknown', True),
        ):
            with self.subTest(ver=ver), \
                    patch.object(detection.shutil, 'which', return_value=None), \
                    patch.object(detection, '_query_cli_version', return_value=ver), \
                    patch.object(detection, '_tool_path', return_value=None):
                fc = detection.detect_backends()['filmcraft']
            self.assertTrue(fc['selectable'])
            self.assertEqual(fc['verified_baseline'], '0.20.1')
            self.assertEqual(fc['version_status'], expected_status)
            if expect_warn:
                self.assertTrue(fc['warning'])
            else:
                self.assertIsNone(fc['warning'])

    def test_backend_find_cli_delegates_to_find_craft_cli(self):
        import sys
        scripts_dir = str(detection.RUNTIME / 'scripts')
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import effectcraft_backend
        import filmcraft_backend

        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        ec_exe = 'effectcraft-cli.exe' if os.name == 'nt' else 'effectcraft-cli'
        fc_cli = self.make_file(f'OpenEditTools/filmcraft-0.20.1/{exe}')
        ec_cli = self.make_file(f'OpenEditTools/effectcraft-0.24.1/{ec_exe}')
        with patch.object(detection.shutil, 'which', return_value=None):
            self.assertEqual(filmcraft_backend.find_cli(), str(fc_cli.resolve()))
            self.assertEqual(effectcraft_backend.find_cli(), str(ec_cli.resolve()))
            with patch.dict(os.environ, {'OPENEDIT_FILMCRAFT_CLI': str(self.root / 'absent.exe')}):
                with self.assertRaisesRegex(ValueError, 'OPENEDIT_FILMCRAFT_CLI'):
                    filmcraft_backend.find_cli()

    def test_runtime_verified_version_upgrades_baseline_status(self):
        exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        self.make_file(f'OpenEditTools/filmcraft-0.21.0/{exe}')
        self.make_file('OpenEditTools/upstream-state.json').write_text(
            '{"schema_version": 1, "verified_versions": {"filmcraft": {"version": "0.21.0"}}}',
            encoding='utf-8',
        )
        with patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_query_cli_version', return_value='0.21.0'), \
                patch.object(detection, '_tool_path', return_value=None):
            fc_ok = detection.detect_backends()['filmcraft']
        self.assertEqual(fc_ok['version_status'], 'verified_runtime')
        self.assertIsNone(fc_ok['warning'])

        with patch.object(detection.shutil, 'which', return_value=None), \
                patch.object(detection, '_query_cli_version', return_value='0.22.0'), \
                patch.object(detection, '_tool_path', return_value=None):
            fc_warn = detection.detect_backends()['filmcraft']
        self.assertEqual(fc_warn['version_status'], 'unverified_newer')
        self.assertTrue(fc_warn['warning'])


if __name__ == '__main__':
    unittest.main()
