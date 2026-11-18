"""Synthetic regression for substituted native runtime resource identities."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
scripts = root / 'src/jianying_engine/runtime/scripts'
if not scripts.is_dir():
    scripts = root / 'jianying_engine/runtime/scripts'
sys.path.insert(0, str(scripts))
import runtime_admission as ra
import file_hash
from runtime_admission_self_check import fake_install, write_report


class AbsentGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'admissions.json'
        self.enterContext(patch.dict(os.environ, {'JIANYING_RUNTIME_ADMISSIONS': str(self.store)}))
        self.install = fake_install(self.root, '99.0.0.1')
        self.reference = ra.bundled('chroma')[0]
        self.removed = next(rel for rel in ra.bundled('chroma')[1] if rel.endswith('.js') and '~' in rel)
        target = self.install / self.removed
        target.unlink()
        (target.parent / 'ai-creation.merged.js').write_bytes(b'merged synthetic bundle')
        self.identity, self.absent = ra.candidate_identity('chroma', self.install)
        self.identities = {'chroma': self.identity}
        self.report = self.root / 'report.json'
        write_report(self.report, self.install, self.reference, self.identities)
        data = json.loads(self.report.read_text(encoding='utf8'))
        data['features']['chroma']['absent_contract_files'] = self.absent
        self.report.write_text(json.dumps(data), encoding='utf8')

    def test_missing_or_forged_absent_list_is_refused_on_commit(self):
        for absent in (None, {'chroma': []}, {'chroma': ['forged.js']}):
            with self.subTest(absent=absent), self.assertRaises(ValueError):
                ra.admit(self.install.name, self.reference, self.identities, self.report, absent)
        self.assertFalse(self.store.exists())

    def test_legacy_missing_absent_list_cannot_skip_directory_recheck(self):
        ra.admit(self.install.name, self.reference, self.identities, self.report,
                 {'chroma': self.absent})
        self.assertTrue(ra.matches('chroma', self.install))
        data = ra.load()
        data['versions'][self.install.name]['absent_contract_files'] = {}
        self.store.write_text(json.dumps(data), encoding='utf8')
        self.assertFalse(ra.matches('chroma', self.install))
        (self.install / self.removed).with_name('new-unpinned.js').write_bytes(b'added')
        self.assertFalse(ra.matches('chroma', self.install))

    def test_correct_evidence_accepts_unchanged_and_refuses_directory_addition(self):
        ra.admit(self.install.name, self.reference, self.identities, self.report,
                 {'chroma': self.absent})
        self.assertTrue(ra.matches('chroma', self.install))
        (self.install / self.removed).with_name('new-unpinned.js').write_bytes(b'added')
        self.assertFalse(ra.matches('chroma', self.install))

    def test_report_missing_substitution_list_is_refused(self):
        data = json.loads(self.report.read_text(encoding='utf8'))
        data['features']['chroma'].pop('absent_contract_files')
        self.report.write_text(json.dumps(data), encoding='utf8')
        with self.assertRaises(ValueError):
            ra.admit(self.install.name, self.reference, self.identities, self.report,
                     {'chroma': self.absent})
        self.assertFalse(self.store.exists())

    def test_original_contract_reappearing_invalidates_admission(self):
        ra.admit(self.install.name, self.reference, self.identities, self.report,
                 {'chroma': self.absent})
        (self.install / self.removed).write_bytes(b'contract restored')
        self.assertFalse(ra.matches('chroma', self.install))

    def test_substitute_same_size_and_mtime_content_change_is_rejected(self):
        ra.admit(self.install.name, self.reference, self.identities, self.report,
                 {'chroma': self.absent})
        target = (self.install / self.removed).with_name('ai-creation.merged.js')
        before = target.stat()
        target.write_bytes(b'x' * before.st_size)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(ra.matches('chroma', self.install))

    def test_legacy_report_must_itself_certify_the_substituted_directory(self):
        ra.admit(self.install.name, self.reference, self.identities, self.report,
                 {'chroma': self.absent})
        report = json.loads(self.report.read_text(encoding='utf8'))
        report['features']['chroma'].pop('absent_contract_files')
        self.report.write_text(json.dumps(report), encoding='utf8')
        store = ra.load()
        store['versions'][self.install.name]['report_sha256'] = file_hash.sha256(self.report)
        self.store.write_text(json.dumps(store), encoding='utf8')
        self.assertFalse(ra.matches('chroma', self.install))


if __name__ == '__main__':
    unittest.main()
