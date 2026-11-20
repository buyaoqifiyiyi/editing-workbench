"""Content identity guards for provenance sidecars and source media hashes."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import openedit_engine

SCRIPTS = Path(openedit_engine.__file__).resolve().parent / 'runtime' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import file_hash
import music_edit
import runtime_core


class ContentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'audio.wav'
        self.path.write_bytes(b'aaaa')

    def replace_preserving_stat(self):
        stat = self.path.stat()
        self.path.write_bytes(b'bbbb')
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual((self.path.stat().st_size, self.path.stat().st_mtime_ns), (stat.st_size, stat.st_mtime_ns))

    def test_shared_hash_detects_same_stat_replacement(self):
        old = file_hash.sha256(self.path)
        self.replace_preserving_stat()
        self.assertEqual(file_hash.sha256(self.path), hashlib.sha256(b'bbbb').hexdigest())
        self.assertFalse(file_hash.matches(self.path, old))

    def test_provenance_detects_same_stat_source_replacement(self):
        old_hash = file_hash.sha256(self.path)
        sidecar = self.path.with_name(self.path.name + '.provenance.json')
        runtime_core.write_json(sidecar, {
            'local_path': str(self.path.resolve()),
            'license_verification': 'agent_review_recorded',
            'review_note': 'Checked fixture',
            'sha256': old_hash,
        })
        plan = {'clips': [{'path': str(self.path.resolve())}], 'audio': [], 'overlays': []}
        self.assertEqual(runtime_core.provenance(plan)[0]['sha256'], old_hash)
        self.replace_preserving_stat()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            runtime_core.provenance(plan)

    def test_music_edit_check_sources_detects_same_stat_replacement(self):
        old_hash = file_hash.sha256(self.path)
        request = {'music': {'path': str(self.path), 'sha256': old_hash}, 'sources': []}
        music_edit.check_sources(request)
        self.replace_preserving_stat()
        with self.assertRaisesRegex(ValueError, 'changed'):
            music_edit.check_sources(request)


if __name__ == '__main__':
    unittest.main()
