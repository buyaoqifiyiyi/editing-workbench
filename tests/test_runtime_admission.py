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

    def test_scoped_feature_source_identity_ignores_unrelated_script_edits(self):
        for feature in ra.FEATURES:
            self.assertIsNotNone(ra.feature_source_identity(feature), f'Missing files for {feature}')
        install = fake_install(self.root, '99.0.0.4')
        identities = {
            feature: {rel: file_hash.sha256(install / rel) for rel in ra.bundled(feature)[1]}
            for feature in ('preview', 'chroma')
        }
        reference = ra.bundled('preview')[0]
        report = self.root / '99.0.0.4.json'
        write_report(report, install, reference, identities)
        real_source = ra.source_identity()
        payload = json.loads(report.read_text(encoding='utf-8'))
        payload['feature_source_identities'] = {
            feature: ra.feature_source_identity(feature, real_source)
            for feature in ra.FEATURES
        }
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        ra.admit(install.name, reference, identities, report)
        self.assertTrue(ra.matches('preview', install))
        self.assertTrue(ra.matches('chroma', install))

        unrelated = dict(real_source, **{'scripts/mesh_avatar.py': '1' * 64})
        with patch.object(ra, 'source_identity', return_value=unrelated):
            self.assertTrue(ra.matches('preview', install))
            self.assertTrue(ra.matches('chroma', install))

        chroma_only = dict(real_source, **{'scripts/native_chroma.py': '2' * 64})
        with patch.object(ra, 'source_identity', return_value=chroma_only):
            self.assertTrue(ra.matches('preview', install))
            self.assertFalse(ra.matches('chroma', install))

        shared_core = dict(real_source, **{'scripts/editor.py': '3' * 64})
        with patch.object(ra, 'source_identity', return_value=shared_core):
            self.assertFalse(ra.matches('preview', install))
            self.assertFalse(ra.matches('chroma', install))

        for shared_rel in (
            'scripts/editor_validate.py',
            'scripts/editor_build.py',
            'scripts/editor_env.py',
            'scripts/native_protocol.py',
        ):
            changed = dict(real_source, **{shared_rel: '5' * 64})
            with patch.object(ra, 'source_identity', return_value=changed):
                self.assertFalse(ra.matches('preview', install), shared_rel)
                self.assertFalse(ra.matches('chroma', install), shared_rel)

    def test_legacy_unscoped_report_stays_strict(self):
        install, reference, identities, report = self.fixture('99.0.0.5')
        ra.admit(install.name, reference, identities, report)
        altered = dict(ra.source_identity(), **{'scripts/mesh_avatar.py': '4' * 64})
        with patch.object(ra, 'source_identity', return_value=altered):
            self.assertFalse(ra.matches('corner', install))

    def test_stubbed_contract_applies_only_after_admission(self):
        unadmitted = fake_install(self.root, '99.0.0.7')
        stub = dict(ra._asset('chroma2-v1.json'), runtime_sha256={'missing.node': '0' * 64})
        self.assertIsNone(ra.expected('chroma', unadmitted, stub))

        install = fake_install(self.root, '99.0.0.6')
        identities = {'chroma': {rel: file_hash.sha256(install / rel) for rel in ra.bundled('chroma')[1]}}
        reference = ra.bundled('chroma')[0]
        report = self.root / '99.0.0.6.json'
        write_report(report, install, reference, identities)
        ra.admit(install.name, reference, identities, report)
        self.assertEqual(ra.expected('chroma', install, stub), {'missing.node': '0' * 64})
        self.assertEqual(ra.expected('chroma', install, ra._asset('chroma2-v1.json')), identities['chroma'])


if __name__ == '__main__':
    unittest.main()
