"""Production admission and diagnostic boundaries; no UI or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from jianying_engine import api

sys.path.insert(0, str(api.RUNTIME / 'scripts'))
import editor
import run_checks


class RuntimeSafety(unittest.TestCase):
    def test_unadmitted_parameters_rejected_before_build_writes(self):
        with tempfile.TemporaryDirectory(prefix='jy-admission-') as folder:
            root = Path(folder)
            source = root / 'source.png'
            Image.new('RGB', (320, 240), 'green').save(source)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            base = {'schema_version': 1, 'name': '准入守卫测试', 'mode': 'script',
                    'canvas': {'width': 320, 'height': 240, 'fps': 30},
                    'clips': [{'path': str(source), 'start': 0, 'duration': 1},
                              {'path': str(source), 'start': 0, 'duration': 1, 'track': 'overlay'}]}
            safe_plan = root / 'safe-plan.json'
            safe_plan.write_text(json.dumps(base, ensure_ascii=False), encoding='utf-8')
            safe_draft = root / 'safe-draft'
            editor.build_draft(SimpleNamespace(plan=str(safe_plan), out=str(safe_draft)))
            offline = root / 'offline'
            offline.mkdir()
            cases = [({'chroma': {'color': '#00FF00FF'}}, 'chroma'),
                     ({'mix_mode': '滤色'}, 'mix_mode'),
                     ({'mask': {'name': '矩形', 'experimental': True, 'feather': 20}}, 'feather'),
                     ({'mask': {'name': '矩形', 'experimental': True, 'feather': 0,
                                'keyframes': [{'property': 'feather', 'time': 0, 'value': 0},
                                              {'property': 'feather', 'time': .5, 'value': 50}]}}, 'feather')]
            for index, (fields, expected) in enumerate(cases):
                with self.subTest(expected=expected, index=index):
                    plan = copy.deepcopy(base)
                    plan['clips'][1].update(fields)
                    path = root / f'plan-{index}.json'
                    path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
                    before = path.read_bytes()
                    for action in (lambda: editor.validate_plan(path),
                                   lambda: editor.build_draft(SimpleNamespace(plan=str(path), out=str(root / f'out-{index}')))):
                        with self.assertRaisesRegex(ValueError, expected):
                            action()
                    self.assertFalse((root / f'out-{index}').exists())
                    self.assertEqual(path.read_bytes(), before)
                    # Existing plaintext draft publication must use the same production gate.
                    saved_plan = safe_draft / 'edit-plan.json'
                    saved_plan.write_bytes(path.read_bytes())
                    draft_before = editor.draft_tree_digest(safe_draft)
                    with self.assertRaisesRegex(ValueError, expected):
                        editor.publish_draft(SimpleNamespace(draft=str(safe_draft), draft_root=str(offline), offline_copy=True))
                    self.assertEqual(editor.draft_tree_digest(safe_draft), draft_before)
                    self.assertEqual(list(offline.iterdir()), [])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_doctor_separates_runtime_readiness_from_feature_evidence(self):
        report = editor.doctor()
        self.assertEqual(report['ready_scope'], 'runtime_dependencies_only')
        self.assertFalse(report['compatibility']['all_features_verified'])
        self.assertEqual(report['compatibility']['feature_evidence'], 'consult_capabilities_and_project_receipts')

    def test_reviewed_matting_registration_is_an_explicit_operation(self):
        command = api.command_for('admit-matting', ['--help'])
        self.assertEqual(command[2], 'admit-matting')
        self.assertIn('externally reviewed', api.operations()['admit-matting'])

    def test_doctor_reports_effect_cache_and_admissions_without_blocking_ready(self):
        report = editor.doctor()
        for key in ('effect_cache_dir', 'effect_cache_ready', 'cached_native_resources',
                    'jianying_install', 'runtime_admissions', 'runtime_admission_error'):
            self.assertIn(key, report)
        with patch.object(editor, 'effect_cache_dir', return_value=None), \
                patch.object(editor, 'cached_native_resources', return_value={'hsl': False, 'rectangle_mask': False}):
            missing_report = editor.doctor()
        self.assertFalse(missing_report['effect_cache_ready'])
        self.assertEqual(missing_report['ready_scope'], 'runtime_dependencies_only')
        self.assertTrue(missing_report['ready'])
        self.assertTrue(any(a['code'] == 'effect_cache_missing' and 'hsl' in a['missing']
                            for a in missing_report['migration_instructions']))

    def test_run_checks_skips_when_local_effect_cache_missing(self):
        reason = run_checks.skip_reason('speed_curve', run_checks.CHECKS['speed_curve'],
                                        SimpleNamespace(install=None), native_ready=True,
                                        cached_resources={'rectangle_mask': False})
        self.assertIsNotNone(reason)
        self.assertIn('rectangle_mask', reason)
        chroma_reason = run_checks.skip_reason('chroma', run_checks.CHECKS['chroma'],
                                               SimpleNamespace(install=None), native_ready=True,
                                               cached_resources={'rectangle_mask': False})
        if editor.os.name == 'nt':
            self.assertIsNone(chroma_reason)

    def test_recorded_install_discovers_admitted_preview_build(self):
        admitted_install = Path('C:/Fake/11.7.0.14652')
        with patch.object(editor, 'jianying_install', return_value=admitted_install), \
                patch('runtime_admission.preview_manifest', return_value={'version': '11.7.0.14652'}):
            self.assertEqual(run_checks.recorded_install(), admitted_install)

    def test_runtime_common_locks_and_validators(self):
        import runtime_admission
        import runtime_common

        self.assertEqual(runtime_common.RUNTIME_FEATURES, runtime_admission.FEATURES)
        with tempfile.TemporaryDirectory(prefix='jy-common-') as folder:
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

    def test_editor_facade_and_native_protocol_registry(self):
        import subprocess
        import editor_build
        import editor_env
        import editor_validate
        import native_protocol

        expected_exports = (
            'probe', 'frame', 'new_directory', 'draft_location', 'wait_for_registration',
            'jianying_running', 'qt_ini_value', 'register_media_library', 'draft_tree_digest',
            'refresh_registration', 'doctor', 'inspect_media', 'analyze_reference',
            'validate_grading', 'validate_visual', 'native_enum', 'validate_native',
            'effect_params', 'apply_animations', 'planned_animation_id', 'cached_animation_id',
            'animation_availability', 'animation_cache_status', 'animation_preflight',
            'drop_unavailable_animations', 'apply_native', 'native_catalog', 'validate_plan',
            'text_segment', 'bind_text_font', 'srt_time', 'provenance', 'source_delivery',
            'delivery', 'native_delivery', 'verify_native', 'verify_structure',
            'build_draft', 'publish_draft',
        )
        for symbol in expected_exports:
            self.assertTrue(callable(getattr(editor, symbol)), symbol)
        self.assertIs(editor.doctor, editor_env.doctor)
        self.assertIs(editor.validate_plan, editor_validate.validate_plan)
        self.assertIs(editor.build_draft, editor_build.build_draft)

        # Fresh process import in reverse order (submodules before editor facade) has no cycle.
        probe_cmd = [
            sys.executable, '-c',
            'import sys; sys.path.insert(0, %r); import native_protocol, editor_validate, editor_build, editor_env, editor' % str(api.RUNTIME / 'scripts'),
        ]
        subprocess.run(probe_cmd, check=True)

        registry = native_protocol.adapter_registry()
        apply_names = [row['name'] for row in registry if 'apply_to_content' in row['phases']]
        self.assertEqual(
            apply_names,
            ['text_glow', 'matting_cache', 'chroma', 'mix_mode', 'beat',
             'masks', 'mask_feather', 'extensions', 'primary_wheel', 'composites'],
        )
        preview_rows = sorted(
            (r for r in registry if r['preview_order'] is not None),
            key=lambda r: r['preview_order'],
        )
        self.assertEqual(
            [row['preview_order'] for row in preview_rows],
            [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110],
        )
        self.assertEqual(
            [row['name'] for row in preview_rows],
            ['local_font', 'mask_feather', 'chroma', 'mix_mode', 'text_highlight', 'text_alpha',
             'corner_motion', 'green_lightness', 'hsl_channels', 'beat', 'primary_wheel'],
        )
        self.assertEqual(
            list(native_protocol.iter_objects({'a': [{'b': 1}], 'c': {'d': 2}})),
            [{'a': [{'b': 1}], 'c': {'d': 2}}, {'b': 1}, {'d': 2}],
        )
        with tempfile.TemporaryDirectory(prefix='jy-native-proto-') as folder:
            root = Path(folder)
            (root / 'sub').mkdir()
            (root / 'sub' / 'file.txt').write_bytes(b'abc')
            tree = native_protocol.hash_tree(root)
            self.assertEqual(tree, {'sub/file.txt': hashlib.sha256(b'abc').hexdigest()})
            with self.assertRaisesRegex(ValueError, 'regular directory'):
                native_protocol.hash_tree(root / 'sub' / 'file.txt')
            with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda p: p.name == 'file.txt'):
                with self.assertRaisesRegex(ValueError, 'contains a link'):
                    native_protocol.hash_tree(root)
            with self.assertRaisesRegex(ValueError, 'formal edit-plan.json'):
                native_protocol.verify_plan_preview(
                    {}, root, observed=True,
                    plan_declared=lambda p: False,
                    missing_plan_error='missing formal edit-plan.json',
                )


if __name__ == '__main__':
    unittest.main()
