"""Real temporary installation files, with registry and executable version reads isolated."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine import software_detection as detection


class SoftwareDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registered = []
        for item in (
            patch.object(detection, '_registered_apps', side_effect=lambda: self.registered),
            patch.object(detection, '_known_roots', return_value=[]),
            patch.object(detection.shutil, 'which', return_value=None),
            patch.object(detection, '_file_version', return_value='21.1.1.6'),
            patch.object(detection.importlib.util, 'find_spec', return_value=object()),
            patch.dict(os.environ, {'LOCALAPPDATA': str(self.root), 'JIANYING_RESOLVE_ROOT': '',
                                    'JIANYING_FFMPEG': '', 'JIANYING_FFPROBE': ''}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.tools = {'ffmpeg': self.file('tools/ffmpeg.exe'), 'ffprobe': self.file('tools/ffprobe.exe')}
        tool_patch = patch.object(detection, '_tool_path', side_effect=self.tools.get)
        tool_patch.start()
        self.addCleanup(tool_patch.stop)

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture executable; never run')
        return path

    def configuration(self, name, value):
        path = self.root / 'JianyingEngine' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def resolve_install(self):
        executable = self.file('custom/Resolve.exe')
        self.file('custom/ResolvePython/ResolvePython.exe')
        self.file('custom/fusionscript.dll')
        return executable

    def test_registered_install_then_uninstall_is_reflected_without_cached_state(self):
        executable = self.file('AE/AfterFX.exe')
        self.file('AE/AfterFX.com')
        self.file('AE/aerender.exe')
        self.registered.append(('after-effects', executable.parent))
        first = detection.detect_backends()['after-effects']
        self.assertTrue(first['installed'])
        self.assertTrue(first['selectable'])
        self.assertEqual(first['path'], str(executable))
        executable.unlink()
        second = detection.detect_backends()['after-effects']
        self.assertFalse(second['installed'])
        self.assertFalse(second['selectable'])
        self.assertEqual(second['state'], 'missing')

    def test_resolve_explicit_configuration_and_version_admission(self):
        executable = self.resolve_install()
        self.configuration('resolve.json', {'resolve_root': str(executable.parent)})
        result = detection.detect_backends()['resolve']
        self.assertTrue(result['selectable'])
        self.assertEqual(result['path'], str(executable))
        with patch.object(detection, '_file_version', return_value='22.0.0.0'):
            result = detection.detect_backends()['resolve']
            self.assertTrue(result['installed'])
            self.assertFalse(result['selectable'])
            self.assertIn('21.1.1', result['reason'])
        with patch.object(detection, '_file_version', return_value=None):
            self.assertFalse(detection.detect_backends()['resolve']['compatible'])

    def test_invalid_explicit_override_does_not_fall_back_to_another_install(self):
        executable = self.resolve_install()
        self.registered.append(('resolve', executable.parent))
        with patch.dict(os.environ, {'JIANYING_RESOLVE_ROOT': str(self.root / 'removed')}):
            self.assertFalse(detection.detect_backends()['resolve']['selectable'])
        for root in ('custom', 123, ['invalid']):
            self.configuration('resolve.json', {'resolve_root': root})
            self.assertFalse(detection.detect_backends()['resolve']['selectable'])
        self.assertIsNone(detection._path('custom'))

    def test_incomplete_resolve_and_ae_cannot_be_selected(self):
        self.registered.extend([('resolve', self.file('Resolve/Resolve.exe').parent),
                                ('after-effects', self.file('AE/AfterFX.exe').parent)])
        result = detection.detect_backends()
        for backend in ('resolve', 'after-effects'):
            self.assertTrue(result[backend]['installed'])
            self.assertFalse(result[backend]['compatible'])
            self.assertIn('缺少', result[backend]['reason'])

    def test_independent_ae_does_not_require_premiere_and_detects_custom_root(self):
        executable = self.file('custom-ae/Support Files/AfterFX.exe')
        self.file('custom-ae/Support Files/AfterFX.com')
        self.file('custom-ae/Support Files/aerender.exe')
        self.registered.append(('after-effects', executable.parent.parent))
        self.assertTrue(detection.detect_backends()['after-effects']['selectable'])
        self.configuration('adobe.json', {'ae_root': str(executable.parent),
                                         'pr_exe': str(self.root / 'removed-pr.exe')})
        result = detection.detect_backends()
        self.assertTrue(result['after-effects']['selectable'])
        self.assertFalse(result['premiere']['selectable'])

    def test_installed_premiere_remains_unsupported(self):
        executable = self.file('PR/Adobe Premiere Pro.exe')
        self.registered.append(('premiere', executable.parent))
        result = detection.detect_backends()['premiere']
        self.assertTrue(result['installed'])
        self.assertEqual(result['state'], 'unsupported')
        self.assertFalse(result['selectable'])

    def test_jianying_needs_real_executable_and_resources(self):
        executable = self.file('Jianying/JianyingPro.exe')
        self.registered.append(('jianying', executable.parent))
        self.assertFalse(detection.detect_backends()['jianying']['selectable'])
        (executable.parent / 'Resources/Font/SystemFont').mkdir(parents=True)
        self.assertTrue(detection.detect_backends()['jianying']['selectable'])

    def test_missing_media_tools_blocks_native_integration(self):
        executable = self.resolve_install()
        self.configuration('resolve.json', {'resolve_root': str(executable.parent)})
        self.assertTrue(detection.detect_backends()['resolve']['selectable'])
        self.tools['ffmpeg'] = None
        self.assertFalse(detection.detect_backends()['resolve']['selectable'])

    def test_only_the_four_requested_applications_are_discovered(self):
        self.registered.append(('kdenlive', self.file('KD/kdenlive.exe').parent))
        self.registered.append(('blender', self.file('Blender/blender.exe').parent))
        self.assertEqual(set(detection.detect_backends()), {'resolve', 'jianying', 'after-effects', 'premiere'})

    def test_registry_command_hints_are_parsed_without_shell_execution(self):
        for command in ('"C:\\Apps\\App.exe",0', 'C:\\Program Files\\App.exe,0',
                        '"C:\\Apps\\Uninstall.exe" /silent'):
            self.assertTrue(str(detection._command_path(command)).lower().endswith('.exe'))
        self.assertIsNone(detection._command_path('powershell -command arbitrary'))

    def test_invalid_saved_configuration_cannot_claim_compatibility(self):
        executable = self.resolve_install()
        self.registered.append(('resolve', executable.parent))
        self.configuration('resolve.json', {})
        (self.root / 'JianyingEngine/resolve.json').write_text('broken json', encoding='utf-8')
        result = detection.detect_backends()['resolve']
        self.assertTrue(result['installed'])
        self.assertFalse(result['selectable'])
        self.assertIn('配置无法读取', result['reason'])

    def test_path_discovery_checks_the_executable_still_exists(self):
        executable = self.file('portable/AfterFX.exe')
        self.file('portable/AfterFX.com')
        self.file('portable/aerender.exe')
        with patch.object(detection.shutil, 'which', side_effect=lambda name: str(executable) if name == 'AfterFX.exe' else None):
            self.assertTrue(detection.detect_backends()['after-effects']['selectable'])
            executable.unlink()
            self.assertFalse(detection.detect_backends()['after-effects']['installed'])


if __name__ == '__main__':
    unittest.main()
