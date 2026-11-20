"""Local media inspection, reference frame extraction, and FFmpeg VFR normalization."""
import json
from pathlib import Path
import re

import file_hash
import runtime_core as core


def _extract_frames(media_path, out_dir, duration, count):
    frames = []
    if count <= 0:
        return frames
    out_dir.mkdir(parents=True, exist_ok=True)
    times = [0.0] if duration <= 0 else [round(duration * (i + 0.5) / count, 3) for i in range(count)]
    for idx, ts in enumerate(times):
        png = out_dir / f'frame-{idx}.png'
        args = [core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n']
        if duration > 0:
            args.extend(['-ss', str(ts)])
        args.extend(['-i', str(media_path), '-frames:v', '1', str(png)])
        core.run(args, timeout=60)
        if png.is_file():
            frames.append({'time': ts, 'path': str(png)})
    return frames


def inspect_media(input_path, out_dir, frames=3, max_files=200):
    """Probe a single local file or media folder and optionally extract sample frames."""
    core.number(frames, 'frames', 1, 12)
    core.number(max_files, 'max_files', 1, 1000)
    target = Path(input_path).resolve()
    out = core.new_directory(out_dir)

    candidates = []
    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        for root, _, files in core.os.walk(target):
            for name in sorted(files):
                p = Path(root) / name
                if p.suffix.lower() in core.MEDIA_EXT:
                    candidates.append(p)
                    if len(candidates) >= max_files:
                        break
            if len(candidates) >= max_files:
                break
    else:
        raise ValueError(f'Input path not found: {target}')

    probed = []
    for idx, media_file in enumerate(candidates):
        info = core.probe(media_file)
        if info.get('has_video'):
            info['sample_frames'] = _extract_frames(media_file, out / f'item_{idx}', info.get('duration', 0), int(frames))
        probed.append(info)

    report = {'status': 'passed', 'input': str(target), 'count': len(probed), 'items': probed}
    core.write_json(out / 'inspect.json', report)
    return report


def extract_reference(input_path, out_dir, *, start=0.0, duration=None, threshold=0.35, max_frames=24):
    """Extract bounded scene-change and uniform frames from a local reference video."""
    target = Path(input_path).resolve()
    info = core.probe(target)
    if info['kind'] != 'video':
        raise ValueError('Reference extraction requires a local video file')
    core.number(start, 'start', 0, max(0.0, info['duration']))
    limit = info['duration'] - start
    span = limit if duration is None else core.number(duration, 'duration', 0.1, limit)
    core.number(threshold, 'threshold', 0.01, 1.0)
    max_frames = int(core.number(max_frames, 'max_frames', 2, 96))

    out = core.new_directory(out_dir)
    frames_dir = out / 'frames'
    frames_dir.mkdir()
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin', '-n',
        '-ss', str(start), '-t', str(span), '-i', str(target),
        '-vf', f"select='gt(scene,{threshold})+isnan(prev_selected_t)',showinfo",
        '-fps_mode', 'vfr', '-frames:v', str(max_frames), str(frames_dir / 'cut-%03d.png'),
    ], timeout=180)
    extracted = sorted(str(p) for p in frames_dir.glob('*.png'))
    if not extracted:
        step = max(span / max_frames, 0.25)
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-ss', str(start), '-t', str(span), '-i', str(target),
            '-vf', f'fps=1/{step:.4f}', '-frames:v', str(max_frames), str(frames_dir / 'sample-%03d.png'),
        ], timeout=180)
        extracted = sorted(str(p) for p in frames_dir.glob('*.png'))

    report = {
        'status': 'passed',
        'input': str(target),
        'sha256': file_hash.sha256(target),
        'duration': info['duration'],
        'start': start,
        'window': span,
        'frames': extracted,
    }
    core.write_json(out / 'reference.json', report)
    return report


def normalize_media(input_path, out_path, *, fps=30, width=None, height=None, codec='h264', crf=16):
    """Normalize VFR or exotic-codec media to CFR H.264/AAC or ProRes for reliable FilmCraft ingest."""
    src = Path(input_path).resolve()
    info = core.probe(src)
    if info['kind'] != 'video':
        raise ValueError('normalize-media requires a local video input')
    dst = Path(out_path).resolve()
    if dst.exists() or dst.is_symlink():
        raise ValueError(f'Output file already exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    fps = core.number(fps, 'fps', 1, 120)
    crf = int(core.number(crf, 'crf', 0, 51))
    vf_parts = [f'fps={fps}']
    if width is not None or height is not None:
        w = int(core.number(width or -2, 'width', -2, 7680))
        h = int(core.number(height or -2, 'height', -2, 4320))
        vf_parts.append(f'scale={w}:{h}:flags=lanczos')

    cmd = [
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-i', str(src), '-vf', ','.join(vf_parts),
    ]
    if codec == 'prores':
        cmd.extend(['-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le'])
    elif codec == 'h264':
        cmd.extend(['-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf), '-pix_fmt', 'yuv420p'])
    else:
        raise ValueError(f'Unsupported normalization codec: {codec}')

    if info.get('has_audio'):
        cmd.extend(['-c:a', 'pcm_s16le' if codec == 'prores' else 'aac', '-ar', '48000', '-ac', '2'])
        if codec == 'h264':
            cmd.extend(['-b:a', '192k'])
    else:
        cmd.append('-an')
    cmd.extend(['-movflags', '+faststart', str(dst)])
    core.run(cmd, timeout=600)

    # Verify strict decode on normalized output.
    core.run([core.tool('ffmpeg'), '-v', 'error', '-xerror', '-i', str(dst), '-f', 'null', '-'], timeout=180)
    out_info = core.probe(dst)
    return {
        'status': 'passed',
        'source': str(src),
        'output': str(dst),
        'sha256': file_hash.sha256(dst),
        'probe': out_info,
    }
