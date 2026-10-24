"""Production admission and diagnostic boundaries; no UI or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image
from jianying_engine import api

sys.path.insert(0, str(api.RUNTIME / 'scripts'))
import editor


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


if __name__ == '__main__':
    unittest.main()
