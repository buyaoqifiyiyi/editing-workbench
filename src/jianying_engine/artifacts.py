"""Discover and preview workspace deliverables without following links outside it."""
import hashlib
import json
from pathlib import Path
import os
import re

MEDIA_TYPES = {'.mp4': 'video', '.mov': 'video', '.mkv': 'video', '.webm': 'video',
               '.mp3': 'audio', '.wav': 'audio', '.m4a': 'audio', '.flac': 'audio',
               '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image'}
TEXT_TYPES = {'.md': '文档', '.txt': '文本', '.srt': '字幕', '.vtt': '字幕', '.ass': '字幕', '.json': '数据'}
NATIVE_PROJECT_TYPES = {'.drp': '达芬奇工程', '.aep': 'AE工程', '.blend': 'Blender工程',
                        '.prproj': 'Premiere工程', '.kdenlive': 'Kdenlive工程', '.otio': '时间轴交换文件'}
FILE_TYPES = {**NATIVE_PROJECT_TYPES, '.avi': '视频中间素材'}
SKIP = {'.git', '.venv', 'venv', 'node_modules', 'engine', 'Skill维护层', '运行数据', '__pycache__',
        'assets', 'frames', '素材', 'input', 'inputs', 'media', 'media-manifests'}
PROCESS_DIRS = {'scout', 'details', 'graphics', 'frames', 'preview', 'previews', 'native', 'rhythm',
                'cache', 'caches', 'tmp', 'temp', 'logs', '勘景', '抽帧', '预览', '缓存', '临时', '日志'}
PROCESS_FILES = {'media.json', 'sections.json', 'acceptance.json', 'verification.json', 'publish-result.json',
                 'audio-levels.txt'}
DELIVERY_MANIFEST = 'workbench-delivery.json'


def _declarations(project):
    manifest = project / DELIVERY_MANIFEST
    if not manifest.is_file() or manifest.is_symlink():
        return {}
    try:
        data = json.loads(read_text(manifest))
        if data.get('schema_version') != 1 or not isinstance(data.get('items'), list):
            return {}
        declared = {}
        for item in data['items']:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path'].strip():
                continue
            relative = Path(item['path'])
            if relative.is_absolute() or '..' in relative.parts:
                continue
            path = project / relative
            if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents) if p.is_relative_to(project)):
                continue
            if path.is_dir():
                path = path / 'draft_content.json'
            if path.is_symlink() or path.is_junction():
                continue
            resolved = path.resolve()
            if resolved.is_relative_to(project) and resolved.is_file() and resolved.stat().st_size:
                title = item.get('title')
                declared[resolved] = title.strip()[:240] if isinstance(title, str) else ''
        return declared
    except (OSError, ValueError, AttributeError):
        return {}


def categorize(workspace, entries):
    """Only explicit project declarations are deliverables; keep other files discoverable."""
    root = Path(workspace).resolve()
    paths = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            continue
        try:
            candidate = Path(entry['path'])
            if not candidate.is_relative_to(root) or any(p.is_symlink() or p.is_junction()
                    for p in (candidate, *candidate.parents) if p.is_relative_to(root)):
                continue
            path = candidate.resolve()
            if path.is_relative_to(root) and path.is_file():
                paths.append((entry, path))
        except OSError:
            continue
    bundles = []
    for path in sorted((p.parent for _, p in paths if p.name == 'draft_content.json'), key=lambda p: len(p.parts)):
        if not any(path.is_relative_to(parent) for parent in bundles):
            bundles.append(path)
    parents = {parent for _, path in paths for parent in path.parents if parent.is_relative_to(root)}
    manifest_roots = sorted((p for p in parents if (p / DELIVERY_MANIFEST).is_file()),
                            key=lambda p: len(p.parts), reverse=True)
    declarations, found, seen = {}, [], set()
    for entry, path in paths:
        bundle = next((parent for parent in bundles if path.is_relative_to(parent)), None)
        if bundle and path != bundle / 'draft_content.json':
            continue
        if path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(root)
        fallback = root if len(relative.parts) == 1 else root / relative.parts[0]
        project = next((p for p in manifest_roots if path.is_relative_to(p)), fallback)
        if project not in declarations:
            declarations[project] = _declarations(project)
        declared = declarations[project]
        directories = [re.sub(r'^\d+[-_ ]*', '', p.casefold()) for p in relative.parts[:-1]]
        process = (path.name.casefold() in PROCESS_FILES or path.name.casefold().endswith('-output.json')
                   or any(p in PROCESS_DIRS or p.startswith(('native-', 'preview-', 'tmp-', 'temp-')) for p in directories))
        category = 'deliverable' if path in declared else 'process' if process else 'project'
        project_label = str(project.relative_to(root)) if project != root else root.name
        found.append({**entry, 'category': category, 'project': project_label,
                      'display_name': declared.get(path) or (bundle.name if bundle else entry.get('name', path.name)),
                      'type': 'draft' if bundle else entry.get('type'), 'bundle': bool(bundle)})
    return found


def discover(workspace, sources=()):
    root = Path(workspace).resolve()
    known_sources = {Path(item['path']).resolve() for item in sources}
    folder_sources = {Path(item['path']).resolve() for item in sources if item.get('kind') == 'folder'} - {root}
    found = []
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        directories[:] = sorted(name for name in directories if name not in SKIP
                                 and not name.startswith('.') and not (parent / name).is_symlink()
                                 and not (parent / name).is_junction() and (parent / name).resolve() not in folder_sources)
        if 'draft_content.json' in files:
            directories[:] = []
            files = ['draft_content.json']  # A native editable draft is one directory bundle.
        for name in sorted(files):
            path = parent / name
            if name.startswith('.') or path.is_symlink() or path.is_junction():
                continue
            kind = (MEDIA_TYPES.get(path.suffix.lower()) or TEXT_TYPES.get(path.suffix.lower())
                    or FILE_TYPES.get(path.suffix.lower()))
            if kind is None or path.resolve() in known_sources:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            identity = hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:24]
            found.append({'id': identity, 'name': name, 'path': str(path), 'size': size,
                          'type': 'draft' if name == 'draft_content.json' else kind,
                          'preview_kind': ('file' if path.suffix.lower() in FILE_TYPES
                                           else MEDIA_TYPES.get(path.suffix.lower(), 'text')),
                          'url': '/api/artifacts/' + identity})
    return categorize(root, found)


def registered_path(workspace, entries, identity):
    entry = next((item for item in entries if item['id'] == identity), None)
    if entry is None:
        raise ValueError('成果不存在或尚未刷新。')
    path = Path(entry['path']).resolve()
    if not path.is_relative_to(Path(workspace).resolve()) or not path.is_file():
        raise ValueError('成果路径已失效或超出当前工作区。')
    if path.suffix.lower() not in MEDIA_TYPES | TEXT_TYPES | FILE_TYPES:
        raise ValueError('此类型不支持在工作台预览。')
    return entry, path


def read_text(path):
    with path.open('rb') as stream:
        data = stream.read(524289)
    if len(data) > 524288:
        raise ValueError('文件较大，请打开所在目录查看。')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('该成果不是UTF-8文本，请打开所在目录查看。') from exc
