"""Owned Codex app-server JSONL bridge. It never logs in or retries model turns."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .jobs import _ProcessTree
from . import __version__

REQUEST_METHODS = frozenset({
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/permissions/requestApproval', 'item/tool/requestUserInput',
    'mcpServer/elicitation/request', 'applyPatchApproval', 'execCommandApproval',
})
MAX_LINE = 8 * 1024 * 1024
EXECUTION_POLICY = {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}


class CodexRpcError(RuntimeError):
    """Classify known server failures without exposing arbitrary provider messages."""

    def __init__(self, code, message=''):
        self.code = code
        self.active_writer_conflict = (code == -32600 and isinstance(message, str)
                                       and re.fullmatch(r'thread [A-Za-z0-9-]+ already has an active writer', message.strip()) is not None)
        detail = '旧会话正被其他 Codex 进程占用。' if self.active_writer_conflict else 'request failed'
        super().__init__(f'Codex RPC {code}: {detail}')


def _spawn(command, env=None):
    tree, process = _ProcessTree(), None
    try:
        options = ({'creationflags': subprocess.CREATE_NO_WINDOW | 0x4} if os.name == 'nt'
                   else {'start_new_session': True})
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, env=dict(os.environ, **(env or {})), **options)
        tree.attach_and_resume(process)
        return process, tree
    except BaseException:
        tree.close()
        if process is not None:
            process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
        raise


def _finish(process, tree):
    tree.close()
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _version(path):
    process, tree = _spawn([path, '--version'])
    try:
        stdout, _ = process.communicate(timeout=10)
        if process.returncode:
            raise RuntimeError('Codex version check failed')
        match = re.search(r'codex(?:-cli)?\s+([^\s]+)', stdout.decode('utf-8', errors='replace'))
        if not match:
            raise ValueError('Selected executable did not identify itself as Codex CLI')
        return match.group(1)
    finally:
        _finish(process, tree)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')
    return value


def _cwd(value):
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('cwd must be an existing directory')
    return str(path)


class CodexBridge:
    request_timeout = 30.0

    def __init__(self, on_event, codex_path=None):
        if not callable(on_event):
            raise TypeError('on_event must be callable')
        self.on_event, self.codex_path = on_event, codex_path
        self.state = 'disconnected'
        self._process = self._tree = None
        self._reader_task = self._stderr_task = None
        self._pending, self._server_requests = {}, {}
        self._next_id = 0
        self._write_lock, self._connect_lock = asyncio.Lock(), asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._event_tasks = set()
        self._info = None
        self.stderr_summary = {'bytes': 0, 'lines': 0}
        self._closed_event_sent = False

    def _emit(self, event):
        async def deliver():
            try:
                result = self.on_event(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # A failed UI consumer cannot stall RPC responses or approve a pending request.
                pass
        task = asyncio.create_task(deliver())
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def _send(self, message):
        raw = (json.dumps(message, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > MAX_LINE:
            raise ValueError('Codex message exceeds the bridge size limit')
        async with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or self.state in ('closed', 'disconnected'):
                raise ConnectionError('Codex connection is closed')
            def write():
                process.stdin.write(raw)
                process.stdin.flush()
            try:
                await asyncio.to_thread(write)
            except (OSError, ValueError) as exc:
                raise ConnectionError('Codex transport write failed') from exc

    async def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({'id': request_id, 'method': method, 'params': params})
            return await asyncio.wait_for(asyncio.shield(future), self.request_timeout)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve a disconnect error even if cancellation interrupted the send.

    async def _read_stderr(self, process):
        # Keep only bounded metadata, never raw logs that could contain provider URLs or credentials.
        while chunk := await asyncio.to_thread(process.stderr.read1, 4096):
            self.stderr_summary['bytes'] = min(2**63 - 1, self.stderr_summary['bytes'] + len(chunk))
            self.stderr_summary['lines'] = min(2**63 - 1, self.stderr_summary['lines'] + chunk.count(b'\n'))

    def _disconnected(self, error):
        if self.state != 'closed':
            self.state = 'disconnected'
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(error or 'Codex connection closed'))
        self._pending.clear()
        self._server_requests.clear()
        if not self._closed_event_sent:
            self._closed_event_sent = True
            self._emit({'kind': 'connection_closed', 'error': error})

    async def _read_stdout(self, process):
        error = None
        try:
            while line := await asyncio.to_thread(process.stdout.readline, MAX_LINE + 1):
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    raise ValueError('Invalid JSONL framing')
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('Expected a JSON object')
                request_id, method = message.get('id'), message.get('method')
                if 'method' in message:
                    if not isinstance(method, str):
                        raise ValueError('Invalid method')
                    params = message.get('params', {})
                    if 'id' in message:
                        if type(request_id) not in (str, int):
                            raise ValueError('Invalid request id')
                        if method not in REQUEST_METHODS:
                            await self._send({'id': request_id, 'error': {'code': -32601, 'message': 'Client does not support this server request'}})
                            self._emit({'kind': 'unsupported_request', 'id': request_id, 'method': method})
                        else:
                            if request_id in self._server_requests:
                                raise ValueError('Duplicate outstanding server request id')
                            self._server_requests[request_id] = {'method': method, 'params': params}
                            self._emit({'kind': 'request', 'id': request_id, 'method': method, 'params': params})
                    else:
                        if method == 'serverRequest/resolved' and isinstance(params, dict):
                            self._server_requests.pop(params.get('requestId'), None)
                        if method == 'turn/completed' and isinstance(params, dict):
                            turn_id = (params.get('turn') or {}).get('id')
                            for key, pending in list(self._server_requests.items()):
                                p = pending.get('params') or {}
                                if p.get('threadId') == params.get('threadId') and p.get('turnId') == turn_id:
                                    self._server_requests.pop(key, None)
                        # Account notifications need no identity details in the workbench.
                        if method.startswith('account/'):
                            params = {}
                        self._emit({'kind': 'notification', 'method': method, 'params': params})
                elif 'id' in message and type(request_id) in (str, int):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        if 'error' in message:
                            code = (message.get('error') or {}).get('code', 'unknown')
                            future.set_exception(CodexRpcError(code, (message.get('error') or {}).get('message', '')))
                        elif 'result' in message:
                            future.set_result(message['result'])
                        else:
                            raise ValueError('RPC response has no result or error')
                else:
                    raise ValueError('Invalid JSON-RPC message')
        except asyncio.CancelledError:
            error = 'Codex reader cancelled'
            raise
        except Exception as exc:
            # Do not echo malformed stdout or provider error text into logs/UI.
            error = f'Codex transport failed ({type(exc).__name__})'
        finally:
            self._disconnected(error)
            if self._tree is not None:
                self._tree.close()

    async def connect(self):
        async with self._connect_lock:
            if self.state == 'ready':
                return dict(self._info)
            if self._process is not None:
                await self.close()
            path = self.codex_path or shutil.which('codex.exe') or shutil.which('codex')
            if not path or not Path(path).is_file():
                raise FileNotFoundError('Codex CLI executable was not found; install it or supply codex_path')
            path = str(Path(path).resolve())
            if Path(path).suffix.lower() in ('.cmd', '.bat', '.ps1'):
                raise ValueError('Supply the real Codex executable, not a shell wrapper')
            version = await asyncio.to_thread(_version, path)
            self.state, self._closed_event_sent = 'connecting', False
            self.stderr_summary = {'bytes': 0, 'lines': 0}
            launching = asyncio.create_task(asyncio.to_thread(_spawn, [path, 'app-server', '--listen', 'stdio://']))
            try:
                self._process, self._tree = await asyncio.shield(launching)
            except asyncio.CancelledError:
                process, tree = await launching
                await asyncio.to_thread(_finish, process, tree)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                self.state = 'disconnected'
                raise
            self._reader_task = asyncio.create_task(self._read_stdout(self._process))
            self._stderr_task = asyncio.create_task(self._read_stderr(self._process))
            try:
                await self._request('initialize', {'clientInfo': {'name': 'jianying_workbench', 'title': 'Jianying Workbench', 'version': __version__}})
                await self._send({'method': 'initialized', 'params': {}})
                models, cursor, seen = [], None, set()
                while True:
                    params = {'limit': 100}
                    if cursor is not None:
                        params['cursor'] = cursor
                    page = await self._request('model/list', params)
                    if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                        raise ValueError('Codex returned an invalid model list')
                    models.extend(page['data'])
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise ValueError('Codex model pagination did not advance')
                    seen.add(cursor)
                authenticated = None
                try:
                    account = await self._request('account/read', {'refreshToken': False})
                    if isinstance(account, dict) and 'account' in account:
                        authenticated = (True if account['account'] is not None else
                                         False if account.get('requiresOpenaiAuth') is True else None)
                except RuntimeError:
                    pass  # Older/custom-provider servers may not expose account state.
                self.state = 'ready'
                self._info = {'cli_path': path, 'version': version, 'models': models,
                              'account': {'authenticated': authenticated}, 'state': 'ready'}
                return dict(self._info)
            except BaseException:
                await self.close()
                raise

    def _ready(self):
        if self.state != 'ready':
            raise ConnectionError('Connect the Codex bridge first')

    async def start_thread(self, cwd, developer_instructions):
        self._ready()
        if not isinstance(developer_instructions, str):
            raise ValueError('developer_instructions must be text')
        result = await self._request('thread/start', {'cwd': _cwd(cwd), 'developerInstructions': developer_instructions,
                                     **EXECUTION_POLICY,
                                     'approvalsReviewer': 'user'})
        return _identifier(result['thread']['id'], 'thread id')

    async def resume_thread(self, thread_id, cwd):
        self._ready()
        return await self._request('thread/resume', {'threadId': _identifier(thread_id, 'thread_id'), 'cwd': _cwd(cwd),
                                   **EXECUTION_POLICY, 'approvalsReviewer': 'user',
                                   **({'developerInstructions': self._workbench_instructions} if hasattr(self, '_workbench_instructions') else {})})

    def set_instructions(self, thread_id, text):
        _identifier(thread_id, 'thread_id')
        if not isinstance(text, str):
            raise ValueError('Instructions must be text')
        self._workbench_instructions = text

    async def fork_thread(self, thread_id, cwd):
        self._ready()
        result = await self._request('thread/fork', {
            'threadId': _identifier(thread_id, 'thread_id'), 'cwd': _cwd(cwd),
            **EXECUTION_POLICY, 'approvalsReviewer': 'user',
            **({'developerInstructions': self._workbench_instructions} if hasattr(self, '_workbench_instructions') else {})})
        _identifier(result['thread']['id'], 'forked thread id')
        return result

    async def start_turn(self, thread_id, text, model=None, effort=None, images=None):
        self._ready()
        if not isinstance(text, str) or (not text.strip() and not images):
            raise ValueError('A turn needs text or local images')
        items = [{'type': 'text', 'text': text}] if text else []
        if images is not None:
            if not isinstance(images, (list, tuple)):
                raise ValueError('images must be a list of local file paths')
            for image in images:
                path = Path(image).expanduser().resolve()
                if not path.is_file():
                    raise ValueError('Image path must name an existing local file')
                items.append({'type': 'localImage', 'path': str(path)})
        params = {'threadId': _identifier(thread_id, 'thread_id'), 'input': items,
                  'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}}
        if model is not None:
            params['model'] = _identifier(model, 'model')
        if effort is not None:
            params['effort'] = _identifier(effort, 'effort')
        result = await self._request('turn/start', params)
        return result['turn']

    async def interrupt(self, thread_id, turn_id):
        self._ready()
        return await self._request('turn/interrupt', {'threadId': _identifier(thread_id, 'thread_id'),
                                                    'turnId': _identifier(turn_id, 'turn_id')})

    async def respond(self, request_id, result):
        self._ready()
        if type(request_id) not in (str, int) or request_id not in self._server_requests:
            raise ValueError('No pending server request with this id')
        if not isinstance(result, dict):
            raise ValueError('Server request result must be an object')
        self._server_requests.pop(request_id)
        await self._send({'id': request_id, 'result': result})

    async def close(self):
        async with self._close_lock:
            self.state = 'closed'
            process, tree = self._process, self._tree
            self._disconnected(None)
            if process is not None:
                await asyncio.to_thread(_finish, process, tree)
                tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
            self._process = self._tree = None
            self._reader_task = self._stderr_task = None
            # Flush ordinary synchronous event collectors, without waiting on interactive UI callbacks.
            await asyncio.sleep(0)
