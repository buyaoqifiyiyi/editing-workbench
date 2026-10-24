"""Static local fonts copied into generated drafts as verified dependencies."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import stat


SUFFIXES = {'.otf', '.ttf'}
VARIABLE_TABLES = {'CFF2', 'fvar', 'gvar'}
MAX_BYTES = 100 * 1024 * 1024
ROOT = 'Resources/local-fonts'


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def regular_font(path):
    path = Path(path)
    require(path.is_absolute(), 'Local font path must be absolute')
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError('Local font file is missing or unreadable: ' + str(path)) from error
    require(stat.S_ISREG(info.st_mode) and not linked(path),
            'Local font must be a non-symlink regular file')
    require(path.suffix.lower() in SUFFIXES, 'Local font must be a standalone TTF or OTF file')
    require(12 <= info.st_size <= MAX_BYTES, 'Local font size is invalid')
    return path.resolve(strict=True)


def inspect(path):
    source = regular_font(path)
    raw = source.read_bytes()
    require(len(raw) == source.stat().st_size, 'Local font changed while reading')
    require(raw[:4] in {b'OTTO', b'\x00\x01\x00\x00'},
            'Unsupported local font signature; font collections are not accepted')
    try:
        from fontTools.ttLib import TTFont
    except ImportError as error:
        raise ValueError('Local font support requires fonttools==4.60.2') from error
    try:
        with TTFont(BytesIO(raw), lazy=False, checkChecksums=0,
                    ignoreDecompileErrors=False) as font:
            variable = VARIABLE_TABLES.intersection(font.keys())
            require(not variable, 'Variable local fonts are not supported: ' + ', '.join(sorted(variable)))
            for tag in ('head', 'hhea', 'maxp', 'hmtx', 'cmap', 'name'):
                require(tag in font, 'Local font is missing required table: ' + tag)
                font[tag]
            if raw[:4] == b'OTTO':
                require('CFF ' in font, 'OTF local font must contain a static CFF table')
                cff = font['CFF '].cff
                require(cff.major == 1, 'Only static CFF OTF fonts are supported')
                require(len(cff.topDictIndex[0].CharStrings) == font['maxp'].numGlyphs,
                        'Local OTF glyph count is inconsistent')
            else:
                require('loca' in font and 'glyf' in font, 'TTF local font has no outline tables')
                font['loca']
                font['glyf']
            glyph_count = font['maxp'].numGlyphs
            require(type(glyph_count) is int and glyph_count > 0
                    and len(font.getGlyphOrder()) == glyph_count,
                    'Local font glyph inventory is invalid')
            names = font['name']
            postscript_name = names.getDebugName(6) or ''
            require(postscript_name, 'Local font has no PostScript name')
            family = names.getBestFamilyName() or ''
            style = names.getBestSubFamilyName() or ''
    except ValueError:
        raise
    except Exception as error:
        raise ValueError('Cannot parse local font ' + str(source) + ': ' + str(error)) from error
    require(digest(source) == hashlib.sha256(raw).hexdigest(), 'Local font changed while reading')
    return {
        'source': str(source),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'size': len(raw),
        'format': source.suffix.lower().lstrip('.'),
        'postscript_name': postscript_name,
        'family': family,
        'style': style,
        'glyph_count': glyph_count,
    }


def inspect_plan(plan):
    assets = {}
    for item in plan.get('captions', []):
        if 'font_path' not in item:
            continue
        source = item['font_path']
        asset = inspect(source)
        previous = assets.get(source)
        if previous is not None:
            require(previous['sha256'] == asset['sha256'],
                    'Local font changed while validating captions')
            continue
        asset['captions'] = []
        assets[source] = asset
    for index, item in enumerate(plan.get('captions', [])):
        if 'font_path' in item:
            assets[item['font_path']]['captions'].append(index)
    return assets


def relative(asset):
    return ROOT + '/' + asset['sha256'] + '.' + asset['format']


def path_for(root, asset):
    return (Path(root) / Path(relative(asset))).resolve()


def copy_assets(assets, root):
    root = Path(root)
    for asset in assets.values():
        destination = path_for(root, asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            require(destination.is_file() and not destination.is_symlink()
                    and destination.stat().st_size == asset['size']
                    and digest(destination) == asset['sha256'],
                    'Existing local font dependency changed')
            continue
        with Path(asset['source']).open('rb') as source, destination.open('xb') as stream:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        require(destination.stat().st_size == asset['size']
                and digest(destination) == asset['sha256']
                and digest(asset['source']) == asset['sha256'],
                'Local font changed while copying')


def manifest(assets):
    result = []
    seen = set()
    for asset in sorted(assets.values(), key=relative):
        asset.setdefault('relative', relative(asset))
        if asset['relative'] in seen:
            continue
        seen.add(asset['relative'])
        result.append({key: asset[key] for key in
                       ('source', 'relative', 'sha256', 'size', 'format', 'postscript_name',
                        'family', 'style', 'glyph_count')})
    return result


def expected_font(asset, root):
    return {'id': '', 'path': path_for(root, asset).as_posix()}


def bind_material(material, asset, root):
    content = json.loads(material.get('content', '{}'))
    require(isinstance(content, dict) and isinstance(content.get('styles'), list)
            and content['styles'] and all(isinstance(style, dict) for style in content['styles']),
            'Local font text content/style is invalid')
    font = expected_font(asset, root)
    for style in content['styles']:
        style['font'] = dict(font)
    material['font_path'] = font['path']
    material.pop('font_id', None)
    material['content'] = json.dumps(content, ensure_ascii=False, separators=(',', ':'))
    return material


def verify_files(assets, root):
    expected = set()
    folder = Path(root) / ROOT
    if assets:
        require(folder.is_dir() and not linked(folder), 'Local font resource directory is missing')
    for asset in assets.values():
        location = path_for(root, asset)
        require(location.is_file() and not linked(location)
                and location.stat().st_size == asset['size']
                and digest(location) == asset['sha256'],
                'Copied local font bytes changed: ' + relative(asset))
        expected.add(relative(asset))
    if folder.exists():
        actual = set()
        for item in folder.rglob('*'):
            require(not linked(item), 'Local font resource tree contains a symlink or junction')
            if item.is_file():
                actual.add((Path(ROOT) / item.relative_to(folder)).as_posix())
        require(actual == expected, 'Local font resource inventory differs from the plan')


def _material_by_caption(content, plan):
    materials = {item['id']: item for item in content.get('materials', {}).get('texts', [])
                 if isinstance(item, dict) and item.get('id')}
    segments = {(track['name'], segment['target_timerange']['start']): segment
                for track in content.get('tracks', []) for segment in track.get('segments', [])}
    result = []
    for item in plan.get('captions', []):
        if 'font_path' not in item:
            continue
        start = round(item['start'] * 1_000_000)
        segment = segments.get((item['track'], start))
        require(segment is not None, 'Local font text segment is missing')
        material = materials.get(segment.get('material_id'))
        require(material is not None, 'Local font text material is missing')
        result.append((item, material))
    return result


def verify_bindings(content, plan, root, assets=None, file_root=None):
    assets = inspect_plan(plan) if assets is None else assets
    verify_files(assets, root if file_root is None else file_root)
    count = 0
    for item, material in _material_by_caption(content, plan):
        asset = assets[item['font_path']]
        font = expected_font(asset, root)
        require(material.get('font_path') == font['path'], 'Local font top-level path mismatch')
        content_data = json.loads(material.get('content', '{}'))
        actual = [style.get('font') for style in content_data.get('styles', []) if 'font' in style]
        require(actual == [font] * len(actual), 'Local font rich-text path mismatch')
        count += 1
    return count


def preview_check(content, root):
    """Validate draft-local font references without reopening the original source font."""
    root = Path(root).resolve()
    referenced = set()
    count = 0
    for material in content.get('materials', {}).get('texts', []):
        top = material.get('font_path')
        data = json.loads(material.get('content', '{}'))
        styles = data.get('styles', [])
        fonts = [style.get('font') for style in styles if isinstance(style, dict) and 'font' in style]
        paths = ([top] if isinstance(top, str) and top else []) + [
            font.get('path') for font in fonts if isinstance(font, dict) and isinstance(font.get('path'), str)]
        local_paths = []
        for raw in paths:
            path = Path(raw).resolve()
            try:
                relative_path = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative_path.startswith(ROOT + '/'):
                local_paths.append(relative_path)
        if not local_paths:
            continue
        require(isinstance(top, str) and top, 'Local font top-level path is missing')
        top_path = Path(top).resolve()
        require(top_path.is_relative_to(root)
                and top_path.relative_to(root).as_posix() in local_paths,
                'Local font top-level path is outside the draft font directory')
        require(fonts and all(isinstance(font, dict) and font.get('id') in ('', None)
                              and font.get('path') == top
                              for font in fonts),
                'Local font rich-text binding differs from the material path')
        referenced.add(top_path.relative_to(root).as_posix())
        count += 1
    manifest_path = root / '_skill_manifest.json'
    if not referenced:
        if manifest_path.is_file():
            record = json.loads(manifest_path.read_text(encoding='utf-8'))
            require(not record.get('local_fonts'), 'Local font manifest has no matching materials')
        return 0
    require(manifest_path.is_file(), 'Local font draft requires its generated manifest')
    record = json.loads(manifest_path.read_text(encoding='utf-8'))
    entries = record.get('local_fonts')
    require(isinstance(entries, list), 'Local font manifest inventory is missing')
    require(bool(entries) == bool(referenced), 'Local font manifest and materials disagree')
    assets = {entry['relative']: entry for entry in entries}
    verify_files(assets, root)
    require(referenced == set(assets), 'Local font material and manifest inventory differ')
    return count


def rebase(content, plan, target_root, assets=None):
    assets = inspect_plan(plan) if assets is None else assets
    for item, material in _material_by_caption(content, plan):
        bind_material(material, assets[item['font_path']], target_root)
    return content
