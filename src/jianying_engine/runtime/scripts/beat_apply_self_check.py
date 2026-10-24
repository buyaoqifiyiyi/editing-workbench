"""Synthetic-candidate plan contract; real detection is checked independently."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
import wave
from PIL import Image
import beat_apply
import editor


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    media = root / 'media'
    media.mkdir()
    Image.new('RGB', (160, 90), (40, 80, 120)).save(media / 'still.png')
    with wave.open(str(media / 'music.wav'), 'wb') as handle:
        handle.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
        handle.writeframes(bytes(22050 * 5 * 2))
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-f', 'lavfi', '-i',
                'color=c=blue:s=160x90:r=30:d=3', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                str(media / 'clip.mp4')])
    base = {'schema_version': 1, 'name': '节拍应用合同', 'mode': 'script',
            'canvas': {'width': 160, 'height': 90, 'fps': 30},
            'clips': [{'path': 'media/still.png', 'start': 0, 'duration': 2},
                      {'path': 'media/clip.mp4', 'start': 2, 'duration': 2, 'source_in': .25}],
            'audio': [{'path': 'media/music.wav', 'track': 'music', 'start': 0,
                       'duration': 4, 'source_in': .25}]}
    source = root / 'plan.json'
    editor.write_json(source, base)
    original = source.read_bytes()
    data = {'schema_version': 1, 'source': {'path': str((media / 'music.wav').resolve()),
             'sha256': hashlib.sha256((media / 'music.wav').read_bytes()).hexdigest()},
            'selection': {'start_us': 250000, 'sample_rate': 22050, 'sample_count': 88200},
            'beat_samples': [11025 * i for i in range(1, 8)], 'tempo_bpm': 120.,
            'algorithm': {'synthetic': True}, 'params': {}}
    checks = []
    def apply(name, plan=None, result=None, every=2):
        input_path = source
        if plan is not None:
            input_path = root / (name + '-input.json')
            editor.write_json(input_path, plan)
        out = root / 'outputs' / (name + '.json')
        out.parent.mkdir(exist_ok=True)
        args = Args(plan=input_path, beats=root / 'controlled-candidates', every=every, out=out)
        with patch('beats.load_result', return_value=copy.deepcopy(data if result is None else result)):
            report = beat_apply.apply(args)
        return out, report
    out, report = apply('every-two')
    assert report['boundaries_us'] == [0, 1000000, 2000000]
    result = editor.read_json(out)
    assert result['clips'][1]['source_in'] == .25 and result['audio'][0]['source_in'] == .25
    assert all(Path(c['path']).is_absolute() for c in result['clips'] + result['audio'])
    assert source.read_bytes() == original
    checks.append('known candidate source/music clock, every-N, source-in and absolute media paths')
    one, report_one = apply('every-one', every=1)
    assert report_one['boundaries_us'] == [0, 500000, 1000000]
    checks.append('every-one uses first eligible candidate without inventing boundaries')
    fractional = copy.deepcopy(data)
    fractional['beat_samples'] = [10000, 20000, 30000, 40000]
    _, quantized = apply('frame-grid', result=fractional, every=1)
    frames = [0, round(10000 * 30 / 22050), round(20000 * 30 / 22050)]
    assert quantized['frame_numbers'] == frames
    assert quantized['boundaries_us'] == [round(x * 1000000 / 30) for x in frames]
    checks.append('integer-frame-first microsecond quantization')
    for sample, shift, fps, expected, second in ((3072, 680, 25, 3, 22050),
                                               (32512, 533, 60, 89, 60000)):
        q = copy.deepcopy(base)
        q['canvas']['fps'] = fps
        precise = copy.deepcopy(data)
        precise['selection']['start_us'] = 250000 + shift
        precise['beat_samples'] = [sample, second]
        _, exact = apply('fraction-boundary-' + str(fps), plan=q, result=precise, every=1)
        assert exact['frame_numbers'][1] == expected
        assert exact['frame_numbers'][2] > expected
        checks.append('exact sample clock avoids double-rounding at ' + str(fps) + ' fps, frame ' + str(expected))
    def refuse(label, plan=None, result=None, every=2):
        target = root / 'outputs' / ('refused-' + str(len(checks)) + '.json')
        try:
            apply('refused-' + str(len(checks)), plan, result, every)
        except (ValueError, TypeError, FileExistsError):
            assert not target.exists()
            checks.append(label)
        else:
            raise AssertionError('Expected refusal: ' + label)
    for value in (0, 17, True, 1.5):
        refuse('invalid interval ' + str(value), every=value)
    short = copy.deepcopy(data)
    short['beat_samples'] = [11025]
    refuse('not enough candidates', result=short)
    collision = copy.deepcopy(data)
    collision['beat_samples'] = [10, 20, 30, 40]
    refuse('frame quantization collision', result=collision, every=1)
    wrong = copy.deepcopy(data)
    wrong['source']['path'] = str(media / 'clip.mp4')
    refuse('wrong report media', result=wrong)
    outside = copy.deepcopy(data)
    outside['beat_samples'][-1] = outside['selection']['sample_count']
    refuse('candidate outside detection coverage', result=outside)
    for field, value in [('track', 'overlay'), ('speed', 2), ('volume', 1),
                         ('transform', {'scale_x': .5}), ('keyframes', [{'property': 'alpha', 'time': 0, 'value': .5}])]:
        q = copy.deepcopy(base)
        q['clips'][0][field] = value
        refuse('unsupported clip ' + field, plan=q)
    for field, value in [('track', 'voice'), ('speed', .5), ('fade_in', .1),
                         ('keyframes', [{'time': 0, 'value': .5}])]:
        q = copy.deepcopy(base)
        q['audio'][0][field] = value
        refuse('unsupported music ' + field, plan=q)
    q = copy.deepcopy(base)
    q['clips'].reverse()
    refuse('original clip array must remain timeline-ordered', plan=q)
    q = copy.deepcopy(base)
    q['captions'] = [{'text': '禁止重定时', 'start': 0, 'duration': 1}]
    refuse('captions cannot be silently retimed', plan=q)
    q = copy.deepcopy(base)
    q['clips'][1]['source_in'] = 1
    long_cuts = copy.deepcopy(data)
    long_cuts['beat_samples'] = [11025, 77175]
    refuse('retiming cannot exceed real video source range', plan=q, result=long_cuts, every=1)
    prior = out.read_bytes()
    with patch('beats.load_result', return_value=data):
        for target in (out, source):
            try:
                beat_apply.apply(Args(plan=source, beats=root, every=2, out=target))
            except ValueError:
                checks.append('refuse existing output or original plan ' + target.name)
            else:
                raise AssertionError('Expected output preservation refusal')
    assert out.read_bytes() == prior and source.read_bytes() == original
    built = editor.build_draft(Args(plan=out, out=root / 'draft'))
    assert built['verification']['draft_structure'] == 'passed'
    normalized, _, _ = editor.validate_plan(out)
    archive = root / 'archive'
    archive.mkdir()
    published = editor.publish_draft(Args(draft=root / 'draft', draft_root=archive,
                                          offline_copy=True, user_data=None))
    content = editor.read_json(Path(published['draft']) / 'draft_content.json')
    editor.verify_structure(content, normalized)
    segments = {track['name']: track['segments'] for track in content['tracks']}
    assert [s['target_timerange'] for s in segments['video']] == [
        {'start': 0, 'duration': 1000000}, {'start': 1000000, 'duration': 1000000}]
    assert segments['music'][0]['target_timerange'] == {'start': 0, 'duration': 2000000}
    assert segments['music'][0]['source_timerange'] == {'start': 250000, 'duration': 2000000}
    checks.append('formal build and offline publish preserve native clip/music microsecond boundaries')
    assert source.read_bytes() == original
    report = {'status': 'passed', 'check_count': len(checks), 'checks': checks,
              'candidate_source': 'controlled mocked load_result; real detection tested separately',
              'input_plan_unchanged': True, 'listening_UI_save_reopen_export': 'unverified'}
    editor.write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out.resolve()))
