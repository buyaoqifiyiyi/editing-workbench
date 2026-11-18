"""Synthetic guard tests for the actual still/sequence preview runtime argument."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
scripts = root / 'src/jianying_engine/runtime/scripts'
if not scripts.is_dir():
    scripts = root / 'jianying_engine/runtime/scripts'
sys.path.insert(0, str(scripts))
import editor
import native_preview


class PreviewRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / '11.7.0.14652'
        self.install.mkdir()
        self.good = self.install / 'vebun.exe'
        self.good.write_bytes(b'measured')
        self.other = self.root / 'other.exe'
        self.other.write_bytes(b'unmeasured')
        self.draft = self.root / 'draft_content.json'
        self.draft.write_text('{}', encoding='utf-8')
        self.manifest = {'files': {}, 'admitted_from': '11.6.0.14552',
                         'runtime': {'sha256': hashlib.sha256(self.good.read_bytes()).hexdigest()}}

    def test_derived_still_frame_rejects_unverified_runtime_before_launch(self):
        with patch.object(native_preview, 'preview_manifest', return_value=self.manifest), \
                patch.object(native_preview.subprocess, 'Popen', side_effect=AssertionError('should not spawn')):
            with self.assertRaisesRegex(ValueError, 'Unverified native preview runtime'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5], runtime=self.other)

    def test_derived_still_frame_accepts_measured_runtime_to_next_stage(self):
        with patch.object(native_preview, 'preview_manifest', return_value=self.manifest), \
                patch.object(editor, 'require_plaintext_draft'), \
                patch.object(editor, 'read_json', side_effect=RuntimeError('REACHED_CONTENT')):
            with self.assertRaisesRegex(RuntimeError, 'REACHED_CONTENT'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5], runtime=self.good)

    def test_existing_reference_still_frame_keeps_legacy_runtime_policy(self):
        manifest = {key: value for key, value in self.manifest.items() if key != 'admitted_from'}
        with patch.object(native_preview, 'preview_manifest', return_value=manifest), \
                patch.object(editor, 'require_plaintext_draft'), \
                patch.object(editor, 'read_json', side_effect=RuntimeError('REACHED_CONTENT')):
            with self.assertRaisesRegex(RuntimeError, 'REACHED_CONTENT'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5], runtime=self.other)

    def test_reference_sequence_rejects_unverified_runtime(self):
        manifest = {key: value for key, value in self.manifest.items() if key != 'admitted_from'}
        with patch.object(native_preview, 'preview_manifest', return_value=manifest):
            with self.assertRaisesRegex(ValueError, 'Unverified native sequence runtime'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5], runtime=self.other, sequence={})

    def test_missing_explicit_runtime_does_not_fall_back_to_another_executable(self):
        with patch.object(native_preview, 'preview_manifest', return_value=self.manifest), \
                patch.object(editor, 'require_plaintext_draft'), \
                patch.object(editor, 'read_json', side_effect=RuntimeError('REACHED_CONTENT')):
            with self.assertRaisesRegex(ValueError, 'runtime executable is missing'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5], runtime=self.root/'missing.exe')

    def test_implicit_runtime_uses_measured_vebun_fallback(self):
        with patch.object(native_preview, 'preview_manifest', return_value=self.manifest), \
                patch.object(editor, 'require_plaintext_draft'), \
                patch.object(editor, 'read_json', side_effect=RuntimeError('REACHED_CONTENT')):
            with self.assertRaisesRegex(RuntimeError, 'REACHED_CONTENT'):
                native_preview.preview(self.install, self.draft, self.root/'out', [.5])


if __name__ == '__main__':
    unittest.main()
