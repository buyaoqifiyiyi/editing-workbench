"""Thin transition alias delegating `jianying_engine.api` to `openedit_engine.api`."""
import sys as _sys
from openedit_engine import api as _target

_sys.modules[__name__] = _target
