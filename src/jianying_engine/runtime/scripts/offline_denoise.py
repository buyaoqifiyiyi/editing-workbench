"""Create aligned dry/FFT-denoised PCM audio excerpts in a new directory; no native model or plan mutation."""
import argparse
from array import array
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave

import editor

MAX_SECONDS = 300


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pcm_info(path):
    """Check actual PCM bytes, not just container duration or a declared frame count."""
    with wave.open(str(path), 'rb') as handle:
        rate, channels, width, frames = handle.getframerate(), handle.getnchannels(), handle.getsampwidth(), handle.getnframes()
        if width != 2 or channels not in (1, 2) or rate not in (44100, 48000) or handle.getcomptype() != 'NONE':
            raise ValueError('Expected 44.1/48 kHz mono/stereo PCM16 WAV')
        count, peak, clipped = 0, 0, 0
        while block := handle.readframes(65536):
            if len(block) % (channels * width):
                raise ValueError('Truncated PCM frame')
            samples = array('h', block)
            if sys.byteorder != 'little':
                samples.byteswap()
            count += len(block) // (channels * width)
            peak = max(peak, max(abs(value) for value in samples))
            clipped += sum(value in (-32768, 32767) for value in samples)
        if count != frames or count == 0:
            raise ValueError('Empty/truncated PCM audio')
    return {'sample_rate': rate, 'channels': channels, 'sample_width': width, 'frames': frames,
            'duration': frames / rate, 'sample_peak': peak / 32768, 'full_scale_samples': clipped}


