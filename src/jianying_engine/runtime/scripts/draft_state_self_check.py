"""Check the draft-state guard: a Jianying-saved (encrypted) draft is named as such and refused, never decrypted, overwritten or parsed; no UI or rendering."""
import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    plaintext = root / 'plaintext.json'
    editor.write_json(plaintext, {'schema_version': 1, 'name': 'draft state check'})
    bom = root / 'bom.json'
    bom.write_bytes(b'\xef\xbb\xbf{"ok": true}')
    # Same shape as a Jianying-saved draft: base64 text whose first byte is not '{'.
    ciphertext = root / 'ciphertext.json'
    ciphertext.write_bytes(b'eyJjYW52YXMiOnsiaGVpZ2h0IjoxMDgwfX0=' * 64)
    corrupt = root / 'corrupt.json'
    corrupt.write_text('{"tracks": [', encoding='utf-8')
    missing = root / 'missing.json'

    expected = [(plaintext, 'plaintext'), (bom, 'plaintext'), (ciphertext, 'ciphertext'),
                (corrupt, 'corrupt'), (missing, 'missing')]
    for path, state in expected:
        actual = editor.draft_state(path)
        assert actual == state, f'{path.name}: {actual} != {state}'
        checks.append(f'state recognised: {state}')
    assert editor.require_plaintext_draft(plaintext, 'to publish this draft') == plaintext
    checks.append('plaintext draft accepted')

    for path, state in [(ciphertext, 'ciphertext'), (corrupt, 'corrupt'), (missing, 'missing')]:
        for purpose in ('to publish this draft', 'to render native stills from this draft'):
            try:
                editor.require_plaintext_draft(path, purpose)
            except ValueError as exc:
                assert 'Refusing' in str(exc) and state in str(exc), str(exc)
            else:
                raise AssertionError(f'Accepted a {state} draft {purpose}')
        checks.append(f'named refusal: {state}')
    try:
        editor.require_plaintext_draft(ciphertext, 'to publish this draft')
    except ValueError as exc:
        assert 'never decrypts' in str(exc), str(exc)
    else:
        raise AssertionError('Ciphertext refusal does not state the no-decrypt boundary')
    checks.append('ciphertext refusal states the no-decrypt boundary')

    # Real command path: a draft saved by Jianying must be refused by publish before anything is staged or copied.
    saved = root / 'saved-draft'
    saved.mkdir()
    (saved / 'draft_content.json').write_bytes(ciphertext.read_bytes())
    editor.write_json(saved / 'draft_meta_info.json', {'draft_name': 'saved-draft'})
    editor.write_json(saved / '_skill_manifest.json', {'generator': 'jianying-editor', 'version': 2,
                                                       'draft_content_sha256': digest(saved / 'draft_content.json')})
    before = digest(saved / 'draft_content.json')
    archive = root / 'archive'
    archive.mkdir()
    try:
        editor.publish_draft(Args(draft=str(saved), draft_root=str(archive), offline_copy=True, wait_seconds=0))
    except ValueError as exc:
        assert 'ciphertext' in str(exc), str(exc)
    else:
        raise AssertionError('publish accepted a draft saved by Jianying')
    assert digest(saved / 'draft_content.json') == before
    assert list(archive.iterdir()) == []
    checks.append('publish refuses a Jianying-saved draft without touching source or archive')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'draft-state-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    editor.emit(check(args.out.resolve()))