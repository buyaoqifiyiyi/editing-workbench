"""Read-only lookup of inspected template projects; never imports or edits drafts."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import zipfile
import runtime_paths

def data_directory():
    """Machine-specific template state stays outside the portable Skill."""
    value = os.environ.get('JIANYING_TEMPLATE_DATA_DIR')
    path = Path(value).expanduser() if value else Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'JianyingEditor' / 'template-data'
    return runtime_paths.external_directory(path, 'JIANYING_TEMPLATE_DATA_DIR')


INDEX = data_directory() / 'template-index.json'


def read_index():
    if not INDEX.exists():
        raise ValueError(f'Template index not found: {INDEX}; restore the external data or set JIANYING_TEMPLATE_DATA_DIR. '
                         'A new library can be created with verify_templates.py --draft SOURCE --out NEW_FOLDER --register')
    return json.loads(INDEX.read_text(encoding='utf-8-sig'))


def search(projects, query='', project_id=None):
    terms = query.casefold().split()
    return [p for p in projects if (project_id is None or p['id'] == project_id)
            and all(term in json.dumps([p['title'], p['features'], p['effect_names'], p['animation_names']],
                                       ensure_ascii=False).casefold() for term in terms)]


def decoded_name(info):
    if info.flag_bits & 0x800:
        return info.filename.replace('\\', '/')
    try:
        return info.filename.encode('cp437').decode('gbk').replace('\\', '/')
    except UnicodeError:
        return info.filename.replace('\\', '/')


def verify(project, root):
    results = []
    for source in project['sources']:
        path = Path(source.get('root', root)) / source['path'].replace('\\', '/')
        try:
            if source['kind'] == 'folder':
                raw = path.read_bytes()
            else:
                with zipfile.ZipFile(path) as archive:
                    member = next(i for i in archive.infolist() if decoded_name(i) == source['member'])
                    raw = archive.read(member)
            digest = hashlib.sha256(raw).hexdigest()
            results.append({'source': source, 'status': 'unchanged' if digest == project['sha256'] else 'changed',
                            'sha256': digest})
        except (OSError, StopIteration, zipfile.BadZipFile, RuntimeError) as error:
            results.append({'source': source, 'status': 'unavailable', 'error': str(error) or 'ZIP member not found'})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--search', default='', help='All space-separated terms must match title or observed features/effects')
    parser.add_argument('--id', help='Exact TPL identifier')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--verify-source', action='store_true', help='Read selected draft JSON and compare its SHA256')
    parser.add_argument('--source-root', type=Path, help='Override the original template folder after migration')
    args = parser.parse_args()
    if not 1 <= args.limit <= 2000:
        parser.error('--limit must be between 1 and 2000')
    sys.stdout.reconfigure(encoding='utf-8')
    index = read_index()
    matches = search(index['projects'], args.search, args.id)
    import verify_templates
    registry = json.loads(verify_templates.REGISTRY.read_text(encoding='utf-8')) if verify_templates.REGISTRY.exists() else {}
    environment = verify_templates.fingerprint() if registry else None
    selected = [dict(p, backend_admission=verify_templates.admission(p, registry, environment))
                for p in matches[:args.limit]]
    if args.verify_source:
        root = args.source_root or Path(index['source_root'])
        selected = [dict(p, source_verification=verify(p, root)) for p in selected]
    print(json.dumps({'checked_date': index['checked_date'], 'source_root': str(args.source_root or index['source_root']),
                      'matches': len(matches), 'projects': selected}, ensure_ascii=False, indent=2))
    if args.id and not matches:
        return 1
    if args.verify_source and any(s['status'] != 'unchanged' for p in selected for s in p['source_verification']):
        return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
