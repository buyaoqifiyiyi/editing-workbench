"""Optional 320x180 native still previews; no UI, export or algorithm job claims."""
import argparse
from fractions import Fraction
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
import native_beat
import native_primary_wheel


SEQUENCE_FPS = (15, 30)
MAX_SEQUENCE_SECONDS = 20
MAX_SEQUENCE_FRAMES = 600
SEQUENCE_CLOCK_TOLERANCE_US = 1
SEQUENCE_SUPERVISOR_CAP_SECONDS = 900
SEQUENCE_KEYS = frozenset({
    'schema_version', 'start_frame', 'duration_frames', 'draft_fps',
    'sample_fps', 'frame_step', 'frame_count',
})


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def draft_clock(content):
    """Return the raw microsecond duration, seconds and fps of a plaintext draft."""
    raw_duration, fps = content.get('duration', 0), content.get('fps', 0)
    if (isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float))
            or not math.isfinite(raw_duration) or raw_duration <= 0):
        raise ValueError('Unsupported draft duration')
    duration = raw_duration / 1e6
    if (isinstance(fps, bool) or not isinstance(fps, (int, float))
            or not math.isfinite(fps) or not 1 <= fps <= 120):
        raise ValueError('Unsupported draft fps')
    return raw_duration, duration, fps


def _frame_time_us(frame_index, fps):
    """Round a positive project-frame boundary to the nearest microsecond."""
    return (frame_index * 1_000_000 + fps // 2) // fps


def _frame_center_time_us(frame_index, fps):
    """Seek inside a project frame, avoiding ambiguous decode at its boundary."""
    return ((2 * frame_index + 1) * 1_000_000 + fps) // (2 * fps)


def sequence_timing(draft_fps, start, duration, fps):
    """Build an exact project-frame sequence and its native-preview times."""
    if type(draft_fps) is not int or not 1 <= draft_fps <= 120:
        raise ValueError('Sequence preview requires an integer draft fps from 1 to 120')
    if type(fps) is not int or fps not in SEQUENCE_FPS:
        raise ValueError('Sequence preview fps must be 15 or 30')
    if draft_fps % fps:
        raise ValueError('Sequence preview fps must divide the draft fps')
    for value, label in ((start, 'start'), (duration, 'duration')):
        if (isinstance(value, bool)
                or not isinstance(value, (int, float, Fraction))
                or not math.isfinite(value)):
            raise ValueError(label + ' must be a finite number')
    start_exact, duration_exact = Fraction(str(start)), Fraction(str(duration))
    if start_exact < 0:
        raise ValueError('start must be non-negative')
    if duration_exact <= 0:
        raise ValueError('duration must be positive')
    if duration_exact > MAX_SEQUENCE_SECONDS:
        raise ValueError(f'duration exceeds the {MAX_SEQUENCE_SECONDS}-second sequence limit')
    start_frames = start_exact * draft_fps
    duration_frames = duration_exact * draft_fps
    rounded_start = round(start_frames)
    rounded_duration = round(duration_frames)
    if abs(start_frames - rounded_start) > Fraction(1, 1_000_000):
        raise ValueError('start must align to a whole draft frame')
    if abs(duration_frames - rounded_duration) > Fraction(1, 1_000_000):
        raise ValueError('duration must align to whole draft frames')
    start_frame, duration_frames = rounded_start, rounded_duration
    frame_step = draft_fps // fps
    if duration_frames % frame_step:
        raise ValueError('duration must align to the sampled frame grid')
    frame_count = duration_frames // frame_step
    if not 1 <= frame_count <= MAX_SEQUENCE_FRAMES:
        raise ValueError(f'sequence preview requires 1-{MAX_SEQUENCE_FRAMES} sampled frames')
    times_us = [
        _frame_center_time_us(start_frame + index * frame_step, draft_fps)
        for index in range(frame_count)
    ]
    if _frame_time_us(start_frame + duration_frames, draft_fps) > 2**53 - 1:
        raise ValueError('Sequence clock exceeds the native safe integer range')
    spec = {
        'schema_version': 1,
        'start_frame': start_frame,
        'duration_frames': duration_frames,
        'draft_fps': draft_fps,
        'sample_fps': fps,
        'frame_step': frame_step,
        'frame_count': frame_count,
    }
    return spec, [value / 1_000_000 for value in times_us]


def validate_sequence(sequence, times, draft_duration_us, draft_fps):
    """Admit >8 preview times only when a complete spec reproduces every time."""
    if not isinstance(sequence, dict) or set(sequence) != SEQUENCE_KEYS:
        raise ValueError('Sequence preview requires the complete sequence specification')
    for key in SEQUENCE_KEYS - {'schema_version'}:
        if type(sequence[key]) is not int:
            raise ValueError('Sequence field ' + key + ' must be an integer')
    if sequence['schema_version'] != 1 or type(sequence['schema_version']) is not int:
        raise ValueError('Unsupported sequence specification version')
    if sequence['draft_fps'] != draft_fps:
        raise ValueError('Sequence draft fps does not match the draft')
    expected, expected_times = sequence_timing(
        sequence['draft_fps'],
        Fraction(sequence['start_frame'], sequence['draft_fps']),
        Fraction(sequence['duration_frames'], sequence['draft_fps']),
        sequence['sample_fps'],
    )
    if sequence != expected:
        raise ValueError('Sequence specification fields do not recompute consistently')
    if _frame_time_us(sequence['start_frame'] + sequence['duration_frames'], sequence['draft_fps']) > draft_duration_us:
        raise ValueError('Sequence range exceeds the draft duration')
    if not isinstance(times, list) or len(times) != sequence['frame_count']:
        raise ValueError('Sequence preview requires exactly one time per sampled frame')
    for index, (actual, expected_value) in enumerate(zip(times, expected_times)):
        if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                or not math.isfinite(actual) or actual != expected_value):
            raise ValueError(f'Sequence time {index} does not match the project frame grid')
    timeout = min(
        SEQUENCE_SUPERVISOR_CAP_SECONDS,
        20 + 2 * sequence['frame_count'],
    )
    return expected, expected_times, SEQUENCE_CLOCK_TOLERANCE_US, timeout


