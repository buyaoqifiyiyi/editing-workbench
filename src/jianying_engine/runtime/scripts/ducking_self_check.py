"""Offline ducking regression: envelope from a synthetic voice stem, build/readback, and every loud refusal.

No desktop control and no real song: the stem is generated here (two speech bursts over a quiet floor), so the
check proves the mapping, the ramp shape, the readback of every KFTypeVolume point, and that bad input fails
loudly instead of writing a silent envelope.
"""
import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as Args
import wave

import numpy as np

import ducking
import editor


def write_stem(path, seconds=8.0, rate=44100, bursts=((1.0, 2.0), (4.0, 5.0))):
    rng = np.random.default_rng(7)
    samples = rng.normal(0, 1e-4, int(seconds * rate))
    for begin, end in bursts:
        span = slice(int(begin * rate), int(end * rate))
        samples[span] += rng.normal(0, 0.2, span.stop - span.start)
    pcm = np.clip(samples, -1, 1)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm * 32767).astype('<i2').tobytes())
    return path


def run_cli(arguments):
    """CLI path in-process: a ValueError carries the loud refusal, a return means the JSON report was written."""
    try:
        ducking.main(arguments)
        return None
    except ValueError as error:
        return str(error)


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    stem = write_stem(root / 'stem.wav')
    silent = write_stem(root / 'silent.wav', bursts=())
    music_file = write_stem(root / 'music.wav', bursts=((0.0, 8.0),))
    poster = root / 'base.png'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'testsrc=s=640x360', '-frames:v', '1', str(poster)])

    def plan_of(name):
        return {'schema_version': 1, 'name': name, 'mode': 'script', 'canvas': {'width': 640, 'height': 360, 'fps': 30},
                'clips': [{'path': str(poster), 'start': 0, 'duration': 7}],
                'audio': [{'path': str(stem), 'start': 1, 'duration': 6, 'source_in': 1, 'speed': 1, 'track': 'voice'},
                          {'path': str(music_file), 'start': 1, 'duration': 6, 'source_in': 1, 'speed': 1,
                           'track': 'music', 'volume': 0.8}]}

    # 1. Speech detection: both bursts found, quiet floor does not fire, threshold is the guarded floor.
    samples, rate = ducking.read_mono(stem)
    db, times = ducking.frame_db(samples, rate)
    intervals, threshold_db = ducking.speech_intervals(db, times)
    assert len(intervals) == 2, intervals
    for (begin, end), (want_begin, want_end) in zip(intervals, ((1.0, 2.0), (4.0, 5.0))):
        assert abs(begin - want_begin) <= 0.06 and abs(end - want_end) <= 0.06, (begin, end)
    assert threshold_db == -55.0 and abs(float(np.percentile(db, 20)) + 80) < 6
    checks.append('synthetic stem: two bursts detected, quiet floor ignored, threshold at the -55 dBFS guard')

    # 2. Envelope shape from an exact segment list: clip-local keys, base outside, ducked inside, real ramps.
    item = plan_of('shape')['audio'][1]
    segments = root / 'segments.json'
    editor.write_json(segments, {'segments': [{'start': 1.0, 'end': 2.0}, {'start': 4.0, 'end': 5.0}]})
    exact = ducking.intervals_from_json(segments)
    keys = ducking.ducking_keys(exact, item)
    ducked = round(0.8 * 10 ** (-12 / 20.0), 6)
    assert [key['time'] for key in keys] == [0.0, 1.0, 1.35, 2.85, 3.0, 4.0, 4.35], keys
    assert [key['value'] for key in keys] == [ducked, ducked, 0.8, 0.8, ducked, ducked, 0.8], keys
    editor.validate_parameter_keys(keys, item['duration'], 'ducking', 0, 10, item['source_in'], item['speed'])
    checks.append('envelope: 7 clip-local keys, base 0.8 / ducked 0.201, ramps 0.15 s down and 0.35 s up')

    full_keys = ducking.ducking_keys([[0, 20]], item)
    assert full_keys == [{'time': 0.0, 'value': ducked}], full_keys
    zero_keys = ducking.ducking_keys([[2, 3]], item, attack=0, release=0)
    assert zero_keys == [{'time': 0.0, 'value': 0.8}, {'time': 0.999998, 'value': 0.8},
                         {'time': 1.0, 'value': ducked}, {'time': 2.0, 'value': ducked},
                         {'time': 2.000002, 'value': 0.8}], zero_keys
    for speed in (0.5, 2):
        scaled = dict(item, speed=speed)
        scaled_keys = ducking.ducking_keys([[2, 3]], scaled, attack=0, release=0)
        editor.validate_parameter_keys(scaled_keys, scaled['duration'], 'zero-ramp', 0, 10,
                                       scaled['source_in'], speed)
        assert any(key['value'] == ducked for key in scaled_keys), scaled_keys
    near_end = ducking.ducking_keys([[2, 6.99]], item, release=0)
    assert near_end[-1] == {'time': 5.990002, 'value': 0.8}, near_end
    checks.append('boundary envelope: full-clip speech keeps ducking; zero ramps preserve local/source microsecond order')

    # 3. The stem path must agree with the segment path for the same intervals, and silence must not duck.
    stem_keys = ducking.ducking_keys(intervals, item)
    detected = root / 'detected.json'
    editor.write_json(detected, [{'start': begin, 'end': end} for begin, end in intervals])
    assert stem_keys == ducking.ducking_keys(ducking.intervals_from_json(detected), item)
    moments = [editor.us(key['time']) for key in stem_keys]
    assert moments == sorted(set(moments)) and stem_keys[0]['time'] == 0.0, stem_keys
    assert {key['value'] for key in stem_keys} <= {0.8, ducked}, stem_keys
    quiet, _ = ducking.speech_intervals(*ducking.frame_db(*ducking.read_mono(silent)))
    assert quiet == [] and ducking.ducking_keys([], item) == []
    checks.append('stem path keeps the same contract as the segment path; a silent stem yields no keys')

    # 4. CLI + formal build: every volume keyframe is read back from the draft.
    plan_path = root / 'ducking.json'
    editor.write_json(plan_path, plan_of('人声分离离线混音'))
    ducked_path = root / 'ducking-ducked.json'
    assert run_cli([str(plan_path), '--vocals', str(stem), '--out', str(ducked_path)]) is None
    ducked_plan = editor.read_json(ducked_path)
    assert 'keyframes' not in ducked_plan['audio'][0] and len(ducked_plan['audio'][1]['keyframes']) == len(stem_keys)
    result = editor.build_draft(Args(plan=str(ducked_path), out=str(root / 'draft')))
    verification = result['verification']
    assert verification['status'] == 'structure_checked' and verification['native_effects']['parameter_readback'] == 'passed', verification
    assert verification['native_effects']['extensions']['audio_volume_keyframes'] == 1
    content = editor.read_json(root / 'draft' / 'draft_content.json')
    normalized = editor.read_json(root / 'draft' / 'edit-plan.json')
    track = next(t for t in content['tracks'] if t['type'] == 'audio' and len(t['segments']) == 1
                 and t['segments'][0]['material_id'] == next(m['id'] for m in content['materials']['audios']
                                                             if 'music.wav' in m['path']))
    points = track['segments'][0]['common_keyframes'][0]['keyframe_list']
    expected = [round((item['source_in'] + key['time']) * 1_000_000) for key in ducked_plan['audio'][1]['keyframes']]
    assert [p['time_offset'] for p in points] == expected, (points, expected)
    assert [p['values'][0] for p in points] == [key['value'] for key in ducked_plan['audio'][1]['keyframes']]
    assert not any('keyframes' in t for t in content['tracks'] if t['type'] == 'audio' and t is not track)
    checks.append('formal build: parameter readback passed, every source-time point matches the plan, voice stem untouched')

    full_plan = plan_of('full-clip-ducking')
    full_plan['audio'][1]['keyframes'] = full_keys
    full_path = root / 'full-clip.json'
    editor.write_json(full_path, full_plan)
    full_result = editor.build_draft(Args(plan=str(full_path), out=str(root / 'full-clip-draft')))
    assert full_result['verification']['native_effects']['parameter_readback'] == 'passed'
    checks.append('formal build: full-clip constant ducking key survives native parameter readback')

    relative_plan = plan_of('relative-media-ducking')
    for group in ('clips', 'audio'):
        for clip in relative_plan[group]:
            clip['path'] = Path(clip['path']).name
    relative_path = root / 'relative.json'
    editor.write_json(relative_path, relative_plan)
    before = relative_path.read_bytes()
    moved_path = root / 'relocated' / 'ducked.json'
    assert run_cli([str(relative_path), '--speech-json', str(segments), '--out', str(moved_path)]) is None
    moved = editor.read_json(moved_path)
    assert relative_path.read_bytes() == before
    for group in ('clips', 'audio'):
        for original, relocated in zip(relative_plan[group], moved[group]):
            assert Path(relocated['path']) == (root / original['path']).resolve()
    moved_result = editor.build_draft(Args(plan=str(moved_path), out=str(root / 'relocated-draft')))
    assert moved_result['verification']['native_effects']['parameter_readback'] == 'passed'
    checks.append('relocated plan: relative media retain source identity and build, source plan is byte-identical')

    # Nested dependencies use the existing migration resolver, even without building these optional effects.
    nested_plan = copy.deepcopy(relative_plan)
    nested_plan['audio'][1]['denoise'] = {'model_path': 'stem.wav'}
    nested_plan['clips'][0]['matting_cache'] = {'manifest': 'segments.json'}
    nested_path = root / 'nested.json'
    editor.write_json(nested_path, nested_plan)
    nested_before = nested_path.read_bytes()
    nested_out = root / 'relocated' / 'nested.json'
    assert run_cli([str(nested_path), '--speech-json', str(segments), '--out', str(nested_out)]) is None
    nested = editor.read_json(nested_out)
    assert Path(nested['audio'][1]['denoise']['model_path']) == stem.resolve()
    assert Path(nested['clips'][0]['matting_cache']['manifest']) == segments.resolve()
    assert nested_path.read_bytes() == nested_before
    nested_plan['audio'][1]['denoise']['model_path'] = 'missing-model.onnx'
    editor.write_json(nested_path, nested_plan)
    missing_out = root / 'relocated' / 'missing.json'
    error = run_cli([str(nested_path), '--speech-json', str(segments), '--out', str(missing_out)])
    assert error and 'missing-model.onnx' in error and not missing_out.exists(), error
    checks.append('relocated dependencies: model/manifest resolve from source; missing dependency refuses before output')

    # 5. Serialized tampering must be rejected by readback, not silently accepted.
    for label, mutate in {
            'volume key time': lambda c: points_mutate(c, len(stem_keys), lambda p: p[1].update(time_offset=expected[1] + 1000)),
            'volume key value': lambda c: points_mutate(c, len(stem_keys), lambda p: p[2].update(values=[0.9])),
    }.items():
        corrupt = copy.deepcopy(content)
        mutate(corrupt)
        try:
            editor.verify_structure(corrupt, normalized)
        except ValueError as error:
            assert 'volume keyframe' in str(error), error
            checks.append('tamper rejected: ' + label)
        else:
            raise AssertionError('Readback accepted corruption: ' + label)

    # 6. Invalid plans and bad CLI input must fail loudly.
    invalid = {
        'first key not at zero': lambda p: p['audio'][1]['keyframes'][0].update(time=0.5),
        'keys out of order': lambda p: p['audio'][1]['keyframes'][5].update(time=2.0),
        'gain above the admitted range': lambda p: p['audio'][1]['keyframes'][2].update(value=12),
        'key at the clip end': lambda p: p['audio'][1]['keyframes'][-1].update(time=6),
    }
    for label, mutate in invalid.items():
        candidate = copy.deepcopy(ducked_plan)
        mutate(candidate)
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except ValueError:
            checks.append('invalid plan rejected: ' + label)
        else:
            raise AssertionError('Accepted invalid plan: ' + label)
    refusals = {
        'positive ducking gain': [str(plan_path), '--vocals', str(stem), '--out', str(root / 'a.json'), '--duck-db', '3'],
        'both speech sources': [str(plan_path), '--vocals', str(stem), '--speech-json', str(segments),
                                '--out', str(root / 'b.json')],
        'output over the input': [str(plan_path), '--vocals', str(stem), '--out', str(plan_path)],
        'no clip on the track': [str(plan_path), '--vocals', str(stem), '--out', str(root / 'c.json'),
                                 '--track', 'nowhere'],
        'reversed segment': [str(plan_path), '--speech-json', str(root / 'reversed.json'), '--out', str(root / 'd.json')],
    }
    editor.write_json(root / 'reversed.json', [{'start': 3.0, 'end': 2.0}])
    for label, arguments in refusals.items():
        error = run_cli(arguments)
        assert error, 'accepted: ' + label
        checks.append('refused: ' + label)
    for protected in (segments, stem, ducked_path):
        before = protected.read_bytes()
        error = run_cli([str(plan_path), '--speech-json', str(segments), '--out', str(protected)])
        assert error and protected.read_bytes() == before, (protected, error)
    checks.append('existing output refused: speech JSON, source stem and prior output remain byte-identical')
    nonfinite_out = root / 'nonfinite-output.json'
    empty_segments = root / 'empty-segments.json'
    editor.write_json(empty_segments, [])
    for flag in ('duck-db', 'attack', 'release', 'floor-margin-db', 'threshold-floor-db', 'min-speech', 'merge-gap'):
        for value in ('nan', 'inf', '-inf'):
            error = run_cli([str(plan_path), '--speech-json', str(empty_segments), '--out', str(nonfinite_out),
                             '--' + flag + '=' + value])
            assert error and 'finite' in error and not nonfinite_out.exists(), (flag, value, error)
    invalid_segments = root / 'nonfinite-segments.json'
    for field in ('start', 'end'):
        for value in (float('nan'), float('inf'), float('-inf')):
            segment = {'start': 1, 'end': 2, field: value}
            editor.write_json(invalid_segments, [segment])
            error = run_cli([str(plan_path), '--speech-json', str(invalid_segments), '--out', str(nonfinite_out)])
            assert error and 'finite' in error and not nonfinite_out.exists(), (field, value, error)
    checks.append('nonfinite input refused: every CLI float and segment endpoint rejects NaN/Infinity before output')
    eight_bit = root / 'eight-bit.wav'
    with wave.open(str(eight_bit), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(bytes([128]) * 8000)
    refused_out = root / 'eight-bit-output.json'
    refused = subprocess.run([sys.executable, str(Path(ducking.__file__)), str(plan_path),
                              '--vocals', str(eight_bit), '--out', str(refused_out)],
                             capture_output=True, text=True, encoding='utf-8', timeout=30)
    refusal = json.loads(refused.stdout)
    assert refused.returncode == 1 and refusal['status'] == 'failed', refusal
    assert '8-bit' in refusal['error'] and not refused_out.exists(), refusal
    checks.append('8-bit WAV CLI: unsupported sample width returns JSON failure and creates no output')
    assert not (root / 'a.json').exists() and not (root / 'b.json').exists()
    checks.append('no partial output written for refused runs')

    report = {'status': 'passed', 'checks': checks, 'checks_count': len(checks),
              'ducking_inputs': {'stem': str(stem), 'silent': str(silent), 'plan': str(plan_path),
                                 'ducked_plan': str(ducked_path), 'draft': str(root / 'draft')},
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'ducking-check.json', report)
    return report


def points_mutate(content, count, mutate):
    """Find the ducked music segment's keyframe list in a serialized draft and mutate one point."""
    for track in content['tracks']:
        if track['type'] != 'audio':
            continue
        for segment in track['segments']:
            groups = segment.get('common_keyframes', [])
            if groups and len(groups[0]['keyframe_list']) == count:
                mutate(groups[0]['keyframe_list'])
                return
    raise AssertionError('No serialized volume keyframe list with ' + str(count) + ' points')


if __name__ == '__main__':
    editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    arguments = parser.parse_args()
    editor.emit(check(arguments.out.resolve()))
