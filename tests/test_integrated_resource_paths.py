"""Consolidated resources keep their original configuration and storage protection."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine import api
from jianying_engine.jobs import JobManager

sys.path.insert(0, str(api.RUNTIME / 'scripts'))
import runtime_paths


class IntegratedResourcePaths(unittest.TestCase):
    def test_integrated_and_legacy_paths_keep_local_config_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default = root / 'local/JianyingEngine/config.env'
            default.parent.mkdir(parents=True)
            default.write_text('fake-default', encoding='utf-8')
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'local'), 'JIANYING_STOCK_CONFIG': ''}):
                for relative in ('skills/jianying-editor', 'skills/editing-workbench/backends/jianying'):
                    resource = root / relative
                    resource.mkdir(parents=True)
                    config = resource / 'config.env'
                    config.write_text('fake-per-host', encoding='utf-8')
                    with patch.object(runtime_paths, 'RAW_ROOT', resource):
                        self.assertEqual(runtime_paths.stock_config(), config)
                        with patch.dict(os.environ, {'JIANYING_STOCK_CONFIG': str(default)}):
                            self.assertEqual(runtime_paths.stock_config(), default.resolve())
                        config.unlink()
                        self.assertEqual(runtime_paths.stock_config(), default)

    def test_integrated_non_home_workspace_protects_entire_skill_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / 'skills/editing-workbench/backends/jianying'
            with patch.object(runtime_paths, 'RAW_ROOT', resource):
                for target in (resource / 'cache', root / 'skills/editing-workbench/cache',
                               root / 'skills/unrelated-skill/cache'):
                    with self.subTest(target=target), self.assertRaises(ValueError):
                        runtime_paths.external_directory(target, 'cache')
                self.assertEqual(runtime_paths.external_directory(root / 'external', 'cache'),
                                 (root / 'external').resolve())

    def test_job_receipts_cannot_be_written_into_integrated_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'skills/editing-workbench/backends/jianying/jobs'
            with self.assertRaises(ValueError):
                JobManager(path)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
