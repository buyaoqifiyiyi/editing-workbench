"""Persistence and fail-closed checks for version admission, using synthetic files only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'src' / 'jianying_engine' / 'runtime' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import file_hash
import runtime_admission as ra
from runtime_admission_self_check import fake_install, write_report


class AdmissionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'admissions.json'
        self.enterContext(patch.dict(os.environ, {'JIANYING_RUNTIME_ADMISSIONS': str(self.store)}))

    def fixture(self, version):
        install = fake_install(self.root, version)
        identities = {'corner': {rel: file_hash.sha256(install / rel) for rel in ra.bundled('corner')[1]}}
        report = self.root / (version + '.json')
        reference = ra.bundled('corner')[0]
        write_report(report, install, reference, identities)
        return install, reference, identities, report

    def test_two_concurrent_admissions_keep_both_versions(self):
        fixtures = [self.fixture('99.0.0.1'), self.fixture('99.0.0.2')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ra.admit, install.name, reference, identities, report)
                       for install, reference, identities, report in fixtures]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(set(ra.load()['versions']), {'99.0.0.1', '99.0.0.2'})
        for install, _, _, _ in fixtures:
            self.assertTrue(ra.matches('corner', install))

    def test_malformed_record_fails_closed(self):
        install, _, _, _ = self.fixture('99.0.0.3')
        for data in ('{', '[]', '{"schema_version":1,"versions":[]}'):
            self.store.write_text(data, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Invalid runtime admission record'):
                ra.expected('corner', install)


if __name__ == '__main__':
    unittest.main()
