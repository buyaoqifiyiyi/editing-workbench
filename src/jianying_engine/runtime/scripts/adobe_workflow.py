"""Bounded PR + AE handoff. Generated JSX only; no QE, plugins or preference changes."""
import argparse
from contextlib import contextmanager
import csv
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

import numpy as np

import adobe_compile
import editor


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_new(path, text):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def record(root, name, data):
    destination = root / name
    if destination.is_symlink() or destination.is_junction():
        raise ValueError('Refusing linked receipt')
    temporary = root / ('.' + uuid.uuid4().hex + '.json')
    write_new(temporary, json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(destination)


def config_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JianyingEngine' / 'adobe.json'


@contextmanager
def ae_host_lock():
    """Serialize owned AE sessions across workers; the OS releases locks on exit."""
    if os.name != 'nt':
        raise ValueError('AE execution is currently Windows-only')
    import msvcrt
    directory = config_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'ae-host.lock').open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError('AE workflow is busy in another worker; retry after it finishes') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def applications(ae_root=None, pr_exe=None, *, require_pr=True):
    saved = {}
    if config_path().is_file():
        saved = json.loads(config_path().read_text(encoding='utf-8-sig'))
    result = {}
    root = ae_root or saved.get('ae_root')
    premiere = pr_exe or saved.get('pr_exe')
    if root:
        root = Path(root).resolve(strict=True)
        for filename in ('AfterFX.exe', 'AfterFX.com', 'aerender.exe'):
            if not (root / filename).is_file():
                raise ValueError('Incomplete AE executable directory: ' + filename)
        result['ae_root'] = str(root)
    if premiere and require_pr:
        premiere = Path(premiere).resolve(strict=True)
        if not premiere.is_file() or premiere.name.casefold() != 'adobe premiere pro.exe':
            raise ValueError('Expected the Premiere executable')
        result['pr_exe'] = str(premiere)
    return result


def configure(args):
    data = applications(args.ae_root, args.pr_exe)
    if set(data) != {'ae_root', 'pr_exe'}:
        raise ValueError('Explicit AE executable directory and Premiere executable are required')
    config_path().parent.mkdir(parents=True, exist_ok=True)
    record(config_path().parent, config_path().name, data)
    return {'status': 'configured', 'config': str(config_path()), **data,
            'host_execution': False, 'integrity_or_license_certified': False}


def doctor(args):
    data = applications(args.ae_root, args.pr_exe)
    files = ([Path(data['ae_root']) / n for n in ('AfterFX.exe', 'AfterFX.com', 'aerender.exe')]
             if data.get('ae_root') else [])
    if data.get('pr_exe'):
        files.append(Path(data['pr_exe']))
    return {'status': 'configured' if len(files) == 4 else 'needs_configuration',
            'applications': data, 'files': [{'path': str(p), 'sha256': sha(p)} for p in files],
            'compiler': 'pr-ae-v1', 'premiere_control': 'native_XML_import; unattended_control_not_admitted',
            'host_execution': False, 'integrity_or_license_certified': False}


def prepare(args):
    original = args.plan.resolve(strict=True)
    plan = adobe_compile.validate_plan(original, editor.probe)
    root = args.out.absolute()
    if root.exists() or root.is_symlink() or not root.parent.is_dir():
        raise ValueError('Choose a new output directory with an existing parent')
    sources = {Path(item['path']) for group in ('clips', 'audio') for item in plan.get(group, [])}
    identity = {'plan_path': str(original), 'plan_sha256': sha(original),
                'sources': {str(p): sha(p) for p in sorted(sources)}, 'compiler_sha256': sha(adobe_compile.__file__)}
    root.mkdir()
    root = root.resolve()
    (root / 'renders').mkdir()
    write_new(root / 'create-ae.jsx', adobe_compile.compile_jsx(plan, root))
    write_new(root / 'premiere.xml', adobe_compile.compile_xmeml(plan, root))
    write_new(root / 'normalized-plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    receipt = {'schema_version': 1, 'backend': 'pr-ae', 'status': 'prepared_not_executed', **identity,
               'ae_project': str(root / 'ae-reopened.aep'), 'premiere_xml': str(root / 'premiere.xml'),
               'native_project_verified': False, 'native_render_verified': False,
               'premiere_import_save_reopen_export': 'not_verified',
               'effect_editability': 'AE native project; rendered shots on Premiere timeline'}
    record(root, 'handoff.json', receipt)
    return receipt


