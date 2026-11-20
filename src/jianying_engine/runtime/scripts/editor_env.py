"""Environment discovery, media probing, and draft registration helpers for editor."""
import configparser
import importlib.metadata
import json
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time
import uuid

import file_hash
import runtime_admission


def probe(path):
    import editor

    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f'Missing local media: {path}')
    data = json.loads(editor.run([editor.tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format',
                                   '-of', 'json', str(path)]).stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    duration = data.get('format', {}).get('duration')
    if duration is None:
        duration = max((float(s.get('duration', 0)) for s in videos + audios), default=0)
    duration = float(duration)
    editor.number(duration, 'media duration')
    kind = 'image' if path.suffix.lower() in editor.IMAGE_EXT and videos else 'video' if videos else 'audio'
    if not videos and not audios:
        raise ValueError(f'No supported media stream: {path}')
    info = {'path': str(path), 'kind': kind, 'duration': 0 if kind == 'image' else duration,
            'has_audio': bool(audios)}
    if audios:
        info.update(sample_rate=int(audios[0].get('sample_rate', 0)), channels=audios[0].get('channels', 0),
                    audio_codec=audios[0].get('codec_name', ''))
    if videos:
        v = videos[0]
        numerator, denominator = v.get('avg_frame_rate', '0/1').split('/')
        info.update(width=v['width'], height=v['height'],
                    fps=float(numerator) / float(denominator) if float(denominator) else 0)
        if kind == 'video':  # GIF probes as bgra; its transparency is unverified and stays as before
            pix_fmt = v.get('pix_fmt', '')
            info.update(codec=v.get('codec_name', ''), pix_fmt=pix_fmt,
                        alpha=v.get('codec_name') != 'gif' and (bool(editor.ALPHA_PIX_FMT.match(pix_fmt)) or
                                                               str(v.get('tags', {}).get('alpha_mode', '')) == '1'))
    if kind != 'image' and duration <= 0:
        raise ValueError(f'Media duration could not be measured: {path}')
    return info


def frame(path, timestamp, destination):
    import editor

    seek = [] if Path(path).suffix.lower() in editor.IMAGE_EXT else ['-ss', f'{timestamp:.6f}']
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                *seek, '-i', path, '-frames:v', '1',
                '-vf', 'scale=960:960:force_original_aspect_ratio=decrease', '-q:v', '3', destination])
    if not Path(destination).is_file() or not Path(destination).stat().st_size:
        raise ValueError(f'Frame extraction produced no image at {timestamp}s')


def new_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def draft_location(user_data=None):
    import editor

    if user_data is None:
        override = editor.os.environ.get('JIANYING_USER_DATA')
        local = editor.os.environ.get('LOCALAPPDATA')
        if not local and not override:
            return {'active_draft_root': None, 'setting_file': None, 'homepage_index': None}
        user_data = Path(editor.os.path.expandvars(override)).expanduser() if override else Path(local) / 'JianyingPro' / 'User Data'
        if override and not user_data.is_dir():
            raise ValueError(f'JIANYING_USER_DATA directory is unavailable: {user_data}')
    user_data = Path(user_data)
    setting = user_data / 'Config' / 'globalSetting'
    default_root = user_data / 'Projects' / 'com.lveditor.draft'
    custom = ''
    if setting.is_file():
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(setting.read_text(encoding='utf-8-sig'))
        custom = editor.qt_ini_value(config.get('General', 'currentCustomDraftPath', fallback=''))
    active = Path(custom) if custom else default_root
    if custom and not active.is_dir():
        raise ValueError(f'Configured Jianying draft directory is unavailable: {active}')
    return {'active_draft_root': str(active.resolve()) if active.is_dir() else None,
            'setting_file': str(setting), 'homepage_index': str(default_root / 'root_meta_info.json')}


def wait_for_registration(destination, index_path, seconds):
    import editor

    deadline = time.monotonic() + seconds
    while index_path:
        try:
            entries = editor.read_json(index_path)['all_draft_store']
            matches = [x for x in entries if Path(x.get('draft_fold_path', '')).resolve() == destination]
            if len(matches) == 1:
                return {'status': 'registered', 'draft_id': matches[0]['draft_id'], 'index': index_path}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # The native writer may be replacing the index during this read.
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return {'status': 'unverified', 'index': index_path}


