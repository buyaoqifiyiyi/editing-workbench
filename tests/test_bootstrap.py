"""Offline unit tests for tiered openedit bootstrap installer and catalog safety."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openedit_engine import api

SCRIPTS = Path(__file__).resolve().parents[1] / 'src' / 'openedit_engine' / 'runtime' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import bootstrap_tools  # noqa: E402
import upstream_guard  # noqa: E402


class _FakeHttpResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BootstrapTests(unittest.TestCase):
    def test_catalog_integrity_and_no_personal_paths(self):
        ids = [entry['id'] for entry in bootstrap_tools.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 20)
        for entry in bootstrap_tools.CATALOG:
            self.assertIn(entry['tier'], bootstrap_tools.TIERS)
            self.assertTrue(entry['license'].strip(), entry['id'])
            serialized = json.dumps(entry, ensure_ascii=False)
            self.assertNotIn('MECHREVO', serialized)
            self.assertNotIn('C:\\Users\\', serialized)

    def test_dry_run_empty_and_prepopulated_temp_dirs(self):
        with tempfile.TemporaryDirectory(prefix='openedit-boot-test-') as tmp:
            root = Path(tmp)
            tools_dir = root / 'tools'
            engine_dir = root / 'engine'
            with patch.object(bootstrap_tools, '_system_ffmpeg_status', return_value={}):
                core_rep = bootstrap_tools.run_bootstrap(
                    tier='core', tools_dir=tools_dir, engine_dir=engine_dir, dry_run=True,
                )
            self.assertEqual(core_rep['status'], 'passed')
            self.assertEqual([c['id'] for c in core_rep['components']], ['filmcraft', 'effectcraft', 'ffmpeg'])
            self.assertEqual(core_rep['summary']['planned'], 3)
            self.assertFalse(tools_dir.exists())

            full_rep = bootstrap_tools.run_bootstrap(
                tier='gpu-heavy', tools_dir=tools_dir, engine_dir=engine_dir, dry_run=True,
            )
            self.assertEqual(full_rep['status'], 'passed')
            self.assertEqual(len(full_rep['components']), len(bootstrap_tools.CATALOG))

            # Pre-populate a small subset inside isolated temp dirs
            (tools_dir / 'models' / 'vad').mkdir(parents=True, exist_ok=True)
            (tools_dir / 'models' / 'vad' / 'silero_vad.onnx').write_bytes(b'vad-fixture')
            opt_init = engine_dir / 'optional' / 'pyscenedetect-0.7.2' / 'scenedetect' / '__init__.py'
            opt_init.parent.mkdir(parents=True, exist_ok=True)
            opt_init.write_text('# fixture', encoding='utf-8')
            sub_rep = bootstrap_tools.run_bootstrap(
                only=['vad', 'pyscenedetect'], tools_dir=tools_dir, engine_dir=engine_dir, dry_run=True,
            )
            self.assertEqual(sub_rep['summary']['already_installed'], 2)
            self.assertEqual(sub_rep['summary']['planned'], 0)

    def test_download_file_sha256_verify_and_atomic_cleanup(self):
        payload = b'openedit-bootstrap-verified-bytes'
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = '0' * 64
        with tempfile.TemporaryDirectory(prefix='openedit-dl-test-') as tmp:
            dest = Path(tmp) / 'nested' / 'model.onnx'
            with patch('urllib.request.urlopen', return_value=_FakeHttpResponse(payload)):
                info = bootstrap_tools.download_file('https://example.invalid/model.onnx', dest, expected_sha256=good_sha)
            self.assertEqual(info['sha256'], good_sha)
            self.assertEqual(dest.read_bytes(), payload)
            self.assertFalse(dest.with_name('model.onnx.part').exists())

            bad_dest = Path(tmp) / 'nested' / 'corrupt.onnx'
            with patch('urllib.request.urlopen', return_value=_FakeHttpResponse(payload)):
                with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                    bootstrap_tools.download_file('https://example.invalid/bad.onnx', bad_dest, expected_sha256=bad_sha)
            self.assertFalse(bad_dest.exists())
            self.assertFalse(bad_dest.with_name('corrupt.onnx.part').exists())

    def test_zip_slip_rejected_by_safe_extract(self):
        with tempfile.TemporaryDirectory(prefix='openedit-zip-test-') as tmp:
            root = Path(tmp)
            zpath = root / 'evil.zip'
            with zipfile.ZipFile(zpath, 'w') as zf:
                zf.writestr('../escape.txt', 'bad')
            with self.assertRaises(RuntimeError):
                upstream_guard._safe_extract_zip(zpath, root / 'out')

    def test_only_selection_unknown_rejection_and_api_schema(self):
        with self.assertRaisesRegex(ValueError, 'Unknown component'):
            bootstrap_tools.select_components(only=['no-such-model'])
        chosen = bootstrap_tools.select_components(only=['vad', 'flow'])
        self.assertEqual([c['id'] for c in chosen], ['vad', 'flow'])

        tokens = api.arguments_from_params('bootstrap', {
            'tier': 'ai',
            'only': ['vad', 'flow'],
            'hf_endpoint': 'https://hf-mirror.com',
            'dry_run': True,
        })
        self.assertIn('--tier', tokens)
        self.assertIn('--dry-run', tokens)
        self.assertIn('https://hf-mirror.com', tokens)
        cmd = api.command_for('bootstrap', tokens)
        self.assertEqual(Path(cmd[1]).name, 'bootstrap_tools.py')


if __name__ == '__main__':
    unittest.main()
