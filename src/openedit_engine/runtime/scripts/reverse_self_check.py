"""Real reverse processing, decoded frame/sample comparisons and editable draft readback."""
import argparse
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import editor
import reverse_media


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


def decoded(path, audio=False, start=0, duration=1):
    command = [editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-ss', str(start), '-t', str(duration), '-i', str(path)]
    command += (['-map', '0:a:0', '-f', 's16le', '-acodec', 'pcm_s16le', '-'] if audio else
                ['-map', '0:v:0', '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-'])
    result = subprocess.run(command, capture_output=True, check=True)
    return result.stdout


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    source = root / '原片 时间与音频.mov'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', "nullsrc=s=160x90:r=30:d=2,geq=lum='20+N*3':cb=128:cr=128",
                '-f', 'lavfi', '-i', 'aevalsrc=0.2*sin(2*PI*(220*t+60*t*t)):s=48000:d=2',
                '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-ac', '2', source])
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = []
    request = SimpleNamespace(input=str(source), start=.5, duration=1., audio='reverse', out=str(root / 'reversed'))
    result = reverse_media.execute(request)
    target = Path(result['output_media'])
    source_frames = np.frombuffer(decoded(source, start=.5), np.uint8).reshape(30, 90, 160, 3)
    reversed_frames = np.frombuffer(decoded(target), np.uint8).reshape(30, 90, 160, 3)
    source_levels = source_frames.mean(axis=(1, 2, 3))
    reversed_levels = reversed_frames.mean(axis=(1, 2, 3))
    identities = [int(np.abs(source_levels - level).argmin()) for level in reversed_levels]
    assert identities == list(range(29, -1, -1)), identities
    pixel_error = float(np.abs(reversed_frames.astype(float) - source_frames[::-1]).mean())
    assert pixel_error < 2, pixel_error
    checks.append('all 30 selected decoded frame identities strictly reversed; nonzero source offset and duration correct')
    source_pcm = np.frombuffer(decoded(source, audio=True, start=.5), '<i2').reshape(-1, 2)
    actual_pcm = np.frombuffer(decoded(target, audio=True), '<i2').reshape(-1, 2)
    assert actual_pcm.shape == source_pcm.shape == (48000, 2)
    assert np.array_equal(actual_pcm, source_pcm[::-1])
    checks.append('all 48000 stereo PCM samples reversed exactly, with no AAC delay or padding')
    muted = copy.copy(request)
    muted.audio, muted.out = 'mute', str(root / 'muted')
    muted_report = reverse_media.execute(muted)
    assert not editor.probe(muted_report['output_media'])['has_audio']
    checks.append('explicit mute removes the source audio stream')
    plan = {'schema_version': 1, 'name': '倒放原生草稿测试', 'mode': 'script',
            'canvas': {'width': 160, 'height': 90, 'fps': 30},
            'clips': [{'path': str(target), 'start': 0, 'duration': 1, 'source_in': 0, 'volume': 1}],
            'captions': [{'text': '倒放素材仍可剪辑', 'start': 0, 'duration': 1}]}
    plan_path = root / 'edit-plan.json'
    editor.write_json(plan_path, plan)
    calls = []
    client = _FakeFilmCraft(calls)
    with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
            patch('craft_rpc.CraftMcpClient', return_value=client):
        built = editor.build_draft(SimpleNamespace(plan=str(plan_path), out=str(root / 'project')))
    assert built['status'] == 'passed'
    assert str(target) in client.imported_paths
    place = next(p for cid, p in calls if cid == 'timeline.place')
    assert round(place['seconds'] * 1_000_000) == 0
    assert round(place['duration'] * 1_000_000 / editor.TICKS_PER_SEC) == 1000000
    assert place['audio'] is True
    checks.append('validate/build produces project referencing the new reversed media and explicit audio flag')

    def reject(label, expected, **updates):
        options = copy.copy(request)
        options.out = str(root / ('rejected-' + str(len(checks))))
        for key, value in updates.items():
            setattr(options, key, value)
        before = {path: path.read_bytes() for path in (Path(options.out),) if path.is_file()}
        existed = Path(options.out).exists()
        try:
            reverse_media.execute(options)
        except (ValueError, OSError) as exc:
            assert expected.lower() in str(exc).lower(), (label, str(exc))
        else:
            raise AssertionError('Accepted: ' + label)
        assert existed or not Path(options.out).exists()
        assert all(path.read_bytes() == raw for path, raw in before.items())
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
        checks.append(label)

    reject('existing output refused', 'already exists', out=request.out)
    reject('input file as output refused', 'already exists', out=str(source))
    reject('missing input refused', 'local video', input=str(root / 'missing.mov'))
    reject('negative source time refused', 'range', start=-1)
    reject('nonfinite source time refused', 'finite', start=float('nan'))
    reject('duration above bounded limit refused', 'range', duration=11)
    reject('source range overflow refused', 'exceeds', start=1.5)
    reject('non-frame-aligned range refused', 'align', start=.01)
    reject('single-frame selection refused', 'two frames', duration=1 / 30)
    reject('implicit audio policy refused', 'explicit', audio=None)
    reject('reverse audio on silent source refused', 'one audio', input=muted_report['output_media'], start=0)
    large = root / 'large.mov'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=black:s=1920x1080:r=30:d=0.1',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', large])
    reject('decoded-memory budget enforced before processing', '512 MiB', input=str(large), start=0, duration=4, audio='mute')
    slow = root / '12fps.mov'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=12:d=2',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', slow])
    reject('unadmitted frame rate refused', 'integer frame rates', input=str(slow), start=0, audio='mute')
    rotated = root / 'rotated.mov'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-display_rotation:v:0', '90', '-i', source, '-map', '0', '-c', 'copy', rotated])
    assert any(side.get('rotation') for stream in reverse_media.media_details(rotated, '-show_streams')['streams']
               for side in stream.get('side_data_list', []))
    reject('display-matrix rotation refused', 'rotated', input=str(rotated))
    # Local network playlists are not allowed to turn this offline operation into a fetcher.
    playlist = root / 'remote.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:1/never-fetch.ts\n#EXT-X-ENDLIST\n', encoding='utf-8')
    reject('network protocols refused even inside local playlists', 'whitelist', input=str(playlist), start=0, audio='mute')
    rate_matrix = []
    for fps, rate, channels in ((24, 44100, 1), (25, 48000, 1), (50, 44100, 2), (60, 48000, 2)):
        media = root / f'matrix-{fps}.mov'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-f', 'lavfi', '-i', f"nullsrc=s=160x90:r={fps}:d=1.5,geq=lum='20+N*3':cb=128:cr=128",
                    '-f', 'lavfi', '-i', f'aevalsrc=0.2*sin(2*PI*(220*t+60*t*t)):s={rate}:d=1.5',
                    '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-ac', str(channels), media])
        options = SimpleNamespace(input=str(media), start=0, duration=1, audio='reverse', out=str(root / f'output-{fps}'))
        matrix_result = reverse_media.execute(options)
        frames_in = np.frombuffer(decoded(media), np.uint8).reshape(fps, 90, 160, 3)
        frames_out = np.frombuffer(decoded(matrix_result['output_media']), np.uint8).reshape(fps, 90, 160, 3)
        reference = frames_in.mean(axis=(1, 2, 3))
        observed = frames_out.mean(axis=(1, 2, 3))
        assert [int(np.abs(reference - value).argmin()) for value in observed] == list(range(fps - 1, -1, -1))
        pcm_in = np.frombuffer(decoded(media, audio=True), '<i2').reshape(rate, channels)
        pcm_out = np.frombuffer(decoded(matrix_result['output_media'], audio=True), '<i2').reshape(rate, channels)
        assert np.array_equal(pcm_out, pcm_in[::-1])
        rate_matrix.append({'fps': fps, 'audio_rate': rate, 'channels': channels, 'frame_order': 'passed', 'pcm_exact': True})
        checks.append(f'{fps}fps / {rate}Hz / {channels}channel input: every frame and PCM sample reversed')
    report_on_disk = editor.read_json(root / 'reversed/reverse-report.json')
    assert report_on_disk == result and hashlib.sha256(source.read_bytes()).hexdigest() == original
    checks.append('report matches output and input content hash remains unchanged')
    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'frame_identities': identities, 'mean_pixel_error': pixel_error,
              'audio_samples_per_channel': len(actual_pcm), 'audio_samples_exact': True,
              'additional_rate_matrix': rate_matrix,
              'source_sha256_unchanged': original, 'model_calls': 0, 'ui_operations': 0,
              'boundary': 'decoded media + native draft structure + offline publish; no UI or continuous audiovisual/export proof'}
    editor.write_json(root / 'reverse-self-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args()
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    if args.out:
        editor.emit(check(Path(args.out).resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix='jy-reverse-check-') as temporary:
            editor.emit(check(Path(temporary) / 'check'))