def preview_manifest(install):
    """Select the manifest whose recorded version matches the install directory, or one derived for a
    build that requalify admitted against a bundled manifest."""
    import runtime_admission
    candidates = {path.stem[len('native-preview-'):]: path for path in
                  (editor.SKILL_ROOT / 'assets').glob('native-preview-*.json')}
    manifest_path = candidates.get(install.name)
    if manifest_path is None:
        derived = runtime_admission.preview_manifest(install)
        if derived is not None:
            return derived
        raise ValueError('No native preview manifest for install directory ' + str(install)
                         + '; after a Jianying update run: jianying-engine requalify --candidate <new install>')
    manifest = editor.read_json(manifest_path)
    if manifest.get('version') != install.name:
        raise ValueError('Native preview manifest version does not match ' + str(install))
    return manifest


def preview(install, draft, output, times, runtime=None, sequence=None):
    install, draft, output = install.resolve(), draft.resolve(), output.resolve()
    editor.require_plaintext_draft(draft, 'to render a native preview from this draft')
    manifest = preview_manifest(install)
    if sequence is not None and manifest.get('admitted_from'):
        raise ValueError('Continuous native preview is not yet qualified on this Jianying version; still frames only')
    identities = {}
    for name, expected in manifest['files'].items():
        file = install / name
        if not file.is_file() or digest(file) != expected:
            raise ValueError('Unverified or missing native preview runtime: ' + str(file))
        identities[name] = expected
    if runtime is None:
        runtime = install / 'bun.exe'
        if not runtime.is_file():  # Jianying 11.6 ships its Bun runtime as vebun.exe
            runtime = install / 'vebun.exe'
    else:
        runtime = Path(runtime)
    runtime = runtime.resolve()
    if not runtime.is_file():
        raise ValueError('Native preview runtime executable is missing')
    if sequence is not None or manifest.get('admitted_from'):
        expected_runtime = manifest.get('runtime', {}).get('sha256')
        if not expected_runtime or digest(runtime) != expected_runtime:
            raise ValueError('Unverified native sequence runtime executable' if sequence is not None
                             else 'Unverified native preview runtime executable')
        identities[str(runtime)] = expected_runtime
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
    native_beat.preview_check(content, draft.parent, install)
    native_primary_wheel.preview_check(content, draft.parent, install)
    raw_duration, duration, fps = draft_clock(content)
    if sequence is None:
        if not 1 <= len(times) <= 8 or any(
                not math.isfinite(t) or not 0 <= t < duration for t in times):
            raise ValueError('Require 1-8 finite preview positions inside the draft duration')
        sequence_spec = None
        tolerance_us = math.ceil(1e6 / fps)
        timeout = 10 + 4 * len(times)
    else:
        sequence_spec, times, tolerance_us, timeout = validate_sequence(
            sequence, times, raw_duration, fps)
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
    if digest(copied) != before:
        raise ValueError('Draft changed while preparing native preview')
    config = {'install': str(install), 'draft': str(copied), 'times': times,
              'toleranceUs': tolerance_us}
    if sequence_spec is not None:
        config['sequence'] = sequence_spec
    editor.write_json(output / 'request.json', config)
    env = os.environ.copy()
    env['PATH'] = str(install / 'vm_sdk') + os.pathsep + str(install) + os.pathsep + env.get('PATH', '')
    started = time.monotonic()
    child = subprocess.Popen([str(runtime), str(Path(__file__).with_suffix('.cjs')), str(output / 'request.json')],
        cwd=install, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    killed = False
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()  # Only this owned worker; a killed run always fails admission.
        stdout, stderr = child.communicate(timeout=3)
        killed = True
    (output / 'native.log').write_bytes(stdout)
    (output / 'native.stderr.log').write_bytes(stderr)
    checkpoint_file = output / 'checkpoint.json'
    checkpoint = editor.read_json(checkpoint_file) if checkpoint_file.is_file() else {}
    unchanged = digest(draft) == before
    identities_after = {name: digest(install / name) for name in identities}
    runtime_unchanged = identities_after == identities
    frames = checkpoint.get('frames', [])
    success = (child.returncode == 0 and not killed and unchanged and runtime_unchanged and checkpoint.get('success') is True
               and len(frames) == len(times) and (checkpoint.get('steps') or [{}])[-1].get('phase') == 'calling_shipped_self_termination')
    stable_reads = {}
    if sequence_spec is not None and success:
        try:
            frame_steps = [step for step in checkpoint.get('steps', []) if step.get('phase') == 'frame']
            if len(frame_steps) != len(frames):
                raise ValueError('Sequence checkpoint does not contain every stable frame')
            for index, (frame, step) in enumerate(zip(frames, frame_steps)):
                target_us = _frame_center_time_us(
                    sequence_spec['start_frame'] + index * sequence_spec['frame_step'],
                    sequence_spec['draft_fps'],
                )
                if (frame.get('file') != f'frame-{index}.vef'
                        or frame.get('targetUs') != target_us
                        or step.get('index') != index
                        or step.get('timestampUs') != frame.get('timestampUs')
                        or type(step.get('stableReads')) is not int
                        or step['stableReads'] < 3):
                    raise ValueError('Sequence checkpoint frame identity or stability differs')
                stable_reads[index] = step['stableReads']
        except (TypeError, ValueError) as error:
            success = False
            checkpoint['decode_error'] = str(error)
    decoded = []
    if success:
        try:
            for index, frame in enumerate(frames):
                packet = (output / f'frame-{index}.vef').read_bytes()
                magic, version, header, width, height, stride, fmt, timestamp, _ = struct.unpack_from('<4sHHIIIIQQ', packet)
                if ((magic, version, header, width, height, stride, fmt) != (b'VEF1', 1, 40, 320, 180, 1280, 1)
                    or len(packet) != 230440 or abs(timestamp - round(times[index] * 1e6)) > config['toleranceUs']):
                    raise ValueError('Invalid or stale output packet')
                if sequence_spec is not None:
                    target_us = _frame_center_time_us(
                        sequence_spec['start_frame'] + index * sequence_spec['frame_step'],
                        sequence_spec['draft_fps'],
                    )
                    if frame.get('timestampUs') != timestamp:
                        raise ValueError('Sequence packet and checkpoint timestamps differ')
                    previous_target = (
                        _frame_center_time_us(
                            sequence_spec['start_frame'] + (index - 1) * sequence_spec['frame_step'],
                            sequence_spec['draft_fps'],
                        ) if index else None
                    )
                png = output / f'frame-{index}.png'
                Image.frombytes('RGBA', (width, height), packet[header:], 'raw', 'RGBA', stride).save(png)
                evidence = {'path': str(png), 'sha256': digest(png), 'timestamp_us': timestamp}
                if sequence_spec is not None:
                    evidence.update({
                        'index': index,
                        'project_frame_index': sequence_spec['start_frame'] + index * sequence_spec['frame_step'],
                        'target_us': target_us,
                        'quantization_error_us': timestamp - target_us,
                        'adjacent_gap_us': target_us - previous_target if previous_target is not None else None,
                        'stable_reads': stable_reads[index],
                    })
                decoded.append(evidence)
        except (OSError, ValueError, struct.error) as error:
            success = False
            checkpoint['decode_error'] = str(error)
    report = {'status': 'passed' if success else 'failed', 'runtime_version': manifest['version'],
              'worker_pid': child.pid, 'exit_code': child.returncode, 'supervisor_kill': killed,
              'elapsed_seconds': round(time.monotonic() - started, 3), 'source_draft_unchanged': unchanged,
              'runtime_identity': identities, 'runtime_identity_after': identities_after,
              'runtime_unchanged': runtime_unchanged, 'frames': decoded, 'checkpoint': checkpoint,
              'termination': 'shipped_self_termination' if success else 'failed',
              'offline_algorithm_jobs_verified': False, 'audio_review': 'unverified',
              'visual_review': 'unverified', 'final_check_owner': 'user'}
    if sequence_spec is not None:
        report['sequence'] = {
            'spec': sequence_spec,
            'clock_tolerance_us': tolerance_us,
            'supervisor_timeout_seconds': timeout,
        }
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
