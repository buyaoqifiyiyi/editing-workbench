"""Create a bounded reversed source clip; never mutates a plan, draft or original media."""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import editor
import file_hash

MAX_SECONDS = 10
MAX_FRAME_BYTES = 512 * 1024 * 1024
FPS = (24, 25, 30, 50, 60)


def media_details(path, *extra):
    result = editor.run([editor.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                         *extra, '-of', 'json', path])
    return json.loads(result.stdout)


def frame_times(path, start, end):
    data = media_details(path, '-select_streams', 'v:0', '-read_intervals', f'{start:.9f}%{end:.9f}',
                         '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time')
    times = [editor.number(float(frame['best_effort_timestamp_time']), 'frame timestamp')
             for frame in data.get('frames', [])]
    return [time for time in times if start - .00001 <= time < end - .00001]


def execute(args):
    source, output = Path(args.input).resolve(), Path(args.out).resolve()
    if not source.is_file():
        raise ValueError('Input must be an existing local video file')
    if output.exists():
        raise FileExistsError('Output directory already exists; choose a new version')
    start = editor.number(args.start, 'start')
    duration = editor.number(args.duration, 'duration', .000001, MAX_SECONDS)
    if args.audio not in ('reverse', 'mute'):
        raise ValueError('Choose explicit --audio reverse|mute')
    details = media_details(source, '-show_streams', '-show_format')
    videos = [stream for stream in details.get('streams', []) if stream.get('codec_type') == 'video']
    audios = [stream for stream in details.get('streams', []) if stream.get('codec_type') == 'audio']
    if len(videos) != 1 or videos[0].get('disposition', {}).get('attached_pic') or source.suffix.lower() in editor.IMAGE_EXT:
        raise ValueError('Exactly one real video stream is required; images/cover art are unsupported')
    video = videos[0]
    fps = float(Fraction(video.get('avg_frame_rate', '0/1')))
    if fps not in FPS or Fraction(video.get('r_frame_rate', '0/1')) != Fraction(video.get('avg_frame_rate', '0/1')):
        raise ValueError('Only constant integer frame rates 24/25/30/50/60 are admitted')
    width, height = video['width'], video['height']
    if width % 2 or height % 2 or width * height > 1920 * 1080 or video.get('pix_fmt') not in ('yuv420p', 'yuvj420p'):
        raise ValueError('Only even-sized 8-bit YUV420 video up to 1920x1080 pixels is admitted')
    if (video.get('sample_aspect_ratio') not in (None, '1:1', 'N/A') or
            video.get('color_transfer') in ('smpte2084', 'arib-std-b67') or
            video.get('field_order') not in (None, 'unknown', 'progressive') or
            float(video.get('tags', {}).get('rotate', 0)) != 0 or
            any(float(side.get('rotation', 0)) != 0 for side in video.get('side_data_list', []))):
        raise ValueError('Rotated, anamorphic, interlaced or HDR video is not admitted')
    if abs(float(video.get('start_time', 0))) > .000001:
        raise ValueError('Video stream must start at timestamp zero')
    first, count = round(start * fps), round(duration * fps)
    if count < 2 or abs(first / fps - start) > .000001 or abs(count / fps - duration) > .000001:
        raise ValueError('Start/duration must align to video frames and select at least two frames')
    start, duration = first / fps, count / fps
    if width * height * 3 * count > MAX_FRAME_BYTES:
        raise ValueError('Decoded frame memory estimate exceeds 512 MiB; select a shorter clip')
    end = start + duration
    video_duration = float(video.get('duration', details.get('format', {}).get('duration', 0)))
    if end > video_duration + .000001:
        raise ValueError('Selected range exceeds the video duration')
    times = frame_times(source, start, end)
    if len(times) != count or any(abs(time - (first + index) / fps) > .00002 for index, time in enumerate(times)):
        raise ValueError('Selected video has irregular/missing frame timestamps; variable frame rate is not admitted')
    audio = None
    if args.audio == 'reverse':
        if len(audios) != 1:
            raise ValueError('Audio reversal requires exactly one audio stream; choose mute for silent input')
        audio = audios[0]
        rate, channels = int(audio.get('sample_rate', 0)), audio.get('channels', 0)
        audio_duration = float(audio.get('duration', details.get('format', {}).get('duration', 0)))
        if (rate not in (44100, 48000) or channels not in (1, 2) or
                abs(float(audio.get('start_time', 0))) > 1 / rate or end > audio_duration + 1 / rate):
            raise ValueError('Audio requires aligned 44.1/48 kHz mono/stereo coverage for the selected range')
    source_hash = file_hash.sha256(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jy-reverse-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'result'
        stage.mkdir()
        target = stage / 'reversed.mov'
        # Both filters buffer only the selected range. MOV keeps reversed audio as PCM, avoiding AAC padding.
        command = [editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                   '-protocol_whitelist', 'file,pipe', '-ss', f'{start:.9f}', '-t', f'{duration:.9f}', '-i', source,
                   '-map', '0:v:0', '-vf', 'reverse,setpts=PTS-STARTPTS',
                   '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                   '-fps_mode', 'passthrough', '-map_metadata', '-1', '-map_chapters', '-1']
        if audio:
            command += ['-map', '0:a:0', '-af', 'areverse,asetpts=PTS-STARTPTS', '-c:a', 'pcm_s16le']
        else:
            command += ['-an']
        editor.run([*command, target], timeout=300)
        info = editor.probe(target)
        actual_times = frame_times(target, 0, duration)
        if (len(actual_times) != count or any(abs(time - index / fps) > .00002 for index, time in enumerate(actual_times)) or
                info['width'] != width or info['height'] != height or abs(info['duration'] - duration) > .0001 or
                info['has_audio'] != bool(audio)):
            raise ValueError('Reversed media failed dimensions, duration, frame timing or audio-policy verification')
        if audio:
            actual_audio = next(stream for stream in media_details(target, '-show_streams')['streams']
                                if stream.get('codec_type') == 'audio')
            if abs(float(actual_audio['duration']) - duration) > 2 / rate:
                raise ValueError('Reversed audio does not cover the selected video duration')
        if file_hash.sha256(source) != source_hash:
            raise ValueError('Source changed during processing; result will not be delivered')
        report = {'status': 'passed', 'operation': 'offline_reverse', 'input': str(source),
                  'source_sha256': source_hash, 'source_in': start, 'duration': duration, 'frame_count': count,
                  'fps': fps, 'audio_policy': args.audio, 'output_media': str(output / target.name),
                  'output_sha256': file_hash.sha256(target), 'decoded_frame_bytes_estimate': width * height * 3 * count,
                  'source_modified': False, 'ui_compatibility': 'unverified',
                  'ffmpeg_version': editor.run([editor.tool('ffmpeg'), '-version']).stdout.splitlines()[0],
                  'scope': 're-encoded reversed source media; no native reverse flag or original frame/audio editability',
                  'verification': 'frame timestamps/count, duration, dimensions, audio policy and file identities',
                  'plan_use': 'Use output_media as a new clip, source_in=0; preserve this directory. Audio volume is explicit.',
                  'boundary': 'No mirroring of captions, effects, keys or other timeline tracks; no UI/playback/export proof'}
        # Exclusive output creation. Failures during final I/O may leave a partial folder, never a success report.
        shutil.copytree(stage, output)
        with (output / 'reverse-report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--audio', choices=('reverse', 'mute'), required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        editor.emit(execute(args))
        return 0
    except (ValueError, OSError, TypeError, KeyError, ZeroDivisionError, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
