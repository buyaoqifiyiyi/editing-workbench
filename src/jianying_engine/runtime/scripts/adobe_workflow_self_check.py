"""Test trusted Adobe handoffs and drift rejection without opening Adobe apps."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import adobe_compile
import adobe_workflow
import editor


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    source = root / 'source.mov'
    source.write_bytes(b'isolated source identity')
    plan_path = root / 'plan.json'
    plan = {'schema_version': 1, 'backend': 'pr-ae', 'name': 'Trusted handoff',
            'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': 'source.mov', 'start': 0, 'source_in': 0, 'duration': 1}]}
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    facts = {'kind': 'video', 'duration': 2, 'width': 320, 'height': 180,
             'fps': 30, 'has_audio': False}
    passed = []

    def refuses(label, call):
        try:
            call()
        except (ValueError, FileExistsError):
            passed.append(label)
        else:
            raise AssertionError(label + ' did not reject')

    with patch.object(adobe_workflow.editor, 'probe', return_value=facts):
        out = root / 'bundle'
        result = adobe_workflow.prepare(SimpleNamespace(plan=plan_path, out=out))
        assert result['status'] == 'prepared_not_executed'
        assert not (out / 'ae-project.aep').exists()
        loaded_root, loaded = adobe_workflow.load_bundle(out)
        assert loaded_root == out.resolve() and loaded['duration_frames'] == 30
        passed.append('prepare_load_without_host')
        refuses('existing_output', lambda: adobe_workflow.prepare(SimpleNamespace(plan=plan_path, out=out)))
        script = out / 'create-ae.jsx'
        original_script = script.read_text(encoding='utf-8')
        script.write_text(original_script + '\nalert("altered");', encoding='utf-8')
        refuses('altered_jsx', lambda: adobe_workflow.load_bundle(out))
        script.write_text(original_script, encoding='utf-8')
        xml = out / 'premiere.xml'
        original_xml = xml.read_text(encoding='utf-8')
        xml.write_text(original_xml.replace('shot-001.avi', 'other.avi'), encoding='utf-8')
        refuses('altered_xml', lambda: adobe_workflow.load_bundle(out))
        xml.write_text(original_xml, encoding='utf-8')
        source.write_bytes(b'changed source')
        refuses('changed_source', lambda: adobe_workflow.load_bundle(out))
        source.write_bytes(b'isolated source identity')
        original_plan = plan_path.read_text(encoding='utf-8')
        plan_path.write_text(original_plan + ' ', encoding='utf-8')
        refuses('changed_plan', lambda: adobe_workflow.load_bundle(out))
        plan_path.write_text(original_plan, encoding='utf-8')
        receipt = json.loads((out / 'handoff.json').read_text(encoding='utf-8'))
        receipt['compiler_sha256'] = '0' * 64
        (out / 'handoff.json').write_text(json.dumps(receipt), encoding='utf-8')
        refuses('changed_compiler_identity', lambda: adobe_workflow.load_bundle(out))
        invalid = dict(plan, filters=[{'name': 'legacy preset'}])
        plan_path.write_text(json.dumps(invalid), encoding='utf-8')
        refuses('legacy_effect_not_silently_dropped', lambda: adobe_compile.validate_plan(plan_path, lambda _: facts))
    report = {'status': 'passed', 'checks': passed, 'host_execution': False,
              'native_ae_render': 'not_tested_here', 'premiere_import_export': 'not_tested_here'}
    editor.write_json(root / 'checks.json', report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    editor.emit(check(args.out.resolve()))
