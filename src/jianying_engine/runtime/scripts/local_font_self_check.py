"""Exercise static local-font packaging, rebinding, tamper rejection and offline publication."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as Args

from PIL import Image

import editor
import local_font
import migrate


def system_font():
    candidates = [
        Path(r'C:\Windows\Fonts\simhei.ttf'),
        Path(r'C:\Windows\Fonts\arial.ttf'),
        Path(r'C:\Windows\Fonts\segoeui.ttf'),
        Path(r'C:\Windows\Fonts\simhei.ttf'),
        Path('/System/Library/Fonts/Supplemental/Arial.ttf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    ]
    return next((path for path in candidates if path.is_file()), None)


def check(root, install=None):
    root.mkdir(parents=True, exist_ok=False)
    source_font = system_font()
    if source_font is None:
        raise RuntimeError('A local TTF test font is required')
    assets = root / 'assets'
    assets.mkdir()
    font = assets / 'Local Test.ttf'
    shutil.copyfile(source_font, font)
    image = root / 'control.png'
    Image.new('RGB', (640, 360), '#16324A').save(image)
    font_hash = hashlib.sha256(font.read_bytes()).hexdigest()
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()

    base = {
        'schema_version': 1,
        'name': '本地字体兼容检查',
        'mode': 'script',
        'canvas': {'width': 640, 'height': 360, 'fps': 30},
        'clips': [{'path': str(image), 'start': 0, 'duration': 3}],
        'captions': [
            {'text': '本地字体 Local 123', 'start': 0.2, 'duration': 2.2,
             'font_path': 'assets/Local Test.ttf', 'size': 12, 'y': 0},
            {'text': '默认字体', 'start': 2.5, 'duration': 0.4, 'border': False},
        ],
    }
    plan = root / 'edit-plan.json'
    editor.write_json(plan, base)
    draft = root / 'draft'
    built = editor.build_draft(Args(plan=str(plan), out=str(draft)))
    checks = ['build/readback with two independent text materials']
    assert built['verification']['draft_structure'] == 'passed'
    assert built['verification']['native_effects']['counts']['local_fonts'] == 1

    normalized = editor.read_json(draft / 'edit-plan.json')
    source_path = normalized['captions'][0]['font_path']
    assert Path(source_path) == font.resolve()
    content = editor.read_json(draft / 'draft_content.json')
    text = content['materials']['texts'][0]
    rich = json.loads(text['content'])
    expected = local_font.expected_font(local_font.inspect(font), draft)
    assert text['font_path'] == expected['path']
    assert rich['styles'][0]['font'] == expected
    copied = Path(expected['path'])
    assert copied.is_file() and hashlib.sha256(copied.read_bytes()).hexdigest() == font_hash
    checks.append('static TTF copied into draft and both font paths bind to it')
    assert local_font.preview_check(content, draft) == 1
    checks.append('draft-only font preflight passes without reading the source path')

    manifest = editor.read_json(draft / '_skill_manifest.json')
    assert manifest['local_fonts'][0]['sha256'] == font_hash
    assert manifest['local_fonts'][0]['relative'].startswith('Resources/local-fonts/')
    checks.append('draft manifest records font identity and relative dependency')

    migration_plan = copy.deepcopy(base)
    migration_plan['captions'][0]['font_path'] = r'Z:\Fonts\Local Test.ttf'
    migration_plan['clips'][0]['path'] = str(image)
    migration_plan_path = root / 'migration-plan.json'
    editor.write_json(migration_plan_path, migration_plan)
    mapped, changes, missing = migrate.remap_plan(
        migration_plan_path, migrate.mappings([r'Z:\Fonts=' + str(assets)]))
    assert not missing and Path(mapped['captions'][0]['font_path']) == font.resolve()
    assert any(change['item'] == 'captions[0].font_path' for change in changes)
    checks.append('migration remaps an absent font root explicitly')

    def rejected(label, call, expected_text):
        try:
            call()
        except (ValueError, FileNotFoundError) as error:
            assert expected_text in str(error), str(error)
        else:
            raise AssertionError('Accepted: ' + label)
        checks.append(label)

    bad = copy.deepcopy(content)
    bad_rich = json.loads(bad['materials']['texts'][0]['content'])
    bad_rich['styles'][0]['font']['path'] = 'D:'
    bad['materials']['texts'][0]['content'] = json.dumps(bad_rich, ensure_ascii=False)
    rejected('tampered rich-text font path rejected',
             lambda: editor.verify_structure(bad, normalized, draft, draft), 'text font identity/path')
    bad = copy.deepcopy(content)
    bad['materials']['texts'][0]['font_path'] = 'D:'
    rejected('tampered material font path rejected',
             lambda: editor.verify_structure(bad, normalized, draft, draft), 'default or local text font path')
    rejected('preview rejects tampered rich-text font path',
             lambda: local_font.preview_check(bad, draft), 'outside the draft font directory')

    original_font = copied.read_bytes()
    copied.write_bytes(original_font[:-1] + bytes([original_font[-1] ^ 1]))
    rejected('changed copied font bytes rejected',
             lambda: editor.verify_structure(content, normalized, draft, draft), 'bytes changed')
    copied.write_bytes(original_font)

    invalid = copy.deepcopy(base)
    invalid['captions'][0]['font'] = '励字志向黑简_特粗'
    editor.write_json(root / 'invalid.json', invalid)
    rejected('font and font_path mutually exclusive',
             lambda: editor.validate_plan(root / 'invalid.json'), 'mutually exclusive')
    invalid = copy.deepcopy(base)
    invalid['captions'][0]['font_path'] = 'assets/missing.ttf'
    editor.write_json(root / 'invalid.json', invalid)
    rejected('missing font rejected',
             lambda: editor.validate_plan(root / 'invalid.json'), 'missing or unreadable')
    invalid = copy.deepcopy(base)
    invalid['captions'][0]['keyframes'] = [{'property': 'position_x', 'time': 0, 'value': 0}]
    editor.write_json(root / 'invalid.json', invalid)
    rejected('unverified local-font motion rejected',
             lambda: editor.validate_plan(root / 'invalid.json'), 'static horizontal text only')
    invalid = copy.deepcopy(base)
    invalid['composites'] = [{'name': 'local font composite', 'tracks': ['video', 'captions']}]
    editor.write_json(root / 'invalid.json', invalid)
    rejected('local font in composite rejected',
             lambda: editor.validate_plan(root / 'invalid.json'), 'composites is not verified')

    variable = Path(r'C:\Windows\Fonts\NotoSansSC-VF.ttf')
    if variable.is_file():
        invalid = copy.deepcopy(base)
        invalid['captions'][0]['font_path'] = str(variable)
        editor.write_json(root / 'invalid.json', invalid)
        rejected('variable font rejected',
                 lambda: editor.validate_plan(root / 'invalid.json'), 'Variable local fonts')

    before_publish = (draft / 'draft_content.json').read_bytes()
    offline = root / 'offline'
    offline.mkdir()
    published = editor.publish_draft(Args(draft=str(draft), draft_root=str(offline), offline_copy=True))
    published_root = Path(published['draft'])
    published_content = editor.read_json(published_root / 'draft_content.json')
    published_text = published_content['materials']['texts'][0]
    published_font = Path(published_text['font_path'])
    assert published_font.is_file() and published_root in published_font.parents
    assert json.loads(published_text['content'])['styles'][0]['font']['path'] == published_text['font_path']
    assert (draft / 'draft_content.json').read_bytes() == before_publish
    editor.verify_structure(published_content, normalized, published_root, published_root)
    checks.append('offline publish rebinds font path and leaves source build unchanged')

    font.unlink()
    assets.rmdir()
    inventory = {item['source']: item for item in manifest['local_fonts']}
    local_font.verify_files(inventory, published_root)
    assert local_font.verify_bindings(
        published_content, normalized, published_root, inventory) == 1
    checks.append('published draft remains verifiable after original font removal')
    preview = None
    if install is not None:
        preview = root / 'native-preview'
        result = subprocess.run(
            [sys.executable, str(Path(editor.__file__)), 'native-preview',
             '--install', str(Path(install).resolve()),
             '--draft', str(published_root / 'draft_content.json'),
             '--out', str(preview), '--times', '0.5', '1.5'],
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            raise RuntimeError('Native local-font preview failed: ' + result.stderr[-2000:])
        report = editor.read_json(preview / 'preview.json')
        assert report.get('status') == 'passed', report
        checks.append('native preview from published draft after source font removal')
    assert hashlib.sha256(image.read_bytes()).hexdigest() == image_hash

    report = {
        'status': 'passed',
        'checks_count': len(checks),
        'checks': checks,
        'font_source': str(source_font),
        'font_sha256': font_hash,
        'source_font_removed_after_publication': True,
        'source_image_unchanged': True,
        'ui_edit_save_reopen': 'unverified',
        'native_preview': str(preview) if preview else 'not requested',
        'native_preview_status': 'passed' if preview else 'not_run',
        'export': 'unverified',
    }
    editor.write_json(root / 'local-font-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--install', type=Path,
                        help='Run the optional native still preview against this Jianying install')
    args = parser.parse_args()
    editor.emit(check(args.out.resolve(), args.install))
