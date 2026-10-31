"""Bounded 320x180 H264 review video from real native Jianying PNG frames."""
import argparse
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from PIL import Image

import editor
import file_hash
import native_preview


def _duration_seconds(sequence):
    return Fraction(sequence['duration_frames'], sequence['draft_fps'])


def _seconds(value):
    return format(float(value), '.12g')


def _local_audio_path(value):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('audio must be one local filesystem path')
    scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*):', value)
    if scheme and not re.match(r'^[A-Za-z]:[\\/]', value):
        raise ValueError('audio must not be a URL or use a URI scheme')
    expanded = os.path.expandvars(value)
    if expanded.startswith(('\\\\', '//')) or (os.name == 'nt' and ':' in expanded[2:]):
        raise ValueError('audio must be a local file, not a network or device path')
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        raise ValueError('audio must be an absolute local filesystem path')
    path = path.resolve()
    if (expanded.startswith(('\\\\', '//')) or path.drive.startswith('\\\\')
            or (os.name == 'nt' and ':' in str(path)[2:])):
        raise ValueError('audio must be a local file, not a network or device path')
    if not path.is_file():
        raise ValueError('audio must be one existing local file')
    return path


def prepare_audio(value, source_in, gain, duration):
    """Validate one caller-selected audio range and record its pre-render identity."""
    present = value is not None
    if not present:
        if source_in is not None or gain is not None:
            raise ValueError('audio-source-in and audio-volume require audio')
        return None
    if source_in is None or gain is None:
        raise ValueError('audio requires explicit audio-source-in and audio-volume')
    for number, label in ((source_in, 'audio-source-in'), (gain, 'audio-volume')):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(label + ' must be a finite number')
    if source_in < 0:
        raise ValueError('audio-source-in must be non-negative')
    if not 0 <= gain <= 4:
        raise ValueError('audio-volume must be a linear gain from 0 to 4')
    path = _local_audio_path(value)
    identity = file_hash.sha256(path)
    probe = json.loads(editor.run([
        editor.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file',
        '-show_streams', '-show_format', '-of', 'json', str(path),
    ]).stdout)
    streams = probe.get('streams', [])
    audios = [stream for stream in streams if stream.get('codec_type') == 'audio']
    videos = [stream for stream in streams if stream.get('codec_type') == 'video'
              and not stream.get('disposition', {}).get('attached_pic')]
    if len(audios) != 1 or videos:
        raise ValueError('audio must be a single audio file, not video or an image')
    measured_duration = float(audios[0].get('duration') or probe.get('format', {}).get('duration', 0))
    if not math.isfinite(measured_duration) or measured_duration <= 0:
        raise ValueError('audio source duration could not be measured')
    duration_value = float(duration)
    if not math.isfinite(duration_value) or not 0 < duration_value <= native_preview.MAX_SEQUENCE_SECONDS:
        raise ValueError('audio selection duration must be positive and at most 20 seconds')
    source_out = Fraction(str(source_in)) + Fraction(str(duration))
    if source_out > Fraction(str(measured_duration)):
        raise ValueError('audio selection exceeds the measured source duration')
    if file_hash.sha256(path) != identity:
        raise ValueError('Source audio changed while measuring its range')
    return {
        'path': str(path),
        'kind': 'audio',
        'sha256': identity,
        'source_in_seconds': source_in,
        'duration_seconds': duration_value,
        'source_out_seconds': float(source_out),
        'selection_duration_mode': 'exact_sequence_duration',
        'gain_linear': gain,
        'measured_source_duration_seconds': measured_duration,
        'source_sample_rate': int(audios[0].get('sample_rate', 0)),
        'source_channels': audios[0].get('channels'),
    }