def load_bundle(path):
    root = path.resolve(strict=True)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError('Expected a regular handoff directory')
    receipt = json.loads((root / 'handoff.json').read_text(encoding='utf-8'))
    if receipt.get('schema_version') != 1 or receipt.get('backend') != 'pr-ae':
        raise ValueError('Unsupported handoff')
    original = Path(receipt['plan_path'])
    if sha(original) != receipt['plan_sha256'] or sha(adobe_compile.__file__) != receipt['compiler_sha256']:
        raise ValueError('Plan or compiler changed; prepare a new handoff')
    plan = adobe_compile.validate_plan(original, editor.probe)
    sources = {str(Path(item['path'])) for group in ('clips', 'audio') for item in plan.get(group, [])}
    if sources != set(receipt['sources']) or any(sha(p) != receipt['sources'][p] for p in sources):
        raise ValueError('Source media changed; prepare a new handoff')
    for filename, content in (('create-ae.jsx', adobe_compile.compile_jsx(plan, root)),
                              ('premiere.xml', adobe_compile.compile_xmeml(plan, root))):
        file = root / filename
        if file.is_symlink() or file.read_text(encoding='utf-8') != content:
            raise ValueError('Generated handoff altered: ' + filename)
    return root, plan


def active_ae():
    if os.name != 'nt':
        raise ValueError('AE execution is currently Windows-only')
    busy = []
    for name in ('AfterFX.exe', 'AfterFX.com', 'aerender.exe'):
        output = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + name, '/FO', 'CSV', '/NH'],
                                capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        rows = csv.reader(output.stdout.decode('utf-8', errors='replace').splitlines())
        busy.extend(row[0] for row in rows if row and row[0].casefold() == name.casefold())
    return busy


