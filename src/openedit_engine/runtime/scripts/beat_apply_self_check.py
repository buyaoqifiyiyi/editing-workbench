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
import beats
import file_hash


class _FakeFilmCraft:
    def __init__(self, calls):
        self.calls = calls
        self.seq = {'video': [{'items': []}], 'audio': [{'items': []}], 'durationFrames': 60}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call_tool(self, name, params=None):
        params = params or {}
        if name == 'project_inspect':
            return {'root': {'children': [{'item': f'item_{i}', 'name': p} for i, p in enumerate(self.names)]}}
        if name == 'media_import':
            self.names = [Path(p).name for p in params['paths']]
            return {'imported': len(self.names)}
        if name == 'sequence_inspect':
            return self.seq
        if name == 'command_run':
            cid, p = params['id'], params.get('params', {})
            self.calls.append((cid, p))
            if cid == 'timeline.place':
                is_aud = p.get('video') is False
                track = p['track']
                idx = int(track[1:]) - 1
                target = self.seq['audio'] if is_aud else self.seq['video']
                while len(target) <= idx:
                    target.append({'items': []})
                target[idx]['items'].append({'clip': f'c_{len(self.calls)}', 'start': round(p['seconds'] * editor.TICKS_PER_SEC), 'duration': p['duration']})
            elif cid == 'file.saveAs':
                Path(p['path']).write_text('{}', encoding='utf-8')
        return {}