def measure_latency(directory, rate, channels, filter_spec):
    """Measure this executable/filter's content latency, rather than assuming equal length means alignment."""
    source, target = directory / 'latency-input.wav', directory / 'latency-output.wav'
    impulse = rate // 10
    data = array('h', [0]) * (rate // 2 * channels)
    for channel in range(channels):
        data[impulse * channels + channel] = 30000
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(source), 'wb') as handle:
        handle.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        handle.writeframes(data.tobytes())
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-protocol_whitelist', 'file,pipe', '-i', source, '-af', filter_spec, '-c:a', 'pcm_s16le', target])
    info = pcm_info(target)
    if (info['sample_rate'], info['channels'], info['frames']) != (rate, channels, rate // 2):
        raise ValueError('Latency probe changed PCM shape')
    with wave.open(str(target), 'rb') as handle:
        result = array('h', handle.readframes(handle.getnframes()))
    if sys.byteorder != 'little':
        result.byteswap()
    positions, amplitudes = [], []
    for channel in range(channels):
        values = result[channel::channels]
        peak = max(abs(value) for value in values)
        peaks = [i for i, value in enumerate(values) if abs(value) == peak]
        if peak < 15000 or len(peaks) != 1:
            raise ValueError('Latency probe requires one preserved impulse peak per channel')
        positions.append(peaks[0])
        amplitudes.append(peak)
    offsets = [p - impulse for p in positions]
    if len(set(offsets)) != 1 or not 0 <= offsets[0] <= rate // 10:
        raise ValueError('Latency probe found invalid or different channel delays')
    return {'samples': offsets[0], 'seconds': offsets[0] / rate, 'input_peak_sample': impulse,
            'output_peak_samples': positions, 'output_peak_amplitudes': amplitudes,
            'input_sha256': digest(source), 'output_sha256': digest(target), 'method': 'same-filter PCM impulse peak per channel'}


def execute(args):
    source, output = Path(args.input).resolve(), Path(args.out).resolve()
    if not source.is_file():
        raise ValueError('Input must be an existing local media file')
    if output.exists():
        raise FileExistsError('Output already exists; choose a new directory')
    start = editor.number(args.start, 'start')
    duration = editor.number(args.duration, 'duration', .1, MAX_SECONDS)
    reduction = editor.number(args.reduction, 'reduction', 3, 24)
    floor = editor.number(args.noise_floor, 'noise-floor', -80, -20)
    details = json.loads(editor.run([editor.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                    '-show_streams', '-show_format', '-of', 'json', source]).stdout)
    audios = [s for s in details.get('streams', []) if s.get('codec_type') == 'audio']
    if len(audios) != 1:
        raise ValueError('Exactly one audio stream is required; select/remux ambiguous streams first')
    audio = audios[0]
    rate, channels = int(audio.get('sample_rate', 0)), audio.get('channels', 0)
    if rate not in (44100, 48000) or channels not in (1, 2) or audio.get('channel_layout') not in (None, 'mono', 'stereo'):
        raise ValueError('Only 44.1/48 kHz mono/stereo input is admitted; no automatic resampling/downmix')
    if abs(editor.number(float(audio.get('start_time', 0)), 'audio start', -1e9, 1e9)) > 1 / rate:
        raise ValueError('Audio stream must start at timestamp zero')
    source_seconds = editor.number(float(audio.get('duration', details.get('format', {}).get('duration', 0))), 'audio duration')
    if start > source_seconds or source_seconds > sys.maxsize / rate:
        raise ValueError('Selected start or source clock exceeds the supported audio range')
    first, count = round(start * rate), round(duration * rate)
    if first + count > round(source_seconds * rate):
        raise ValueError('Selected range exceeds the audio stream duration')
    start, duration = first / rate, count / rate
    source_hash = digest(source)
    filter_spec = (f'afftdn=nr={reduction}:nf={floor}:nt=w:tn=0:tr=0:om=o:nl=none:'
                   'ad=0.5:rf=-38:fo=1:bm=1.25:gs=0')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jy-denoise-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'result'
        stage.mkdir()
        dry, wet = stage / 'dry.wav', stage / 'denoised.wav'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-protocol_whitelist', 'file,pipe', '-ss', f'{start:.12f}', '-i', source,
                    '-map', '0:a:0', '-vn', '-sn', '-dn', '-t', f'{duration:.12f}',
                    '-af', f'atrim=end_sample={count},asetpts=N/SR/TB',
                    '-c:a', 'pcm_s16le', '-map_metadata', '-1', '-map_chapters', '-1', dry], timeout=180)
        dry_info = pcm_info(dry)
        if (dry_info['sample_rate'], dry_info['channels'], dry_info['frames']) != (rate, channels, count):
            raise ValueError('Decoded excerpt does not cover the exact selected samples; no padding is inserted')
        latency = measure_latency(Path(temporary), rate, channels, filter_spec)
        delay = latency['samples']
        # Zero tail drains the measured filter delay; decode coverage above must be complete before this step.
        aligned_filter = (f'apad=pad_len={delay},' + filter_spec +
                          f',atrim=start_sample={delay}:end_sample={count + delay},asetpts=N/SR/TB')
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-protocol_whitelist', 'file,pipe', '-i', dry, '-map', '0:a:0', '-af', aligned_filter,
                    '-c:a', 'pcm_s16le', '-map_metadata', '-1', wet], timeout=180)
        wet_info = pcm_info(wet)
        if (wet_info['sample_rate'], wet_info['channels'], wet_info['frames']) != (rate, channels, count):
            raise ValueError('Denoising changed sample rate, channels or frame count')
        if digest(source) != source_hash:
            raise ValueError('Source changed during processing; result will not be delivered')
        report = {'status': 'passed', 'operation': 'offline_denoise', 'input': str(source), 'source_sha256': source_hash,
                  'source_bytes': source.stat().st_size, 'requested_start': args.start, 'requested_duration': args.duration,
                  'source_in': start, 'duration': duration, 'first_sample': first, 'sample_count': count,
                  'output_media': str(output / wet.name), 'comparison_media': str(output / dry.name),
                  'output_sha256': digest(wet), 'comparison_sha256': digest(dry), 'dry': dry_info, 'denoised': wet_info,
                  'params': {'method': 'ffmpeg_afftdn', 'noise_floor_db': floor, 'reduction_db': reduction,
                             'noise_type': 'white', 'adaptive_noise_tracking': False, 'channel_link': 'none', 'filter': filter_spec,
                             'aligned_filter': aligned_filter}, 'latency_calibration': latency,
                  'source_modified': False, 'ffmpeg_version': editor.run([editor.tool('ffmpeg'), '-version']).stdout.splitlines()[0],
                  'verification': 'actual PCM byte/frame counts, rate/channels, parameters and file identities; not listening',
                  'plan_use': 'Use output_media as ordinary audio, source_in=0; for video excerpts use the original source_in and mute source audio. Do not also enable native denoise.',
                  'boundaries': ['New PCM16 audio excerpts, not a native denoise parameter or editable spectral operation.',
                                 'Nearest-sample range; no resampling, downmix, time stretching, automatic noise profiling or loudness normalization.',
                                 'FFT white-noise estimate may damage speech/music or leave nonstationary noise; compare dry.wav and denoised.wav.',
                                 'No semantic source separation, no arbitrary quality guarantee, no UI/playback/listening/export proof.']}
        # Exclusive destination, as with reverse_media; a final I/O failure is never reported as success.
        shutil.copytree(stage, output)
        with (output / 'denoise-report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--noise-floor', type=float, required=True, help='Explicit white-noise floor estimate in dB (-80..-20)')
    parser.add_argument('--reduction', type=float, required=True, help='Explicit reduction in dB (3..24); not a quality score')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        editor.emit(execute(args))
        return 0
    except (ValueError, OSError, TypeError, KeyError, wave.Error, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
