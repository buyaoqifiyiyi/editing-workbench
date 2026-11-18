"""Runtime admission store, provisional requalify mode, install pin and frame comparison; no Jianying needed.

A build that is neither measured nor admitted must be refused with the requalify hint; an admitted build is
accepted only while its files still match the recorded hashes; the provisional candidate mode only applies
to the exact candidate directory; the derived preview manifest follows the admission record.
"""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import editor
import file_hash
import native_advanced
import requalify
import runtime_admission as ra


def fake_install(root, name):
    install = root / name
    for rel in ra.gated_paths():
        (install / rel).parent.mkdir(parents=True, exist_ok=True)
        (install / rel).write_bytes(('fake ' + rel).encode())
    (install / 'Resources/Font/SystemFont').mkdir(parents=True, exist_ok=True)
    return install


def refused(fn, needle):
    try:
        fn()
    except ValueError as exc:
        assert needle in str(exc), exc
        return
    raise AssertionError('expected refusal containing ' + needle)


def write_report(path, candidate, reference, identities):
    report = {'admission_state': 'eligible_not_committed', 'candidate': str(candidate), 'reference': str(reference),
              'admitted': list(identities), 'identities': identities,
              'features': {feature: {'status': 'admitted'} for feature in identities},
              'source_identity': ra.source_identity()}
    path.write_text(json.dumps(report), encoding='utf-8')


