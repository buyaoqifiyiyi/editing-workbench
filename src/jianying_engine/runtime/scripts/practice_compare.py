"""Render a local, bounded A/B comparison from two explicitly chosen videos.

The source videos are not edited. A comparison is evidence to inspect, not an
automatic judgment that either edit is better or that the sources are equivalent.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import editor
import file_hash


def compare(args):
    duration = editor.number(args.duration, 'duration', 0.1, 20)
    left_start = editor.number(args.left_start, 'left start')
    right_start = editor.number(args.right_start, 'right start')
    for label, value in [('goal', args.goal), ('change', args.change)]:
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f'{label} must be non-empty text of at most 500 characters')
    left, right = editor.probe(args.left), editor.probe(args.right)
    for label, info, start in [('left', left, left_start), ('right', right, right_start)]:
        if info['kind'] != 'video':
            raise ValueError(f'{label} must be a video')
        if start + duration > info['duration'] + 0.000001:
            raise ValueError(f'{label} selected range exceeds media duration')
    if args.audio != 'none' and not {'left': left, 'right': right}[args.audio]['has_audio']:
        raise ValueError(f'{args.audio} video has no audio')
    if Path(args.out).exists() or Path(args.out).is_symlink():
        raise ValueError('Output directory already exists; refusing to overwrite')
    identities = {}
    for info in (left, right):
        path = info['path']
        if path not in identities:
            identities[path] = file_hash.sha256(path)
    output = editor.new_directory(args.out)
    target = output / 'comparison.mp4'
    transform = (f'fps={args.fps},scale=320:180:force_original_aspect_ratio=decrease,'
                 'pad=320:180:(ow-iw)/2:(oh-ih)/2:black,setsar=1,setpts=PTS-STARTPTS')
    graph = f'[0:v:0]{transform}[left];[1:v:0]{transform}[right];[left][right]hstack=inputs=2[v]'
    cmd = [editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n',
           '-ss', left_start, '-i', left['path'], '-ss', right_start, '-i', right['path'],
           '-filter_complex_threads', '1', '-filter_complex', graph, '-map', '[v]']
    if args.audio == 'none':
        cmd += ['-an']
    else:
        cmd += ['-map', '0:a:0' if args.audio == 'left' else '1:a:0',
                '-c:a', 'aac', '-b:a', '160k', '-ar', '48000']
    cmd += ['-t', duration, '-c:v', 'libx264', '-preset', 'fast', '-crf', '19',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(target)]
    editor.run(cmd, timeout=300)
    for path, identity in identities.items():
        if file_hash.sha256(path) != identity:
            raise ValueError('Input changed during comparison; reject output')
    probe = editor.run([editor.tool('ffprobe'), '-v', 'error', '-count_frames',
                        '-show_streams', '-of', 'json', target], timeout=120)
    streams = json.loads(probe.stdout)['streams']
    video = next(item for item in streams if item['codec_type'] == 'video')
    expected = round(duration * args.fps)
    if int(video['nb_read_frames']) != expected:
        raise ValueError(f'Comparison frame count differs: {video["nb_read_frames"]} vs {expected}')
    has_audio = any(item['codec_type'] == 'audio' for item in streams)
    if has_audio != (args.audio != 'none'):
        raise ValueError('Comparison audio stream differs from requested mode')
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', target,
                '-f', 'null', '-'], timeout=300)
    report = {'status': 'rendered_for_review', 'goal': args.goal.strip(), 'change': args.change.strip(),
              'left': {'path': left['path'], 'start': left_start, 'sha256': identities[left['path']]},
              'right': {'path': right['path'], 'start': right_start, 'sha256': identities[right['path']]},
              'duration_seconds': duration, 'fps': args.fps, 'frames': expected,
              'audio': args.audio, 'comparison': str(target), 'comparison_sha256': file_hash.sha256(target),
              'input_identity_verified': True, 'full_decode': 'passed',
              'same_source_or_music': 'not_verified', 'human_watching': 'not_performed',
              'human_listening': 'not_performed', 'aesthetic_judgment': 'not_performed',
              'native_export': False,
              'limitations': ['Left and right are aligned by their requested starts; narrative and semantic equivalence are not inferred.',
                              'Only one explicitly selected audio track is included; no native timeline mixing is reproduced.',
                              'Scaling and time sampling are review proxies, not original-resolution native output.']}
    editor.write_json(output / 'comparison.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--left', required=True)
    parser.add_argument('--right', required=True)
    parser.add_argument('--left-start', type=float, default=0)
    parser.add_argument('--right-start', type=float, default=0)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--fps', type=int, choices=(15, 30), default=15)
    parser.add_argument('--audio', choices=('none', 'left', 'right'), required=True)
    parser.add_argument('--goal', required=True)
    parser.add_argument('--change', required=True)
    parser.add_argument('--out', required=True)
    try:
        editor.emit(compare(parser.parse_args(argv)))
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc)})
        return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
