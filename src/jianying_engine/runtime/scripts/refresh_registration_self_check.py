"""Offline registration-refresh regression: sandbox draft root, sandbox homepage index, stand-in watcher.

Jianying is never started. The check builds a fake user data directory (so draft_location() resolves
inside the sandbox), runs a thread that registers folders the way Jianying 11.6 does — creations
only, a folder already present when the watcher starts stays invisible — and proves the byte-exact
round-trip, the already-registered no-op, the honest unverified report, and every loud refusal. It
also records that the native watcher keeps an index entry after its folder disappears, which is why
a dangling entry is refused instead of repaired.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import editor


def write_index(path, entries):
    """Write the homepage index the way the native writer replaces it: whole file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps({'all_draft_store': entries, 'draft_ids': [x['draft_id'] for x in entries]},
                                    ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def read_entries(path):
    return json.loads(path.read_text(encoding='utf-8'))['all_draft_store']


class Watcher(threading.Thread):
    """Stand-in for the Jianying 11.6 draft-root watcher.

    It registers a folder only when it observes the folder appear (the first sweep only records what
    was already there), and it never removes an entry whose folder disappeared.
    """
    def __init__(self, root, index):
        super().__init__(daemon=True)
        self.root, self.index, self.stopped = root, index, threading.Event()
        self.observed = None

    def run(self):
        while not self.stopped.is_set():
            current = {item.resolve() for item in self.root.iterdir() if item.is_dir()}
            if self.observed is not None:
                created = sorted(current - self.observed)
                if created:
                    entries = read_entries(self.index)
                    registered = {Path(x['draft_fold_path']).resolve() for x in entries}
                    for path in created:
                        if path in registered:
                            continue
                        entries.append({'draft_id': uuid.uuid4().hex.upper(), 'draft_name': path.name,
                                        'draft_fold_path': str(path)})
                        write_index(self.index, entries)
            self.observed = current
            time.sleep(0.2)


def sandbox(out):
    """A user data directory whose globalSetting points at a draft root inside the sandbox."""
    user_data = out / 'User Data'
    root = out / 'Drafts'
    (user_data / 'Config').mkdir(parents=True)
    root.mkdir()
    (user_data / 'Config' / 'globalSetting').write_text(
        '[General]\ncurrentCustomDraftPath=' + str(root) + '\n', encoding='utf-8')
    index = user_data / 'Projects' / 'com.lveditor.draft' / 'root_meta_info.json'
    write_index(index, [])
    os.environ['JIANYING_USER_DATA'] = str(user_data)
    return user_data, root, index


def make_draft(root, name):
    draft = root / name
    (draft / 'sub').mkdir(parents=True)
    (draft / 'draft_meta_info.json').write_text(json.dumps({'draft_name': name}, ensure_ascii=False), encoding='utf-8')
    (draft / 'draft_content.json').write_bytes(bytes(range(256)) * 400)
    (draft / 'sub' / 'note.txt').write_text('占位\n', encoding='utf-8')
    return draft


def refusal(call, fragment):
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), f'expected {fragment!r} in {error!r}'
        return str(error)
    raise AssertionError(f'expected a refusal containing {fragment!r}')


def run_cli(arguments):
    """Call main() in-process (it reads sys.argv) and return (exit code, stdout text)."""
    buffer = io.StringIO()
    saved = editor.sys.argv
    editor.sys.argv = ['editor.py'] + list(arguments)
    try:
        with contextlib.redirect_stdout(buffer):
            code = editor.main()
    finally:
        editor.sys.argv = saved
    return code, buffer.getvalue()


