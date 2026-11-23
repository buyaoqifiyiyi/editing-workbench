"""Local media inspection, reference frame extraction, and FFmpeg VFR normalization."""
import json
import os
from pathlib import Path
import re
import statistics
import sys

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
        item_warnings = []
        if info.get('has_video'):
            info['sample_frames'] = _extract_frames(media_file, out / f'item_{idx}', info.get('duration', 0), int(frames))
            if info.get('vfr'):
                item_warnings.append('variable_frame_rate')
            if core.is_filmcraft_hi10p(info):
                item_warnings.append('h264_10bit_hi10p_unsupported_by_filmcraft')
        if item_warnings:
            info['warnings'] = item_warnings
        info['needs_normalization'] = bool(item_warnings)
        probed.append(info)

    report = {'status': 'passed', 'input': str(target), 'count': len(probed), 'items': probed}
    core.write_json(out / 'inspect.json', report)
    return report


def _load_pyscenedetect():
    """Load optional PySceneDetect 0.7.2 lazily using existing cv2."""
    explicit = os.environ.get('OPENEDIT_PYSCENEDETECT_DIR')
    if explicit:
        explicit_dir = Path(os.path.expandvars(explicit)).expanduser()
        if not explicit_dir.is_dir():
            raise ValueError(f'OPENEDIT_PYSCENEDETECT_DIR does not exist: {explicit_dir}')
        candidates = [explicit_dir]
    else:
        local = Path(os.environ.get('LOCALAPPDATA') or Path.home())
        candidates = [
            local / 'OpenEditEngine' / 'optional' / 'pyscenedetect-0.7.2',
        ]
    for cand in candidates:
        if cand.is_dir():
            resolved = str(cand.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            break
    try:
        import cv2  # noqa: F401
        import scenedetect
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:
        raise ValueError(
            '--detector pyscenedetect requires optional PySceneDetect 0.7.2 and cv2; '
            f'default --detector ffmpeg remains unaffected ({exc})'
        ) from None
    return scenedetect, open_video, SceneManager, ContentDetector


def _extract_reference_pyscenedetect(target, info, frames_dir, *, start, span, content_threshold, min_scene_seconds, max_frames, modules):
    scenedetect, open_video, SceneManager, ContentDetector = modules
    window_end = start + span
    fps = float(info.get('fps') or 30.0)
    video = open_video(str(target))
    try:
        if getattr(video, 'frame_rate', None):
            fps = float(video.frame_rate)
        if start > 0:
            video.seek(start)
        min_scene_len = max(1, round(min_scene_seconds * fps))
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=content_threshold, min_scene_len=min_scene_len))
        scene_manager.detect_scenes(video, end_time=window_end)
        raw_scenes = scene_manager.get_scene_list(start_in_scene=True)
    finally:
        cap = getattr(video, 'capture', None)
        if cap is not None:
            cap.release()

    scenes = []
    for tc_start, tc_end in raw_scenes:
        s = max(start, min(window_end, float(tc_start.seconds)))
        e = max(s, min(window_end, float(tc_end.seconds)))
        if e - s >= 0.01:
            scenes.append({
                'index': len(scenes),
                'start': round(s, 3),
                'end': round(e, 3),
                'duration': round(e - s, 3),
            })
    if not scenes:
        scenes = [{'index': 0, 'start': round(start, 3), 'end': round(window_end, 3), 'duration': round(span, 3)}]

    cuts = [sc['start'] for sc in scenes[1:]]
    durations = [sc['duration'] for sc in scenes]
    median_dur = round(float(statistics.median(durations)), 3)
    cuts_per_min = round(len(cuts) * 60.0 / span, 2) if span > 0 else 0.0

    if len(scenes) <= max_frames:
        picked = scenes
    else:
        picked = [scenes[round(i * (len(scenes) - 1) / (max_frames - 1))] for i in range(max_frames)]

    extracted = []
    max_seek = max(start, window_end - 0.01)
    for idx, sc in enumerate(picked):
        ts = round(min(max_seek, sc['start'] + min(0.1, sc['duration'] * 0.25)), 3)
        png = frames_dir / f'scene-{idx:03d}.png'
        core.run([
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-ss', str(ts), '-i', str(target), '-frames:v', '1', str(png),
        ], timeout=60)
        if png.is_file():
            extracted.append(str(png))

    return {
        'detector': 'pyscenedetect',
        'detector_version': getattr(scenedetect, '__version__', '0.7.2'),
        'detector_params': {'content_threshold': content_threshold, 'min_scene_seconds': min_scene_seconds},
        'scenes': scenes,
        'cuts': cuts,
        'scene_count': len(scenes),
        'cut_count': len(cuts),
        'median_scene_seconds': median_dur,
        'cuts_per_minute': cuts_per_min,
        'frames': extracted,
        'boundaries': [
            'PySceneDetect ContentDetector provides candidate visual cuts inside the requested window only.',
            'Window edges may clip leading/trailing shots; does not inspect camera motion, effects, or audio.',
        ],
    }