def jianying_running():
    """Read-only process probe; the draft watcher that registers new folders exists only while Jianying runs."""
    import editor

    if editor.os.name != 'nt':
        return False
    try:
        listing = editor.run(['tasklist', '/FI', 'IMAGENAME eq JianyingPro.exe', '/NH'], timeout=30).stdout
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False
    return 'JianyingPro.exe' in listing


def qt_ini_value(raw):
    """Decode a QSettings INI string: optional quotes, backslash escapes and \\xHHHH for non-ASCII
    (a Chinese draft folder is written that way); surrogate pairs are joined. A value with single
    backslashes and no \\x code was not written by Qt and is kept as it is."""
    import editor

    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if '\\\\' not in value and not re.search(r'\\x[0-9a-fA-F]{4}', value):
        return value
    decoded = re.sub(r'\\(?:x([0-9a-fA-F]{1,4})|(.))',
                     lambda m: chr(int(m.group(1), 16)) if m.group(1) else editor.QT_ESCAPES.get(m.group(2), m.group(2)), value)
    return decoded.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')


def register_media_library(content, internal_roots):
    """Give each top-level local video, photo and audio material a media-library entry and point its
    local_material_id at it (one entry per file), so Jianying lists the files in its media panel.
    Draft-internal resources and nested sub-draft copies are left as they are.
    shortcut: top-level materials only, extend to sub-drafts if their own media panel needs it."""
    internal = [Path(r).resolve() for r in internal_roots]
    library, entries = [], {}
    for kind, metetype in (('videos', None), ('audios', 'music')):
        for material in content['materials'].get(kind, []):
            path = material.get('path') or ''
            if not path or not Path(path).is_file() or any(Path(path).resolve().is_relative_to(r) for r in internal):
                continue
            mtype = metetype or ('photo' if material.get('type') == 'photo' else 'video')
            key = str(Path(path).resolve()).lower()
            if key not in entries:
                entries[key] = str(uuid.uuid4()).upper()
                library.append({
                    'ai_group_type': '', 'create_time': -1, 'duration': 5_000_000 if mtype == 'photo' else material.get('duration', 0),
                    'enter_from': 0, 'extra_info': Path(path).name, 'file_Path': path,
                    'height': material.get('height', 0), 'id': entries[key], 'import_time': -1, 'import_time_ms': -1,
                    'item_source': 1, 'material_color_tag': '', 'md5': '', 'metetype': mtype,
                    'roughcut_time_range': {'duration': -1, 'start': -1}, 'sub_time_range': {'duration': -1, 'start': -1},
                    'type': 0, 'width': material.get('width', 0)})
            material['local_material_id'] = entries[key]
    return library


def draft_tree_digest(root):
    """{relative path: [bytes, sha256]} so a round-trip can prove the folder came back byte for byte."""
    root = Path(root)
    return {item.relative_to(root).as_posix(): [item.stat().st_size, file_hash.sha256(item)]
            for item in sorted(root.rglob('*')) if item.is_file()}


