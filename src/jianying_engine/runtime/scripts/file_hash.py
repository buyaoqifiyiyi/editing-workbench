"""Streaming SHA256 without reusing digests for mutable file paths."""
import hashlib
from pathlib import Path


def sha256(path):
    """Hash current bytes even when size and modification time are unchanged."""
    path = Path(path).resolve()
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def matches(path, expected):
    path = Path(path)
    return path.is_file() and sha256(path) == expected
