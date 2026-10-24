"""Bounded native Resolve handoff; never launches or terminates the user's host."""
import argparse
from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import editor
import resolve_compile
import resolve_native


NATIVE = Path(__file__).with_name('resolve_native.py')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    path = Path(path)
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    return path


def write_new(path, data):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(data)


def encoded(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def record(root, name, data):
    target = root / name
    if target.is_symlink() or target.is_junction():
        raise ValueError('Refusing linked receipt')
    temporary = root / ('.' + uuid.uuid4().hex + '.json')
    write_new(temporary, encoded(data))
    temporary.replace(target)


def config_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JianyingEngine' / 'resolve.json'


def applications(resolve_root=None):
    saved = json.loads(regular(config_path()).read_text(encoding='utf-8-sig')) if config_path().exists() else {}
    selected = resolve_root or os.environ.get('JIANYING_RESOLVE_ROOT') or saved.get('resolve_root')
    root = Path(selected or (Path(os.environ.get('ProgramFiles', 'C:/Program Files')) /
                           'Blackmagic Design' / 'DaVinci Resolve')).resolve()
    files = {'executable': root / 'Resolve.exe', 'python': root / 'ResolvePython/ResolvePython.exe',
             'script_library': root / 'fusionscript.dll'}
    return {'resolve_root': str(root), 'files': {k: str(p) for k, p in files.items()},
            'available': all(p.is_file() for p in files.values())}


def application_identity(apps):
    if not apps['available']:
        raise ValueError('Resolve installation/bundled Python unavailable; configure --resolve-root')
    return {key: {'path': str(regular(path)), 'sha256': sha(path)} for key, path in apps['files'].items()}


def executable_version(path):
    """Read the Windows PE resource without launching Resolve or its scripting API."""
    if os.name != 'nt' or not Path(path).is_file():
        return None
    import ctypes
    import struct
    from ctypes import wintypes
    api = ctypes.WinDLL('version', use_last_error=True)
    size_fn = api.GetFileVersionInfoSizeW
    size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size_fn.restype = wintypes.DWORD
    size = size_fn(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    load = api.GetFileVersionInfoW
    load.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    load.restype = wintypes.BOOL
    query = api.VerQueryValueW
    query.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    query.restype = wintypes.BOOL
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not load(str(path), 0, size, buffer) or not query(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        return None
    fields = struct.unpack('<13I', ctypes.string_at(pointer, 52))
    if fields[0] != 0xFEEF04BD:
        return None
    return [fields[2] >> 16, fields[2] & 65535, fields[3] >> 16, fields[3] & 65535]


@contextmanager
def resolve_host_lock():
    if os.name != 'nt':
        raise ValueError('Native Resolve execution is currently Windows-only')
    import msvcrt
    config_path().parent.mkdir(parents=True, exist_ok=True)
    with (config_path().parent / 'resolve-host.lock').open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError('Resolve workflow busy in another worker') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def doctor(args):
    apps = applications(args.resolve_root)
    return {'status': 'configured' if apps['available'] else 'needs_configuration',
            **apps, 'identity': application_identity(apps) if apps['available'] else {},
            'executable_version': executable_version(apps['files']['executable']),
            'admitted_native_versions': ['21.1.1'],
            'host_execution': False, 'security_settings_changed': False,
            'native_API_connection': 'not_tested_by_read_only_doctor',
            'integrity_or_license_certified': False}


def configure(args):
    apps = applications(args.resolve_root)
    application_identity(apps)
    config_path().parent.mkdir(parents=True, exist_ok=True)
    record(config_path().parent, config_path().name, {'resolve_root': apps['resolve_root']})
    return {'status': 'configured', 'config': str(config_path()), **apps, 'host_execution': False}


def source_identity(plan):
    return {p: sha(regular(p)) for p in sorted({item['path'] for kind in ('clips', 'audio') for item in plan[kind]})}


def validate_cfr_timestamps(timestamps, required_frames):
    if not required_frames <= len(timestamps) <= 18001:
        raise ValueError('CFR source has too few selected frames or exceeds the initial 18001-frame admission limit')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in timestamps):
        raise ValueError('CFR source has missing or nonfinite frame timestamps')
    if any(abs((right - left) - 1 / 30) > 5e-5 for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError('VFR or discontinuous video is not admitted; prepare a separate CFR selection first')


def verify_cfr(plan):
    """Fresh full-source PTS check; average frame rate alone does not prove CFR."""
    sources = {}
    for clip in plan['clips']:
        if clip['info']['duration'] > 600:
            raise ValueError('Initial video sources must be at most 600 seconds; save a separate selection first')
        required = clip['source_in_frames'] + clip['duration_frames']
        sources[clip['path']] = max(required, sources.get(clip['path'], 0))
    for path, required in sources.items():
        result = editor.run([editor.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                             '-read_intervals', '%+#18002', '-show_frames', '-show_entries',
                             'frame=best_effort_timestamp_time', '-of', 'json', path], timeout=120)
        frames = json.loads(result.stdout).get('frames', [])
        try:
            timestamps = [float(frame['best_effort_timestamp_time']) for frame in frames]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Missing valid per-frame PTS for CFR source admission') from exc
        validate_cfr_timestamps(timestamps, required)


def code_identity():
    return {name: sha(regular(path)) for name, path in
            [('compiler', resolve_compile.__file__), ('native', NATIVE), ('workflow', __file__)]}


def bundle_fingerprint(root, plan, identity):
    value = {'bundle': str(root.resolve()), 'normalized': resolve_compile.normalized_fingerprint(plan), **identity}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode('utf-8')).hexdigest()


def prepare(args):
    original = regular(args.plan).resolve(strict=True)
    plan = resolve_compile.validate_plan(original, editor.probe)
    verify_cfr(plan)
    root = args.out.absolute()
    if root.exists() or root.is_symlink() or root.is_junction() or not root.parent.is_dir():
        raise ValueError('Choose a new output directory with an existing parent')
    identity = {'plan_path': str(original), 'plan_sha256': sha(original),
                'sources': source_identity(plan), 'code_sha256': code_identity()}
    fingerprint = bundle_fingerprint(root, plan, identity)
    root.mkdir()
    root = root.resolve()
    write_new(root / 'normalized-plan.json', encoded(plan))
    if plan.get('captions'):
        write_new(root / 'captions.srt', resolve_compile.compile_srt(plan))
    receipt = {'schema_version': 1, 'backend': 'resolve', 'status': 'prepared_not_executed',
               **identity, 'normalized_sha256': sha(root / 'normalized-plan.json'),
               'fingerprint': fingerprint, 'project_name': plan['name'][:75] + '_' + fingerprint[:12],
               'native_project_verified': False, 'native_render_verified': False,
               'human_viewing_listening': 'not_performed'}
    record(root, 'handoff.json', receipt)
    return receipt


def load_bundle(path):
    raw = Path(path).absolute()
    if raw.is_symlink() or raw.is_junction() or not raw.is_dir():
        raise ValueError('Expected a regular handoff directory')
    root = raw.resolve(strict=True)
    receipt = json.loads(regular(root / 'handoff.json').read_text(encoding='utf-8'))
    if receipt.get('schema_version') != 1 or receipt.get('backend') != 'resolve':
        raise ValueError('Unsupported Resolve handoff')
    if receipt.get('code_sha256') != code_identity():
        raise ValueError('Compiler/native/workflow changed; prepare a new handoff')
    original = regular(receipt['plan_path'])
    if sha(original) != receipt['plan_sha256']:
        raise ValueError('Source plan changed; prepare a new handoff')
    plan = resolve_compile.validate_plan(original, editor.probe)
    verify_cfr(plan)
    if source_identity(plan) != receipt['sources']:
        raise ValueError('Source media changed; prepare a new handoff')
    normalized = regular(root / 'normalized-plan.json')
    if (sha(normalized) != receipt['normalized_sha256'] or normalized.read_text(encoding='utf-8') != encoded(plan)
            or bundle_fingerprint(root, plan, {key: receipt[key] for key in
                    ('plan_path', 'plan_sha256', 'sources', 'code_sha256')}) != receipt['fingerprint']
            or receipt['project_name'] != plan['name'][:75] + '_' + receipt['fingerprint'][:12]):
        raise ValueError('Generated Resolve handoff altered')
    if plan.get('captions'):
        if regular(root / 'captions.srt').read_text(encoding='utf-8') != resolve_compile.compile_srt(plan):
            raise ValueError('Generated subtitles altered')
    return root, plan, receipt


def verify_media(root, plan):
    output = regular(root / 'output.mp4')
    drp = regular(root / 'project.drp')
    native = json.loads(regular(root / 'native-result.json').read_text(encoding='utf-8'))
    if native.get('status') != 'native_completed' or native.get('render_status', {}).get('JobStatus') != 'Complete':
        raise ValueError('Successful native render receipt required')
    readback = json.loads(regular(root / 'readback.json').read_text(encoding='utf-8'))
    if readback.get('before_close') != readback.get('after_reopen') or not readback.get('before_close'):
        raise ValueError('Exact native project save/reopen readback required')
    resolve_native.validate_readback(readback['after_reopen'], plan)
    for filename in ('output.mp4', 'project.drp', 'readback.json'):
        if native.get('outputs', {}).get(filename) != sha(regular(root / filename)):
            raise ValueError('Native output changed: ' + filename)
    result = editor.run([editor.tool('ffprobe'), '-v', 'error', '-count_frames', '-show_streams',
                         '-of', 'json', output])
    streams = json.loads(result.stdout)['streams']
    video = next((s for s in streams if s['codec_type'] == 'video'), None)
    audio = next((s for s in streams if s['codec_type'] == 'audio'), None)
    canvas = plan['canvas']
    if (not video or video.get('codec_name') != 'h264'
            or int(video.get('nb_read_frames', -1)) != plan['duration_frames']
            or video.get('width') != canvas['width'] or video.get('height') != canvas['height']
            or abs(float(Fraction(video['avg_frame_rate'])) - canvas['fps']) > 1e-6):
        raise ValueError('Native render frame count, codec, dimensions or frame rate differs from plan')
    wants_audio = any(c['volume'] > 0 and c['info']['has_audio'] for c in plan['clips'] + plan['audio'])
    if wants_audio and (not audio or audio.get('codec_name') != 'aac'
                        or int(audio.get('sample_rate', 0)) != 48000 or audio.get('channels') != 2):
        raise ValueError('Expected AAC 48kHz stereo native audio')
    if not wants_audio and audio:
        raise ValueError('Unexpected audio in a silent plan')
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', '-i', output, '-f', 'null', '-'])
    delivered = [output, drp]
    if plan.get('captions'):
        delivered.append(regular(root / 'captions.srt'))
    return {'technical_media_verification': 'passed', 'native_project_verified': True,
            'native_render_verified': True, 'human_viewing_listening': 'not_performed',
            'audio_content_alignment': 'not_certified_by_stream_checks',
            'outputs': [{'path': str(p), 'sha256': sha(p), 'bytes': p.stat().st_size} for p in delivered]}


def run(args):
    with resolve_host_lock():
        root, plan, receipt = load_bundle(args.bundle)
        for name in ('attempt.json', 'execution.json', 'native-result.json', 'readback.json', 'project.drp',
                     'output.mp4', 'native.stdout.log', 'native.stderr.log'):
            if (root / name).exists() or (root / name).is_symlink():
                raise ValueError('Resolve handoff already attempted or has outputs; prepare a new version')
        apps = applications(args.resolve_root)
        identity = application_identity(apps)
        write_new(root / 'attempt.json', encoded({'started': time.time(), 'application_identity': identity}))
        record(root, 'execution.json', {'status': 'running', 'host_termination_policy': 'never'})
        try:
            command = [apps['files']['python'], str(NATIVE), 'run', '--bundle', str(root),
                       '--normalized-sha256', receipt['normalized_sha256'],
                       '--project-name', receipt['project_name'], '--timeout', str(args.timeout)]
            with (root / 'native.stdout.log').open('xb') as stdout, (root / 'native.stderr.log').open('xb') as stderr:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                        shell=False, timeout=args.timeout + 30,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('Resolve helper failed; inspect native-result.json and isolated logs')
            load_bundle(root)
            if application_identity(applications(args.resolve_root)) != identity:
                raise ValueError('Resolve executable identity changed during execution')
            verified = verify_media(root, plan)
            verified.update(status='technically_completed', project_name=receipt['project_name'],
                            full_app_restart='not_tested_by_this_workflow')
            record(root, 'execution.json', verified)
            return verified
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            record(root, 'execution.json', {'status': 'failed', 'error': str(exc),
                                           'host_terminated': False, 'host_state': 'inspect_before_any_retry',
                                           'human_viewing_listening': 'not_performed'})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('doctor', 'configure'):
        p = commands.add_parser(operation)
        p.add_argument('--resolve-root', type=Path, required=operation == 'configure')
    for operation in ('validate', 'prepare'):
        p = commands.add_parser(operation)
        p.add_argument('--plan', type=Path, required=True)
        if operation == 'prepare':
            p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('run')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--resolve-root', type=Path)
    p.add_argument('--timeout', type=int, default=600, choices=range(1, 1801), metavar='1..1800')
    p = commands.add_parser('verify')
    p.add_argument('--bundle', type=Path, required=True)
    p = commands.add_parser('return-ae')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--clip-index', type=int, required=True, help='1-based target Resolve clip')
    p.add_argument('--shot-index', type=int, default=1, help='1-based rendered AE shot')
    p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == 'return-ae':
        import resolve_ae
        result = resolve_ae.return_shot(args)
    elif args.operation == 'validate':
        result = {'status': 'validated_not_executed', 'plan': resolve_compile.validate_plan(args.plan, editor.probe)}
    elif args.operation == 'verify':
        root, plan, _ = load_bundle(args.bundle)
        result = verify_media(root, plan)
    else:
        result = globals()[args.operation](args)
    editor.emit(result)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
