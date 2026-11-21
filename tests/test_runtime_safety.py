"""Open-source validation guards, doctor boundaries, and runtime_common checks; no GUI."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from openedit_engine import api

SCRIPTS = str(api.RUNTIME / 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import editor
import run_checks
import runtime_common
import runtime_core
import runtime_paths


class RuntimeSafety(unittest.TestCase):
    def test_invalid_plan_parameters_rejected_before_build_writes(self):
        with tempfile.TemporaryDirectory(prefix='oe-safety-') as folder:
            root = Path(folder)
            source = root / 'source.png'
            Image.new('RGB', (320, 240), 'green').save(source)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            base = {
                'schema_version': 1, 'name': '开源校验测试', 'mode': 'script',
                'canvas': {'width': 320, 'height': 240, 'fps': 30},
                'clips': [{'path': str(source), 'start': 0, 'duration': 1},
                          {'path': str(source), 'start': 0, 'duration': 1, 'track': 'overlay'}],
            }
            cases = [
                ({'duration': float('nan')}, 'finite'),
                ({'fit': 'stretch'}, 'contain'),
                ({'transform': {'flip_horizontal': 'yes'}}, 'boolean'),
                ({'grading': {'luma_curve': [[0, 0], [0.6, 0.5], [0.4, 0.8]]}}, 'strictly increase'),
            ]
            for index, (fields, expected) in enumerate(cases):
                with self.subTest(expected=expected, index=index):
                    plan = copy.deepcopy(base)
                    plan['clips'][1].update(fields)
                    path = root / f'plan-{index}.json'
                    path.write_text(json.dumps(plan, ensure_ascii=False, allow_nan=True), encoding='utf-8')
                    before = path.read_bytes()
                    for action in (
                        lambda: editor.validate_plan(path),
                        lambda: editor.build_draft(SimpleNamespace(plan=str(path), out=str(root / f'out-{index}'))),
                    ):
                        with self.assertRaisesRegex(ValueError, expected):
                            action()
                    self.assertFalse((root / f'out-{index}').exists())
                    self.assertEqual(path.read_bytes(), before)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_doctor_reports_open_license_profile_and_missing_cli_instructions(self):
        report = editor.doctor()
        self.assertIn('open_source_only', report['license_profile'])
        self.assertEqual(set(report['backends']), {'filmcraft', 'effectcraft', 'ffmpeg'})
        with patch.dict(editor.os.environ, {'OPENEDIT_FILMCRAFT_CLI': str(Path('C:/nonexistent/fc-cli.exe'))}):
            incomplete = editor.doctor()
        self.assertFalse(incomplete['ready'])
        self.assertTrue(any('filmcraft-cli' in msg for msg in incomplete['instructions']))

    def test_run_checks_skips_missing_model_weights(self):
        reason = run_checks.skip_reason('separate', run_checks.CHECKS['separate'],
                                        SimpleNamespace(demucs_model=None, whisper_model=None))
        self.assertIsNotNone(reason)
        self.assertIn('--demucs-model', reason)

    def test_runtime_paths_model_discovery_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix='oe-models-') as folder:
            root = Path(folder)
            (root / 'demucs').mkdir()
            (root / 'vad').mkdir()
            (root / 'matting').mkdir()
            (root / 'face').mkdir()
            (root / 'faster-whisper-tiny').mkdir()
            (root / 'faster-whisper-base').mkdir()
            (root / 'faster-whisper-tiny' / 'model.bin').write_bytes(b'tiny')
            (root / 'faster-whisper-base' / 'model.bin').write_bytes(b'base')
            (root / 'demucs' / 'htdemucs_fp16weights.onnx').write_bytes(b'dm')
            (root / 'vad' / 'silero_vad.onnx').write_bytes(b'vd')
            (root / 'matting' / 'birefnet_general.onnx').write_bytes(b'bf')
            (root / 'matting' / 'birefnet_lite_fp16.onnx').write_bytes(b'fp16')
            (root / 'matting' / 'rmbg14_quantized.onnx').write_bytes(b'rm')
            (root / 'face' / 'face_detection_yunet_2023mar.onnx').write_bytes(b'yn')
            clean_env = {
                'OPENEDIT_MODELS_DIR': str(root),
                'OPENEDIT_NO_AUTO_MODELS': '',
                'OPENEDIT_DEMUCS_MODEL': '',
                'JIANYING_DEMUCS_MODEL': '',
                'OPENEDIT_WHISPER_MODEL': '',
                'JIANYING_WHISPER_MODEL': '',
                'OPENEDIT_VAD_MODEL': '',
                'OPENEDIT_MATTING_MODEL': '',
                'OPENEDIT_FACE_MODEL': '',
            }
            with patch.dict(editor.os.environ, clean_env, clear=False):
                inv = runtime_paths.discover_local_models()
                self.assertTrue(all(inv[k]['available'] for k in ('demucs', 'whisper', 'vad', 'matting', 'face')))
                self.assertEqual(Path(inv['whisper']['path']).name, 'faster-whisper-base')
                self.assertEqual(Path(inv['matting']['path']).name, 'birefnet_general.onnx')
                self.assertEqual(Path(runtime_paths.discover_model('matting', prefer_fp16=True)).name, 'birefnet_lite_fp16.onnx')
                self.assertEqual(Path(inv['face']['path']).name, 'face_detection_yunet_2023mar.onnx')
                provs = runtime_paths.resolve_ort_providers(['CUDAExecutionProvider', 'CPUExecutionProvider'], gpu=True)
                self.assertEqual(provs[0][0], 'CUDAExecutionProvider')
                self.assertEqual(provs[0][1]['cudnn_conv_algo_search'], 'HEURISTIC')
                with patch.dict(editor.os.environ, {'OPENEDIT_NO_AUTO_MODELS': '1'}):
                    self.assertIsNone(runtime_paths.discover_model('demucs'))
                    self.assertIsNone(runtime_paths.discover_model('whisper'))
                    self.assertIsNone(runtime_paths.discover_model('face'))

    def test_runtime_common_locks_and_validators(self):
        with tempfile.TemporaryDirectory(prefix='oe-common-') as folder:
            lock_path = Path(folder) / 'worker.lock'
            self.assertFalse(runtime_common.is_file_locked(lock_path))
            with runtime_common.file_lock(lock_path, blocking=False, pad_empty=False, busy_message='busy'):
                self.assertTrue(runtime_common.is_file_locked(lock_path))
                with self.assertRaisesRegex(ValueError, 'busy'):
                    with runtime_common.file_lock(lock_path, blocking=False, pad_empty=False, busy_message='busy'):
                        pass
            self.assertFalse(runtime_common.is_file_locked(lock_path))
            self.assertEqual(lock_path.stat().st_size, 0)

        self.assertEqual(runtime_common.check_object({'a': 1}, ('a', 'b'), ('a',), 'cfg'), {'a': 1})
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            runtime_common.check_object({'a': 1, 'z': 2}, ('a',), ('a',), 'cfg')
        with self.assertRaisesRegex(ValueError, 'missing fields'):
            runtime_common.check_object({}, ('a',), ('a',), 'cfg')
        with self.assertRaisesRegex(ValueError, 'finite number'):
            runtime_common.check_number(True, 'v', 0, 10, style='editor')
        with self.assertRaisesRegex(ValueError, 'outside'):
            runtime_common.check_number(11, 'v', 0, 10, style='colon')
        with self.assertRaisesRegex(ValueError, r'\[0, 10\]'):
            runtime_common.check_number(11, 'v', 0, 10, style='range')
        self.assertEqual(runtime_common.check_text('a\tb\nc', 't', 10, allow_whitespace_controls=True), 'a\tb\nc')
        with self.assertRaisesRegex(ValueError, 'control'):
            runtime_common.check_text('a\tb', 't', 10, allow_whitespace_controls=False)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
            runtime_common.unique_json_pairs([('x', 1), ('x', 2)])

    def test_open_scripts_import_cleanly_in_fresh_process(self):
        probe_cmd = [
            sys.executable, '-c',
            'import sys; sys.path.insert(0, %r); import runtime_core, craft_rpc, media_prep, effectcraft_backend, filmcraft_backend, open_engine, editor' % SCRIPTS,
        ]
        subprocess.run(probe_cmd, check=True)
        for symbol in ('validate_plan', 'probe', 'snap_range', 'provenance', 'source_delivery', 'build_draft', 'doctor'):
            self.assertTrue(callable(getattr(editor, symbol)), symbol)
            self.assertIs(getattr(runtime_core, symbol, getattr(editor, symbol)), getattr(editor, symbol))


if __name__ == '__main__':
    unittest.main()
