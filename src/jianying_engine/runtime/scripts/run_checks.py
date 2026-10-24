"""Single entry for the regression checks: builds synthetic media, runs what this machine supports, skips the rest with a reason.

Usage: python run_checks.py [--only NAME ...] [--keep DIR] [--install DIR] [--denoise-plan P] [--loudness-plan P] [--motion-easing-plan P] [--matting-plan P] [--demucs-model P] [--whisper-model P]
Never opens Jianying or publishes to the live draft root; every check writes to a fresh directory.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import editor

SCRIPTS = Path(__file__).resolve().parent
# name -> (script, media/args builder key, needs native Jianying user data, needs Windows)
CHECKS = {
    'music_edit': ('music_edit_self_check.py', 'out', False, False),
    'resolve_workflow': ('resolve_workflow_self_check.py', 'out', False, False),
    'adobe_workflow': ('adobe_workflow_self_check.py', 'out', False, False),
    'shot_exchange': ('shot_exchange_self_check.py', 'out', False, False),
    'karaoke': ('karaoke_self_check.py', 'out', False, False),
    'karaoke_flow': ('karaoke_flow_self_check.py', 'out', False, True),
    'hsl_channels': ('hsl_channels_self_check.py', 'out', False, True),
    'green_lightness': ('green_lightness_self_check.py', 'out', False, True),
    'beats': ('beats_self_check.py', 'out', False, False),
    'beat_apply': ('beat_apply_self_check.py', 'out', False, False),
    'reverse': ('reverse_self_check.py', 'out', False, False),
    'self_check': ('self_check.py', 'work', False, False),
    'timing': ('timing_self_check.py', 'work', False, False),
    'stock': ('stock_self_check.py', 'out', False, False),
    'template_borrow': ('test_template_borrow.py', None, False, False),
    'text_curve': ('text_curve_self_check.py', 'video40', False, False),
    'migration': ('migration_self_check.py', 'out', False, True),
    'speed_curve': ('speed_curve_self_check.py', 'video5', True, False),
    'native': ('native_self_check.py', 'out', True, False),
    'font': ('font_self_check.py', 'outpath', True, False),
    'local_font': ('local_font_self_check.py', 'out', False, False),
    'text_glow': ('text_glow_self_check.py', 'outpath', True, False),
    'grading': ('grading_self_check.py', 'grading', True, False),
    'circle_mask': ('circle_mask_self_check.py', 'circle', True, False),
    'extensions': ('extensions_self_check.py', 'extensions', True, False),
    'native_preview': ('native_preview_self_check.py', 'install', False, True),
    'draft_state': ('draft_state_self_check.py', 'out', False, False),
    'refresh_registration': ('refresh_registration_self_check.py', 'out', False, False),
    'animation_path': ('animation_path_self_check.py', 'out', False, False),
    'ducking': ('ducking_self_check.py', 'out', False, False),
    'separate': ('separate_self_check.py', 'model', False, False),
    'transcribe': ('transcribe_self_check.py', 'whisper', False, False),
    'captions': ('captions_self_check.py', 'out', False, False),
    'tracking': ('tracking_self_check.py', 'out', False, False),
    'tracking_apply': ('tracking_apply_self_check.py', 'out', False, False),
    'denoise': ('denoise_self_check.py', 'plan:denoise_plan', True, False),
    'offline_denoise': ('offline_denoise_self_check.py', 'out', False, False),
    'mask_feather': ('mask_feather_self_check.py', 'out', True, True),
    'chroma': ('chroma_self_check.py', 'out', False, True),
    'mix_mode': ('mix_mode_self_check.py', 'out', False, True),
    'text_highlight': ('text_highlight_self_check.py', 'out', False, True),
    'text_alpha': ('text_alpha_self_check.py', 'out', False, True),
    'text_color_fade': ('text_color_fade_self_check.py', 'out', False, True),
    'corner_pin': ('corner_pin_self_check.py', 'out', False, True),
    'loudness': ('loudness_self_check.py', 'plan:loudness_plan', True, False),
    'motion_easing': ('motion_easing_self_check.py', 'plan:motion_easing_plan', True, False),
    'matting_cache': ('matting_cache_self_check.py', 'plan:matting_plan', True, False),
    'coverage': ('coverage_self_check.py', 'out', False, False),
}


class Media:
    """Synthetic inputs, generated on first use only."""
    def __init__(self, root):
        self.root, self.cache = root, {}

    def get(self, name):
        if name not in self.cache:
            path = self.root / name
            source, extra = {
                'video5.mp4': ('testsrc=s=640x360:r=60:d=5', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
                'video40.mp4': ('testsrc=s=640x360:r=30:d=40', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
                'video6.mp4': ('testsrc=s=640x360:r=24:d=6', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
                'uhd30.mp4': ('color=c=gray:s=3840x2160:r=30:d=30', ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p']),
                'image.png': ('testsrc=s=640x360', ['-frames:v', '1']),
                'foreground.png': ('color=c=orange:s=640x360', ['-frames:v', '1']),
                'background.png': ('color=c=navy:s=640x360', ['-frames:v', '1']),
                'music.wav': ('sine=frequency=330:duration=10', ['-c:a', 'pcm_s16le', '-ac', '2', '-ar', '44100']),
            }[name]
            editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                        '-f', 'lavfi', '-i', source, *extra, path], timeout=600)
            self.cache[name] = path
        return str(self.cache[name])


def recorded_install():
    """Discover an installed build with a matching portable preview manifest."""
    manifests = sorted((editor.SKILL_ROOT / 'assets').glob('native-preview-*.json'),
                       key=lambda path: [int(part) for part in path.stem.rsplit('-', 1)[-1].split('.')],
                       reverse=True)
    installed = editor.jianying_install()
    if installed and any(p.stem == 'native-preview-' + installed.name for p in manifests):
        return installed
    for manifest in manifests:
        recorded = editor.read_json(manifest).get('install')
        if not recorded:
            continue
        install = Path(recorded)
        if install.is_absolute() and install.is_dir() and install.name == manifest.stem[len('native-preview-'):]:
            return install
    return None


def arguments(kind, out, media, options):
    if kind is None:
        return []
    if kind == 'install':
        return ['--install', str(options.install)]
    if kind == 'work':
        return ['--work', str(out)]
    if kind == 'out':
        return ['--out', str(out)]
    if kind == 'outpath':
        return ['--out', str(out)]
    if kind == 'video40':
        return ['--out', str(out), '--video', media.get('video40.mp4')]
    if kind == 'video5':
        return ['--out', str(out), '--video', media.get('video5.mp4')]
    if kind == 'grading':
        return ['--out', str(out), '--image', media.get('image.png'), '--video', media.get('video6.mp4')]
    if kind == 'circle':
        return ['--out', str(out), '--foreground', media.get('foreground.png'),
                '--background', media.get('background.png'), '--video', media.get('video6.mp4')]
    if kind == 'extensions':
        return ['--out', str(out), '--video', media.get('uhd30.mp4'), '--music', media.get('music.wav')]
    if kind.startswith('plan:'):
        return ['--out', str(out), '--plan', str(getattr(options, kind[5:]))]
    if kind == 'model':
        return ['--out', str(out), '--model', str(options.demucs_model)]
    if kind == 'whisper':
        return ['--out', str(out), '--model', str(options.whisper_model)]
    raise ValueError(kind)


def skip_reason(name, spec, options, native_ready):
    _, kind, needs_native, needs_windows = spec
    if name == 'beats':
        try:
            import importlib.util
            if importlib.util.find_spec('librosa') is None or importlib.util.find_spec('numba') is None:
                return 'offline beat analysis needs optional jianying-engine[rhythm] dependencies; unverified here'
        except ImportError:
            return 'offline beat analysis dependencies are unavailable; unverified here'
    if name == 'tracking':
        try:
            import av
            import cv2
            if not callable(getattr(cv2, 'TrackerCSRT_create', None)):
                return 'OpenCV CSRT is unavailable; install requirements-tracking.txt in this interpreter'
        except ImportError:
            return 'offline tracking needs PyAV and OpenCV contrib in this interpreter'
    if needs_windows and os.name != 'nt':
        return 'Windows paths only'
    if needs_native and not native_ready:
        return 'no configured Jianying user data (JIANYING_USER_DATA / LOCALAPPDATA)'
    if kind == 'install' and not options.install:
        return 'no recorded Jianying install directory for the native preview manifests'
    if kind and kind.startswith('plan:') and not getattr(options, kind[5:]):
        return f'needs --{kind[5:].replace("_", "-")} (external measured plan)'
    if kind == 'model' and not options.demucs_model:
        return 'needs --demucs-model (htdemucs ONNX weights; this Skill ships none)'
    if kind == 'model':
        try:
            import onnxruntime  # noqa: F401  - the separation check runs in this same interpreter
        except ImportError:
            return 'onnxruntime is not importable in this interpreter; offline separation stays unverified here'
    if kind == 'whisper' and not options.whisper_model:
        return 'needs --whisper-model (faster-whisper weights; this Skill ships none)'
    if kind == 'whisper':
        try:
            import faster_whisper  # noqa: F401  - the transcription check runs in this same interpreter
        except ImportError:
            return ('faster-whisper is not importable in this interpreter; offline transcription stays '
                    'unverified here')
    try:
        editor.tool('ffmpeg'); editor.tool('ffprobe')
    except ValueError as exc:
        return str(exc)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', nargs='+', choices=list(CHECKS))
    parser.add_argument('--keep', type=Path, help='New directory to keep evidence; default is a temporary directory')
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--install', type=Path, default=None,
                        help='Jianying install directory for the native preview check; default is a discovered '
                             'installation matching a bundled preview manifest')
    for name in ('denoise', 'loudness', 'motion_easing', 'matting'):
        parser.add_argument('--' + name.replace('_', '-') + '-plan', type=Path, dest=name + '_plan')
    parser.add_argument('--demucs-model', type=Path, default=None,
                        help='htdemucs ONNX weights for the offline separation check; without it that check '
                             'skips with a reason, and the Python running this file also needs onnxruntime')
    parser.add_argument('--whisper-model', type=Path, default=None,
                        help='faster-whisper model directory for the offline transcription check (the HF cache '
                             'directory or its snapshot); without it that check skips with a reason, and the '
                             'Python running this file also needs faster-whisper')
    options = parser.parse_args()
    # The checks run as child processes with cwd=SCRIPTS, so a relative plan/model path would be resolved
    # against the scripts directory and read as missing. Anchor every path input to the caller's directory.
    for dest in ('denoise_plan', 'loudness_plan', 'motion_easing_plan', 'matting_plan',
                 'demucs_model', 'whisper_model', 'install'):
        value = getattr(options, dest)
        if value is not None:
            setattr(options, dest, value.resolve())
    options.install = options.install or recorded_install()
    try:
        editor.native_user_data(); native_ready = True
    except (ValueError, OSError):
        native_ready = False
    holder = None
    if options.keep:
        root = options.keep.resolve()
        # The native/font/glow checks copy cache trees whose keys are ~65-120 chars; a long evidence
        # directory overflows MAX_PATH (WinError 206) in checks that are otherwise fine.
        if os.name == 'nt' and len(str(root)) > 120:
            parser.error(f'--keep path is {len(str(root))} characters; keep hard evidence paths at or under 120 '
                         f'(e.g. the workspace 运行数据 directory, or {root.drive}\\jiying-evidence) '
                         f'or run without --keep for a temporary directory')
        root.mkdir(parents=True, exist_ok=False)
    else:
        holder = tempfile.TemporaryDirectory(prefix='jianying-checks-', ignore_cleanup_errors=True); root = Path(holder.name)
    media = Media(root / 'media'); media.root.mkdir()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    results = []
    for name in options.only or CHECKS:
        spec = CHECKS[name]
        reason = skip_reason(name, spec, options, native_ready)
        if reason:
            results.append({'check': name, 'status': 'skipped', 'reason': reason}); continue
        started = time.monotonic()
        try:
            command = [sys.executable, str(SCRIPTS / spec[0]), *arguments(spec[1], root / name, media, options)]
            done = subprocess.run(command, cwd=SCRIPTS, env=environment, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=options.timeout)
            status = 'passed' if done.returncode == 0 else 'failed'
            entry = {'check': name, 'status': status, 'seconds': round(time.monotonic() - started, 1)}
            if status == 'failed':
                entry['tail'] = (done.stdout + done.stderr)[-1500:]
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            entry = {'check': name, 'status': 'failed', 'tail': f'{type(exc).__name__}: {exc}'}
        results.append(entry)
    summary = {key: sum(r['status'] == key for r in results) for key in ('passed', 'failed', 'skipped')}
    editor.emit({'summary': summary, 'native_user_data': native_ready, 'evidence': str(root) if options.keep else None,
                 'results': results})
    if holder:
        holder.cleanup()
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
