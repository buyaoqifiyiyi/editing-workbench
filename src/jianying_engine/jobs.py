"""Bounded, process-owned jobs for one MCP server lifetime; no durable task recovery."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from .api import command_for


class _ProcessTree:
    """A Windows Job Object or a private POSIX process group, never a process-name search."""

    def __init__(self):
        self.handle = None
        self.process = None
        if os.name == 'nt':
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

            class Basic(ctypes.Structure):
                _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                            ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                            ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                            ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]

            class Limits(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                            ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                            ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

    def attach_and_resume(self, process):
        self.process = process
        if os.name == 'nt':
            # CPython closes the primary thread handle; NtResumeProcess resumes this still-suspended process.
            if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
            resume = ctypes.WinDLL('ntdll').NtResumeProcess
            resume.argtypes, resume.restype = [wintypes.HANDLE], ctypes.c_long
            if resume(wintypes.HANDLE(int(process._handle))) != 0:
                raise OSError('Could not resume the owned job process')

    def close(self):
        if os.name == 'nt':
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process = None


def _spawn(command):
    tree = _ProcessTree()
    process = None
    try:
        options = ({'creationflags': subprocess.CREATE_NO_WINDOW | 0x4} if os.name == 'nt'
                   else {'start_new_session': True})  # CREATE_SUSPENDED before Job Object assignment.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False,
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'),
                                   **options)
        tree.attach_and_resume(process)
        return process, tree
    except BaseException:
        tree.close()
        if process is not None:
            process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()
        raise


def _capture(stream, destination, limit):
    total = 0
    with stream, destination.open('xb') as output:
        while chunk := stream.read1(65536):
            remaining = max(0, limit - total)
            output.write(chunk[:remaining])
            output.flush()
            total += len(chunk)
    return total


@dataclass
class _Job:
    job_id: str
    operation: str
    process: subprocess.Popen
    tree: _ProcessTree
    directory: Path
    started: float = field(default_factory=time.time)
    state: str = 'running'
    returncode: int | None = None
    finished: float | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    error: str | None = None
    task: asyncio.Task | None = None


class JobManager:
    def __init__(self, state_dir=None, *, output_limit=1024 * 1024, concurrent_limit=2, job_limit=128):
        default = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'state')) / 'JianyingEngine' / 'jobs'
        self.state_dir = Path(state_dir or default).expanduser().resolve()
        protected = {'jianying_engine', 'editing-workbench', 'jianying-editor', 'jianying-planner', 'jianying-editor-maintainer'}
        if any(part.name.casefold() in protected for part in (self.state_dir, *self.state_dir.parents)):
            raise ValueError('state_dir must be outside the engine package and Skill directories')
        if output_limit < 1 or concurrent_limit < 1 or job_limit < 1:
            raise ValueError('Job limits must be positive')
        self.output_limit, self.concurrent_limit, self.job_limit = output_limit, concurrent_limit, job_limit
        self.jobs = {}
        self.closed = False
        self._lock = asyncio.Lock()

    async def start(self, operation, arguments, timeout_seconds=1800):
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 7200:
            raise ValueError('timeout_seconds must be finite and in (0, 7200]')
        command = command_for(operation, arguments)
        async with self._lock:
            if self.closed:
                raise ValueError('Job manager has closed')
            if len(self.jobs) >= self.job_limit:
                raise ValueError('This server reached its retained job limit; start a new server session')
            if sum(not job.task.done() for job in self.jobs.values()) >= self.concurrent_limit:
                raise ValueError('Concurrent job limit reached; finish or cancel an active job')
            job_id = uuid.uuid4().hex
            directory = self.state_dir / job_id
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
            launching = asyncio.create_task(asyncio.to_thread(_spawn, command))
            try:
                process, tree = await asyncio.shield(launching)
            except asyncio.CancelledError:
                process, tree = await launching
                tree.close()
                await asyncio.to_thread(process.wait)
                process.stdout.close()
                process.stderr.close()
                raise
            job = _Job(job_id, operation, process, tree, directory)
            self.jobs[job_id] = job
            job.task = asyncio.create_task(self._watch(job, timeout_seconds))
        return await self.status(job_id)

    async def _watch(self, job, timeout):
        stdout = asyncio.create_task(asyncio.to_thread(_capture, job.process.stdout, job.directory / 'stdout.log', self.output_limit))
        stderr = asyncio.create_task(asyncio.to_thread(_capture, job.process.stderr, job.directory / 'stderr.log', self.output_limit))
        waiting = asyncio.create_task(asyncio.to_thread(job.process.wait))
        try:
            try:
                await asyncio.wait_for(asyncio.shield(waiting), timeout)
            except asyncio.TimeoutError:
                if job.state == 'running':
                    job.state = 'timing_out'
            finally:
                job.tree.close()  # Includes descendants that outlive a normally completed parent.
                job.returncode = await waiting
            job.stdout_bytes, job.stderr_bytes = await asyncio.gather(stdout, stderr)
            if job.state == 'running':
                job.state = 'succeeded' if job.returncode == 0 else 'failed'
            elif job.state in ('cancelling', 'timing_out'):
                job.state = {'cancelling': 'cancelled', 'timing_out': 'timed_out'}[job.state]
        except Exception as exc:
            job.state, job.error = 'failed', f'{type(exc).__name__}: {exc}'
        finally:
            job.finished = time.time()
            await asyncio.to_thread(self._save, job)

    def _get(self, job_id):
        if job_id not in self.jobs:
            raise ValueError('Unknown job_id in this server session; jobs are not restored across restarts')
        return self.jobs[job_id]

    def _snapshot(self, job):
        result = {'job_id': job.job_id, 'operation': job.operation, 'state': job.state,
                  'returncode': job.returncode, 'started': job.started, 'finished': job.finished,
                  'error': job.error, 'stdout_log': str(job.directory / 'stdout.log'),
                  'stderr_log': str(job.directory / 'stderr.log'),
                  'stdout_truncated': job.stdout_bytes > self.output_limit,
                  'stderr_truncated': job.stderr_bytes > self.output_limit}
        for name in ('stdout', 'stderr'):
            path = job.directory / (name + '.log')
            if path.exists():
                with path.open('rb') as handle:
                    handle.seek(max(0, path.stat().st_size - 8192))
                    result[name + '_tail'] = handle.read(8192).decode('utf-8', errors='replace')
            else:
                result[name + '_tail'] = ''
        return result

    def _save(self, job):
        (job.directory / 'result.json').write_text(json.dumps(self._snapshot(job), ensure_ascii=False, indent=2), encoding='utf-8')

    async def status(self, job_id):
        return await asyncio.to_thread(self._snapshot, self._get(job_id))

    async def read_output(self, job_id, stream='stdout', offset=0, limit=32768):
        if stream not in ('stdout', 'stderr'):
            raise ValueError('stream must be stdout or stderr')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError('offset must be a nonnegative integer; limit must be an integer in [1, 65536]')
        job = self._get(job_id)
        if not job.task.done():
            return {'job_id': job_id, 'state': 'not_ready', 'error': 'Wait for the job to finish before paging its output'}

        def read_page():
            path = job.directory / (stream + '.log')
            text = path.read_bytes().decode('utf-8', errors='replace')
            if offset > len(text):
                raise ValueError('offset exceeds total_characters')
            end = min(len(text), offset + limit)
            truncated = getattr(job, stream + '_bytes') > self.output_limit
            return {'job_id': job_id, 'state': job.state, 'stream': stream, 'offset': offset,
                    'text': text[offset:end], 'next_offset': end if end < len(text) else None,
                    'total_characters': len(text), 'truncated': truncated,
                    'complete_log': not truncated and job.error is None,
                    'encoding': 'utf-8 with replacement for invalid or truncated bytes'}

        return await asyncio.to_thread(read_page)

    async def cancel(self, job_id):
        job = self._get(job_id)
        if job.process.poll() is None and job.state == 'running':
            job.state = 'cancelling'
            job.tree.close()
        await asyncio.shield(job.task)
        return await self.status(job_id)

    async def close(self):
        async with self._lock:
            self.closed = True
            for job in self.jobs.values():
                if job.process.poll() is None and job.state == 'running':
                    job.state = 'cancelling'
                    job.tree.close()
        await asyncio.gather(*(asyncio.shield(job.task) for job in self.jobs.values()))