def check_marks(root):
    """Synthetic reports and real loader/validator using open-source microsecond timing."""
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    for width, height, seconds, audio_seconds, source_in, start, duration in (
            (320, 180, 4, 4, 0, 0, 4), (320, 180, 4, 4, .5, 1, 2.5),
            (640, 360, 10, 12, 2, 1, 8)):
        case = root / str(len(checks))
        case.mkdir()
        video, audio = case / 'video.mp4', case / 'audio.wav'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-f', 'lavfi', '-i', f'color=s={width}x{height}:r=30:d={seconds}',
                    '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)])
        with wave.open(str(audio), 'wb') as handle:
            handle.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
            handle.writeframes(bytes(audio_seconds * 44100 * 2))
        base = {'schema_version': 1, 'name': 'open beat mapping',
                'canvas': dict(width=width, height=height, fps=30),
                'clips': [dict(path=str(video), start=0, source_in=0, duration=seconds, volume=0)],
                'audio': [dict(path=str(audio), track='music', start=start,
                               source_in=source_in, duration=duration, volume=1.0)]}
        source = case / 'plan.json'
        editor.write_json(source, base)
        before = source.read_bytes()
        report_dir = case / 'candidates'
        report_dir.mkdir()
        # Deliberately nonzero analysis start, independent of audio timeline start.
        data = dict(schema_version=1, source=dict(path=str(audio), sha256=file_hash.sha256(audio)),
                    selection=dict(start_us=250000, sample_rate=beats.RATE,
                                   sample_count=(audio_seconds - 1) * beats.RATE),
                    beat_samples=list(range(0, (audio_seconds - 1) * beats.RATE, beats.HOP * 20)),
                    tempo_bpm=120., algorithm=dict(name=beats.ALGORITHM, librosa=beats.LIBROSA,
                        numba=beats.NUMBA, implementation_sha256=beats.implementation_sha()),
                    params=dict(hop_length=beats.HOP, start_bpm=120., tightness=100., trim=True, units='frames'))
        def write_report(value):
            editor.write_json(report_dir / 'beats.json', value)
            editor.write_json(report_dir / 'beat-report.json', dict(schema_version=1, status='succeeded',
                beats_file='beats.json', beats_sha256=file_hash.sha256(report_dir / 'beats.json'),
                source_sha256=value['source']['sha256'], implementation_sha256=beats.implementation_sha(),
                dependencies=dict(librosa=beats.LIBROSA, numba=beats.NUMBA, numpy='synthetic',
                                  scipy='synthetic', llvmlite='synthetic'), boundaries=[], auditory_review='unverified'))
        write_report(data)
        args = Args(plan=source, beats=report_dir, every=4, out=case / 'marks.json', mark_audio=True)
        report = beat_apply.apply(args)
        normalized, _, _ = editor.validate_plan(source)
        result, _, _ = editor.validate_plan(args.out)
        expected = copy.deepcopy(normalized)
        expected['audio'][0]['beat_marks'] = result['audio'][0]['beat_marks']
        expected['notes'] = result['notes']
        assert result == expected and source.read_bytes() == before
        from fractions import Fraction
        points = [round(Fraction(250000) + Fraction(s * 1000000, beats.RATE))
                  for s in data['beat_samples']
                  if source_in * 1000000 <= Fraction(250000) + Fraction(s * 1000000, beats.RATE)
                  < (source_in + duration) * 1000000][3::4]
        assert report['beat_marks_us'] == points
        assert any(p % 100000 for p in points)  # no frame snapping
        checks.append('source clock, every-N, nonzero analysis offset and unchanged timing ' + str(width) + '/' + str(source_in))
        original_output = args.out.read_bytes()
        def refuse(label, value=None, plan=None, every=4, target=None):
            write_report(data if value is None else value)
            editor.write_json(source, base if plan is None else plan)
            target = target or case / 'refused.json'
            saved = target.read_bytes() if target.exists() else None
            try:
                beat_apply.apply(Args(plan=source, beats=report_dir, every=every, out=target, mark_audio=True))
            except (ValueError, TypeError, OSError):
                checks.append(label)
            else:
                raise AssertionError('Expected refusal: ' + label)
            assert (target.read_bytes() if target.exists() else None) == saved
        refuse('existing output', target=args.out)
        refuse('source output conflict', target=source)
        assert args.out.read_bytes() == original_output
        for n in (0, 17, True, 1.5):
            refuse('invalid interval', every=n)
        empty = copy.deepcopy(data); empty.update(beat_samples=[], tempo_bpm=0)
        refuse('empty candidates', value=empty)
        few = copy.deepcopy(data); few['beat_samples'] = [0]
        refuse('insufficient after every-N/selection', value=few)
        for points_bad in ([0, 0], [256, 0], [True], [1], [data['selection']['sample_count']]):
            bad = copy.deepcopy(data); bad['beat_samples'] = points_bad
            refuse('invalid sample order/type/range/hop', value=bad)
        for field, value in [('algorithm', dict(name='forged')), ('source', dict(path=str(video), sha256=file_hash.sha256(video)))]:
            bad = copy.deepcopy(data); bad[field] = value
            refuse('report identity ' + field, value=bad)
        bad = copy.deepcopy(data); bad['source']['sha256'] = '0' * 64
        refuse('stale source SHA', value=bad)
        q = copy.deepcopy(base); q['audio'][0].update(beat_marks=[source_in + .5])
        refuse('existing marks refused', plan=q)
        write_report(data); editor.write_json(source, base)
        edge = copy.deepcopy(data)
        edge['selection']['start_us'] = beat_apply._micros(source_in)
        edge['beat_samples'] = [0]
        write_report(edge)
        edge_result = beat_apply.apply(Args(plan=source, beats=report_dir, every=1,
            out=case / 'inclusive-low.json', mark_audio=True))
        assert edge_result['beat_marks_us'] == [beat_apply._micros(source_in)]
        checks.append('source-in endpoint included without subtracting source-in or adding timeline start')
        edge['selection']['start_us'] = beat_apply._micros(source_in + duration)
        refuse('source-out endpoint excluded', value=edge, every=1)
        write_report(data)
        assert source.read_bytes() == before
    return checks


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
    calls = []
    with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
            patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeFilmCraft(calls)):
        built = editor.build_draft(Args(plan=out, out=root / 'fc-proj'))
    assert built['status'] == 'passed' and Path(built['project']).is_file()
    placed = [p for cid, p in calls if cid == 'timeline.place']
    assert [(p['track'], round(p['seconds'] * 1_000_000), round(p['duration'] * 1_000_000 / editor.TICKS_PER_SEC)) for p in placed] == [
        ('V1', 0, 1000000), ('V1', 1000000, 1000000), ('A1', 0, 2000000)]
    assert round(placed[2]['sourceIn'] * 1_000_000 / editor.TICKS_PER_SEC) == 250000
    checks.append('formal openedit build preserves clip/music microsecond boundaries')
    assert source.read_bytes() == original
    checks.extend(check_marks(root / 'open-marks'))
    report = {'status': 'passed', 'check_count': len(checks), 'checks': checks,
              'candidate_source': 'mocked load_result & synthetic reports with real load_result; mocked FilmCraft RPC',
              'input_plan_unchanged': True, 'listening_UI_save_reopen_export': 'unverified'}
    editor.write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out.resolve()))
