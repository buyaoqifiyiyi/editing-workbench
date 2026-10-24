"""Optional 320x180 native still previews; no UI, export or algorithm job claims."""
import argparse
import hashlib
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from PIL import Image
import editor
import local_font
import native_mask_feather
import native_chroma
import native_mix_mode
import native_text_highlight
import native_text_alpha
import native_advanced
import native_hsl_channels


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def preview_manifest(install):
    """Select the manifest whose recorded version matches the install directory."""
    candidates = {path.stem[len('native-preview-'):]: path for path in
                  (editor.SKILL_ROOT / 'assets').glob('native-preview-*.json')}
    manifest_path = candidates.get(install.name)
    if manifest_path is None:
        raise ValueError('No native preview manifest for install directory ' + str(install)
                         + '; re-verify the shipped modules and add one')
    manifest = editor.read_json(manifest_path)
    if manifest.get('version') != install.name:
        raise ValueError('Native preview manifest version does not match ' + str(install))
    return manifest


def preview(install, draft, output, times, runtime=None):
    install, draft, output = install.resolve(), draft.resolve(), output.resolve()
    manifest = preview_manifest(install)
    identities = {}
    for name, expected in manifest['files'].items():
        file = install / name
        if not file.is_file() or digest(file) != expected:
            raise ValueError('Unverified or missing native preview runtime: ' + str(file))
        identities[name] = expected
    runtime = (runtime or install / 'bun.exe')
    if not runtime.is_file():  # Jianying 11.6 ships its Bun runtime as vebun.exe
        runtime = install / 'vebun.exe'
    runtime = runtime.resolve()
    if not runtime.is_file():
        raise ValueError('Native preview runtime executable is missing')
    content = editor.read_json(draft)  # This interface accepts plaintext generated drafts only.
    if not isinstance(content, dict):
        raise ValueError('Native preview requires a plaintext draft object')
    local_font.preview_check(content, draft.parent)
    native_mask_feather.preview_check(content, draft.parent)
    native_chroma.preview_check(content)
    native_mix_mode.preview_check(content, draft.parent, install)
    native_text_highlight.preview_check(content, draft.parent, install)
    native_text_alpha.preview_check(content, draft.parent, install)
    native_advanced.corner_preview_check(content, draft.parent, install, runtime)
    native_advanced.green_lightness_preview_check(content, draft.parent, install, runtime)
    native_hsl_channels.preview_check(content, draft.parent, install, runtime)
    raw_duration, fps = content.get('duration', 0), content.get('fps', 0)
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or not math.isfinite(raw_duration) or raw_duration <= 0:
        raise ValueError('Unsupported draft duration')
    duration = raw_duration / 1e6
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or not 1 <= fps <= 120:
        raise ValueError('Unsupported draft fps')
    if not 1 <= len(times) <= 8 or any(not math.isfinite(t) or not 0 <= t < duration for t in times):
        raise ValueError('Require 1-8 finite preview positions inside the draft duration')
    for kind in ('videos', 'audios'):
        for material in content.get('materials', {}).get(kind, []):
            source = material.get('path')
            if source and not Path(source).is_file():
                raise ValueError('Native preview source unavailable: ' + source)
    before = digest(draft)
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    copied = workspace / 'draft_content.json'
    shutil.copyfile(draft, copied)
    config = {'install': str(install), 'draft': str(copied), 'times': times,
              'toleranceUs': math.ceil(1e6 / fps)}
    editor.write_json(output / 'request.json', config)
    env = os.environ.copy()
    env['PATH'] = str(install / 'vm_sdk') + os.pathsep + str(install) + os.pathsep + env.get('PATH', '')
    started = time.monotonic()
    child = subprocess.Popen([str(runtime), str(Path(__file__).with_suffix('.cjs')), str(output / 'request.json')],
        cwd=install, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    killed = False
    try:
        stdout, stderr = child.communicate(timeout=10 + 4 * len(times))
    except subprocess.TimeoutExpired:
        child.kill()  # Only this owned worker; a killed run always fails admission.
        stdout, stderr = child.communicate(timeout=3)
        killed = True
    (output / 'native.log').write_bytes(stdout)
    (output / 'native.stderr.log').write_bytes(stderr)
    checkpoint_file = output / 'checkpoint.json'
    checkpoint = editor.read_json(checkpoint_file) if checkpoint_file.is_file() else {}
    unchanged = digest(draft) == before
    frames = checkpoint.get('frames', [])
    success = (child.returncode == 0 and not killed and unchanged and checkpoint.get('success') is True
               and len(frames) == len(times) and (checkpoint.get('steps') or [{}])[-1].get('phase') == 'calling_shipped_self_termination')
    decoded = []
    if success:
        try:
            for index, frame in enumerate(frames):
                packet = (output / f'frame-{index}.vef').read_bytes()
                magic, version, header, width, height, stride, fmt, timestamp, _ = struct.unpack_from('<4sHHIIIIQQ', packet)
                if ((magic, version, header, width, height, stride, fmt) != (b'VEF1', 1, 40, 320, 180, 1280, 1)
                    or len(packet) != 230440 or abs(timestamp - round(times[index] * 1e6)) > config['toleranceUs']):
                    raise ValueError('Invalid or stale output packet')
                png = output / f'frame-{index}.png'
                Image.frombytes('RGBA', (width, height), packet[header:], 'raw', 'RGBA', stride).save(png)
                decoded.append({'path': str(png), 'sha256': digest(png), 'timestamp_us': timestamp})
        except (OSError, ValueError, struct.error) as error:
            success = False
            checkpoint['decode_error'] = str(error)
    report = {'status': 'passed' if success else 'failed', 'runtime_version': manifest['version'],
              'worker_pid': child.pid, 'exit_code': child.returncode, 'supervisor_kill': killed,
              'elapsed_seconds': round(time.monotonic() - started, 3), 'source_draft_unchanged': unchanged,
              'runtime_identity': identities, 'frames': decoded, 'checkpoint': checkpoint,
              'termination': 'shipped_self_termination' if success else 'failed',
              'offline_algorithm_jobs_verified': False, 'audio_review': 'unverified', 'final_check_owner': 'user'}
    editor.write_json(output / 'preview.json', report)
    if not success:
        raise ValueError('Native preview failed; inspect ' + str(output / 'preview.json'))
    return report


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--draft', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--times', type=float, nargs='+', required=True)
    parser.add_argument('--runtime', type=Path)
    args = parser.parse_args()
    try:
        editor.emit(preview(args.install, args.draft, args.out, args.times, args.runtime))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        editor.emit({'error': str(error)})
        raise SystemExit(1)
