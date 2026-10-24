"""Opt-in, draft-owned rectangle mask resource with authenticated red-coverage output."""
import copy
import hashlib
import json
from pathlib import Path
import shutil

MODE = 'red_coverage_v1'
RELATIVE = Path('resources/rectangle-mask-red-coverage-v1')
IDENTITY = RELATIVE.with_suffix('.identity.json')


def contract():
    return json.loads((Path(__file__).resolve().parents[1] / 'assets/mask-red-coverage-v1.json').read_text(encoding='utf-8'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree(root):
    """Do not follow links or accept undeclared companion scripts."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError('Mask resource must be a regular directory, not a link')
    result = {}
    def visit(directory):
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError('Mask resource contains a link: ' + str(path))
            if path.is_dir():
                visit(path)
            elif path.is_file():
                result[path.relative_to(root).as_posix()] = sha(path)
            else:
                raise ValueError('Unsupported mask resource entry: ' + str(path))
    visit(root)
    return result


def check_runtime():
    import editor
    spec = contract()
    install = editor.jianying_install()
    if not install or install.name != spec['runtime_version']:
        raise ValueError('red_coverage_v1 requires the measured Jianying ' + spec['runtime_version'])
    for relative, expected in spec['runtime_sha256'].items():
        path = install / relative
        if not path.is_file() or sha(path) != expected:
            raise ValueError('Mask runtime identity changed: ' + str(path))


def requested(item):
    return item.get('mask', {}).get('rendering') == MODE


def validate_spec(item, plan):
    spec = item['mask']
    if spec.get('rendering') != MODE or spec['name'] != '矩形':
        raise ValueError('red_coverage_v1 supports rectangle masks only')
    if (plan.get('composites') or item['fit'] != 'contain' or item['speed'] != 1 or
            item['transform'] or any(item.get(key) for key in ('keyframes','animations','effects','filters','grading','matting_cache','background','transition')) or
            'speed_curve' in item or any(k['property'] != 'feather' for k in spec.get('keyframes', []))):
        raise ValueError('red_coverage_v1 requires standalone contain speed=1 media without clip motion/effects; mask feather keys only')
    if spec['feather'] > 20 or any(k['value'] > 20 for k in spec.get('keyframes', [])):
        raise ValueError('red_coverage_v1 admits feather 0..20 only')
    if spec.get('round_corner', 0) != 0:
        raise ValueError('red_coverage_v1 rounded corners are unverified')


def source_path(user_data=None):
    import native_advanced
    path = Path(native_advanced.cached_material('rectangle_mask', user_data)['path'])
    if tree(path) != contract()['source_files']:
        raise ValueError('Complete rectangle mask source identity changed or companion files are missing/added')
    return path


def derived_files():
    spec = contract()
    files = dict(spec['source_files'])
    files[spec['fragment']] = spec['patched_sha256']
    return files


def identity():
    return {'schema_version': 1, 'rendering': MODE, 'files': derived_files()}


def verify_directory(root):
    root = Path(root)
    parent = root / RELATIVE.parent
    if not parent.is_dir() or parent.is_symlink() or parent.is_junction():
        raise ValueError('Draft resource parent must not be a link')
    metadata = root / IDENTITY
    if (not metadata.is_file() or metadata.is_symlink() or
            json.loads(metadata.read_text(encoding='utf-8')) != identity() or tree(root / RELATIVE) != derived_files()):
        raise ValueError('Draft-owned mask identity mismatch, missing or added file: ' + str(root))


def prepare(content, plan, physical_root, final_root):
    if not any(requested(item) for item in plan['clips']):
        return
    source = source_path()
    target = Path(physical_root) / RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    spec = contract()
    path = target / spec['fragment']
    text = path.read_text(encoding='utf-8')
    if text.count(spec['before']) != 1:
        raise ValueError('Expected unique mask fragment expression')
    path.write_text(text.replace(spec['before'], spec['after'], 1), encoding='utf-8')
    (Path(physical_root) / IDENTITY).write_text(json.dumps(identity(), indent=2), encoding='utf-8')
    verify_directory(physical_root)
    if tree(source) != spec['source_files']:
        raise ValueError('Source resource changed during mask derivation')
    for mask in selected_masks(content, plan):
        mask['path'] = str(Path(final_root).resolve() / RELATIVE)


def selected_masks(content, plan):
    materials = {m['id']: m for m in content['materials'].get('common_mask', [])}
    segments = {(t['name'],s['target_timerange']['start']):s for t in content['tracks'] for s in t['segments']}
    for item in plan['clips']:
        if requested(item):
            segment = segments[item['track'],round(item['start']*1e6)]
            choices = [materials[r] for r in segment['extra_material_refs'] if r in materials]
            if len(choices) != 1:
                raise ValueError('Derived mask binding must be unique')
            yield choices[0]


def expected_resource(path, physical_root=None, final_root=None):
    path = Path(path)
    # Callers with a known draft root require the exact draft-owned location.
    final = Path(final_root).resolve() if final_root is not None else path.parent.parent.resolve()
    if not path.is_absolute() or path != final / RELATIVE:
        raise ValueError('Derived mask is outside its expected draft resource path')
    physical = Path(physical_root) if physical_root is not None else final
    verify_directory(physical)
    measured = json.loads((Path(__file__).resolve().parents[1] / 'assets/native-extensions-11.5.json').read_text(encoding='utf-8'))
    result = copy.deepcopy(measured['rectangle_mask'])
    result.pop('cache_relative')
    result.pop('file_sha256')
    result['path'] = str(path)
    return result


def relocate(content, plan, source_root, destination_root):
    verify_directory(source_root)
    expected = str(Path(source_root).resolve() / RELATIVE)
    for mask in selected_masks(content, plan):
        if mask['path'] != expected:
            raise ValueError('Unexpected source path for derived mask publication')
        mask['path'] = str(Path(destination_root).resolve() / RELATIVE)


def preview_check(content, root):
    """Fail before the SDK can silently fall back on a missing derived resource."""
    for mask in content.get('materials', {}).get('common_mask', []):
        if Path(mask.get('path', '')).name == RELATIVE.name:
            expected_resource(mask['path'], root, root)