def refresh_registration(draft, wait_seconds=30, settle_seconds=8, watcher_required=True):
    """Re-create an existing draft folder so the running Jianying watcher registers it.

    Jianying 11.6 registers folders whose creation it observes; a folder that already existed when
    the watcher started stays invisible, and restarting Jianying does not register it. The folder is
    copied aside, removed and copied back byte for byte, so an adopted (encrypted) draft keeps the
    app's own files. Nothing else is written: the homepage index is only read.
    """
    import editor

    destination = Path(draft).resolve()
    settings = editor.draft_location()
    active = settings['active_draft_root']
    if not active:
        raise ValueError('No configured Jianying draft root; run doctor and confirm where drafts are stored')
    active = Path(active).resolve()
    if destination.parent != active or not destination.is_dir():
        raise ValueError(f'Draft must be an existing directory directly inside the configured draft root: {active}')
    if not (destination / 'draft_meta_info.json').is_file():
        raise ValueError(f'Not a draft directory (no draft_meta_info.json): {destination}')
    if watcher_required and not editor.jianying_running():
        raise ValueError('Jianying is not running: its draft watcher registers folders only while it runs. Open Jianying, then retry.')
    index = settings['homepage_index']
    known = editor.wait_for_registration(destination, index, 0)
    if known['status'] == 'registered':
        return {'draft': str(destination), 'refresh': 'already_registered', 'global_index_modified': False,
                'registration': known, 'homepage_visibility': 'pending_ui_verification'}
    before = editor.draft_tree_digest(destination)
    with tempfile.TemporaryDirectory(prefix='.jianying-refresh-') as backup:
        staged = Path(backup) / 'draft'
        editor.shutil.copytree(destination, staged)
        if editor.draft_tree_digest(staged) != before:
            raise ValueError('Backup copy differs from the draft; refresh cancelled')
        editor.shutil.rmtree(destination)
        time.sleep(settle_seconds)
        editor.shutil.copytree(staged, destination)
        if editor.draft_tree_digest(destination) != before:
            editor.shutil.rmtree(destination, ignore_errors=True)
            editor.shutil.copytree(staged, destination)
            raise ValueError('Draft changed during the round-trip; the previous copy was restored and registration was not refreshed')
    registration = editor.wait_for_registration(destination, index, wait_seconds)
    return {'draft': str(destination), 'refresh': 'recreated', 'files': len(before), 'byte_identical': True,
            'global_index_modified': False, 'registration': registration,
            'homepage_visibility': 'pending_ui_verification' if registration['status'] == 'registered' else 'unverified'}


