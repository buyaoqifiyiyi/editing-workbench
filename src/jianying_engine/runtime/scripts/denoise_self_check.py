"""Regression for measured native realtime denoise; no UI or effect prerendering."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
import editor


def check(root, plan_path):
    root.mkdir(parents=True, exist_ok=False)
    base = editor.read_json(plan_path)
    base['name'] = '降噪接口回归'
    source = Path(base['audio'][0]['path'])
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = []
    # Distinct materials and independent range/gain mapping on two clips.
    base['audio'][0].update(duration=3, source_in=0.5,
        keyframes=[{'time': 0, 'value': 1}, {'time': 2, 'value': 0.5}])
    base['audio'].append(copy.deepcopy(base['audio'][0]))
    base['audio'][1].update(start=3, source_in=3.5)
    editor.write_json(root / 'plan.json', base)
    result = editor.build_draft(Args(plan=str(root / 'plan.json'), out=str(root / 'draft')))
    content = editor.read_json(root / 'draft' / 'draft_content.json')
    normalized = editor.read_json(root / 'draft' / 'edit-plan.json')
    assert result['verification']['editing_status'] == 'complete'
    assert result['verification']['native_effects']['extensions']['audio_realtime_denoise'] == 2
    materials = content['materials']['realtime_denoises']
    assert len(materials) == 2 and materials[0]['id'] != materials[1]['id']
    checks.append('two clips with distinct materials, source ranges and volume keys')
    for label, field, value in [('disabled', 'is_denoise', False),
                                ('rate', 'denoise_rate', 0.1), ('model', 'path', 'wrong.model')]:
        damaged = copy.deepcopy(content)
        damaged['materials']['realtime_denoises'][0][field] = value
        try:
            editor.verify_structure(damaged, normalized)
        except ValueError:
            checks.append('readback rejects ' + label)
        else:
            raise AssertionError('Accepted corrupted denoise: ' + label)
    damaged = copy.deepcopy(content)
    segment = next(t for t in damaged['tracks'] if t['type'] == 'audio')['segments'][0]
    segment['extra_material_refs'].remove(materials[0]['id'])
    try:
        editor.verify_structure(damaged, normalized)
    except ValueError:
        checks.append('readback rejects missing reference')
    else:
        raise AssertionError('Accepted missing denoise reference')
    # Same-name changed model must fail by bytes, not filename.
    fake = root / 'unet_denoise_44k_music_model_v1.0.model'
    fake.write_bytes(b'changed model fixture')
    invalid = {
        'missing model': lambda p: p['audio'][0]['denoise'].update(model_path=str(root / 'missing.model')),
        'changed model': lambda p: p['audio'][0]['denoise'].update(model_path=str(fake)),
        'unknown parameter': lambda p: p['audio'][0]['denoise'].update(rate=0.5),
        'unsupported speed': lambda p: p['audio'][0].update(speed=1.1),
        'network model': lambda p: p['audio'][0]['denoise'].update(model_path='https://invalid/model'),
    }
    mono = root / 'mono.wav'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', source, '-ac', '1', mono])
    invalid['unsupported mono'] = lambda p: p['audio'][0].update(path=str(mono))
    for label, mutate in invalid.items():
        candidate = copy.deepcopy(base)
        mutate(candidate)
        editor.write_json(root / 'invalid.json', candidate)
        try:
            editor.validate_plan(root / 'invalid.json')
        except ValueError:
            checks.append('validation rejects ' + label)
        else:
            raise AssertionError('Accepted invalid denoise: ' + label)
    control = copy.deepcopy(base)
    control['name'] += '-关闭对照'
    for item in control['audio']:
        del item['denoise']
    editor.write_json(root / 'control.json', control)
    editor.build_draft(Args(plan=str(root / 'control.json'), out=str(root / 'control')))
    assert not editor.read_json(root / 'control' / 'draft_content.json')['materials']['realtime_denoises']
    checks.append('unrequested denoise absent')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == fingerprint
    checks.append('source bytes unchanged')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'ui': 'separately verified in v10; this test is structural', 'root': str(root)}
    editor.write_json(root / '回归结果.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    import json
    for stream in (editor.sys.stdout, editor.sys.stderr):  # stdout is UTF-8 JSON for every caller, not the console code page
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    print(json.dumps(check(args.out, args.plan), ensure_ascii=False, indent=2))