def prepare_sequence(draft, start, duration, fps=30, audio=None, audio_source_in=None,
                     audio_volume=None):
    """Validate the draft clock and build the exact native-preview sequence request."""
    draft = Path(draft).resolve()
    editor.require_plaintext_draft(draft, 'to render a native preview sequence from this draft')
    content = editor.read_json(draft)
    if not isinstance(content, dict):
        raise ValueError('Native preview sequence requires a plaintext draft object')
    raw_duration, _, draft_fps = native_preview.draft_clock(content)
    if isinstance(draft_fps, float) and draft_fps.is_integer():
        draft_fps = int(draft_fps)
    sequence, times = native_preview.sequence_timing(draft_fps, start, duration, fps)
    end_frame = sequence['start_frame'] + sequence['duration_frames']
    if native_preview._frame_time_us(end_frame, sequence['draft_fps']) > raw_duration:
        raise ValueError('sequence range exceeds the draft duration')
    audio_request = prepare_audio(
        audio,
        audio_source_in,
        audio_volume,
        Fraction(sequence['duration_frames'], sequence['draft_fps']),
    )
    return {
        'draft': draft,
        'draft_sha256': file_hash.sha256(draft),
        'sequence': sequence,
        'times': times,
        'audio': audio_request,
    }


def technical_check(video, sequence, audio_requested):
    """Decode every stream and verify the review container against the sequence."""
    path = Path(video)
    probe = json.loads(editor.run([
        editor.tool('ffprobe'), '-v', 'error', '-count_frames',
        '-show_streams', '-show_format', '-of', 'json', str(path),
    ], timeout=120).stdout)
    streams = probe.get('streams', [])
    if (sum(item.get('codec_type') == 'video' for item in streams) != 1
            or sum(item.get('codec_type') == 'audio' for item in streams) != int(audio_requested)):
        raise ValueError('Encoded preview has an unexpected stream count')
    video_stream = next((item for item in streams if item.get('codec_type') == 'video'), None)
    audio_stream = next((item for item in streams if item.get('codec_type') == 'audio'), None)
    if video_stream is None:
        raise ValueError('Encoded preview has no video stream')
    expected_frames = sequence['frame_count']
    expected_fps = sequence['sample_fps']
    expected_duration = float(Fraction(expected_frames, expected_fps))
    try:
        actual_fps = Fraction(video_stream['avg_frame_rate'])
        actual_frames = int(video_stream.get('nb_read_frames'))
        actual_duration = float(video_stream.get('duration') or probe['format']['duration'])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError('Encoded preview metadata is incomplete') from error
    if (video_stream.get('codec_name') != 'h264'
            or video_stream.get('width') != 320
            or video_stream.get('height') != 180
            or video_stream.get('pix_fmt') != 'yuv420p'
            or actual_fps != expected_fps
            or actual_frames != expected_frames
            or not math.isfinite(actual_duration)
            or abs(actual_duration - expected_duration) > 1e-5):
        raise ValueError('Encoded preview video stream differs from the sequence')
    if audio_requested:
        if audio_stream is None or audio_stream.get('codec_name') != 'aac':
            raise ValueError('Encoded preview is missing the requested AAC review audio')
        audio_duration = float(audio_stream.get('duration') or probe['format']['duration'])
        if (int(audio_stream.get('sample_rate', 0)) != 48000
                or int(audio_stream.get('channels', 0)) != 2
                or not math.isfinite(audio_duration)
                or abs(audio_duration - expected_duration) > .05):
            raise ValueError('Encoded preview audio stream differs from the requested range')
    elif audio_stream is not None:
        raise ValueError('Silent sequence unexpectedly contains an audio stream')
    editor.run([
        editor.tool('ffmpeg'), '-v', 'error', '-xerror', '-nostdin',
        '-i', str(path), '-f', 'null', '-',
    ], timeout=600)
    return {
        'status': 'passed',
        'video_codec': video_stream['codec_name'],
        'pix_fmt': video_stream['pix_fmt'],
        'width': video_stream['width'],
        'height': video_stream['height'],
        'fps': str(actual_fps),
        'frames': actual_frames,
        'duration_seconds': actual_duration,
        'audio_codec': audio_stream.get('codec_name') if audio_stream else None,
        'audio_sample_rate': int(audio_stream['sample_rate']) if audio_stream else None,
        'audio_channels': int(audio_stream['channels']) if audio_stream else None,
        'full_decode': 'passed',
    }