def doctor():
    import editor

    versions = []
    if editor.os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for subkey in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                           r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                try:
                    with winreg.OpenKey(hive, subkey) as root:
                        for index in range(winreg.QueryInfoKey(root)[0]):
                            try:
                                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as item:
                                    name = winreg.QueryValueEx(item, 'DisplayName')[0]
                                    if '剪映' in name or 'jianying' in name.lower():
                                        versions.append({'name': name, 'version': winreg.QueryValueEx(item, 'DisplayVersion')[0]})
                            except OSError:
                                continue
                except OSError:
                    continue
    location_error = None
    try:
        location = editor.draft_location()
    except (ValueError, OSError, configparser.Error) as exc:
        location_error = str(exc)
        location = {'active_draft_root': None, 'setting_file': None, 'homepage_index': None}
    candidates = [location['active_draft_root']] if location['active_draft_root'] else []
    local = editor.os.environ.get('LOCALAPPDATA')
    if local:
        for rel in ('JianyingPro/User Data/Projects/com.lveditor.draft',
                    'JianyingPro/User Data/com.lveditor.draft'):
            p = Path(local) / rel
            if p.is_dir() and str(p.resolve()) not in candidates:
                candidates.append(str(p))
    try:
        backend = importlib.metadata.version('pyJianYingDraft')
    except importlib.metadata.PackageNotFoundError:
        backend = None
    backend_error = None
    try:
        editor.load_backend()
    except (ImportError, OSError, ValueError) as exc:
        backend_error = f'{type(exc).__name__}: {exc}'
    backend_importable = backend_error is None
    try:
        import pymediainfo
        media_library = bool(pymediainfo.MediaInfo.can_parse())
    except (ImportError, OSError):
        media_library = False
    tools = {name: editor.tool_path(name) for name in ('ffmpeg', 'ffprobe')}
    execution = {}
    for name, path in tools.items():
        try:
            execution[name] = bool(path and editor.run([path, '-version'], timeout=10).stdout.strip())
        except (ValueError, OSError, subprocess.TimeoutExpired):
            execution[name] = False
    ready = editor.os.name == 'nt' and all(execution.values()) and backend == '0.3.0' and backend_importable and media_library
    actions = []
    if editor.os.name != 'nt':
        actions.append({'code': 'unsupported_os', 'instruction': '当前实现面向Windows剪映；其他系统需单独适配，不能声明迁移通过。'})
    for name in tools:
        if not execution[name]:
            actions.append({'code': name + '_unavailable', 'instruction':
                            f'安装可运行的{name}，设置当前会话JIANYING_{name.upper()}为其完整路径，或加入PATH；重新运行doctor。'})
    if backend != '0.3.0' or not backend_importable or not media_library:
        actions.append({'code': 'dependencies', 'instruction':
                        f'当前解释器为 {editor.sys.executable}。使用外部虚拟环境，在PowerShell运行 & "{editor.sys.executable}" -m pip install -r "{editor.SKILL_ROOT / "scripts/requirements.txt"}" -c "{editor.SKILL_ROOT / "scripts/constraints.txt"}"；不要在引擎runtime目录创建.venv，也不修改全局Python。',
                        'detail': backend_error})
    if location_error or not location['active_draft_root']:
        actions.append({'code': 'draft_directory', 'instruction':
                        '核实本机剪映设置中的草稿保存位置；旧设备盘符不存在时，在剪映设置中选可用目录。特殊用户数据位置可设置JIANYING_USER_DATA；不自动修改全局设置或猜目录。',
                        'detail': location_error})
    known_version = any(v['version'] == editor.REFERENCE_JIANYING_VERSION for v in versions)
    if not versions:
        actions.append({'code': 'installation_unconfirmed', 'instruction':
                        '未从注册表确认Windows剪映安装；核实安装与实际运行版本，暂不声明剪映兼容或发布就绪。'})
    elif not known_version:
        actions.append({'code': 'version_unverified', 'instruction':
                        f'安装版本与高级结构参考{editor.REFERENCE_JIANYING_VERSION}不同；ready只表示运行依赖就绪。'
                        '按能力主表和当前工程回执核实各功能；已有其他版本的分项证据仍有效，不能推定全功能通过或全部不可用。不自动升级/降级剪映。'})
    effect_cache = None
    try:
        effect_cache = editor.effect_cache_dir(editor.configured_user_data())
    except Exception:
        effect_cache = None
    native_resources = editor.cached_native_resources()
    effect_cache_ready = bool(effect_cache is not None and any(native_resources.values()))
    if not effect_cache_ready or not all(native_resources.values()):
        missing_resources = [key for key, ok in native_resources.items() if not ok]
        actions.append({'code': 'effect_cache_missing',
                        'missing': missing_resources,
                        'instruction':
                        '本机剪映 User Data\\Cache\\effect 缺失或不完整；基础剪辑、字幕、音频与不依赖效应包的混剪/色度抠像可正常工作，'
                        '但局部羽化、清晰度/HSL/曲线、模糊、文字发光与指定字体缓存暂不可用，相关自检会跳过。若需启用，请在剪映中下载对应特效/字体包后再运行 doctor。'})
    install_dir = editor.jianying_install()
    runtime_admissions, admission_error = None, None
    if install_dir is not None:
        try:
            runtime_admissions = {feature: bool(runtime_admission.matches(feature, install_dir))
                                  for feature in runtime_admission.FEATURES}
        except (ValueError, OSError) as exc:
            admission_error = f'{type(exc).__name__}: {exc}'
    if admission_error:
        actions.append({'code': 'runtime_admission_error', 'detail': admission_error, 'instruction':
                        '本地版本准入记录读取或校验失败；检查或清理 %LOCALAPPDATA%\\JianyingEngine\\runtime-admissions.json 后重新运行 requalify。'})
    elif runtime_admissions is not None and not all(runtime_admissions.values()):
        unadmitted = [feature for feature, ok in runtime_admissions.items() if not ok]
        actions.append({'code': 'runtime_admission_needed',
                        'unadmitted': unadmitted,
                        'instruction':
                        f'已安装剪映 {install_dir.name} 尚有未准入的版本门控子集（{", ".join(unadmitted)}）；'
                        f'如需在当前版本启用可用子集，请运行 jianying-engine requalify --candidate "{install_dir}"。'})
    return {'python': editor.sys.executable, 'python_version': editor.sys.version.split()[0],
            'skill_root': str(editor.SKILL_ROOT), 'platform': editor.sys.platform,
            **tools, 'tool_execution': execution,
            'backend': backend, 'expected_backend': '0.3.0', 'mediainfo_available': media_library,
            'backend_importable': backend_importable, 'backend_error': backend_error,
            'jianying_installed': versions, 'draft_root_candidates': candidates,
            'jianying_install': str(install_dir) if install_dir is not None else None,
            **location, 'draft_location_error': location_error,
            'effect_cache_dir': str(effect_cache) if effect_cache is not None else None,
            'effect_cache_ready': effect_cache_ready,
            'cached_native_resources': native_resources,
            'runtime_admissions': runtime_admissions,
            'runtime_admission_error': admission_error,
            'publication_ready': bool(ready and versions and location['active_draft_root']),
            'advanced_schema_reference_version': editor.REFERENCE_JIANYING_VERSION,
            'advanced_schema_version_match': known_version,
            'ready_scope': 'runtime_dependencies_only',
            'compatibility': {'all_features_verified': False,
                              'feature_evidence': 'consult_capabilities_and_project_receipts',
                              'ui_save_reopen': 'feature_and_project_specific',
                              'running_version_confirmed': False},
            'migration_instructions': actions,
            'background_only': True, 'ui_compatibility': 'unverified',
            'ready': ready}


