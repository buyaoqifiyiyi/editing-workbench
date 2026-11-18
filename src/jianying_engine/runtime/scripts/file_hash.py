"""Streaming SHA256 without reusing digests for mutable file paths."""
import hashlib
from pathlib import Path


def sha256(path):
    """Hash current bytes even when size and modification time are unchanged."""
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def matches(path, expected):
    path = Path(path)
    return path.is_file() and sha256(path) == expected