def invoke(command, root, label, timeout):
    with (root / (label + '.stdout.log')).open('xb') as stdout, (root / (label + '.stderr.log')).open('xb') as stderr:
        result = subprocess.run([str(x) for x in command], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                shell=False, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(f'{label} returned {result.returncode}; inspect isolated logs')


def run_ae(args):
    with ae_host_lock():
        return _run_ae(args)


def _wait_for_ae_ready(executables, root, warm, timeout=120):
    """Dispatch one read-only probe, then only poll its receipt and owned host."""
    sentinel = root / 'ae-ready.txt'
    script = root / 'ae-ready.jsx'
    token = uuid.uuid4().hex
    if sentinel.exists() or sentinel.is_symlink():
        raise ValueError('AE readiness receipt already exists; choose a new handoff')
    write_new(script, '''(function(){
var destination=new File(%s);
var state=(!app.project || (!app.project.file && app.project.numItems===0)) ? "ready" : "occupied";
if(destination.open("w")){destination.write(%s+":"+state);destination.close();}
})();
''' % (json.dumps(str(sentinel), ensure_ascii=True), json.dumps(token)))
    deadline = time.monotonic() + timeout
    if warm.poll() is not None:
        raise ValueError('Owned AE startup exited; inspect native startup logs')
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            invoke([executables / 'AfterFX.com', '-r', script], root, 'ae-ready-001', min(10, remaining))
        except (subprocess.TimeoutExpired, ValueError):
            pass  # A queued probe may still write its receipt; never redispatch during startup.
    while time.monotonic() < deadline:
        if warm.poll() is not None:
            raise ValueError('Owned AE startup exited; inspect native startup logs')
        if sentinel.is_symlink():
            raise ValueError('Refusing linked AE readiness receipt')
        if sentinel.is_file():
            state = sentinel.read_text(encoding='utf-8-sig')
            if state == token + ':occupied':
                raise ValueError('Owned AE opened an existing/nonempty project; left untouched')
            if state == token + ':ready':
                return
        time.sleep(.5)
    raise ValueError('Owned AE readiness timed out after one probe; startup dialog may be blocking; '
                     'project creation was not dispatched; no forced process termination')


def _run_ae(args):
    root, plan = load_bundle(args.bundle)
    apps = applications(args.ae_root, require_pr=False)
    if not apps.get('ae_root'):
        raise ValueError('Configure Adobe applications or provide --ae-root')
    if active_ae():
        raise ValueError('AE/aerender is already running; existing projects are left untouched')
    if any((root / f).exists() for f in ('ae-project.aep', 'ae-reopened.aep', 'ae-launch.stdout.log')):
        raise ValueError('AE handoff was already attempted; choose a new version to avoid overwrite')
    executables = Path(apps['ae_root'])
    record(root, 'execution.json', {'status': 'creating_native_projects', 'started': time.time()})
    try:
        # Use the desktop executable as the owned host; the console executable
        # dispatches to it only after startup. A console cold-start can drop -r.
        with (root / 'ae-launch.stdout.log').open('xb') as stdout, (root / 'ae-launch.stderr.log').open('xb') as stderr:
            warm = subprocess.Popen([str(executables / 'AfterFX.exe')], stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, shell=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            _wait_for_ae_ready(executables, root, warm)
            load_bundle(root)  # The create script and source identities must still match after startup.
            invoke([executables / 'AfterFX.com', '-r', root / 'create-ae.jsx'], root, 'ae-dispatch', 120)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and (not (root / 'ae-reopened.aep').is_file() or active_ae()):
            time.sleep(.5)
        if not (root / 'ae-reopened.aep').is_file() or active_ae():
            raise ValueError('Native project completion/owned AE exit not confirmed; no forced process termination')
        invoke([executables / 'aerender.exe', '-project', root / 'ae-reopened.aep',
                '-v', 'ERRORS_AND_PROGRESS'], root, 'aerender', args.timeout)
        load_bundle(root)  # Recheck source identity after host execution, before publishing success.
        result = verify_media(root, plan)
        result.update(status='ae_rendered_pr_import_pending', ae_project=str(root / 'ae-reopened.aep'),
                      premiere_xml=str(root / 'premiere.xml'), premiere_import_save_reopen_export='not_verified')
        record(root, 'execution.json', result)
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        record(root, 'execution.json', {'status': 'failed', 'error': str(exc),
                                      'premiere_import_save_reopen_export': 'not_verified'})
        raise


def _same_native_text(actual, expected, label='text readback'):
    """Compare native text values in canvas/time units, allowing SDK float rounding."""
    if type(expected) in (int, float):
        if type(actual) not in (int, float) or not math.isfinite(actual) or abs(actual - expected) > 1e-5:
            raise ValueError(label + ': native value differs')
    elif isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError(label + ': native fields differ')
        for key, value in expected.items():
            _same_native_text(actual[key], value, label + '.' + key)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError(label + ': native list differs')
        for index, value in enumerate(expected):
            _same_native_text(actual[index], value, label + '[' + str(index) + ']')
    elif type(actual) is not type(expected) or actual != expected:
        raise ValueError(label + ': native content differs')


def verify_text_readback(root, plan):
    if not plan.get('captions'):
        return {'status': 'not_requested'}
    path = root / 'ae-text-readback.json'
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError('Missing regular native AE text readback')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    expected = adobe_compile.caption_layer_specs(plan)
    for stage in ('before_save', 'after_reopen'):
        _same_native_text(data.get(stage), expected, stage)
    return {'status': 'native_parameters_save_reopen_verified', 'path': str(path), 'sha256': sha(path),
            'content_animation': 'requires_separate_render_witness'}


def verify_foreground_readback(root, plan):
    if not any(clip.get('foreground_mask') for clip in plan['clips']):
        return {'status': 'not_requested'}
    path = root / 'ae-layer-readback.json'
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError('Missing regular native AE foreground readback')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    expected = adobe_compile.foreground_layer_specs(plan)
    for stage in ('before_save', 'after_reopen'):
        _same_native_text(data.get(stage), expected, 'foreground.' + stage)
    return {'status': 'native_layer_order_mask_and_mute_save_reopen_verified',
            'path': str(path), 'sha256': sha(path), 'occlusion': 'requires_separate_render_witness'}


def verify_media(root, plan):
    text_readback = verify_text_readback(root, plan)
    foreground_readback = verify_foreground_readback(root, plan)
    outputs = []
    fps = plan['canvas']['fps']
    for index, clip in enumerate(plan['clips'], 1):
        path = root / 'renders' / f'shot-{index:03}.avi'
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing regular AE render: ' + str(path))
        result = editor.run([editor.tool('ffprobe'), '-v', 'error', '-count_frames', '-show_streams',
                             '-of', 'json', path])
        streams = json.loads(result.stdout)['streams']
        video = next((s for s in streams if s['codec_type'] == 'video'), None)
        expected = round(clip['duration'] * fps)
        if (not video or video.get('codec_name') != 'rawvideo' or int(video.get('nb_read_frames', -1)) != expected
            or video['width'] != plan['canvas']['width'] or video['height'] != plan['canvas']['height']
            or abs(float(Fraction(video['avg_frame_rate'])) - fps) > 1e-6):
            raise ValueError('AE render frame count, size or rate differs from plan: ' + str(path))
        editor.run([editor.tool('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', '-i', path, '-f', 'null', '-'])
        audio = next((s for s in streams if s['codec_type'] == 'audio'), None)
        source = editor.probe(clip['path'])
        audio_check = {'policy': 'muted', 'verified': True}
        if source['has_audio'] and clip.get('volume', 0) > 0:
            if (not audio or audio.get('codec_name') != 'pcm_s16le'
                or int(audio.get('sample_rate', 0)) != 48000 or audio.get('channels') != 2):
                raise ValueError('Expected 48kHz stereo PCM16 source audio in AE render')
            actual = decoded_audio(path)
            expected_audio = decoded_audio(clip['path'], clip['source_in'], clip['duration'], clip['volume'])
            sample_count = round(clip['duration'] * 48000)
            if abs(len(actual) - sample_count) > 1 or abs(len(expected_audio) - sample_count) > 1:
                raise ValueError('AE render/source selection audio sample count differs from duration')
            actual_rms = np.sqrt(np.mean(actual.astype('float64') ** 2, axis=0))
            expected_rms = np.sqrt(np.mean(expected_audio.astype('float64') ** 2, axis=0))
            if any(abs(a - e) > max(.0001, e * .1) for a, e in zip(actual_rms, expected_rms)):
                raise ValueError('AE source selection/audio gain RMS differs from plan')
            audio_check = {'policy': 'source_with_gain', 'samples': len(actual),
                           'actual_rms': actual_rms.tolist(), 'reference_rms': expected_rms.tolist(),
                           'content_alignment': 'not_certified_by_RMS; separate_witness_checks_required'}
        elif audio:
            muted = decoded_audio(path)
            if len(muted) and float(np.max(np.abs(muted))) > .0001:
                raise ValueError('Muted shot unexpectedly contains non-silent audio')
        outputs.append({'path': str(path), 'sha256': sha(path), 'frames': expected,
                        'video_codec': video['codec_name'], 'audio_codec': audio['codec_name'] if audio else None,
                        'full_decode': 'passed', 'audio_check': audio_check})
    return {'outputs': outputs, 'technical_media_verification': 'passed',
            'native_text_parameters': text_readback, 'native_foreground_parameters': foreground_readback,
            'human_viewing_listening': 'not_performed'}


def decoded_audio(path, source_in=None, duration=None, gain=1):
    command = [editor.tool('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', '-i', str(path)]
    if source_in is not None:
        command.extend(['-ss', str(source_in), '-t', str(duration)])
    command.extend(['-map', '0:a:0', '-af', 'volume=' + str(gain), '-ar', '48000', '-ac', '2', '-f', 'f32le', '-'])
    output = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=120)
    return np.frombuffer(output.stdout, dtype='<f4').reshape(-1, 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('doctor', 'configure'):
        p = commands.add_parser(operation)
        p.add_argument('--ae-root', type=Path)
        p.add_argument('--pr-exe', type=Path)
    p = commands.add_parser('validate')
    p.add_argument('--plan', type=Path, required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('run-ae')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--ae-root', type=Path)
    p.add_argument('--timeout', type=int, default=600, choices=range(1, 1801), metavar='1..1800')
    p = commands.add_parser('verify')
    p.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == 'doctor':
        result = doctor(args)
    elif args.operation == 'configure':
        result = configure(args)
    elif args.operation == 'validate':
        result = {'status': 'validated_not_executed', 'plan': adobe_compile.validate_plan(args.plan, editor.probe)}
    elif args.operation == 'prepare':
        result = prepare(args)
    elif args.operation == 'run-ae':
        result = run_ae(args)
    else:
        root, plan = load_bundle(args.bundle)
        result = verify_media(root, plan)
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
