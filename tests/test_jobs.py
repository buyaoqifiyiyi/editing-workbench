import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine.jobs import JobManager


class JobsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jianying-engine-jobs-test-')
        self.manager = JobManager(self.temp.name, output_limit=1024)

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def launch(self, code, timeout=10):
        with patch('jianying_engine.jobs.command_for', return_value=[sys.executable, '-c', code]):
            result = await self.manager.start('test', [], timeout)
        return result['job_id']

    async def done(self, job_id):
        await asyncio.wait_for(self.manager.jobs[job_id].task, 15)
        return await self.manager.status(job_id)

    async def test_exit_codes_and_external_receipt(self):
        job = await self.launch("import json; print(json.dumps({'status':'ok'}))")
        result = await self.done(job)
        self.assertEqual((result['state'], result['returncode']), ('succeeded', 0))
        self.assertEqual(json.loads(result['stdout_tail']), {'status': 'ok'})
        self.assertTrue((Path(result['stdout_log']).parent / 'result.json').is_file())
        failed = await self.done(await self.launch("import sys;print('bad',file=sys.stderr);sys.exit(7)"))
        self.assertEqual((failed['state'], failed['returncode']), ('failed', 7))
        self.assertIn('bad', failed['stderr_tail'])

    async def test_output_cap_drains_both_pipes(self):
        job = await self.launch("import sys;sys.stdout.write('x'*100000);sys.stderr.write('y'*100000)")
        result = await self.done(job)
        self.assertEqual(result['state'], 'succeeded')
        for name in ('stdout', 'stderr'):
            self.assertTrue(result[name + '_truncated'])
            self.assertEqual(Path(result[name + '_log']).stat().st_size, 1024)

    async def test_running_job_exposes_flushed_progress(self):
        job = await self.launch("import time;print('ready',flush=True);time.sleep(60)")
        for _ in range(100):
            status = await self.manager.status(job)
            if 'ready' in status['stdout_tail']:
                break
            await asyncio.sleep(.02)
        self.assertEqual(status['state'], 'running')
        self.assertIn('ready', status['stdout_tail'])
        await self.manager.cancel(job)

    async def test_unicode_output_paging_and_refusals(self):
        expected = '中文🙂é' * 15
        job = await self.launch(f'import sys;sys.stdout.write({expected!r});sys.stderr.write("错误Ω")')
        await self.done(job)
        pages, offset = [], 0
        while True:
            page = await self.manager.read_output(job, offset=offset, limit=7)
            pages.append(page['text'])
            self.assertEqual(page['total_characters'], len(expected))
            self.assertFalse(page['truncated'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        self.assertEqual(''.join(pages), expected)
        self.assertEqual((await self.manager.read_output(job, 'stderr'))['text'], '错误Ω')
        self.assertEqual((await self.manager.read_output(job, offset=len(expected)))['text'], '')
        for kwargs in ({'stream': '../result.json'}, {'stream': 'STDOUT'}, {'offset': -1},
                       {'offset': 1.5}, {'offset': True}, {'offset': len(expected) + 1},
                       {'limit': 0}, {'limit': 65537}, {'limit': True}):
            with self.assertRaises(ValueError):
                await self.manager.read_output(job, **kwargs)
        active = await self.launch('import time;time.sleep(60)')
        self.assertEqual((await self.manager.read_output(active))['state'], 'not_ready')
        await self.manager.cancel(active)
        large = await self.launch("print('中文'*10000)")
        await self.done(large)
        clipped = await self.manager.read_output(large)
        self.assertTrue(clipped['truncated'])
        self.assertFalse(clipped['complete_log'])

    async def test_timeout_and_cancel_reap(self):
        job = await self.launch('import time;time.sleep(60)', .15)
        result = await self.done(job)
        self.assertEqual(result['state'], 'timed_out')
        self.assertIsNotNone(result['returncode'])
        job = await self.launch('import time;time.sleep(60)')
        result = await self.manager.cancel(job)
        self.assertEqual(result['state'], 'cancelled')
        self.assertIsNotNone(self.manager.jobs[job].process.poll())
        self.assertTrue(self.manager.jobs[job].task.done())

    async def test_whitelist_and_limits(self):
        with self.assertRaises(ValueError):
            JobManager(Path(self.temp.name) / 'JIANYING_ENGINE' / 'logs')
        with self.assertRaises(ValueError):
            await self.manager.start('not-a-real-operation', [])
        for timeout in (0, -1, float('nan'), float('inf'), 7201):
            with self.assertRaises(ValueError):
                await self.manager.start('doctor', [], timeout)
        one = await self.launch('import time;time.sleep(60)')
        two = await self.launch('import time;time.sleep(60)')
        with self.assertRaisesRegex(ValueError, 'Concurrent'):
            await self.launch('print(1)')
        await self.manager.cancel(one)
        await self.manager.cancel(two)
        with self.assertRaises(ValueError):
            await self.manager.status('../unknown')

    async def test_shutdown_does_not_kill_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            job = await self.launch('import time;time.sleep(60)')
            await self.manager.close()
            self.assertEqual((await self.manager.status(job))['state'], 'cancelled')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object ownership check')
    async def test_windows_child_tree_is_reaped(self):
        pid_file = Path(self.temp.name) / 'child.pid'
        code = ("import subprocess,sys,time;from pathlib import Path;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(60)")
        job = await self.launch(code)
        child_pid = ''
        for _ in range(100):
            child_pid = pid_file.read_text() if pid_file.exists() else ''
            if child_pid.isdigit():
                break
            await asyncio.sleep(.02)
        self.assertTrue(child_pid.isdigit())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, int(child_pid))
        self.assertTrue(handle)
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
            await self.manager.cancel(job)
            self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
        finally:
            kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