def encode_video(output, sequence, audio_request=None):
    """Encode the existing native PNG files; no scaling or spatial filters are applied."""
    output = Path(output).resolve()
    if not output.is_dir():
        raise ValueError('Preview sequence workspace is missing')
    for index in range(sequence['frame_count']):
        frame = output / f'frame-{index}.png'
        if not frame.is_file() or not frame.stat().st_size:
            raise ValueError('Native preview PNG sequence is incomplete')
        with Image.open(frame) as image:
            if image.format != 'PNG' or image.size != (320, 180):
                raise ValueError('Native preview frames must be 320x180 PNG files')
            image.verify()
    target = output / 'preview.mp4'
    if target.exists():
        raise ValueError('Preview review video already exists')
    duration = _duration_seconds(sequence)
    command = [
        editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', str(sequence['sample_fps']), '-start_number', '0',
        '-i', str(output / 'frame-%d.png'),
    ]
    if audio_request is not None:
        if file_hash.sha256(audio_request['path']) != audio_request['sha256']:
            raise ValueError('Source audio changed before review encoding')
        command.extend([
            '-protocol_whitelist', 'file',
            '-ss', _seconds(audio_request['source_in_seconds']),
            '-t', _seconds(duration),
            '-i', audio_request['path'],
        ])
    command.extend(['-frames:v', str(sequence['frame_count']), '-map', '0:v:0'])
    if audio_request is None:
        command.append('-an')
    else:
        command.extend([
            '-map', '1:a:0',
            '-af', 'volume=' + _seconds(audio_request['gain_linear']),
            '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
        ])
    command.extend([
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-pix_fmt', 'yuv420p', '-r', str(sequence['sample_fps']),
        '-t', _seconds(duration), '-map_metadata', '-1',
        '-movflags', '+faststart', str(target),
    ])
    editor.run(command, timeout=600)
    technical = technical_check(target, sequence, audio_request is not None)
    return {
        'path': str(target),
        'sha256': file_hash.sha256(target),
        'size_bytes': target.stat().st_size,
        'duration_seconds': technical['duration_seconds'],
        'frame_count': technical['frames'],
        'video_codec': technical['video_codec'],
        'audio_codec': technical['audio_codec'],
        'technical_decode': technical,
        'spatial_processing': 'native PNG sequence only; no scale or spatial filter',
    }


