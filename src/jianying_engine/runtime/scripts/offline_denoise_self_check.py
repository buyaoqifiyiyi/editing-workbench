"""Offline FFT denoise checks with known PCM/noise; no model, native audio capture or human listening."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as Args
import wave

import numpy as np
from PIL import Image

import editor
import offline_denoise


def write_pcm(path, data, rate):
    if data.ndim == 1:
        data = data[:, None]
    with wave.open(str(path), 'wb') as handle:
        handle.setparams((data.shape[1], 2, rate, 0, 'NONE', 'not compressed'))
        handle.writeframes(np.rint(np.clip(data, -.99, .99) * 32768).astype('<i2').tobytes())


def read_pcm(path):
    with wave.open(str(path), 'rb') as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), '<i2').reshape(-1, handle.getnchannels()).astype(float) / 32768


def fixture(rate=48000, channels=2):
    t = np.arange(rate * 4) / rate
    envelope = ((t >= .6) & (t < 3.5)) * np.minimum(1, np.maximum(0, (t-.6)*50)) * np.minimum(1, np.maximum(0, (3.5-t)*50))
    clean = np.column_stack([.18 * envelope * np.sin(2*np.pi*(440 + 220*c)*t) for c in range(channels)])
    noise = np.random.default_rng(20261019).normal(0, .012, clean.shape)
    return clean, clean + noise


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    source = root / 'noisy.wav'
    clean, noisy = fixture()
    write_pcm(source, noisy, 48000)
    source_hash = offline_denoise.digest(source)
    def args(name, **changes):
        values = dict(input=str(source), start=0, duration=4, noise_floor=-40, reduction=12, out=str(root / name))
        values.update(changes)
        return Args(**values)
    checks = []
    report = offline_denoise.execute(args('main'))
    wet, dry = read_pcm(report['output_media']), read_pcm(report['comparison_media'])
    assert wet.shape == dry.shape == (192000, 2)
    assert np.array_equal(dry, read_pcm(source)) and offline_denoise.digest(source) == source_hash
    assert report['params']['channel_link'] == 'none' and report['params']['adaptive_noise_tracking'] is False
    checks.append('PCM count/rate/stereo and dry excerpt preserved, source identity unchanged')
    quiet = slice(4800, 19200)
    drop = 20*np.log10(np.sqrt(np.mean(dry[quiet]**2)) / np.sqrt(np.mean(wet[quiet]**2)))
    error_before, error_after = np.mean((dry-clean)**2), np.mean((wet-clean)**2)
    # Known stationary white noise + tonal fixture only. These are not speech-quality thresholds.
    assert drop > 3, drop
    assert error_after < error_before, (error_before, error_after)
    active = slice(48000, 144000)
    channel_correlations = []
    for channel in range(2):
        target = clean[active, channel]
        actual = wet[active, channel]
        gain = np.dot(actual, target) / np.dot(target, target)
        corr = float(np.corrcoef(actual, target)[0,1])
        assert .8 < gain < 1.1 and corr > .99, (gain, corr)
        assert abs(np.corrcoef(actual, clean[active, 1-channel])[0,1]) < .01
        channel_correlations.append(corr)
    checks.append('known stationary-noise energy/error reduced and distinct channel tones retained without swap')
    for rate in (44100, 48000):
        for channels in (1, 2):
            _, data = fixture(rate, channels)
            media = root / f'{rate}-{channels}.wav'
            write_pcm(media, data, rate)
            before = offline_denoise.digest(media)
            result = offline_denoise.execute(args(f'subset-{rate}-{channels}', input=str(media), start=.1234567, duration=1.234567))
            assert result['first_sample'] == round(.1234567*rate) and result['sample_count'] == round(1.234567*rate)
            expected = read_pcm(media)[result['first_sample']:result['first_sample']+result['sample_count']]
            assert np.array_equal(read_pcm(result['comparison_media']), expected)
            assert result['denoised']['frames'] == result['sample_count'] and result['denoised']['channels'] == channels
            assert offline_denoise.digest(media) == before
            checks.append(f'nearest-sample nonzero range at {rate}Hz/{channels} channel(s), exact PCM selection')
    silence = root / 'silence.wav'
    write_pcm(silence, np.zeros(48000), 48000)
    result = offline_denoise.execute(args('silent', input=str(silence), duration=1))
    assert not read_pcm(result['output_media']).any()
    checks.append('silence remains silence; no empty-track padding')
    pulses = root / 'pulses.wav'
    pulse_data = np.zeros((48000, 2))
    pulse_data[[2400, 24000, 47520], :] = .8
    write_pcm(pulses, pulse_data, 48000)
    pulse_result = offline_denoise.execute(args('pulses', input=str(pulses), duration=1))
    pulse_wet = read_pcm(pulse_result['output_media'])
    for expected in (2400, 24000, 47520):
        for channel in (0, 1):
            assert np.argmax(np.abs(pulse_wet[expected-10:expected+11, channel])) + expected - 10 == expected
    assert pulse_result['latency_calibration']['samples'] > 0
    checks.append('independent early/middle/tail transients keep exact sample positions after measured latency correction')
    image = root / 'image.png'
    Image.new('RGB', (320, 240), '#214260').save(image)
    plan = {'schema_version': 1, 'name': 'offline-denoise-check', 'mode': 'script',
            'canvas': {'width': 320, 'height': 240, 'fps': 30},
            'clips': [{'path': str(image), 'start': 0, 'duration': 4}],
            'audio': [{'path': report['output_media'], 'start': 0, 'duration': 4, 'source_in': 0, 'volume': 1}]}
    plan_path = root / 'plan.json'
    editor.write_json(plan_path, plan)
    frozen = plan_path.read_bytes()
    editor.build_draft(Args(plan=str(plan_path), out=str(root / 'draft')))
    native = editor.read_json(root / 'draft/draft_content.json')
    segment = next(t for t in native['tracks'] if t['type'] == 'audio')['segments'][0]
    material = next(m for m in native['materials']['audios'] if m['id'] == segment['material_id'])
    assert Path(material['path']) == Path(report['output_media'])
    assert segment['source_timerange'] == segment['target_timerange'] == {'start': 0, 'duration': 4000000}
    assert not native['materials'].get('realtime_denoises') and plan_path.read_bytes() == frozen
    archive = root / 'archive'
    archive.mkdir()
    editor.publish_draft(Args(draft=str(root / 'draft'), draft_root=str(archive), offline_copy=True))
    checks.append('formal build/offline publication binds ordinary audio with exact source/target range, no native double denoise')

    def reject(label, **changes):
        request = args('reject-' + str(len(checks)), **changes)
        before = offline_denoise.digest(source)
        try:
            offline_denoise.execute(request)
        except (ValueError, OSError, wave.Error):
            pass
        else:
            raise AssertionError(label + ' accepted')
        assert not Path(request.out).exists() and offline_denoise.digest(source) == before
        checks.append('refused: ' + label)
    for label, changes in (
        ('negative start', {'start': -1}), ('nonfinite start', {'start': float('nan')}),
        ('oversized finite start', {'start': 1e308}),
        ('bool duration', {'duration': True}), ('zero duration', {'duration': 0}),
        ('over 300 seconds', {'duration': 301}), ('beyond audio', {'start': 3, 'duration': 2}),
        ('noise floor outside range', {'noise_floor': -19}), ('nonfinite noise floor', {'noise_floor': float('inf')}),
        ('reduction outside range', {'reduction': 25}), ('bool reduction', {'reduction': True}),
        ('missing input', {'input': str(root / 'absent.wav')}), ('network URL', {'input': 'https://example.invalid/audio.wav'}),
    ):
        reject(label, **changes)
    for rate, channels in ((32000, 1), (48000, 3)):
        media = root / f'unsupported-{rate}-{channels}.wav'
        write_pcm(media, np.zeros((rate, channels)), rate)
        reject(f'unsupported {rate}Hz/{channels} channels', input=str(media), duration=1)
    reject('no audio', input=str(image), duration=1)
    multiple = root / 'two-audio.mkv'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-i', source,
                '-map', '0:a:0', '-map', '0:a:0', '-c:a', 'pcm_s16le', multiple])
    reject('multiple audio streams', input=str(multiple), duration=1)
    shifted = root / 'shifted.mkv'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-itsoffset', '1', '-i', source,
                '-map', '0:a:0', '-c:a', 'pcm_s16le', shifted])
    reject('nonzero audio clock', input=str(shifted), duration=1)
    playlist = root / 'network.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttps://example.invalid/segment.ts\n#EXT-X-ENDLIST\n', encoding='utf-8')
    reject('local playlist cannot access network protocol', input=str(playlist), duration=1)
    original_outputs = {p.name: offline_denoise.digest(p) for p in (root / 'main').iterdir()}
    try:
        offline_denoise.execute(args('main'))
    except FileExistsError:
        pass
    else:
        raise AssertionError('existing output overwritten')
    assert {p.name: offline_denoise.digest(p) for p in (root / 'main').iterdir()} == original_outputs
    checks.append('existing output files/report preserved')
    failed_cli = subprocess.run([sys.executable, str(Path(offline_denoise.__file__)), '--input', str(source), '--duration', '1',
                                 '--reduction', '12', '--out', str(root / 'missing-floor')], capture_output=True)
    assert failed_cli.returncode != 0 and not (root / 'missing-floor').exists()
    checks.append('CLI requires explicit noise floor and reduction')
    failed_cli = subprocess.run([sys.executable, str(Path(offline_denoise.__file__)), '--input', str(source), '--start', '1e308',
                                 '--duration', '1', '--noise-floor', '-40', '--reduction', '12',
                                 '--out', str(root / 'overflow-cli')], capture_output=True, encoding='utf-8')
    assert failed_cli.returncode == 1 and json.loads(failed_cli.stdout)['status'] == 'failed'
    assert not (root / 'overflow-cli').exists() and 'Traceback' not in failed_cli.stderr
    checks.append('oversized finite CLI start returns structured failure without arithmetic overflow')
    result = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'stationary_fixture': {'quiet_noise_reduction_db': float(drop), 'known_reference_mse_before': float(error_before),
                                     'known_reference_mse_after': float(error_after), 'channel_correlations': channel_correlations},
              'scope': 'synthetic known reference and actual PCM/build/offline publish; no speech listening/native PCM capture',
              'paid_calls': 0, 'ui_operations': 0}
    editor.write_json(root / 'offline-denoise-check.json', result)
    return result


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    options = parser.parse_args()
    editor.emit(check(options.out.resolve()))
