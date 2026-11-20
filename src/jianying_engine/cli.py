"""Thin transition alias delegating `jianying_engine.cli` to `openedit_engine.cli`."""
import sys as _sys
from openedit_engine import cli as _target

if __name__ == '__main__':
    raise SystemExit(_target.main())
_sys.modules[__name__] = _target
