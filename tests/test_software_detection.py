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
            'JIANYING_FFMPEG': '',
            'JIANYING_FFPROBE': '',
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


if __name__ == '__main__':
    unittest.main()
