"""Thin transition alias delegating `jianying_engine.software_detection` to `openedit_engine.software_detection`."""
import sys as _sys
from openedit_engine import software_detection as _target

_sys.modules[__name__] = _target