def check(out):
    out.mkdir(parents=True, exist_ok=False)
    user_data, root, index = sandbox(out)
    checks = []

    # 1. Already registered: the folder must not be touched at all.
    first = make_draft(root, '_自检-已登记')
    write_index(index, [{'draft_id': 'AAAA1111', 'draft_name': first.name, 'draft_fold_path': str(first)}])
    digest = editor.draft_tree_digest(first)
    stamps = {p.as_posix(): p.stat().st_mtime_ns for p in first.rglob('*')}
    report = editor.refresh_registration(first, wait_seconds=0, settle_seconds=1, watcher_required=False)
    assert report['refresh'] == 'already_registered' and report['registration']['status'] == 'registered'
    assert report['global_index_modified'] is False
    assert editor.draft_tree_digest(first) == digest
    assert {p.as_posix(): p.stat().st_mtime_ns for p in first.rglob('*')} == stamps
    checks.append('already_registered is a no-op: registration reported, no file rewritten')

    # 2. What the native watcher registers is the creation, so the round-trip must register the draft.
    #    The draft exists before the watcher starts, exactly like the draft the user could not see.
    second = make_draft(root, '_自检-未登记')
    before = editor.draft_tree_digest(second)
    watcher = Watcher(root, index)
    watcher.start()
    time.sleep(0.5)
    assert not any(Path(x['draft_fold_path']).resolve() == second.resolve() for x in read_entries(index))
    report = editor.refresh_registration(second, wait_seconds=10, settle_seconds=1, watcher_required=False)
    assert report['refresh'] == 'recreated' and report['byte_identical'] is True
    assert report['files'] == len(before) == 3
    assert report['registration']['status'] == 'registered'
    assert report['homepage_visibility'] == 'pending_ui_verification'
    assert editor.draft_tree_digest(second) == before
    entries = read_entries(index)
    assert len([x for x in entries if Path(x['draft_fold_path']).resolve() == second.resolve()]) == 1
    assert [x['draft_id'] for x in entries if Path(x['draft_fold_path']).resolve() == first.resolve()] == ['AAAA1111']
    checks.append('a pre-existing folder registers only after the byte-exact round-trip')
    checks.append('the index keeps its other entries; only the native writer adds one')

    # 3. Without a watcher the report stays honest instead of claiming a homepage entry.
    watcher.stopped.set()
    watcher.join(timeout=5)
    third = make_draft(root, '_自检-无监视')
    report = editor.refresh_registration(third, wait_seconds=0, settle_seconds=1, watcher_required=False)
    assert report['refresh'] == 'recreated' and report['registration']['status'] == 'unverified'
    assert report['homepage_visibility'] == 'unverified'
    checks.append('without a watcher the result stays unverified, never "registered"')

    # 4. A folder the watcher registered and that then disappeared keeps its entry (observed natively),
    #    so a dangling entry has no folder to re-create and must be refused.
    gone = make_draft(root, '_自检-残留条目')
    write_index(index, read_entries(index) + [{'draft_id': 'BBBB2222', 'draft_name': gone.name,
                                               'draft_fold_path': str(gone)}])
    shutil.rmtree(gone)
    assert any(x['draft_id'] == 'BBBB2222' for x in read_entries(index))
    message = refusal(lambda: editor.refresh_registration(gone, wait_seconds=0, watcher_required=False),
                      'existing directory directly inside the configured draft root')
    checks.append('a dangling index entry is refused, not repaired: ' + message)

    # 5. Refusals: outside the root, the root itself, a non-draft folder, and Jianying not running.
    outside = out / 'elsewhere' / 'x'
    (outside / 'sub').mkdir(parents=True)
    (outside / 'draft_meta_info.json').write_text('{}', encoding='utf-8')
    refusal(lambda: editor.refresh_registration(outside, watcher_required=False), 'directly inside the configured draft root')
    refusal(lambda: editor.refresh_registration(root, watcher_required=False), 'directly inside the configured draft root')
    plain = root / '_自检-不是草稿'
    plain.mkdir()
    (plain / 'note.txt').write_text('no meta\n', encoding='utf-8')
    refusal(lambda: editor.refresh_registration(plain, watcher_required=False), 'Not a draft directory')
    running = editor.jianying_running
    editor.jianying_running = lambda: False
    try:
        message = refusal(lambda: editor.refresh_registration(third, wait_seconds=0), 'Jianying is not running')
    finally:
        editor.jianying_running = running
    checks.append('refused loudly: outside the root, the root itself, a non-draft folder, Jianying not running')
    checks.append('the not-running refusal names the reason: ' + message)

    # 6. CLI contract: JSON only, rc 0 on success and rc 1 for an out-of-range wait.
    still_third = editor.draft_tree_digest(third)
    editor.jianying_running = lambda: True
    try:
        code, text = run_cli(['refresh-registration', '--draft', str(third), '--wait-seconds', '0'])
        payload = json.loads(text)
        assert code == 0 and payload['refresh'] == 'recreated' and payload['registration']['status'] == 'unverified'
        code, text = run_cli(['refresh-registration', '--draft', str(third), '--wait-seconds', '60'])
        payload = json.loads(text)
        assert code == 1 and payload['status'] == 'failed' and 'wait-seconds' in payload['error']
    finally:
        editor.jianying_running = running
    assert editor.draft_tree_digest(third) == still_third
    checks.append('CLI keeps the JSON contract: rc 0 with a report, rc 1 with a loud wait-seconds refusal')

    report = {'status': 'passed', 'checks': checks, 'checks_count': len(checks),
              'sandbox': {'user_data': str(user_data), 'draft_root': str(root), 'index': str(index)},
              'observations': {'watcher_registers_creations_only': True, 'entry_survives_missing_folder': True},
              'jianying_started': False, 'ui_compatibility': 'unverified'}
    editor.write_json(out / 'refresh-check.json', report)
    return report


if __name__ == '__main__':
    editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    arguments = parser.parse_args()
    editor.emit(check(arguments.out.resolve()))