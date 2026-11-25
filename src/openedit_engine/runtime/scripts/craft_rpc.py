"""Minimal stdio JSON-RPC 2.0 MCP client for headless FilmCraft and EffectCraft processes."""
import json
import os
from pathlib import Path
import subprocess


class CraftMcpClient:
    """Spawn and drive an ArtCraft `*-cli mcp` server cleanly over stdio JSON-RPC."""

    def __init__(self, command, *, timeout=180):
        self.command = [str(part) for part in command]
        self.timeout = timeout
        self._proc = None
        self._next_id = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def start(self):
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        self.request(
            'initialize',
            {
                'protocolVersion': '2025-03-26',
                'capabilities': {},
                'clientInfo': {'name': 'openedit-engine', 'version': '1.1.0'},
            },
        )
        self.notify('notifications/initialized')

    def _send(self, payload):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError('MCP server is not running')
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
        self._proc.stdin.flush()

    def _recv_id(self, request_id):
        while True:
            line = self._proc.stdout.readline()
            if not line:
                stderr = self._proc.stderr.read() if self._proc.stderr else ''
                rc = self._proc.poll()
                raise RuntimeError(
                    f'MCP server closed stdout while waiting for id={request_id} (rc={rc}): {stderr[-1000:]}'
                )
            text = line.strip()
            if not text:
                continue
            message = json.loads(text)
            if message.get('id') == request_id:
                return message

    def request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        payload = {'jsonrpc': '2.0', 'id': rid, 'method': method}
        if params is not None:
            payload['params'] = params
        self._send(payload)
        resp = self._recv_id(rid)
        if 'error' in resp:
            raise RuntimeError(f'MCP {method} error: {resp["error"]}')
        return resp.get('result', {})

    def notify(self, method, params=None):
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        self._send(payload)

    def list_tools(self):
        return self.request('tools/list').get('tools', [])

    def call_tool(self, name, arguments=None):
        result = self.request('tools/call', {'name': name, 'arguments': arguments or {}})
        is_err = result.get('isError', False)
        contents = result.get('content', [])
        if any(item.get('type') == 'image' for item in contents) and not is_err:
            return contents
        parsed = []
        for item in contents:
            if item.get('type') == 'text':
                raw = item.get('text', '')
                try:
                    parsed.append(json.loads(raw))
                except ValueError:
                    parsed.append(raw)
            else:
                parsed.append(item)
        value = parsed[0] if len(parsed) == 1 else parsed
        if is_err:
            raise RuntimeError(f'MCP tool {name} failed: {value}')
        return value

    def close(self):
        if self._proc is None:
            return 0, ''
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        stderr = proc.stderr.read() if proc.stderr else ''
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        return rc, stderr


def extract_png_bytes(tool_result):
    """Extract raw PNG bytes from an inline MCP image content reply."""
    import base64

    items = tool_result if isinstance(tool_result, list) else [tool_result]
    for item in items:
        if isinstance(item, dict) and item.get('type') == 'image' and 'data' in item:
            return base64.b64decode(item['data'])
    return None
