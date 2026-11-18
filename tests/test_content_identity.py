"""Content identity guards for native resource admission and loudness measurement."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import jianying_engine

SCRIPTS = Path(jianying_engine.__file__).resolve().parent / 'runtime' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import editor
import file_hash

class ContentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'audio.wav'
        self.path.write_bytes(b'aaaa')
        self.item = {'path': str(self.path), 'source_in': 0, 'duration': 4, 'loudness': {'target_lufs': -23}}
        editor._LOUDNESS.clear()
        self.addCleanup(editor._LOUDNESS.clear)

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

    def test_loudness_remeasures_new_bytes_and_reuses_unchanged(self):
        def measure(item):
            return {'avg_loudness': -41.0 if Path(item['path']).read_bytes() == b'aaaa' else -27.0}
        with patch.object(editor, '_measure_loudness', side_effect=measure) as measure_mock:
            self.assertEqual(editor.measure_loudness(self.item)['avg_loudness'], -41.0)
            self.assertEqual(editor.measure_loudness(self.item)['avg_loudness'], -41.0)
            self.assertEqual(measure_mock.call_count, 1)
            self.replace_preserving_stat()
            self.assertEqual(editor.measure_loudness(self.item)['avg_loudness'], -27.0)
            self.assertEqual(measure_mock.call_count, 2)

    def test_loudness_rejects_source_change_during_measurement(self):
        def change(item):
            self.replace_preserving_stat()
            return {'avg_loudness': -41.0}
        with patch.object(editor, '_measure_loudness', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed'):
                editor.measure_loudness(self.item)
        self.assertFalse(editor._LOUDNESS)

if __name__ == '__main__':
    unittest.main()
