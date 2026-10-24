"""Streaming SHA256 with a per-process cache keyed by path, size and mtime."""
import hashlib
from pathlib import Path

_CACHE = {}


def sha256(path):
    """Hash without loading the whole file; any size or mtime change invalidates the cached digest."""
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _CACHE:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                digest.update(block)
        _CACHE[key] = digest.hexdigest()
    return _CACHE[key]


def matches(path, expected):
    path = Path(path)
    return path.is_file() and sha256(path) == expected
