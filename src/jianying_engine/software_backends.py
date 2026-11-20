"""Thin transition alias delegating `jianying_engine.software_backends` to `openedit_engine.software_backends`."""
import sys as _sys
from openedit_engine import software_backends as _target

_sys.modules[__name__] = _target
