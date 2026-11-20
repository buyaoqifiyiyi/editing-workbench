"""Thin transition alias delegating `jianying_engine.artifacts` to `openedit_engine.artifacts`."""
import sys as _sys
from openedit_engine import artifacts as _target

_sys.modules[__name__] = _target
