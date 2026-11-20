"""Thin transition alias delegating `jianying_engine.mcp_server` to `openedit_engine.mcp_server`."""
import sys as _sys
from openedit_engine import mcp_server as _target

if __name__ == '__main__':
    raise SystemExit(_target.main())
_sys.modules[__name__] = _target
