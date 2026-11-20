"""Stdlib-only primitives shared across the engine package and runtime scripts."""
import contextlib
import ctypes
import hashlib
import math
import os
from pathlib import Path
import struct

RUNTIME_FEATURES = (
    'preview', 'chroma', 'mask_feather', 'mix',
    'text_alpha', 'text_highlight', 'corner', 'green_lightness',
)


def runtime_source_identity(runtime_dir):
    """Hash runtime scripts (.py/.cjs) and bundled assets/*.json under runtime_dir."""
    runtime = Path(runtime_dir)
    scripts = runtime / 'scripts'
    files = [p for p in scripts.iterdir() if p.is_file() and p.suffix in ('.py', '.cjs')]
    files += [p for p in (runtime / 'assets').glob('*.json') if p.is_file()]
    result = {}
    for path in sorted(files):
        with path.open('rb') as stream:
            result[path.relative_to(runtime).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def acquire_file_lock(path, *, blocking=False, pad_empty=False, pad_byte=b'\0'):
    """Open and lock 1 byte of path; returns None on non-blocking contention."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if pad_empty and stream.seek(0, os.SEEK_END) == 0:
            stream.write(pad_byte)
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(stream.fileno(), mode, 1)
        else:
            import fcntl
            flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), flags)
    except OSError:
        stream.close()
        if blocking:
            raise
        return None
    except BaseException:
        stream.close()
        raise
    return stream


def release_file_lock(stream):
    """Unlock and close a lock stream obtained from acquire_file_lock."""
    if stream is None:
        return
    try:
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        stream.close()


@contextlib.contextmanager
def file_lock(path, *, blocking=False, pad_empty=False, pad_byte=b'\0', busy_message='Resource is busy'):
    """Acquire an OS-backed file lock and always release it on exit."""
    stream = acquire_file_lock(path, blocking=blocking, pad_empty=pad_empty, pad_byte=pad_byte)
    if stream is None:
        raise ValueError(busy_message)
    try:
        yield stream
    finally:
        release_file_lock(stream)


def is_file_locked(path):
    """Probe whether another holder currently owns the OS lock on path."""
    stream = acquire_file_lock(path, blocking=False, pad_empty=False)
    if stream is None:
        return True
    release_file_lock(stream)
    return False


def pe_file_version_parts(path):
    """Read [major, minor, build, revision] from a Windows PE executable, or None."""
    if os.name != 'nt' or not Path(path).is_file():
        return None
    try:
        from ctypes import wintypes
        library = ctypes.WinDLL('version', use_last_error=True)
        size_fn = library.GetFileVersionInfoSizeW
        size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        size_fn.restype = wintypes.DWORD
        count = size_fn(str(path), None)
        if not count:
            return None
        buffer = ctypes.create_string_buffer(count)
        load = library.GetFileVersionInfoW
        load.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        load.restype = wintypes.BOOL
        query = library.VerQueryValueW
        query.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        query.restype = wintypes.BOOL
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if (not load(str(path), 0, count, buffer)
                or not query(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length))
                or length.value < 52):
            return None
        fields = struct.unpack('<13I', ctypes.string_at(pointer, 52))
        if fields[0] != 0xFEEF04BD:
            return None
        return [fields[2] >> 16, fields[2] & 65535, fields[3] >> 16, fields[3] & 65535]
    except (OSError, ValueError):
        return None


def pe_file_version(path):
    """Return dotted Windows PE version string, or None."""
    parts = pe_file_version_parts(path)
    return '.'.join(str(v) for v in parts) if parts is not None else None


def check_object(value, allowed, required, label, *, require_text_keys=False):
    """Validate a strict JSON object with allowed and required key sets."""
    if not isinstance(value, dict) or (require_text_keys and any(not isinstance(k, str) for k in value)):
        suffix = ' with text keys' if require_text_keys else ''
        raise ValueError(f'{label}: expected object{suffix}')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f'{label}: unsupported fields {sorted(unknown)}')
    missing = set(required) - set(value)
    if missing:
        raise ValueError(f'{label}: missing fields {sorted(missing)}')
    return value


def check_number(value, label, low=0, high=None, *, style='colon'):
    """Validate a finite numeric scalar within [low, high]."""
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    out_of_range = finite and (value < low or (high is not None and value > high))
    if style == 'editor':
        if not finite:
            raise ValueError(f'{label} must be a finite number')
        if out_of_range:
            raise ValueError(f'{label} is outside its supported range')
    elif style == 'range':
        if not finite or out_of_range:
            raise ValueError(f'{label}: expected finite number in [{low}, {high}]')
    else:
        if not finite:
            raise ValueError(f'{label}: expected finite number')
        if out_of_range:
            raise ValueError(f'{label}: outside [{low}, {high}]')
    return value


def check_text(value, label, maximum, *, allow_whitespace_controls=False):
    """Validate nonempty text within length and character safety limits."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label}: expected nonempty text, max {maximum} characters')
    if (any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(ord(c) < 32 and not (allow_whitespace_controls and c in '\n\t') for c in value)):
        raise ValueError(f'{label}: unsupported control or surrogate character')
    return value


def unique_json_pairs(pairs):
    """object_pairs_hook that rejects duplicate JSON object keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result
