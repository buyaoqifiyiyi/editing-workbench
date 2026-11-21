"""Configurable native media APIs and per-user Windows credential storage, shared by UI and CLI."""
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOLS = ('openai-compatible', 'gemini')
_connection_results = {}
_test_lock = threading.Lock()


def config_path(path=None):
    return Path(path or os.environ.get('OPENEDIT_OMNI_CONFIG') or os.environ.get('JIANYING_OMNI_CONFIG') or
                Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'JianyingEngine/omni.json').resolve()


def validate(value, allow_incomplete=False):
    required = {'enabled', 'base_url', 'api_key_env'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'protocol', 'model', 'request_options'}:
        raise ValueError('视听配置字段无效。')
    value = dict(value)
    # Older settings did not describe the API family. Keep the encrypted key, then require a model choice.
    if 'protocol' not in value and 'model' not in value:
        value.update(protocol='openai-compatible', model='', request_options={})
    if value.get('protocol') not in PROTOCOLS:
        raise ValueError('请选择受支持的视听接口协议。')
    model = value.get('model')
    if (not isinstance(model, str) or len(model) > 200 or any(c.isspace() or ord(c) < 32 for c in model)
            or (not model.strip() and not allow_incomplete)):
        raise ValueError('请填写服务端提供的模型 ID。')
    if model and value['protocol'] == 'gemini':
        model = model.removeprefix('models/')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', model):
            raise ValueError('Gemini 模型 ID 格式无效。')
    value['model'] = model
    options = value.setdefault('request_options', {})
    if not isinstance(options, dict):
        raise ValueError('高级请求参数必须是 JSON 对象。')
    reserved = {'model', 'messages', 'contents', 'stream', 'modalities', 'response_format', 'responseMimeType',
                'headers', 'authorization', 'api_key', 'key', 'base_url', 'tools', 'tool_choice', 'systemInstruction'}
    if reserved & set(options):
        raise ValueError('高级参数不能覆盖模型、媒体内容、认证、工具或返回格式。')
    try:
        encoded = json.dumps(options, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('高级参数必须是有限数值的 JSON。') from None
    if len(encoded) > 4096:
        raise ValueError('高级请求参数超过4096字符。')
    if type(value['enabled']) is not bool:
        raise ValueError('启用状态必须是布尔值。')
    if not isinstance(value['base_url'], str) or len(value['base_url']) > 2048:
        raise ValueError('请输入有效的接口根地址。')
    url = urllib.parse.urlsplit(value['base_url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or any(c.isspace() for c in value['base_url'])):
        raise ValueError('接口根地址须使用 HTTPS，不能包含密钥、空白或查询参数。')
    if not isinstance(value['api_key_env'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', value['api_key_env']):
        raise ValueError('密钥环境变量名无效，请勿将密钥填入变量名。')
    return value


def load_config(path=None, allow_incomplete=False):
    target = config_path(path)
    if target.stat().st_size > 16384:
        raise ValueError('视听配置文件过大。')
    return validate(json.loads(target.read_text(encoding='utf-8-sig')), allow_incomplete=allow_incomplete)


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('当前仅Windows支持保存加密密钥；其他平台请使用环境变量。')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    protect = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    protect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    if not protect(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError('Windows密钥加密/解密失败，请在当前Windows账户重新保存密钥。')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def key_path(path=None):
    return config_path(path).with_suffix('.key')


def get_key(cfg, path=None):
    target = key_path(path)
    if target.exists():
        if target.stat().st_size > 16384:
            raise ValueError('加密密钥文件无效，请重新保存。')
        try:
            return _crypt(base64.b64decode(target.read_bytes(), validate=True), decrypt=True).decode('utf-8')
        except (ValueError, UnicodeError):
            raise ValueError('无法读取已保存密钥，请在当前Windows账户重新保存。') from None
    return os.environ.get(cfg['api_key_env'], '')


def status(path=None):
    result = {'model': '', 'protocol': 'openai-compatible', 'request_options': {}, 'base_url': '', 'enabled': False, 'ready': False,
              'credential_present': False, 'credential_source': 'none', 'network_called': False,
              'config': str(config_path(path)), 'api_key_env': 'MULTIMODAL_API_KEY', 'connection_test': None}
    try:
        cfg = load_config(path, allow_incomplete=True)
        result.update(cfg)
        key = get_key(cfg, path)
        present = bool(key)
        result.update(credential_present=present, ready=cfg['enabled'] and present and bool(cfg['model']),
                      credential_source=('saved' if key_path(path).exists() else 'environment') if present else 'none')
        result['message'] = ('旧配置已保留密钥，请填写协议和模型 ID 后重新保存。' if not cfg['model'] else
                             '配置就绪，尚未验证API连通性与余额。' if result['ready'] else
                             '已停用视听分析。' if not cfg['enabled'] else '请填写API密钥或配置密钥环境变量。')
        cached = _connection_results.get(str(config_path(path)))
        if cached and cached[0] == _fingerprint(cfg, key):
            result['connection_test'] = dict(cached[1])
    except FileNotFoundError:
        result['message'] = '尚未配置视听API。'
    except (ValueError, OSError, TypeError):
        result['message'] = '视听配置或加密密钥无法读取，请重新保存。'
    return result


def _fingerprint(cfg, key):
    return hashlib.sha256((json.dumps(cfg, sort_keys=True) + '\0' + key).encode('utf-8')).digest()


class RequestError(ValueError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RequestError('Omni endpoint redirect refused; configure the final HTTPS API root')


def send_request(cfg, body, config=None, timeout=180):
    """One authenticated request; no redirects, retries, or provider-body logging."""
    cfg = validate(cfg)
    key = get_key(cfg, config)
    if not cfg['enabled'] or not key:
        raise ValueError('Omni is disabled or its API key environment variable is missing')
    if cfg['protocol'] == 'gemini':
        suffix = '/models/' + urllib.parse.quote(cfg['model'], safe='') + ':generateContent'
        headers = {'x-goog-api-key': key, 'Content-Type': 'application/json'}
    else:
        suffix = '/chat/completions'
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    request = urllib.request.Request(cfg['base_url'].rstrip('/') + suffix,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise RequestError(f'Multimodal API HTTP {code}; no automatic retry or response-body logging', code) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        raise RequestError('Multimodal API connection failed or timed out; billing/completion unknown, no automatic retry') from None
    if len(raw) > 4 * 1024 * 1024:
        raise RequestError('Multimodal API response exceeds 4 MiB')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise RequestError('Multimodal API did not return valid JSON') from None


def test_connection(path=None):
    """Explicit, bounded text-only probe. It does not verify media or account balance."""
    if not _test_lock.acquire(blocking=False):
        raise ValueError('正在测试视听连接，请等待当前测试结束。')
    started = time.monotonic()
    result = {'success': False, 'tested_at': datetime.now(timezone.utc).isoformat(),
              'latency_ms': 0, 'network_called': False, 'model': '', 'protocol': '', 'endpoint_host': ''}
    fingerprint = None
    try:
        try:
            cfg = load_config(path)
            key = get_key(cfg, path)
            fingerprint = _fingerprint(cfg, key)
            result.update(model=cfg['model'], protocol=cfg['protocol'],
                          endpoint_host=urllib.parse.urlsplit(cfg['base_url']).hostname)
            if not cfg['enabled'] or not key:
                result['message'] = '请先启用视听分析并保存有效密钥，再测试连接。'
                return result
            expected = 'CONNECTION_OK_' + secrets.token_hex(4)
            prompt = 'Connection test. Reply with exactly this text and nothing else: ' + expected
            if cfg['protocol'] == 'gemini':
                body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                        'generationConfig': {**cfg['request_options'], 'maxOutputTokens': 256}}
            else:
                body = {**cfg['request_options'], 'model': cfg['model'],
                        'messages': [{'role': 'user', 'content': prompt}], 'stream': False, 'max_tokens': 256}
            result['network_called'] = True
            response = send_request(cfg, body, path, timeout=45)
            if cfg['protocol'] == 'gemini':
                candidate = response['candidates'][0]
                answer = ''.join(p.get('text', '') for p in candidate['content']['parts'] if not p.get('thought'))
                finished = candidate.get('finishReason') == 'STOP'
                usage = response.get('usageMetadata', {})
            else:
                candidate = response['choices'][0]
                answer = candidate['message']['content']
                finished = candidate.get('finish_reason') == 'stop'
                usage = response.get('usage', {})
            allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'promptTokenCount',
                       'candidatesTokenCount', 'totalTokenCount', 'thoughtsTokenCount'}
            if isinstance(usage, dict):
                result['usage'] = {k: v for k, v in usage.items() if k in allowed and type(v) is int and v >= 0}
            if finished and isinstance(answer, str) and answer.strip() == expected:
                result.update(success=True, http_status=200,
                    message='连接成功：模型已完成最小文本请求。尚未验证音视频分析效果或账户余额。')
            else:
                result.update(http_status=200,
                    message='接口已响应，但模型未完整返回测试答案。请检查思考参数、模型支持及输出限制；未自动重试。')
        except RequestError as exc:
            code = exc.http_status
            messages = {400: '请求参数不受支持，请核对模型、接口协议和高级参数。',
                        401: '密钥无效或已过期，请重新保存该服务的密钥。',
                        403: '访问被拒绝，请检查模型权限、地域或账号状态。',
                        404: '未找到接口或模型，请核对根地址和模型 ID。',
                        402: '账户余额或付费状态不足，请在服务控制台检查。',
                        429: '请求受限，可能是额度不足或限流，请在服务控制台检查。'}
            result['message'] = ('连接失败：HTTP ' + str(code) + '。' + messages.get(code, '服务暂时不可用，请稍后手动重试。')
                                 if code else '连接未完成：网络、超时、重定向或响应格式异常；完成及计费状态不确定，未自动重试。')
            if code:
                result['http_status'] = code
        except (KeyError, IndexError, TypeError, ValueError, OSError, AttributeError):
            result['message'] = ('接口返回内容不符合所选协议，请检查地址和模型；未自动重试。' if result['network_called'] else
                                 '配置或密钥无法读取，请先检查配置并保存。')
        return result
    finally:
        result['latency_ms'] = round((time.monotonic() - started) * 1000)
        if fingerprint is not None:
            _connection_results[str(config_path(path))] = (fingerprint, dict(result))
        _test_lock.release()


def _write_atomic(path, data):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(base_url, enabled, api_key='', clear_key=False, path=None, protocol=None, model=None, request_options=None):
    target = config_path(path)
    try:
        previous = load_config(target, allow_incomplete=True)
    except (OSError, ValueError, TypeError):
        previous = {'api_key_env': 'MULTIMODAL_API_KEY'}
    if not isinstance(base_url, str):
        raise ValueError('接口地址必须是文本。')
    cfg = validate({'enabled': enabled, 'base_url': base_url.strip().rstrip('/'), 'api_key_env': previous['api_key_env'],
                    'protocol': protocol if protocol is not None else previous.get('protocol'),
                    'model': model if model is not None else previous.get('model'),
                    'request_options': request_options if request_options is not None else previous.get('request_options', {})})
    if not isinstance(api_key, str) or len(api_key) > 4096 or any(c.isspace() for c in api_key):
        raise ValueError('密钥格式无效，请勿包含空格或换行。')
    if type(clear_key) is not bool or (clear_key and api_key):
        raise ValueError('清除密钥时请留空密钥输入框。')
    encrypted = base64.b64encode(_crypt(api_key.encode('utf-8'))) if api_key else None
    secret = key_path(target)
    old_secret = secret.read_bytes() if secret.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if encrypted is not None:
            _write_atomic(secret, encrypted)
        elif clear_key:
            secret.unlink(missing_ok=True)
        _write_atomic(target, json.dumps(cfg, ensure_ascii=False, indent=2).encode('utf-8'))
    except OSError:
        if encrypted is not None or clear_key:
            if old_secret is None:
                secret.unlink(missing_ok=True)
            else:
                _write_atomic(secret, old_secret)
        raise
    return status(target)
