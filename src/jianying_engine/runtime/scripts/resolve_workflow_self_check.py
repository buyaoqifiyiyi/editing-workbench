"""Offline Resolve bundle identity guards. No native host or media decoder runs."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import editor
import resolve_workflow as workflow


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    source = root / 'source.mp4'
    source.write_bytes(b'self-check source identity; not real media')
    plan = root / 'plan.json'
    plan.write_text(json.dumps({'schema_version': 1, 'backend': 'resolve', 'name': 'Offline check',
        'canvas': {'width': 320, 'height': 180, 'fps': 30},
        'clips': [{'path': str(source), 'start': 0, 'source_in': 0, 'duration': 1}]}), encoding='utf-8')
    facts = {'kind': 'video', 'duration': 2, 'width': 320, 'height': 180, 'fps': 30, 'has_audio': False}
    passed = []
    with patch.object(editor, 'probe', return_value=facts), patch.object(workflow, 'verify_cfr'):
        bundle = root / 'bundle'
        workflow.prepare(SimpleNamespace(plan=plan, out=bundle))
        workflow.load_bundle(bundle)
        passed.append('prepare_recompute_without_host')
        for path in (source, plan, bundle / 'normalized-plan.json'):
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            try:
                workflow.load_bundle(bundle)
            except ValueError:
                passed.append('refuse_changed_' + path.name)
            else:
                raise AssertionError('Changed identity accepted: ' + str(path))
            path.write_bytes(original)
    result = {'status': 'passed', 'checks': passed, 'host_execution': False,
              'native_render': 'not_tested_here', 'human_viewing_listening': 'not_performed'}
    workflow.write_new(root / 'checks.json', workflow.encoded(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out.absolute()))