def render_sequence(install, draft, output, start, duration, fps=30, audio=None,
                    audio_source_in=None, audio_volume=None):
    """Render native PNGs, encode a bounded review video, then publish sequence success."""
    prepared = prepare_sequence(
        draft, start, duration, fps, audio, audio_source_in, audio_volume)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Preview sequence output already exists; choose a new directory')
    editor.tool('ffmpeg')
    editor.tool('ffprobe')
    preview_report = native_preview.preview(
        Path(install), prepared['draft'], output, prepared['times'],
        sequence=prepared['sequence'],
    )
    if preview_report.get('status') != 'passed':
        raise ValueError('Native preview sequence did not pass')
    draft_sha_after_native = file_hash.sha256(prepared['draft'])
    if (draft_sha_after_native != prepared['draft_sha256']
            or preview_report.get('source_draft_unchanged') is not True):
        raise ValueError('Source draft changed during native preview')
    frames = preview_report.get('frames')
    if (not isinstance(frames, list)
            or len(frames) != prepared['sequence']['frame_count']
            or any(frame.get('stable_reads', 0) < 3 for frame in frames)):
        raise ValueError('Native preview sequence frame evidence is incomplete')
    if (preview_report.get('sequence', {}).get('spec') != prepared['sequence']
            or preview_report.get('runtime_unchanged') is not True):
        raise ValueError('Native preview sequence identity evidence is incomplete')
    for index, frame in enumerate(frames):
        path = output / f'frame-{index}.png'
        if frame.get('path') != str(path) or file_hash.sha256(path) != frame.get('sha256'):
            raise ValueError('Native preview PNG identity differs from the frame evidence')
    encoding = encode_video(output, prepared['sequence'], prepared['audio'])
    if any(file_hash.sha256(frame['path']) != frame['sha256'] for frame in frames):
        raise ValueError('Native preview PNG changed during review encoding')
    draft_sha_after_encode = file_hash.sha256(prepared['draft'])
    if draft_sha_after_encode != prepared['draft_sha256']:
        raise ValueError('Source draft changed during review encoding')
    audio_report = prepared['audio']
    if audio_report is not None:
        audio_sha_after = file_hash.sha256(audio_report['path'])
        if audio_sha_after != audio_report['sha256']:
            raise ValueError('Source audio changed during preview sequence generation')
        audio_report = dict(audio_report)
        audio_report.update({
            'sha256_after': audio_sha_after,
            'source_unchanged': True,
            'encoded_review_codec': encoding['audio_codec'],
        })
    preview_path = output / 'preview.json'
    max_clock_error = max(abs(frame['quantization_error_us']) for frame in frames)
    report = {
        'schema_version': 1,
        'status': 'passed',
        'visual_review': 'unverified',
        'sequence': {
            'start_frame': prepared['sequence']['start_frame'],
            'duration_frames': prepared['sequence']['duration_frames'],
            'draft_fps': prepared['sequence']['draft_fps'],
            'sample_fps': prepared['sequence']['sample_fps'],
            'frame_step': prepared['sequence']['frame_step'],
            'frame_count': prepared['sequence']['frame_count'],
            'start_seconds': float(Fraction(
                prepared['sequence']['start_frame'], prepared['sequence']['draft_fps'])),
            'duration_seconds': float(_duration_seconds(prepared['sequence'])),
            'end_seconds_exclusive': float(Fraction(
                prepared['sequence']['start_frame'] + prepared['sequence']['duration_frames'],
                prepared['sequence']['draft_fps'])),
            'clock_tolerance_us': preview_report['sequence']['clock_tolerance_us'],
            'max_abs_clock_error_us': max_clock_error,
            'clock_method': 'nearest microsecond of exact project-frame boundary',
            'supervisor_timeout_seconds': preview_report['sequence']['supervisor_timeout_seconds'],
        },
        'source_identity': {
            'draft_path': str(prepared['draft']),
            'draft_sha256_before': prepared['draft_sha256'],
            'draft_sha256_after': draft_sha_after_encode,
            'draft_unchanged': True,
            'runtime_version': preview_report['runtime_version'],
            'runtime_identity': preview_report['runtime_identity'],
            'runtime_identity_after': preview_report['runtime_identity_after'],
            'runtime_unchanged': True,
        },
        'frames': frames,
        'native_preview_report': {
            'path': str(preview_path),
            'sha256': file_hash.sha256(preview_path),
        },
        'audio': audio_report,
        'output': encoding,
        'boundaries': {
            'native_frame_read': 'passed; three identical reads per requested frame',
            'review_video_decode': 'passed',
            'native_pcm': False,
            'full_project_audio_mix': False,
            'hd_export': False,
            'ui_save_reopen': 'not_run',
            'human_audiovisual_review': 'not_performed',
            'visual_review': 'unverified',
            'composition_correctness': 'unverified; aligned stable reads can retain stale composition',
            'final_export': 'not_claimed',
        },
    }
    editor.write_json(output / 'preview-sequence.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--draft', type=Path, required=True, help='Plaintext draft_content.json')
    parser.add_argument('--out', type=Path, required=True, help='New preview sequence directory')
    parser.add_argument('--start', type=float, required=True)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--fps', type=int, choices=native_preview.SEQUENCE_FPS, default=30)
    parser.add_argument('--audio', help='One local audio file; omit for a silent review video')
    parser.add_argument('--audio-source-in', type=float)
    parser.add_argument('--audio-volume', type=float, help='Fixed linear gain from 0 to 4')
    args = parser.parse_args(argv)
    try:
        editor.emit(render_sequence(
            args.install, args.draft, args.out, args.start, args.duration, args.fps,
            args.audio, args.audio_source_in, args.audio_volume,
        ))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        editor.emit({'status': 'failed', 'error': str(error)})
        return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
