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
NATIVE_PROJECT_TYPES = {'.fcproj': 'FilmCraft工程', '.ecproj': 'EffectCraft工程',
                        '.otio': '时间轴交换文件', '.fcpxml': 'FCPXML时间轴'}
FILE_TYPES = {**NATIVE_PROJECT_TYPES, '.avi': '视频中间素材'}
SKIP = {'.git', '.venv', 'venv', 'node_modules', 'engine', 'Skill维护层', '运行数据', '__pycache__',
        'assets', 'frames', '素材', 'input', 'inputs', 'media', 'media-manifests'}
PROCESS_DIRS = {'scout', 'details', 'graphics', 'frames', 'preview', 'previews', 'overlays', 'rhythm',
                'cache', 'caches', 'tmp', 'temp', 'logs', '勘景', '抽帧', '预览', '缓存', '临时', '日志'}
PROCESS_FILES = {'media.json', 'sections.json', 'acceptance.json', 'verification.json', 'audio-levels.txt'}
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
    parents = {parent for _, path in paths for parent in path.parents if parent.is_relative_to(root)}
    manifest_roots = sorted((p for p in parents if (p / DELIVERY_MANIFEST).is_file()),
                            key=lambda p: len(p.parts), reverse=True)
    declarations, found, seen = {}, [], set()
    for entry, path in paths:
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
                   or any(p in PROCESS_DIRS or p.startswith(('preview-', 'tmp-', 'temp-')) for p in directories))
        category = 'deliverable' if path in declared else 'process' if process else 'project'
        project_label = str(project.relative_to(root)) if project != root else root.name
        found.append({**entry, 'category': category, 'project': project_label,
                      'display_name': declared.get(path) or entry.get('name', path.name),
                      'type': entry.get('type'), 'bundle': False})
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
        for name in sorted(files):
            path = parent / name
            if name.startswith('.') or path.is_symlink() or path.is_junction():
                continue
            ext = path.suffix.lower()
            if ext not in MEDIA_TYPES and ext not in TEXT_TYPES and ext not in FILE_TYPES:
                continue
            if path.resolve() in known_sources:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            kind = MEDIA_TYPES.get(ext) or ('text' if ext in TEXT_TYPES else 'file')
            relative = path.relative_to(root).as_posix()
            item_id = hashlib.sha256(relative.encode()).hexdigest()[:16]
            found.append({
                'id': item_id,
                'name': relative,
                'path': str(path.resolve()),
                'type': kind,
                'type_label': FILE_TYPES.get(ext) or TEXT_TYPES.get(ext, kind),
                'bytes': stat.st_size,
                'modified': stat.st_mtime,
            })
    return categorize(root, found)


def resolve_file(workspace, target, allow_text=True):
    root = Path(workspace).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root / candidate
    for p in (candidate, *candidate.parents):
        if p == root:
            break
        if p.is_symlink() or p.is_junction():
            raise ValueError('不允许访问工作区外的链接文件。')
    path = candidate.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('只能预览当前工作区内的文件。')
    ext = path.suffix.lower()
    if ext not in MEDIA_TYPES and (not allow_text or ext not in TEXT_TYPES):
        raise ValueError('该文件类型暂不支持在页面直接预览。')
    return path, MEDIA_TYPES.get(ext, 'text')


def read_text(path):
    raw = Path(path).read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError('文本文件超过 512 KB，请直接用编辑器打开。')
    return raw.decode('utf-8-sig')
