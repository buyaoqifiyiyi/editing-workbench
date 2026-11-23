"""Single entry for open-source regression checks in isolated temporary directories."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import editor
import runtime_paths

SCRIPTS = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[3]

# name -> (script, arg_kind)
CHECKS = {
    'open_engine': ('open_engine_self_check.py', 'out'),
    'practice_compare': ('practice_compare_self_check.py', 'out'),
    'mesh_avatar': ('mesh_avatar_self_check.py', 'out'),
    'mesh_avatar_project': ('mesh_avatar_project_self_check.py', 'out'),
    'music_edit': ('music_edit_self_check.py', 'out'),
    'karaoke': ('karaoke_self_check.py', 'out'),
    'karaoke_flow': ('karaoke_flow_self_check.py', 'out'),
    'beats': ('beats_self_check.py', 'out'),
    'beat_apply': ('beat_apply_self_check.py', 'out'),
    'reverse': ('reverse_self_check.py', 'out'),
    'stock': ('stock_self_check.py', 'out'),
    'migration': ('migration_self_check.py', 'out'),
    'layout_tracks': ('layout_tracks_self_check.py', 'out'),
    'ducking': ('ducking_self_check.py', 'out'),
    'separate': ('separate_self_check.py', 'model'),
    'transcribe': ('transcribe_self_check.py', 'whisper'),
    'captions': ('captions_self_check.py', 'out'),
    'tracking': ('tracking_self_check.py', 'out'),
    'tracking_apply': ('tracking_apply_self_check.py', 'out'),
    'offline_denoise': ('offline_denoise_self_check.py', 'out'),
    'auto_cut': ('auto_cut_self_check.py', 'vad'),
    'matting': ('matting_self_check.py', 'matting'),
    'auto_reframe': ('auto_reframe_self_check.py', 'face'),
    'rough_cut': ('rough_cut_self_check.py', 'rough'),
    'audio_master': ('audio_master_self_check.py', 'out'),
    'sfx': ('sfx_self_check.py', 'out'),
    'sfx_search': ('sfx_search_self_check.py', 'out'),
    'music_structure': ('music_structure_self_check.py', 'out'),
    'motion_flow': ('motion_flow_self_check.py', 'out'),
    'interpolate': ('interpolate_self_check.py', 'out'),
    'depth_map': ('depth_map_self_check.py', 'out'),
    'shot_search': ('shot_search_self_check.py', 'out'),
    'shader_fx': ('shader_fx_self_check.py', 'out'),
    'hyperframes': ('hyperframes_self_check.py', 'out'),
    'audit_project': ('audit_project_self_check.py', 'out'),
}


def arguments(kind, out, options):
    if kind == 'out':
        return ['--out', str(out)]
    if kind == 'model':
        return ['--out', str(out), '--model', str(options.demucs_model)]
    if kind == 'whisper':
        return ['--out', str(out), '--model', str(options.whisper_model)]
    if kind == 'vad':
        args = ['--out', str(out)]
        vad_model = getattr(options, 'vad_model', None)
        if vad_model:
            try:
                import onnxruntime  # noqa: F401
                args.extend(['--vad-model', str(vad_model)])
            except ImportError:
                pass
        return args
    if kind == 'matting':
        return ['--out', str(out), '--model', str(options.matting_model)]
    if kind == 'face':
        args = ['--out', str(out)]
        face_model = getattr(options, 'face_model', None)
        if face_model:
            args.extend(['--face-model', str(face_model)])
        return args
    if kind == 'rough':
        args = ['--out', str(out)]
        if getattr(options, 'whisper_model', None):
            args.extend(['--whisper-model', str(options.whisper_model)])
        if getattr(options, 'vad_model', None):
            args.extend(['--vad-model', str(options.vad_model)])
        return args
    raise ValueError(kind)


def skip_reason(name, spec, options):
    _, kind = spec
    if name == 'beats':
        try:
            import importlib.util
            if importlib.util.find_spec('librosa') is None or importlib.util.find_spec('numba') is None:
                return 'offline beat analysis needs optional openedit-engine[rhythm] dependencies; unverified here'
        except ImportError:
            return 'offline beat analysis dependencies are unavailable; unverified here'
    if name == 'tracking':
        try:
            import av  # noqa: F401
            import cv2
            if not callable(getattr(cv2, 'TrackerCSRT_create', None)):
                return 'OpenCV CSRT is unavailable; install requirements-tracking.txt in this interpreter'
        except ImportError:
            return 'offline tracking needs PyAV and OpenCV contrib in this interpreter'
    if kind == 'model' and not options.demucs_model:
        return 'needs --demucs-model (htdemucs ONNX weights; engine ships none)'
    if kind == 'model':
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return 'onnxruntime is not importable in this interpreter; offline separation stays unverified here'
    if kind == 'whisper' and not options.whisper_model:
        return 'needs --whisper-model (faster-whisper weights; engine ships none)'
    if kind == 'whisper':
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return 'faster-whisper is not importable in this interpreter; offline transcription stays unverified here'
    if kind == 'face':
        try:
            import cv2  # noqa: F401
        except ImportError:
            return 'opencv (cv2) is required for auto-reframe check'
    if name == 'sfx':
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
        except ImportError:
            return 'numpy and scipy are required for sfx check'
    if name in ('music_structure', 'sfx_search'):
        try:
            import numpy  # noqa: F401
        except ImportError:
            return f'numpy is required for {name} check'
    if name in ('motion_flow', 'depth_map', 'shot_search'):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            return f'opencv (cv2) and numpy are required for {name} check'
    if name == 'shader_fx':
        try:
            import cv2  # noqa: F401
            import moderngl  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            return 'moderngl, opencv (cv2), and numpy are required for shader_fx check'
    if name == 'hyperframes':
        if not shutil.which('node'):
            return 'Node.js (>=22) is required for hyperframes check'
        cli = runtime_paths.discover_hyperframes_cli()
        if cli is None:
            return 'HyperFrames CLI not found under OpenEditTools or OPENEDIT_HYPERFRAMES_CLI'
        if runtime_paths.discover_hyperframes_gsap(cli) is None:
            return 'Local gsap.min.js not found alongside HyperFrames CLI'
        if runtime_paths.discover_cjk_font_file() is None:
            return 'No local static CJK font found for offline HyperFrames rendering'
        try:
            import numpy  # noqa: F401
        except ImportError:
            return 'numpy is required for hyperframes check'
    if kind == 'matting':
        if not getattr(options, 'matting_model', None):
            return 'needs --matting-model (RVM / BiRefNet / RMBG ONNX weights; engine ships none)'
        try:
            import cv2  # noqa: F401
            import onnxruntime  # noqa: F401
        except ImportError:
            return 'onnxruntime and cv2 are required for AI matting check'
    try:
        editor.tool('ffmpeg')
        editor.tool('ffprobe')
    except ValueError as exc:
        return str(exc)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', choices=list(CHECKS))
    parser.add_argument('--keep', type=Path, help='New directory to retain check outputs; default uses a temporary directory')
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--demucs-model', type=Path, default=None, help='htdemucs ONNX weights directory for separation check')
    parser.add_argument('--whisper-model', type=Path, default=None, help='faster-whisper model directory for transcription check')
    parser.add_argument('--vad-model', type=Path, default=None, help='Optional Silero VAD ONNX weights for auto-cut check')
    parser.add_argument('--matting-model', type=Path, default=None, help='Matting ONNX weights for matting check')
    parser.add_argument('--face-model', type=Path, default=None, help='Optional YuNet ONNX weights for auto-reframe check')
    parser.add_argument('--no-auto-models', action='store_true', help='Disable local model auto-discovery in %LOCALAPPDATA%\\OpenEditTools\\models')
    options = parser.parse_args(argv)

    for dest in ('demucs_model', 'whisper_model', 'vad_model', 'matting_model', 'face_model'):
        val = getattr(options, dest)
        if val is not None:
            setattr(options, dest, val.resolve())
    if not options.no_auto_models:
        if options.demucs_model is None:
            options.demucs_model = runtime_paths.discover_model('demucs')
        if options.whisper_model is None:
            options.whisper_model = runtime_paths.discover_model('whisper')
        if options.vad_model is None:
            options.vad_model = runtime_paths.discover_model('vad')
        if options.matting_model is None:
            options.matting_model = runtime_paths.discover_model('matting')
        if options.face_model is None:
            options.face_model = runtime_paths.discover_model('face')

    holder = None
    if options.keep:
        root = options.keep.resolve()
        root.mkdir(parents=True, exist_ok=False)
    else:
        holder = tempfile.TemporaryDirectory(prefix='openedit-checks-', ignore_cleanup_errors=True)
        root = Path(holder.name)

    existing_pp = os.environ.get('PYTHONPATH', '')
    pythonpath = str(SRC_ROOT) + (os.pathsep + existing_pp if existing_pp else '')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', PYTHONPATH=pythonpath)

    results = []
    for name in options.only or CHECKS:
        spec = CHECKS[name]
        reason = skip_reason(name, spec, options)
        if reason:
            results.append({'check': name, 'status': 'skipped', 'reason': reason})
            continue
        started = time.monotonic()
        try:
            command = [sys.executable, str(SCRIPTS / spec[0]), *arguments(spec[1], root / name, options)]
            done = subprocess.run(
                command,
                cwd=SCRIPTS,
                env=environment,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=options.timeout,
            )
            status = 'passed' if done.returncode == 0 else 'failed'
            entry = {'check': name, 'status': status, 'seconds': round(time.monotonic() - started, 1)}
            if status == 'failed':
                entry['tail'] = (done.stdout + done.stderr)[-1500:]
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            entry = {'check': name, 'status': 'failed', 'tail': f'{type(exc).__name__}: {exc}'}
        results.append(entry)

    summary = {key: sum(r['status'] == key for r in results) for key in ('passed', 'failed', 'skipped')}
    editor.emit({'summary': summary, 'evidence': str(root) if options.keep else None, 'results': results})
    if holder:
        holder.cleanup()
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
