"""ACP v1 adapter using owned JSONL transport, without client filesystem or terminal access."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import re
import shutil
import sys
import uuid

from .codex_bridge import CodexBridge, MAX_LINE, _spawn, _finish, _cwd, _identifier
from . import __version__


class _RpcError(RuntimeError):
    def __init__(self, code, message='request failed'):
        self.code = code
        message = str(message)[:4096]
        message = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', message)
        message = re.sub(r'(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|password|secret)\b["\']?\s*[:=]\s*["\']?)[^\s,;"\'}]+', r'\1[redacted]', message)
        message = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', message)
        message = re.sub(r'\bsk-[A-Za-z0-9_-]{6,}', '[redacted]', message)
        super().__init__(f'ACP RPC {code}: {message[:1000]}')


class AcpBridge(CodexBridge):
    def __init__(self, on_event, command, environment=None):
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v or '\x00' in v for v in command):
            raise ValueError('ACP command must be a nonempty executable/argument list')
        super().__init__(on_event)
        self.command = list(command)
        if environment is not None and (not isinstance(environment, dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())):
            raise ValueError('ACP environment must contain string names and values')
        self.environment = dict(environment or {})
        self.capabilities = {}
        self._instructions, self._sessions = {}, set()
        self._session_configs = {}
        self._active_turn = self._prompt_task = None
        self._tools = {}
        self._closing_requested = False

    async def _send(self, message):
        try:
            await super()._send({'jsonrpc': '2.0', **message})
        except ConnectionError as exc:
            raise ConnectionError('ACP transport is closed or unavailable') from exc

    def _ready(self):
        if self.state != 'ready':
            raise ConnectionError('Connect the ACP Agent first')

    async def _rpc(self, method, params, timeout=30):
        self._next_id += 1
        identity = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        try:
            await self._send({'id': identity, 'method': method, 'params': params})
            if timeout is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            self._pending.pop(identity, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _disconnected(self, error):
        if self.state != 'closed':
            self.state = 'disconnected'
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(error or 'ACP connection closed'))
        self._pending.clear()
        self._server_requests.clear()
        if not self._closed_event_sent:
            self._closed_event_sent = True
            self._emit({'kind': 'connection_closed', 'error': error})

    def _notify(self, method, params):
        self._emit({'kind': 'notification', 'method': method, 'params': params})

    async def _permission_result(self, identity, outcome):
        request = self._server_requests.pop(identity, None)
        if request is None:
            return  # A simultaneous prompt response/cancellation already cleared this request.
        await self._send({'id': identity, 'result': {'outcome': outcome}})
        self._notify('serverRequest/resolved', {'threadId': request['params'].get('sessionId'), 'requestId': identity})

    async def _read_stdout(self, process):
        error = None
        try:
            while line := await asyncio.to_thread(process.stdout.readline, MAX_LINE + 1):
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    raise ValueError('Invalid ACP JSONL framing')
                message = json.loads(line)
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('Expected ACP JSON-RPC 2.0')
                identity, method = message.get('id'), message.get('method')
                if 'method' in message:
                    if not isinstance(method, str) or not isinstance(message.get('params', {}), dict):
                        raise ValueError('Invalid ACP method or params')
                    params = message.get('params', {})
                    if 'id' in message:
                        if type(identity) not in (int, str):
                            raise ValueError('Invalid ACP request id')
                        if method != 'session/request_permission':
                            await self._send({'id': identity, 'error': {'code': -32601, 'message': 'Client capability not supported'}})
                            self._emit({'kind': 'unsupported_request', 'id': identity, 'method': method})
                        elif identity in self._server_requests:
                            raise ValueError('Duplicate ACP permission id')
                        elif not isinstance(params.get('options'), list) or not isinstance(params.get('sessionId'), str):
                            await self._send({'id': identity, 'error': {'code': -32602, 'message': 'Invalid permission request'}})
                        else:
                            self._server_requests[identity] = {'params': params}
                            active = self._active_turn
                            if not active or active['sessionId'] != params['sessionId'] or active['cancelling']:
                                await self._permission_result(identity, {'outcome': 'cancelled'})
                            else:
                                self._emit({'kind': 'request', 'id': identity, 'method': 'acp/requestPermission', 'params': params})
                    elif method == 'session/update':
                        self._session_update(params)
                    # Other optional ACP notifications are not misrepresented as Codex events.
                elif 'id' in message and type(identity) in (int, str):
                    future = self._pending.get(identity)
                    if future is not None and not future.done():
                        if 'error' in message:
                            code = (message.get('error') or {}).get('code', 'unknown')
                            future.set_exception(_RpcError(code, (message.get('error') or {}).get('message', 'request failed')))
                        elif 'result' in message:
                            future.set_result(message['result'])
                        else:
                            raise ValueError('ACP response missing result/error')
                else:
                    raise ValueError('Invalid ACP response')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f'ACP transport failed ({type(exc).__name__})'
        finally:
            self._disconnected(error)
            if self._tree:
                self._tree.close()

    def _session_update(self, params):
        identity, update = params.get('sessionId'), params.get('update')
        if identity in self._sessions and isinstance(update, dict):
            if update.get('sessionUpdate') == 'config_option_update':
                self._cache_session(identity, update)
            elif update.get('sessionUpdate') == 'current_mode_update':
                self._session_configs[identity].setdefault('modes', {})['currentModeId'] = update.get('currentModeId')
        active = self._active_turn
        if not active or params.get('sessionId') != active['sessionId']:
            return  # session/load replay must not be appended again to persisted Workbench messages.
        update = params.get('update')
        if not isinstance(update, dict):
            return
        kind = update.get('sessionUpdate')
        common = {'threadId': active['sessionId'], 'turnId': active['id']}
        if kind == 'agent_message_chunk':
            content = update.get('content') or {}
            if content.get('type') == 'text' and isinstance(content.get('text'), str):
                item_id = active['id'] + ':' + str(update.get('messageId') or 'message')
                self._notify('item/agentMessage/delta', {**common, 'itemId': item_id, 'delta': content['text']})
        elif kind == 'plan':
            statuses = {'pending': 'pending', 'in_progress': 'inProgress', 'completed': 'completed'}
            self._notify('turn/plan/updated', {**common, 'plan': [
                {'step': entry['content'], 'status': statuses[entry['status']]}
                for entry in update.get('entries', []) if isinstance(entry, dict)
                and isinstance(entry.get('content'), str) and entry.get('status') in statuses]})
        elif kind in ('tool_call', 'tool_call_update'):
            identity = update.get('toolCallId')
            if not isinstance(identity, str):
                return
            previous = self._tools.setdefault(identity, {})
            previous.update({k: v for k, v in update.items() if v is not None})
            status = previous.get('status', 'pending')
            item = {'id': active['id'] + ':tool:' + identity, 'type': 'mcpToolCall',
                    'server': 'ACP', 'tool': previous.get('title') or previous.get('name') or identity,
                    'status': status}
            if status == 'failed':
                item['error'] = {'message': 'Agent tool reported failure'}
            event = 'item/completed' if status in ('completed', 'failed') else 'item/started'
            self._notify(event, {**common, 'item': item})

    async def connect(self):
        async with self._connect_lock:
            if self.state == 'ready':
                return dict(self._info)
            if self._process is not None:
                await self.close()
            executable = shutil.which(self.command[0]) or self.command[0]
            if not Path(executable).is_file():
                raise FileNotFoundError('ACP executable was not found; configure an installed Agent command')
            if Path(executable).suffix.lower() in ('.cmd', '.bat', '.ps1'):
                raise ValueError('Use the real Agent executable, or node.exe plus its ACP script; shell wrappers are not supported')
            executable = str(Path(executable).resolve())
            self.state, self._closed_event_sent, self._closing_requested = 'connecting', False, False
            self.stderr_summary = {'bytes': 0, 'lines': 0}
            launching = asyncio.create_task(asyncio.to_thread(_spawn, [executable, *self.command[1:]], env=self.environment))
            try:
                self._process, self._tree = await asyncio.shield(launching)
            except asyncio.CancelledError:
                process, tree = await launching
                await asyncio.to_thread(_finish, process, tree)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                self.state = 'disconnected'
                raise
            except BaseException:
                self.state = 'disconnected'
                raise
            self._reader_task = asyncio.create_task(self._read_stdout(self._process))
            self._stderr_task = asyncio.create_task(self._read_stderr(self._process))
            try:
                result = await self._rpc('initialize', {'protocolVersion': 1,
                    'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
                    'clientInfo': {'name': 'jianying-workbench', 'version': __version__}}, self.request_timeout)
                if not isinstance(result, dict) or type(result.get('protocolVersion')) is not int or result['protocolVersion'] != 1:
                    raise ValueError('Agent did not negotiate ACP v1; unsupported protocol')
                self.capabilities = result.get('agentCapabilities') or {}
                self.state = 'ready'
                self._info = {'state': 'ready', 'version': (result.get('agentInfo') or {}).get('version', 'ACP v1'),
                              'cli_path': executable, 'models': [], 'account': {'authenticated': None},
                              'capabilities': {'loadSession': self._resume_method() is not None,
                                               'resumeMethod': self._resume_method(),
                                               'modelSelection': False, 'effortSelection': False},
                              'permission_scope': 'Agent-managed; client fs/terminal disabled; instructions are prompt text, not a sandbox'}
                return dict(self._info)
            except BaseException:
                await self.close()
                raise

    @staticmethod
    def _session_params(cwd):
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').is_file():
            python = python.with_name('python.exe')  # MCP needs stdio even when the desktop host is windowless.
        return {'cwd': _cwd(cwd), 'mcpServers': [{'name': 'jianying-engine', 'command': str(python),
                'args': ['-m', 'jianying_engine.mcp_server'], 'env': []}]}

    async def start_thread(self, cwd, developer_instructions):
        self._ready()
        if not isinstance(developer_instructions, str):
            raise ValueError('developer_instructions must be text')
        result = await self._rpc('session/new', self._session_params(cwd), self.request_timeout)
        identity = _identifier(result.get('sessionId'), 'sessionId')
        self._sessions.add(identity)
        self._cache_session(identity, result)
        self._instructions[identity] = developer_instructions
        return identity

    def set_instructions(self, thread_id, text):
        if thread_id not in self._sessions or not isinstance(text, str):
            raise ValueError('Instructions require an owned ACP session and text')
        self._instructions[thread_id] = text

    async def resume_thread(self, thread_id, cwd):
        self._ready()
        method = self._resume_method()
        if method is None:
            raise ValueError('This Agent cannot load existing sessions; explicitly create a new conversation instead')
        identity = _identifier(thread_id, 'sessionId')
        result = await self._rpc(method, {'sessionId': identity, **self._session_params(cwd)}, self.request_timeout)
        self._sessions.add(identity)
        self._cache_session(identity, result)
        return {'thread': {'id': identity}, 'acp': result}

    def _resume_method(self):
        capabilities = self.capabilities.get('sessionCapabilities') or {}
        if isinstance(capabilities, dict) and isinstance(capabilities.get('resume'), dict):
            return 'session/resume'
        return 'session/load' if self.capabilities.get('loadSession') is True else None

    def _cache_session(self, identity, result):
        if not isinstance(result, dict):
            raise ValueError('ACP session response must be an object')
        cached = self._session_configs.setdefault(identity, {})
        for key in ('configOptions', 'models', 'modes'):
            if key in result:
                cached[key] = result[key]

    @staticmethod
    def _options(option):
        values = []
        for item in option.get('options', []):
            if not isinstance(item, dict):
                continue
            if isinstance(item.get('options'), list):
                values.extend(AcpBridge._options(item))
            elif isinstance(item.get('value'), str):
                values.append(item)
        return values

    def _config(self, identity, category):
        options = self._session_configs.get(identity, {}).get('configOptions', [])
        options = options if isinstance(options, list) else []
        aliases = {'model': ('model',), 'thought_level': ('reasoning_effort', 'thought_level'), 'mode': ('mode',)}
        return next((o for o in options if isinstance(o, dict) and o.get('type') == 'select' and o.get('category') == category), None) or next(
            (o for o in options if isinstance(o, dict) and o.get('type') == 'select' and o.get('id') in aliases[category]), None)

    def session_info(self, thread_id):
        if thread_id not in self._sessions:
            raise ValueError('Create or load this ACP session first')
        cached = self._session_configs.get(thread_id, {})
        model, effort, mode = (self._config(thread_id, key) for key in ('model', 'thought_level', 'mode'))
        efforts = [{'reasoningEffort': o['value'], 'displayName': o.get('name', o['value'])} for o in self._options(effort or {})]
        legacy_models = cached.get('models') if isinstance(cached.get('models'), dict) else {}
        models = [{'id': o['value'], 'model': o['value'], 'displayName': o.get('name', o['value']),
                   'supportedReasoningEfforts': efforts} for o in self._options(model or {})]
        if model is None:
            models = [{'id': o['modelId'], 'model': o['modelId'], 'displayName': o.get('name', o['modelId']),
                       'supportedReasoningEfforts': efforts} for o in legacy_models.get('availableModels', [])
                      if isinstance(o, dict) and isinstance(o.get('modelId'), str)]
        legacy_modes = cached.get('modes') if isinstance(cached.get('modes'), dict) else {}
        modes = [{'id': o['value'], 'name': o.get('name', o['value']), 'description': o.get('description', '')}
                 for o in self._options(mode or {})] if mode else [
                    {'id': o['id'], 'name': o.get('name', o['id']), 'description': o.get('description', '')}
                    for o in legacy_modes.get('availableModes', []) if isinstance(o, dict) and isinstance(o.get('id'), str)]
        return {'models': models, 'currentModel': model.get('currentValue') if model else legacy_models.get('currentModelId'),
                'currentEffort': effort.get('currentValue') if effort else None,
                'configOptions': cached.get('configOptions', []),
                'currentMode': mode.get('currentValue') if mode else legacy_modes.get('currentModeId'), 'modes': modes}

    async def set_session_options(self, thread_id, model=None, effort=None, mode=None):
        self._ready()
        if self._active_turn is not None:
            raise ValueError('Wait for the current ACP prompt before changing options')
        info = self.session_info(thread_id)
        # Validate every requested value before sending the first mutation.
        for category, value, values in (
            ('model', model, [o['id'] for o in info['models']]),
            ('thought_level', effort, [o['value'] for o in self._options(self._config(thread_id, 'thought_level') or {})]),
            ('mode', mode, [o['id'] for o in info['modes']])):
            if value is not None and value not in values:
                raise ValueError(f'ACP {category} value was not declared by this session')
        for category, value in (('model', model), ('thought_level', effort), ('mode', mode)):
            if value is None:
                continue
            option = self._config(thread_id, category)
            if option:
                if value not in [o['value'] for o in self._options(option)]:
                    raise ValueError(f'ACP {category} options changed; refresh the session options')
                result = await self._rpc('session/set_config_option', {'sessionId': thread_id, 'configId': option['id'], 'value': value}, self.request_timeout)
                if not isinstance(result, dict) or not isinstance(result.get('configOptions'), list):
                    raise ValueError('ACP configuration response did not include configOptions')
                self._cache_session(thread_id, result)
                current = self._config(thread_id, category)
                if not current or current.get('currentValue') != value:
                    raise ValueError(f'ACP Agent did not confirm the requested {category} value')
            else:
                if category == 'thought_level':
                    raise ValueError('ACP thought_level options changed; refresh the session options')
                key, method, field = ('models', 'session/set_model', 'modelId') if category == 'model' else ('modes', 'session/set_mode', 'modeId')
                await self._rpc(method, {'sessionId': thread_id, field: value}, self.request_timeout)
                self._session_configs[thread_id][key]['currentModelId' if category == 'model' else 'currentModeId'] = value
        return self.session_info(thread_id)

    async def start_turn(self, thread_id, text, model=None, effort=None, images=None):
        self._ready()
        if images:
            raise ValueError('ACP image prompts are not supported by this bridge; use source paths in text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Prompt text must be nonempty')
        if thread_id not in self._sessions:
            raise ValueError('Create or load this ACP session before sending a prompt')
        if self._active_turn is not None:
            raise ValueError('An ACP prompt is still active; wait for its response or cancel it')
        if model is not None or effort is not None:
            await self.set_session_options(thread_id, model=model, effort=effort)
        turn = {'id': uuid.uuid4().hex, 'status': 'inProgress'}
        instructions = self._instructions.pop(thread_id, '')
        if instructions:
            text = ('工作台任务约定（普通提示文本，不是系统指令或强制沙箱）：\n' + instructions + '\n\n用户要求：\n' + text)
        self._active_turn = {'id': turn['id'], 'sessionId': thread_id, 'cancelling': False}
        self._tools = {}
        self._notify('turn/started', {'threadId': thread_id, 'turn': turn})
        self._prompt_task = asyncio.create_task(self._prompt(thread_id, turn['id'], text))
        return turn

    async def _prompt(self, session_id, turn_id, text):
        turn = {'id': turn_id}
        try:
            result = await self._rpc('session/prompt', {'sessionId': session_id,
                                     'prompt': [{'type': 'text', 'text': text}]}, timeout=None)
            reason = result.get('stopReason') if isinstance(result, dict) else None
            if reason not in ('end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'):
                raise ValueError('ACP prompt returned an unknown stop reason')
            turn.update(status='interrupted' if reason == 'cancelled' else 'completed', stopReason=reason)
        except asyncio.CancelledError:
            turn.update(status='interrupted', error={'message': 'ACP connection was closed'})
        except Exception as exc:
            turn.update(status='interrupted' if self._closing_requested or isinstance(exc, _RpcError) and exc.code == -32800 else 'failed',
                        error={'message': str(exc)})
        finally:
            for identity, request in list(self._server_requests.items()):
                if request['params'].get('sessionId') == session_id:
                    try:
                        await self._permission_result(identity, {'outcome': 'cancelled'})
                    except ConnectionError:
                        self._server_requests.pop(identity, None)
            self._active_turn = None
            self._notify('turn/completed', {'threadId': session_id, 'turn': turn})

    async def interrupt(self, thread_id, turn_id):
        self._ready()
        active = self._active_turn
        if not active or active['id'] != turn_id or active['sessionId'] != thread_id:
            raise ValueError('No matching active ACP turn')
        active['cancelling'] = True
        await self._send({'method': 'session/cancel', 'params': {'sessionId': thread_id}})
        for identity, request in list(self._server_requests.items()):
            if request['params'].get('sessionId') == thread_id:
                await self._permission_result(identity, {'outcome': 'cancelled'})
        return {}  # Prompt completion, not this notification, determines terminal state.

    async def respond(self, request_id, result):
        self._ready()
        if type(request_id) not in (int, str) or request_id not in self._server_requests:
            raise ValueError('No pending ACP permission with this id')
        if not isinstance(result, dict) or result.get('decision') not in ('accept', 'decline'):
            raise ValueError('ACP permission response must contain decision accept or decline')
        params = self._server_requests[request_id]['params']
        kind = 'allow_once' if result['decision'] == 'accept' else 'reject_once'
        options = [o for o in params['options'] if isinstance(o, dict) and isinstance(o.get('optionId'), str)]
        option = next((o for o in options if o.get('kind') == kind), None)
        if option is None and kind == 'allow_once':
            self._notify('error', {'message': 'Agent未提供单次允许选项，本次请求已拒绝；不会升级为永久授权。'})
            option = next((o for o in options if o.get('kind') == 'reject_once'), None)
        outcome = {'outcome': 'selected', 'optionId': option['optionId']} if option else {'outcome': 'cancelled'}
        await self._permission_result(request_id, outcome)

    async def close(self):
        self._closing_requested = True
        await super().close()
        task = self._prompt_task
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        self._prompt_task = self._active_turn = None
        self._sessions.clear()
        self._session_configs.clear()
        self._instructions.clear()