def extract_reference(input_path, out_dir, *, start=0.0, duration=None, detector='ffmpeg', threshold=None, content_threshold=None, min_scene_seconds=None, max_frames=24):
    """Extract bounded scene-change and uniform frames from a local reference video."""
    if detector not in ('ffmpeg', 'pyscenedetect'):
        raise ValueError(f'Unsupported reference detector: {detector}')
    if detector == 'ffmpeg':
        if content_threshold is not None or min_scene_seconds is not None:
            raise ValueError('--content-threshold and --min-scene-seconds are only valid with --detector pyscenedetect')
        threshold = 0.35 if threshold is None else core.number(threshold, 'threshold', 0.01, 1.0)
        psd_modules = None
    else:
        if threshold is not None:
            raise ValueError('--threshold is only valid with --detector ffmpeg; use --content-threshold with pyscenedetect')
        content_threshold = 27.0 if content_threshold is None else core.number(content_threshold, 'content_threshold', 0.1, 255.0)
        min_scene_seconds = 0.3 if min_scene_seconds is None else core.number(min_scene_seconds, 'min_scene_seconds', 0.04, 60.0)
        psd_modules = _load_pyscenedetect()

    target = Path(input_path).resolve()
    info = core.probe(target)
    if info['kind'] != 'video':
        raise ValueError('Reference extraction requires a local video file')
    core.number(start, 'start', 0, max(0.0, info['duration']))
    limit = info['duration'] - start
    span = limit if duration is None else core.number(duration, 'duration', 0.1, limit)
    max_frames = int(core.number(max_frames, 'max_frames', 2, 96))

    out = core.new_directory(out_dir)
    frames_dir = out / 'frames'
    frames_dir.mkdir()
    report = {
        'status': 'passed',
        'input': str(target),
        'sha256': file_hash.sha256(target),
        'duration': info['duration'],
        'start': start,
        'window': span,
    }
    if detector == 'pyscenedetect':
        report.update(_extract_reference_pyscenedetect(
            target, info, frames_dir,
            start=start, span=span,
            content_threshold=content_threshold,
            min_scene_seconds=min_scene_seconds,
            max_frames=max_frames,
            modules=psd_modules,
        ))
    else:
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
        report.update({
            'detector': 'ffmpeg',
            'detector_params': {'threshold': threshold},
            'frames': extracted,
        })

    core.write_json(out / 'reference.json', report)
    return report


def _normalize_single_video(src, dst, info, *, fps, width, height, codec, crf):
    if info['kind'] != 'video':
        raise ValueError('normalize-media requires a local video input')
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


def normalize_media(input_path, out_path, *, fps=30, width=None, height=None, codec='h264', crf=16, force=False, max_files=200):
    """Normalize single file or batch-scan a folder of VFR / Hi10P videos to CFR H.264/AAC or ProRes."""
    src = Path(input_path).resolve()
    if src.is_file():
        info = core.probe(src)
        return _normalize_single_video(
            src, Path(out_path).resolve(), info,
            fps=fps, width=width, height=height, codec=codec, crf=crf,
        )
    if not src.is_dir():
        raise ValueError(f'Input path not found: {src}')

    max_files = int(core.number(max_files, 'max_files', 1, 1000))
    out_dir = core.new_directory(out_path)
    out_ext = '.mov' if codec == 'prores' else '.mp4'
    candidates = []
    truncated = False
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            cand = Path(root) / name
            if cand.suffix.lower() in core.VIDEO_EXT:
                candidates.append(cand)
                if len(candidates) >= max_files:
                    truncated = True
                    break
        if truncated:
            break

    items, path_map, migrate_args = [], {}, []
    used_paths = set()
    normalized_count = 0
    skipped_count = 0
    for media_file in candidates:
        info = core.probe(media_file)
        if info.get('kind') != 'video':
            continue
        reasons = []
        if info.get('vfr'):
            reasons.append('variable_frame_rate')
        if core.is_filmcraft_hi10p(info):
            reasons.append('h264_10bit_hi10p_unsupported_by_filmcraft')
        if force:
            reasons.append('forced')
        if width is not None or height is not None:
            reasons.append('resize_requested')

        rel = media_file.relative_to(src)
        if not reasons:
            skipped_count += 1
            items.append({
                'source': str(media_file),
                'relative': rel.as_posix(),
                'action': 'skipped_compliant',
                'reasons': [],
                'output': str(media_file),
                'probe': info,
            })
            continue

        dst = (out_dir / rel).with_suffix(out_ext)
        suffix_idx = 1
        while dst in used_paths or dst.exists():
            dst = dst.with_name(f'{dst.stem}_{suffix_idx}{out_ext}')
            suffix_idx += 1
        used_paths.add(dst)

        norm_res = _normalize_single_video(
            media_file, dst, info,
            fps=fps, width=width, height=height, codec=codec, crf=crf,
        )
        normalized_count += 1
        path_map[str(media_file)] = norm_res['output']
        migrate_args.append(f'{media_file}={norm_res["output"]}')
        items.append({
            'source': str(media_file),
            'relative': rel.as_posix(),
            'action': 'normalized',
            'reasons': reasons,
            'output': norm_res['output'],
            'sha256': norm_res['sha256'],
            'probe': norm_res['probe'],
        })

    report = {
        'status': 'passed',
        'mode': 'batch',
        'input': str(src),
        'out': str(out_dir),
        'truncated': truncated,
        'summary': {
            'scanned': len(items),
            'normalized': normalized_count,
            'skipped_compliant': skipped_count,
        },
        'path_map': path_map,
        'migrate_args': migrate_args,
        'items': items,
    }
    core.write_json(out_dir / 'prepare-report.json', report)
    return report