def check(out):
    root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    saved = {k: os.environ.get(k) for k in ('JIANYING_RUNTIME_ADMISSIONS', 'JIANYING_REQUALIFY_CANDIDATE', 'JIANYING_INSTALL')}
    os.environ['JIANYING_RUNTIME_ADMISSIONS'] = str(root / 'admissions.json')
    os.environ.pop('JIANYING_REQUALIFY_CANDIDATE', None)
    cases = []
    try:
        install = fake_install(root, '99.0.0.1')
        assert ra.expected('corner', install) is None
        refused(lambda: native_advanced.corner_runtime(install), 'requalify')
        cases.append('unadmitted build refused with requalify hint')

        files = {rel: file_hash.sha256(install / rel) for rel in ra.bundled('corner')[1]}
        refused(lambda: ra.admit(install.name, ra.bundled('corner')[0], {'corner': files}, root / 'missing.json'),
                'report is required')
        (root / 'report.json').write_text('{}', encoding='utf-8')
        refused(lambda: ra.admit(install.name, ra.bundled('corner')[0], {'corner': files}, root / 'report.json'),
                'does not authorize')
        assert not ra.store_path().exists()
        write_report(root / 'report.json', install, ra.bundled('corner')[0], {'corner': files})
        ra.admit(install.name, ra.bundled('corner')[0], {'corner': files}, root / 'report.json')
        assert ra.expected('corner', install) == files and ra.matches('corner', install)
        native_advanced.corner_runtime(install)
        assert ra.expected('green_lightness', install) is None, 'admission is per subset'
        assert ra.preview_manifest(install) is None, 'preview not admitted'
        cases.append('admitted subset accepted, others still refused')

        target = install / next(iter(ra.bundled('corner')[1]))
        target.write_bytes(b'changed after admission')
        assert not ra.matches('corner', install)
        refused(lambda: native_advanced.corner_runtime(install), 'identity changed')
        cases.append('file changed after admission refused')

        other = fake_install(root, '99.0.0.2')
        os.environ['JIANYING_REQUALIFY_CANDIDATE'] = str(other)
        assert ra.expected('corner', other) is None, 'environment alone cannot bypass admission'
        frozen = {f: ra.candidate_identity(f, other)[0] for f in ('corner', 'preview')}
        with ra.candidate_context(other, frozen):
            assert ra.expected('corner', other) is not None and ra.matches('corner', other)
            assert ra.preview_manifest(other)['version'] == '99.0.0.2'
            assert ra.expected('chroma', fake_install(root / 'x', '99.0.0.3')) is None, 'candidate context is exact-path only'
            candidate_file = other / next(iter(frozen['corner']))
            original = candidate_file.read_bytes()
            candidate_file.write_bytes(original + b'tamper')
            assert not ra.matches('corner', other), 'frozen candidate identity must catch changed bytes'
            candidate_file.write_bytes(original)
        assert ra.expected('corner', other) is None and ra.preview_manifest(other) is None
        cases.append('candidate identity is scoped to the review process')

        preview_identity = {'preview': {rel: file_hash.sha256(other / rel) for rel in ra.bundled('preview')[1]}}
        write_report(root / 'r2.json', other, ra.bundled('preview')[0], preview_identity)
        original_report = (root / 'r2.json').read_bytes()
        ra.admit(other.name, ra.bundled('preview')[0], preview_identity, root / 'r2.json')
        manifest = ra.preview_manifest(other)
        assert manifest['version'] == other.name and manifest['admitted_from'] == ra.bundled('preview')[0]
        assert manifest['runtime']['sha256'] == file_hash.sha256(other / manifest['runtime']['file'])
        cases.append('derived preview manifest from admission record')
        with patch.object(ra, 'source_identity', return_value={'changed.py': '0' * 64}):
            assert ra.expected('preview', other) is None, 'changed review source must refuse admission'
        cases.append('changed review source refused')

        (root / 'r2.json').write_text('changed', encoding='utf-8')
        assert ra.expected('preview', other) is None, 'changed report must refuse admission'
        cases.append('changed review report refused')

        (root / 'r2.json').write_bytes(original_report)
        assert ra.expected('preview', other) is not None
        data = ra.load()
        data['versions'][other.name]['features']['text_alpha'] = {}
        ra.store_path().write_text(json.dumps(data), encoding='utf-8')
        assert ra.expected('text_alpha', other) is None, 'empty identity must refuse admission'
        cases.append('empty identity refused')
        altered = data['versions'][other.name]['features']['preview']
        key = next(iter(altered))
        altered[key] = '0' * 64
        ra.store_path().write_text(json.dumps(data), encoding='utf-8')
        assert ra.expected('preview', other) is None, 'store identity must match reviewed report'
        cases.append('altered admission identity refused')

        merged = fake_install(root / 'm', '99.0.0.4')
        gone = next(rel for rel in ra.bundled('chroma')[1] if rel.endswith('.js') and '~' in rel)
        (merged / gone).unlink()
        (merged / gone).with_name('ai-creation.merged.js').write_bytes(b'merged bundle')
        identity, absent = ra.candidate_identity('chroma', merged)
        assert gone in absent and (Path(gone).parent / 'ai-creation.merged.js').as_posix() in identity
        for p in (merged / gone).parent.iterdir():
            p.unlink()
        assert ra.candidate_identity('chroma', merged)[0] is None
        cases.append('renamed contract file pinned through its folder')

        os.environ['JIANYING_INSTALL'] = str(other)
        assert editor.jianying_install() == other
        os.environ['JIANYING_INSTALL'] = str(root / 'no-fonts')
        assert editor.jianying_install() is None
        cases.append('JIANYING_INSTALL pin')

        a, b, c = root / 'a.png', root / 'b.png', root / 'c.png'
        Image.new('RGBA', (8, 8), (10, 20, 30, 255)).save(a)
        Image.new('RGBA', (8, 8), (10, 20, 30, 255)).save(b)
        img = Image.new('RGBA', (8, 8), (10, 20, 30, 255)); img.putpixel((3, 3), (13, 20, 30, 255)); img.save(c)
        assert requalify.frame_diff(a, b) == 0 and requalify.frame_diff(a, c) == 3 > requalify.TOLERANCE
        Image.new('RGBA', (9, 8)).save(c)
        assert requalify.frame_diff(a, c) == 'size'
        cases.append('frame comparison')
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return {'status': 'passed', 'cases': cases}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out))
