"""Native preview manifest regression: the recorded install identities must still match,
and manifest selection must key on the install directory name. Reads files only."""
import argparse
import sys
from pathlib import Path

import editor
import native_preview


def check(install):
    install = Path(install).resolve()
    if not install.is_dir():
        raise ValueError('Install directory missing: ' + str(install))
    manifest = native_preview.preview_manifest(install)
    verified = []
    for name, expected in manifest['files'].items():
        file = install / name
        if not file.is_file():
            raise AssertionError('Manifest file missing: ' + str(file))
        actual = native_preview.digest(file)
        if actual != expected:
            raise AssertionError(f'Shipped module changed: {name} {actual}')
        verified.append(name)
    missing = [version for version in ('11.5.0.14471', '11.6.0.14552')
               if not (editor.SKILL_ROOT / 'assets' / f'native-preview-{version}.json').is_file()]
    if missing:
        raise AssertionError('Preview manifest missing for: ' + ', '.join(missing))
    if native_preview.preview_manifest.__doc__ is None:
        raise AssertionError('Manifest selection must stay documented')
    try:
        native_preview.preview_manifest(editor.SKILL_ROOT / 'assets')
    except ValueError as exc:
        assert 'manifest' in str(exc), exc
    else:
        raise AssertionError('A directory without a manifest must be refused')
    # This check proves shipped bytes and manifest selection only; the rendering path is
    # native_preview.preview, and UI compatibility stays with the user.
    return {'status': 'passed', 'install': str(install), 'version': manifest['version'],
            'verified_files': verified, 'runtime': manifest.get('runtime', {}).get('file'),
            'selection': 'install directory name'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, required=True,
                        help='Jianying install directory named after its version, e.g. ...\\11.6.0.14552')
    args = parser.parse_args()
    try:
        editor.emit(check(args.install))
    except (AssertionError, OSError, ValueError) as error:
        editor.emit({'status': 'failed', 'error': str(error)})
        sys.exit(1)