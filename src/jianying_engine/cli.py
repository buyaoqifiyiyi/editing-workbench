"""Stable local command entry; the legacy execution contract stays in dedicated child processes."""
import json
import subprocess
import sys

from . import __version__
from .api import command_for, operations, paths, read_guide


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if not args or args[0] in ('-h', '--help'):
        print('jianying-engine <operation> [arguments]\nUse <operation> --help for its exact arguments.\n')
        print('\n'.join(f'  {name:22} {description}' for name, description in operations().items()))
        print('\n  paths                  Show the active runtime and guide paths.\n  guide <name>           Read planning/editing/maintenance/capabilities/plan-format/migration.\n  mcp                    Start the optional stdio MCP adapter.\n  workbench              Start the local browser workbench; --open opens its window.')
        return 0
    if args == ['--version']:
        print(__version__)
        return 0
    try:
        if args == ['paths']:
            print(json.dumps(paths(), ensure_ascii=False, indent=2))
            return 0
        if args[0] == 'guide':
            if len(args) != 2:
                raise ValueError('Use: jianying-engine guide <name>')
            print(read_guide(args[1]))
            return 0
        if args[0] == 'mcp':
            return subprocess.call([sys.executable, '-m', 'jianying_engine.mcp_server', *args[1:]])
        if args[0] == 'workbench':
            return subprocess.call([sys.executable, '-m', 'jianying_engine.workbench', *args[1:]])
        return subprocess.call(command_for(args[0], args[1:]))
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
