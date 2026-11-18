"""Real owned child and workbench polling, with an isolated synthetic report and install."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine import software_backends
from jianying_engine.workbench import Workbench


class RequalificationProcessIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_owned_worker_reports_partial_without_duplicate_start(self):
        with tempfile.TemporaryDirectory(prefix='wb-review-integration-') as directory:
            root = Path(directory)
            candidate = root / '99.0.0.1'
            candidate.mkdir()
            (candidate / 'runtime.bin').write_bytes(b'isolated fixture')
            (candidate / 'JianyingPro.exe').write_bytes(b'not an executable')
            workspace = root / 'workspace'
            workspace.mkdir()
            detections = {entry['id']: {'installed': True, 'compatible': entry['single_supported'],
                'selectable': entry['single_supported'], 'state': 'available',
                'label': 'fixture', 'reason': 'fixture', 'path': None, 'version': None}
                for entry in software_backends.catalog()}
            detections['jianying'].update(installed=True, compatible=True, selectable=True,
                                         path=str(candidate / 'JianyingPro.exe'), version=candidate.name)
            fixture = Path(__file__).with_name('fixtures') / 'requalification_worker_fixture.py'
            def command(operation, arguments):
                self.assertEqual(operation, 'requalify')
                return [sys.executable, str(fixture), *arguments]
            env = {'JIANYING_RUNTIME_ADMISSIONS': str(root / 'admissions.json'),
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            with patch.dict(os.environ, env), patch('jianying_engine.workbench.detect_backends', return_value=detections), \
                    patch('jianying_engine.workbench_requalification.command_for', side_effect=command) as launch:
                service = Workbench(root / 'state', workspace)
                try:
                    async with asyncio.timeout(30):
                        while True:
                            catalog = service.backend_state()['catalog']
                            item = next(x for x in catalog if x['id'] == 'jianying')
                            review = item['availability'].get('requalification', {})
                            if review.get('state') in ('partial', 'failed', 'complete'):
                                break
                            await asyncio.sleep(.1)
                    self.assertEqual(review['state'], 'partial', review)
                    self.assertTrue(item['selectable'])
                    self.assertEqual(review['subsets']['preview']['status'], 'admitted')
                    self.assertEqual(review['subsets']['mix']['status'], 'refused')
                    self.assertEqual(review['sequence']['status'], 'unverified')
                    self.assertEqual(launch.call_count, 1)
                    for _ in range(3):
                        service.backend_state()
                    self.assertEqual(launch.call_count, 1)
                    self.assertTrue(Path(review['report']).is_file())
                finally:
                    await service.close()


if __name__ == '__main__':
    unittest.main()
