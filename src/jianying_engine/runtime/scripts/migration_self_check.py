"""Offline migration checks; never opens an app or touches existing drafts."""
from pathlib import Path
import builtins
from types import SimpleNamespace
from unittest.mock import patch
import tempfile

import editor
import migrate
import run_checks
import runtime_paths


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
    # Synthetic input only: no Jianying effect is rendered externally.
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
    normalized, _, _ = editor.validate_plan(out)
    built = editor.build_draft(SimpleNamespace(plan=str(out), out=str(root / 'draft')))
    assert built['verification']['draft_structure'] == 'passed'
    content = editor.read_json(root / 'draft/draft_content.json')
    assert any(v.get('path') == str(image.resolve()) for v in content['materials']['videos'])
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
    invalid = root / 'draft_content.json'
    editor.write_json(invalid, {'tracks': [], 'materials': {}})
    reject('native draft is not treated as an edit plan', lambda: migrate.remap_plan(invalid, []), 'Only schema_version')
    editor.write_json(invalid, dict(plan, clips=[{'path': 'https://example.com/media.png', 'start': 0, 'duration': 1}]))
    reject('URL is not mapped', lambda: migrate.remap_plan(invalid, []), 'Remote URLs')
    with patch.dict(editor.os.environ, {'JIANYING_FFMPEG': str(root / 'missing.exe')}):
        assert editor.tool_path('ffmpeg') is None
        reject('invalid explicit override does not fall back to PATH', lambda: editor.tool('ffmpeg'), 'unavailable')
    local_tools = root / 'portable-skill/tools/bin'
    local_tools.mkdir(parents=True)
    local_tool = local_tools / 'ffmpeg.exe'
    local_tool.write_bytes(b'test-only marker, never executed')
    with patch.object(editor, 'SKILL_ROOT', local_tools.parents[1]), patch.object(editor.shutil, 'which', return_value=None), \
            patch.dict(editor.os.environ, {'JIANYING_FFMPEG': ''}):
        assert Path(editor.tool_path('ffmpeg')) == local_tool
    checks.append('optional tool discovered relative to moved skill root')
    with patch.object(editor, 'SKILL_ROOT', root / 'engine-runtime'), \
            patch.object(runtime_paths, 'RAW_ROOT', local_tools.parents[1]), \
            patch.object(editor.shutil, 'which', return_value=None), \
            patch.dict(editor.os.environ, {'JIANYING_FFMPEG': ''}):
        assert Path(editor.tool_path('ffmpeg')) == local_tool
    checks.append('legacy lexical Skill tool directory remains usable through a runtime junction')
    manifest_root = root / 'manifest-test'
    (manifest_root / 'assets').mkdir(parents=True)
    manifest = manifest_root / 'assets/native-preview-1.2.3.json'
    editor.write_json(manifest, {'version': '1.2.3'})
    with patch.object(editor, 'SKILL_ROOT', manifest_root), patch.object(editor, 'jianying_install', return_value=None):
        assert run_checks.recorded_install() is None
        editor.write_json(manifest, {'version': '1.2.3', 'install': ''})
        assert run_checks.recorded_install() is None
    discovered = root / '1.2.3'
    discovered.mkdir()
    with patch.object(editor, 'SKILL_ROOT', manifest_root), patch.object(editor, 'jianying_install', return_value=discovered):
        assert run_checks.recorded_install() == discovered
    checks.append('empty manifest install never selects cwd; matching discovered installation remains available')
    original_import = builtins.__import__
    def broken_backend(name, *args, **kwargs):
        if name == 'pyJianYingDraft':
            raise ImportError('self-check: broken backend dependency')
        return original_import(name, *args, **kwargs)
    with patch('builtins.__import__', side_effect=broken_backend):
        diagnosis = editor.doctor()
        assert diagnosis['backend'] == '0.3.0'
        assert not diagnosis['backend_importable'] and not diagnosis['ready'] and not diagnosis['publication_ready']
        assert 'broken backend dependency' in diagnosis['backend_error']
        assert any(a['code'] == 'dependencies' and a['detail'] == diagnosis['backend_error']
                   for a in diagnosis['migration_instructions'])
    checks.append('installed metadata does not hide a broken backend import')
    diagnosis = editor.doctor()
    assert diagnosis['backend_importable'] and diagnosis['backend_error'] is None and diagnosis['ready']
    checks.append('real backend import remains ready after the diagnostic probe')
    data = root / 'new-device-user-data'
    (data / 'Config').mkdir(parents=True)
    drafts = root / 'new-device-drafts'
    drafts.mkdir()
    setting = data / 'Config/globalSetting'
    setting.write_text('[General]\ncurrentCustomDraftPath=' + str(drafts).replace('\\', '\\\\'), encoding='utf-8')
    with patch.dict(editor.os.environ, {'JIANYING_USER_DATA': str(data)}):
        assert Path(editor.draft_location()['active_draft_root']) == drafts
        checks.append('new-device user data and configured draft root detected')
        setting.write_text('[General]\ncurrentCustomDraftPath=' + str(root / 'old-missing-drive'), encoding='utf-8')
        diagnosis = editor.doctor()
        assert diagnosis['draft_location_error'] and diagnosis['ready'] and not diagnosis['publication_ready']
        assert any(a['code'] == 'draft_directory' for a in diagnosis['migration_instructions'])
        checks.append('stale old draft path produces setup advice; never silently changed')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'source_plan_unchanged': source.read_bytes() == original,
              'existing_drafts_modified': False, 'new_device_hardware_tested': False,
              'ui_operations': 0, 'root': str(root)}
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
        with tempfile.TemporaryDirectory(prefix='jianying-migration-') as temporary:
            editor.emit(check(Path(temporary) / 'check'))
