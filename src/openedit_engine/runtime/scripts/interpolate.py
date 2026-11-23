"""Offline Vulkan RIFE / FFmpeg motion-interpolated slow-motion & frame-rate conversion."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import file_hash
import runtime_core as core
import runtime_paths

_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def _probe_video_fps_and_frames(path, info):
    ffprobe = core.tool('ffprobe')
    out = core.run([
        ffprobe, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=r_frame_rate,avg_frame_rate,nb_frames',
        '-of', 'default=noprint_wrappers=1', str(path),
    ], timeout=60).stdout
    kv = {}
    for line in out.splitlines():
        if '=' in line:
            k, _, v = line.partition('=')
            kv[k.strip()] = v.strip()

    def _frac(raw):
        if not raw or raw == '0/0':
            return None
        if '/' in raw:
            a, _, b = raw.partition('/')
            try:
                num, den = float(a), float(b)
                return num / den if den > 0 and num > 0 else None
            except ValueError:
                return None
        try:
            v = float(raw)
            return v if v > 0 else None
        except ValueError:
            return None

    fps = _frac(kv.get('avg_frame_rate')) or _frac(kv.get('r_frame_rate')) or 30.0
    try:
        nb = int(kv.get('nb_frames', '0'))
    except ValueError:
        nb = 0
    if nb <= 0:
        dur = float(info.get('duration') or 0.0)
        nb = max(1, int(round(dur * fps)))
    return round(fps, 4), nb


def _atempo_chain(factor):
    """Build valid chained FFmpeg atempo filters (each stage in [0.5, 2.0])."""
    if abs(factor - 1.0) < 1e-4:
        return None
    f = float(factor)
    stages = []
    while f < 0.5:
        stages.append('atempo=0.5')
        f /= 0.5
    while f > 2.0:
        stages.append('atempo=2.0')
        f /= 2.0
    stages.append(f'atempo={f:.6f}')
    return ','.join(stages)


def _resolve_rife_model(rife_exe, model_arg):
    base = rife_exe.parent
    available = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith('rife'))
    if not available:
        raise ValueError(f'No RIFE model folders found next to {rife_exe}')
    if model_arg == 'auto':
        chosen = 'rife-v4.6' if 'rife-v4.6' in available else available[-1]
        return base / chosen, chosen
    cand = base / model_arg
    if not cand.is_dir():
        raise ValueError(f'Requested RIFE model {model_arg!r} not found; available={available}')
    return cand, model_arg


def _run_rife(src, out_mp4, *, out_fps, target_frames, slow_factor, model_arg, deband, gpu_id, has_audio, t_start):
    rife_exe = runtime_paths.discover_rife_cli()
    if rife_exe is None:
        raise ValueError('rife-ncnn-vulkan executable not found (set OPENEDIT_RIFE_CLI or install under OpenEditTools)')
    model_dir, model_name = _resolve_rife_model(rife_exe, model_arg)
    ffmpeg = core.tool('ffmpeg')

    with tempfile.TemporaryDirectory(prefix='oe-rife-') as td:
        td_p = Path(td)
        in_dir = td_p / 'in_frames'
        out_dir = td_p / 'out_frames'
        in_dir.mkdir()
        out_dir.mkdir()

        core.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file,pipe', '-i', str(src),
            '-map', '0:v:0', '-fps_mode', 'passthrough', str(in_dir / '%08d.png'),
        ], timeout=600)
        in_frames = sorted(in_dir.glob('*.png'))
        if len(in_frames) < 2:
            raise ValueError(f'Video interpolation requires >= 2 source frames, got {len(in_frames)}')

        cmd_rife = [
            str(rife_exe),
            '-i', str(in_dir),
            '-o', str(out_dir),
            '-m', str(model_dir),
            '-n', str(int(target_frames)),
            '-g', str(int(gpu_id)),
            '-j', '2:2:2',
        ]
        proc = subprocess.run(
            cmd_rife, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=1800, creationflags=_NO_WIN,
        )
        if proc.returncode != 0:
            raise ValueError(f'rife-ncnn-vulkan failed (rc={proc.returncode}): {proc.stderr[-1500:]}')
        rendered = sorted(out_dir.glob('*.png'))
        if len(rendered) < 2:
            raise RuntimeError(f'rife-ncnn-vulkan produced too few frames ({len(rendered)})')

        cmd_enc = [
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-framerate', f'{out_fps:.6f}', '-i', str(out_dir / '%08d.png'),
        ]
        if has_audio:
            cmd_enc.extend(['-protocol_whitelist', 'file,pipe', '-i', str(src), '-map', '0:v:0', '-map', '1:a:0'])
        else:
            cmd_enc.extend(['-map', '0:v:0'])

        if deband:
            cmd_enc.extend(['-vf', 'deband'])
        cmd_enc.extend([
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '14',
            '-pix_fmt', 'yuv420p', '-r', f'{out_fps:.6f}', '-movflags', '+faststart',
        ])
        if has_audio:
            tempo = _atempo_chain(slow_factor)
            if tempo:
                cmd_enc.extend(['-af', tempo, '-c:a', 'aac', '-b:a', '192k'])
            else:
                cmd_enc.extend(['-c:a', 'aac', '-b:a', '192k'])
            cmd_enc.append('-shortest')
            audio_policy = 'atempo_slowed' if tempo else 'preserved'
        else:
            audio_policy = 'no_source_audio'
        cmd_enc.extend(['-map_metadata', '-1', '-map_chapters', '-1', str(out_mp4)])
        core.run(cmd_enc, timeout=600)

    return 'rife_ncnn_vulkan', model_name, audio_policy, round(time.monotonic() - t_start, 3)


def _run_minterpolate(src, out_mp4, *, out_fps, slow_factor, deband, has_audio, t_start):
    ffmpeg = core.tool('ffmpeg')
    pts_scale = 1.0 / slow_factor
    vf_parts = []
    if abs(slow_factor - 1.0) >= 1e-4:
        vf_parts.append(f'setpts={pts_scale:.6f}*PTS')
    vf_parts.append(f'minterpolate=fps={out_fps:.6f}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1')
    if deband:
        vf_parts.append('deband')

    cmd = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-protocol_whitelist', 'file,pipe', '-i', str(src),
        '-vf', ','.join(vf_parts),
        '-map', '0:v:0', '-c:v', 'libx264', '-preset', 'medium', '-crf', '14',
        '-pix_fmt', 'yuv420p', '-r', f'{out_fps:.6f}', '-movflags', '+faststart',
    ]
    if has_audio:
        tempo = _atempo_chain(slow_factor)
        cmd.extend(['-map', '0:a:0'])
        if tempo:
            cmd.extend(['-af', tempo])
        cmd.extend(['-c:a', 'aac', '-b:a', '192k', '-shortest'])
        audio_policy = 'atempo_slowed' if tempo else 'preserved'
    else:
        audio_policy = 'no_source_audio'
    cmd.extend(['-map_metadata', '-1', '-map_chapters', '-1', str(out_mp4)])
    core.run(cmd, timeout=1800)
    return 'ffmpeg_minterpolate', 'ffmpeg-mci', audio_policy, round(time.monotonic() - t_start, 3)


def run_interpolate(args):
    src = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not src.is_file():
        raise ValueError(f'Input video does not exist: {src}')
    info = core.probe(src)
    if not info.get('has_video'):
        raise ValueError(f'interpolate requires a local video stream: {src}')

    multiplier = core.number(args.multiplier, 'multiplier', 1.0, 8.0)
    slow_factor = core.number(args.slow_factor, 'slow_factor', 0.125, 1.0)
    target_fps = core.number(args.target_fps, 'target_fps', 1.0, 240.0) if args.target_fps is not None else None
    if multiplier <= 1.0 and target_fps is None and slow_factor >= 1.0:
        raise ValueError('Specify --multiplier > 1, --target-fps, or --slow-factor < 1')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    src_fps, src_frames = _probe_video_fps_and_frames(src, info)
    out_fps = round(float(target_fps if target_fps is not None else src_fps * multiplier), 4)
    target_frames = max(2, int(round(src_frames * (out_fps / src_fps) / slow_factor)))

    backend = args.backend
    rife_exe = runtime_paths.discover_rife_cli()
    if backend == 'auto':
        backend = 'rife' if rife_exe is not None else 'minterpolate'

    out_dir = core.new_directory(out_cand)
    out_mp4 = out_dir / 'interpolated.mp4'
    t0 = time.monotonic()
    has_audio = bool(info.get('has_audio'))

    if backend == 'rife':
        used_backend, used_model, audio_policy, elapsed = _run_rife(
            src, out_mp4,
            out_fps=out_fps, target_frames=target_frames, slow_factor=slow_factor,
            model_arg=args.model, deband=bool(args.deband), gpu_id=int(args.gpu_id),
            has_audio=has_audio, t_start=t0,
        )
    else:
        used_backend, used_model, audio_policy, elapsed = _run_minterpolate(
            src, out_mp4,
            out_fps=out_fps, slow_factor=slow_factor, deband=bool(args.deband),
            has_audio=has_audio, t_start=t0,
        )

    # Strict decode verification via FFmpeg
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-v', 'error', '-xerror', '-nostdin',
        '-i', str(out_mp4), '-f', 'null', '-',
    ], timeout=300)
    out_info = core.probe(out_mp4)
    actual_out_fps, actual_out_frames = _probe_video_fps_and_frames(out_mp4, out_info)

    report = {
        'status': 'passed',
        'operation': 'interpolate',
        'backend': used_backend,
        'model': used_model,
        'input': {
            'path': str(src),
            'sha256': file_hash.sha256(src),
            'source_fps': src_fps,
            'source_frames': src_frames,
            'duration': info.get('duration'),
        },
        'output_media': {
            'path': str(out_mp4),
            'bytes': out_mp4.stat().st_size,
            'sha256': file_hash.sha256(out_mp4),
            'output_fps': actual_out_fps,
            'output_frames': actual_out_frames,
            'duration': out_info.get('duration'),
        },
        'slow_factor': slow_factor,
        'deband': bool(args.deband),
        'audio_policy': audio_policy,
        'strict_decode': 'passed',
        'elapsed_seconds': elapsed,
    }
    core.write_json(out_dir / 'interpolate-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video file to interpolate')
    parser.add_argument('--out', required=True, help='New output directory for interpolated.mp4 and interpolate-report.json')
    parser.add_argument('--multiplier', type=float, default=2.0, help='Frame-rate multiplier in [1, 8] (default 2.0)')
    parser.add_argument('--target-fps', type=float, default=None, help='Explicit target FPS in [1, 240] (overrides multiplier)')
    parser.add_argument('--slow-factor', type=float, default=1.0, help='Playback speed factor in [0.125, 1.0] (e.g. 0.5 = 2x slow-mo)')
    parser.add_argument('--model', default='auto', help='RIFE model name (e.g. auto, rife-v4.6, rife-anime)')
    parser.add_argument('--backend', choices=('auto', 'rife', 'minterpolate'), default='auto')
    parser.add_argument('--deband', action='store_true', help='Apply FFmpeg deband filter to reduce 8-bit anime banding')
    parser.add_argument('--gpu-id', type=int, default=0, help='Vulkan GPU device index (-1=CPU, 0=RTX 3060)')
    args = parser.parse_args(argv)
    try:
        report = run_interpolate(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