def inspect_media(args):
    import editor

    source = Path(args.input).resolve()
    if not source.exists():
        raise ValueError(f'Input does not exist: {source}')
    files = [source] if source.is_file() else sorted(p for p in source.rglob('*')
                                                   if p.is_file() and p.suffix.lower() in editor.MEDIA_EXT)
    if not files:
        raise ValueError('No supported media files found')
    output = editor.new_directory(args.out)
    records = []
    for index, path in enumerate(files[:args.max_files]):
        try:
            item = editor.probe(path)
            item['frames'] = []
            if item['kind'] != 'audio':
                duration = item['duration']
                points = [0] if item['kind'] == 'image' else [duration * (i + 0.5) / args.frames for i in range(args.frames)]
                for j, point in enumerate(points):
                    image = output / f'media-{index:04d}-frame-{j:02d}.jpg'
                    editor.frame(str(path), point, str(image))
                    item['frames'].append({'time': round(point, 6), 'path': str(image)})
            item['status'] = 'probed'
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            item = {'path': str(path), 'status': 'failed', 'error': str(exc)}
        records.append(item)
    report = {'files_found': len(files), 'files_processed': len(records),
              'truncated': len(files) > len(records), 'semantic_analysis': 'not_performed',
              'status': 'partial' if any(r['status'] == 'failed' for r in records) else 'success', 'media': records}
    editor.write_json(output / 'media-index.json', report)
    return {'status': report['status'], 'index': str(output / 'media-index.json'),
            'files_processed': len(records), 'truncated': report['truncated']}


