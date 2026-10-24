"""Coverage check for the regression suite itself: every script under scripts/ must be accounted for.

A check that nobody registered never runs, so a new entry point can ship untested while the suite
still reports all green. This check starts from `run_checks.CHECKS`, walks the `import` graph of
scripts/ and refuses when:

* a registered check script is missing from disk,
* a `*_self_check.py` / `test_*.py` file is not registered (a check nobody runs),
* any other script is unreachable from every registered check (code no check exercises).
"""
import argparse
import ast
from pathlib import Path
import sys

import editor
import run_checks

# run_checks.py is the runner: a human starts it, no check imports it except this one.
HARNESS = {'run_checks'}
CHECK_PATTERNS = ('*_self_check.py', 'test_*.py')


def registration():
    """check name -> script file, read live from the suite that will actually run them."""
    return {name: spec[0] for name, spec in run_checks.CHECKS.items()}


def imports_of(path):
    """Module stems imported by the file, nested imports included."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split('.')[0])
    return found


def reachable(scripts, registry):
    """Stems reachable from the registered checks through script-to-script imports."""
    edges = {stem: imports_of(path) & set(scripts) for stem, path in scripts.items()}
    seen = {Path(name).stem for name in registry.values()}
    frontier = list(seen)
    while frontier:
        for target in edges.get(frontier.pop(), ()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def analyse(scripts_dir, registry):
    scripts_dir = Path(scripts_dir)
    scripts = {path.stem: path for path in sorted(scripts_dir.glob('*.py'))}
    registered = set(registry.values())
    shaped = {path.name for pattern in CHECK_PATTERNS for path in scripts_dir.glob(pattern)}
    report = {
        'scripts_dir': str(scripts_dir),
        'scripts': len(scripts),
        'registry_entries': len(registry),
        'missing_files': sorted(name for name in registered if Path(name).stem not in scripts),
        'orphan_checks': sorted(name for name in shaped if name not in registered),
        'unreachable': sorted(stem for stem in scripts
                              if stem not in reachable(scripts, registry) and stem not in HARNESS),
    }
    report['status'] = 'failed' if any(report[key] for key in ('missing_files', 'orphan_checks', 'unreachable')) \
        else 'passed'
    return report


def self_test(root):
    """Prove every rule fires: a stray script and an unregistered check must both be reported."""
    root.mkdir(parents=True, exist_ok=False)
    (root / 'used_self_check.py').write_text('import helper\n', encoding='utf-8')
    (root / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'stray.py').write_text('"""Nothing imports me and no check runs me."""\n', encoding='utf-8')
    registry = {'used': 'used_self_check.py'}
    stray = analyse(root, registry)
    (root / 'forgot_self_check.py').write_text('"""Shaped like a check, registered nowhere."""\n', encoding='utf-8')
    forgot = analyse(root, registry)
    (root / 'stray.py').unlink()
    clean = analyse(root, dict(registry, forgot='forgot_self_check.py'))
    assert stray['unreachable'] == ['stray'] and not stray['orphan_checks'], stray
    # the unregistered check is unreachable too: nothing imports it and no registry entry names it
    assert forgot['orphan_checks'] == ['forgot_self_check.py'], forgot
    assert forgot['unreachable'] == ['forgot_self_check', 'stray'], forgot
    assert clean['status'] == 'passed', clean
    return {'stray_reported': stray['unreachable'], 'orphan_reported': forgot['orphan_checks'],
            'clean_after_fix': clean['status']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True, help='New directory for coverage-check.json')
    parser.add_argument('--scripts', type=Path, default=Path(__file__).resolve().parent,
                        help='Directory to audit; default is the one holding this file')
    parser.add_argument('--self-test', action='store_true', help='Also prove the rules fire on a throwaway fixture')
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=False)
    try:
        report = analyse(args.scripts, registration())
        if args.self_test:
            report['self_test'] = self_test(args.out / 'fixture')
    except (OSError, SyntaxError, ValueError, KeyError, AssertionError) as exc:
        report = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
    editor.write_json(args.out / 'coverage-check.json', report)
    editor.emit(report)
    return 1 if report['status'] != 'passed' else 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())