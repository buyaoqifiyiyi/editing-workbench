"""Command-line entry for the pure open-source editing engine."""
import argparse
import json
import subprocess
import sys

from . import __version__
from .api import backends, command_for, operations, paths, read_guide, search_knowledge


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if not args or args[0] in ('-h', '--help'):
        print('openedit <operation> [arguments]\nUse <operation> --help for its exact arguments.\n')
        print('\n'.join(f'  {name:22} {description}' for name, description in operations().items()))
        print('\n  paths                  Show the active runtime and guide paths.\n  backends [--no-detect] Show supported open-source backends and local availability.\n  guide <name>           Read a registered guide.\n  knowledge <query>      Search bundled editing knowledge.\n  mcp                    Start the stdio MCP adapter.')
        return 0
    if args == ['--version']:
        print(__version__)
        return 0
    try:
        if args == ['paths']:
            print(json.dumps(paths(), ensure_ascii=False, indent=2))
            return 0
        if args[0] == 'backends':
            parser = argparse.ArgumentParser(prog='openedit backends',
                description='List supported open-source backends and read-only local availability.')
            parser.add_argument('--no-detect', action='store_true')
            options = parser.parse_args(args[1:])
            print(json.dumps(backends(detect=not options.no_detect), ensure_ascii=False, indent=2))
            return 0
        if args[0] == 'guide':
            if len(args) != 2:
                raise ValueError('Use: openedit guide <name>')
            print(read_guide(args[1]))
            return 0
        if args[0] == 'knowledge':
            parser = argparse.ArgumentParser(prog='openedit knowledge',
                description='Search local bundled editing guides; no media or network access.')
            parser.add_argument('query')
            parser.add_argument('--limit', type=int, default=5)
            options = parser.parse_args(args[1:])
            print(json.dumps(search_knowledge(options.query, options.limit), ensure_ascii=False, indent=2))
            return 0
        if args[0] == 'mcp':
            return subprocess.call([sys.executable, '-m', 'openedit_engine.mcp_server', *args[1:]])
        return subprocess.call(command_for(args[0], args[1:]))
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