def analyze_reference(args):
    import editor

    info = editor.probe(args.input)
    if info['kind'] != 'video':
        raise ValueError('Reference must be a local video')
    editor.number(args.start, 'start')
    editor.number(args.duration, 'analysis duration', 0.01, 300)
    editor.number(args.threshold, 'scene threshold', 0.01, 1)
    if args.start >= info['duration']:
        raise ValueError('Analysis start is past the end of the reference')
    span = min(args.duration, info['duration'] - args.start)
    detector = getattr(args, 'detector', 'ffmpeg')
    content_threshold = getattr(args, 'content_threshold', 27.0)
    min_scene_seconds = getattr(args, 'min_scene_seconds', 0.3)
    if detector == 'pyscenedetect':
        editor.number(content_threshold, 'content threshold', 0.01, 255)
        editor.number(min_scene_seconds, 'minimum scene seconds', 0.01, 30)
        local_app_data = editor.os.environ.get('LOCALAPPDATA')
        private_package = (Path(local_app_data) / 'JianyingEngine' / 'optional' / 'pyscenedetect-0.7.2'
                           if local_app_data else None)
        if private_package and private_package.is_dir():
            editor.sys.path.insert(0, str(private_package))
        try:
            from scenedetect import open_video, SceneManager
            from scenedetect.detectors import ContentDetector
        except ImportError as exc:
            raise ImportError('PySceneDetect 0.7.2 and one OpenCV distribution are required for --detector pyscenedetect; see reference guide') from exc
        scene_version = importlib.metadata.version('scenedetect')
        if scene_version != '0.7.2':
            raise ValueError(f'PySceneDetect 0.7.2 is required, found {scene_version}')
        video = open_video(info['path'])
        video.seek(float(args.start))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=content_threshold, min_scene_len=float(min_scene_seconds)))
        processed = manager.detect_scenes(video, duration=float(span))
        if processed <= 0:
            raise ValueError('PySceneDetect could not decode frames in the selected window')
        raw_cuts = [float(item.seconds) for item in manager.get_cut_list(show_warning=False)]
        cuts = sorted(set(round(t, 6) for t in raw_cuts
                          if args.start < t < args.start + span))
        detector_details = {'name': 'pyscenedetect-content', 'version': scene_version,
                            'threshold': content_threshold, 'min_scene_seconds': min_scene_seconds,
                            'decoded_frames': processed,
                            'timing_note': 'OpenCV seek/frame clock; VFR and exact frames require separate verification'}
    elif detector == 'ffmpeg':
        result = editor.run([editor.tool('ffmpeg'), '-hide_banner', '-nostdin', '-ss', args.start,
                             '-i', info['path'], '-t', span, '-an',
                             '-vf', rf'select=gt(scene\,{args.threshold}),showinfo', '-f', 'null', '-'], timeout=600)
        cuts = sorted(set(round(args.start + float(x), 6)
                          for x in re.findall(r'pts_time:([0-9.eE+-]+)', result.stderr)
                          if 0 <= float(x) < span))
        detector_details = {'name': 'ffmpeg-scene', 'threshold': args.threshold}
    else:
        raise ValueError('Unknown reference detector')
    end_sample = max(args.start, args.start + span - max(0.1, 1 / (info['fps'] or 30)))
    # Bounded sampling: candidate cuts are hypotheses, never a verified shot list.
    uniform = [args.start + (end_sample - args.start) * i / (args.max_frames - 1)
               for i in range(args.max_frames)]
    points = sorted(set(round(t, 6) for t in uniform +
                        [max(args.start, min(end_sample, c + delta)) for c in cuts for delta in (-0.12, 0.12)]))
    if len(points) > args.max_frames:
        points = [points[round(i * (len(points) - 1) / (args.max_frames - 1))] for i in range(args.max_frames)]
    output = editor.new_directory(args.out)
    frames = []
    for i, point in enumerate(points):
        target = output / f'reference-{i:03d}-{point:.3f}s.jpg'
        editor.frame(info['path'], point, str(target))
        frames.append({'time': point, 'path': str(target)})
    audio_path = None
    if info['has_audio']:
        audio_path = output / 'audio.wav'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-ss', args.start, '-i', info['path'], '-t', span, '-vn', '-ac', '1',
                    '-ar', '16000', '-c:a', 'pcm_s16le', str(audio_path)], timeout=600)
    interior_cuts = [t for t in cuts if args.start < t < args.start + span]
    boundaries = [args.start, *interior_cuts, args.start + span]
    shots = [{'start': round(a, 6), 'end': round(b, 6), 'duration': round(b - a, 6)}
             for a, b in zip(boundaries, boundaries[1:])]
    report = {'source': info, 'window_start': args.start, 'window_duration': span,
              'entire_video_analyzed': args.start == 0 and span >= info['duration'],
              'scene_threshold': args.threshold, 'cut_candidates': cuts, 'frames': frames,
              'detector': detector_details, 'shot_candidates': shots,
              'shot_summary': {'count': len(shots),
                               'median_duration': round(statistics.median(s['duration'] for s in shots), 6),
                               'cuts_per_minute': round(len(interior_cuts) * 60 / span, 6),
                               'meaning': 'window-boundary segments and visual cut candidates, not editorial or style judgments'},
              'audio': str(audio_path) if audio_path else None, 'audio_time_origin': args.start,
              'semantic_analysis': 'not_performed', 'transcription': 'not_performed',
              'ocr': 'not_performed', 'beat_analysis': 'not_performed'}
    editor.write_json(output / 'analysis.json', report)
    return {'analysis': str(output / 'analysis.json'), 'frames': len(frames),
            'cut_candidates': len(cuts), 'entire_video_analyzed': report['entire_video_analyzed'],
            'audio': report['audio'], 'next': 'View the actual frames and analyze audio; cuts are only candidates.'}
