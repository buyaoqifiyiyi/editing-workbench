"""Thin transition alias delegating `jianying_engine.jobs` to `openedit_engine.jobs`."""
import sys as _sys
from openedit_engine import jobs as _target

_sys.modules[__name__] = _target
