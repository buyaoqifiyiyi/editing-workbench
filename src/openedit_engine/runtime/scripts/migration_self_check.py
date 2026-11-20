"""Offline migration checks; never opens a GUI or touches external drafts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import shutil
import tempfile

import editor
import migrate
import runtime_core
import runtime_paths


class _FakeFilmCraft:
    def __init__(self, calls):
        self.calls = calls
        self.seq = {'video': [{'items': []}], 'audio': [{'items': []}], 'durationFrames': 30}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call_tool(self, name, params=None):
        params = params or {}
        if name == 'media_import':
            self.imported_paths = list(params['paths'])
            self.names = [Path(p).name for p in params['paths']]
            return {'imported': len(self.names)}
        if name == 'project_inspect':
            return {'root': {'children': [{'item': f'it_{i}', 'name': n} for i, n in enumerate(self.names)]}}
        if name == 'sequence_inspect':
            return self.seq
        if name == 'command_run':
            cid, p = params['id'], params.get('params', {})
            self.calls.append((cid, p))
            if cid == 'timeline.place':
                self.seq['video'][0]['items'].append({'clip': f'c_{len(self.calls)}', 'start': round(p['seconds'] * editor.TICKS_PER_SEC), 'duration': p['duration']})
            elif cid == 'graphics.newText':
                return {'clip': f'g_{len(self.calls)}'}
            elif cid == 'file.saveAs':
                Path(p['path']).write_text('{}', encoding='utf-8')
        return {}


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    def reject(label, fn, expected):
        try:
            fn()
        except (ValueError, FileExistsError) as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError(label)
        checks.append(label)

    media = root / '新素材 空格'
    media.mkdir()
    nested = media / '子目录'
    nested.mkdir()
    image = nested / '测试.png'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=red:s=16x16', '-frames:v', '1', image])
    source = root / 'old-plan.json'
    plan = {'schema_version': 1, 'name': '迁移结构测试', 'mode': 'script',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': r'Z:\旧素材\子目录\测试.png', 'start': 0, 'duration': 1}],
            'captions': [{'text': '迁移测试', 'start': 0, 'duration': 1}]}
    editor.write_json(source, plan)
    original = source.read_bytes()
    root_mapping = [r'Z:\旧素材=' + str(media)]
    out = root / '版本2' / 'edit-plan.json'
    result = migrate.execute(SimpleNamespace(plan=str(source), map=root_mapping, out=str(out)))
    assert result['status'] == 'new_plan_written' and result['missing_media'] == []
    eq_plan = editor.read_json(out)
    assert eq_plan['clips'][0]['path'] == str(image.resolve())
    assert eq_plan['captions'] == plan['captions'] and source.read_bytes() == original
    checks.append('new root, Unicode and spaces; source unchanged, styles preserved')
    editor.validate_plan(out)
    calls = []
    client = _FakeFilmCraft(calls)
    with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
            patch('craft_rpc.CraftMcpClient', return_value=client):
        built = editor.build_draft(SimpleNamespace(plan=str(out), out=str(root / 'project')))
    assert built['status'] == 'passed' and str(image.resolve()) in client.imported_paths
    checks.append('remapped plan validates and builds with new material reference')
    reject('existing output refused', lambda: migrate.execute(SimpleNamespace(plan=str(source), map=root_mapping, out=str(out))), 'exists')
    dry = migrate.execute(SimpleNamespace(plan=str(source), map=root_mapping, out=None))
    assert dry['status'] == 'paths_ready' and source.read_bytes() == original
    checks.append('dry run does not modify source')
    absent = root / 'must-not-write.json'
    missing = migrate.execute(SimpleNamespace(plan=str(source), map=[], out=str(absent)))
    assert missing['status'] == 'needs_media_mapping' and not absent.exists()
    checks.append('missing media reported; output not written')
    alternate = root / 'specific-root'
    alternate.mkdir()
    choices = migrate.mappings([r'Z:\旧素材=' + str(media), r'Z:\旧素材\子目录=' + str(alternate)])
    assert migrate.map_path(r'z:\旧素材\子目录\测试.png', root, choices) == (alternate / '测试.png').resolve()
    assert migrate.map_path(r'Z:\旧素材2\测试.png', root, choices) != image.resolve()
    checks.append('longest root and case handling; sibling prefixes not matched')
    reject('duplicate old root refused', lambda: migrate.mappings(root_mapping * 2), 'Duplicate')
    reject('missing destination refused', lambda: migrate.mappings([r'Z:\old=' + str(root / 'absent')]), 'does not exist')
    reject('drive-relative paths refused', lambda: migrate.map_path(r'C:media.png', root, []), 'Ambiguous')
    relative_plan = root / 'relative.json'
    relative = dict(plan, clips=[{'path': '新素材 空格/子目录/测试.png', 'start': 0, 'duration': 1}])
    editor.write_json(relative_plan, relative)
    mapped, _, unresolved = migrate.remap_plan(relative_plan, [])
    assert not unresolved and mapped['clips'][0]['path'] == str(image.resolve())
    checks.append('relative paths remain valid when output plan is relocated')
    invalid = root / 'fcproj-like.json'
    editor.write_json(invalid, {'tracks': [], 'materials': {}})
    reject('non-plan project file is not treated as an edit plan', lambda: migrate.remap_plan(invalid, []), 'Only schema_version')
    editor.write_json(invalid, dict(plan, clips=[{'path': 'https://example.com/media.png', 'start': 0, 'duration': 1}]))
    reject('URL is not mapped', lambda: migrate.remap_plan(invalid, []), 'Remote URLs')
    with patch.dict(editor.os.environ, {'OPENEDIT_FFMPEG': str(root / 'missing.exe')}):
        assert editor.tool_path('ffmpeg') is None
        reject('invalid explicit override does not fall back to PATH', lambda: editor.tool('ffmpeg'), 'unavailable')
    local_tools = root / 'portable-skill/tools/bin'
    local_tools.mkdir(parents=True)
    local_tool = local_tools / 'ffmpeg.exe'
    local_tool.write_bytes(b'test-only marker, never executed')
    with patch.object(runtime_core, 'SKILL_ROOT', local_tools.parents[1]), patch.object(shutil, 'which', return_value=None), \
            patch.dict(editor.os.environ, {'OPENEDIT_FFMPEG': '', 'JIANYING_FFMPEG': ''}):
        assert Path(editor.tool_path('ffmpeg')) == local_tool
    checks.append('optional tool discovered relative to moved skill root')
    with patch.object(runtime_core, 'SKILL_ROOT', root / 'engine-runtime'), \
            patch.object(runtime_paths, 'RAW_ROOT', local_tools.parents[1]), \
            patch.object(shutil, 'which', return_value=None), \
            patch.dict(editor.os.environ, {'OPENEDIT_FFMPEG': '', 'JIANYING_FFMPEG': ''}):
        assert Path(editor.tool_path('ffmpeg')) == local_tool
    checks.append('legacy lexical Skill tool directory remains usable through a runtime junction')
    with patch.dict(editor.os.environ, {'OPENEDIT_FILMCRAFT_CLI': str(root / 'missing-filmcraft.exe')}):
        diagnosis = editor.doctor()
        assert not diagnosis['ready'] and diagnosis['instructions']
    checks.append('missing filmcraft backend reports incomplete diagnosis clearly')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'source_plan_unchanged': source.read_bytes() == original,
              'existing_projects_modified': False, 'ui_operations': 0, 'root': str(root)}
    editor.write_json(root / 'migration-self-check.json', report)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args()
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    if args.out:
        editor.emit(check(Path(args.out).resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix='openedit-migration-') as temporary:
            editor.emit(check(Path(temporary) / 'check'))
