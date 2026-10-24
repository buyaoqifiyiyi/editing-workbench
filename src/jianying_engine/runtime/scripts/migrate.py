"""Read-only migration diagnosis and explicit root mapping for new edit plans."""
import argparse
import copy
import ntpath
import os
from pathlib import Path, PureWindowsPath
import sys

import editor


def mappings(values):
    result, seen = [], set()
    for value in values:
        old, separator, new = value.partition('=')
        if not separator or not PureWindowsPath(old).is_absolute() or not PureWindowsPath(new).is_absolute():
            raise ValueError('--map must be an absolute Windows old-root=new-root pair')
        source = PureWindowsPath(ntpath.normpath(old))
        destination = Path(new).resolve()
        identity = str(source).casefold()
        if identity in seen:
            raise ValueError('Duplicate old root mapping; choose one destination explicitly')
        if not destination.is_dir():
            raise ValueError(f'New root does not exist: {destination}')
        seen.add(identity)
        result.append((source, destination))
    return sorted(result, key=lambda pair: len(pair[0].parts), reverse=True)


def map_path(value, plan_directory, roots):
    source = PureWindowsPath(ntpath.normpath(value))
    if source.is_absolute():
        for old, new in roots:
            # Path components, not string prefix or basename: D:\media != D:\media2.
            if tuple(p.casefold() for p in source.parts[:len(old.parts)]) == tuple(p.casefold() for p in old.parts):
                return new.joinpath(*source.parts[len(old.parts):]).resolve()
        return Path(str(source)).resolve()
    if source.drive or source.root:
        raise ValueError(f'Ambiguous drive-relative or root-relative media path: {value}')
    return plan_directory.joinpath(*source.parts).resolve()


def remap_plan(plan_path, roots):
    plan_path = Path(plan_path).resolve()
    original = editor.read_json(plan_path)
    if not isinstance(original, dict) or original.get('schema_version') != 1 or not isinstance(original.get('clips'), list):
        raise ValueError('Only schema_version=1 edit-plan.json is supported; encrypted drafts are not modified')
    plan = copy.deepcopy(original)
    changes, missing = [], []
    for group in ('clips', 'audio'):
        items = plan.get(group, [])
        if not isinstance(items, list):
            raise ValueError(f'{group} must be an array')
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path']:
                raise ValueError(f'{group}[{index}].path must be a nonempty local path')
            old = item['path']
            if '://' in old or '\x00' in old:
                raise ValueError('Remote URLs and NUL are not media paths')
            new = map_path(old, plan_path.parent, roots)
            item['path'] = str(new)
            if old != str(new):
                changes.append({'item': f'{group}[{index}]', 'from': old, 'to': str(new)})
            if not new.is_file():
                missing.append({'item': f'{group}[{index}]', 'old': old, 'expected': str(new)})
            # Nested local files resolve relative to the plan too; remap them or the moved plan breaks.
            for parent, key in (('denoise', 'model_path'), ('matting_cache', 'manifest')):
                spec = item.get(parent)
                if not isinstance(spec, dict) or not isinstance(spec.get(key), str) or not spec[key]:
                    continue
                label, nested = f'{group}[{index}].{parent}.{key}', spec[key]
                if '://' in nested or '\x00' in nested:
                    raise ValueError(f'{label} must be a local path')
                target = map_path(nested, plan_path.parent, roots)
                spec[key] = str(target)
                if nested != str(target):
                    changes.append({'item': label, 'from': nested, 'to': str(target)})
                if not target.is_file():
                    missing.append({'item': label, 'old': nested, 'expected': str(target)})
                if parent == 'matting_cache':
                    changes.append({'item': label, 'note': 'manifest cache_root and sha256 are machine-bound; re-admit the cache on the new device'})
    captions = plan.get('captions', [])
    if not isinstance(captions, list):
        raise ValueError('captions must be an array')
    for index, item in enumerate(captions):
        if not isinstance(item, dict) or 'font_path' not in item:
            continue
        old = item['font_path']
        if not isinstance(old, str) or not old or '://' in old or '\x00' in old:
            raise ValueError(f'captions[{index}].font_path must be a nonempty local path')
        new = map_path(old, plan_path.parent, roots)
        item['font_path'] = str(new)
        if old != str(new):
            changes.append({'item': f'captions[{index}].font_path', 'from': old, 'to': str(new)})
        if not new.is_file() or new.suffix.lower() not in {'.ttf', '.otf'}:
            missing.append({'item': f'captions[{index}].font_path',
                            'old': old, 'expected': str(new)})
    return plan, changes, missing


def execute(args):
    environment = editor.doctor()
    report = {'environment': environment, 'source_modified': False,
              'draft_modified': False, 'path_changes': [], 'missing_media': [],
              'plan_validation': 'not_run', 'ui_compatibility': 'unverified'}
    if not args.plan:
        if args.map or args.out:
            raise ValueError('--map/--out require --plan')
        report['status'] = 'environment_checked'
        return report
    if os.name != 'nt':
        raise ValueError('Plan migration currently supports Windows paths only')
    plan, changes, missing = remap_plan(args.plan, mappings(args.map))
    report.update(path_changes=changes, missing_media=missing,
                  status='needs_media_mapping' if missing else 'paths_ready', output_plan=None)
    if missing:
        report['instructions'] = ['保留目录层级搬迁素材，提供明确--map 旧根=新根；不按文件名猜测或搜索全盘。',
                                  '缺失素材未解决前不会写入迁移计划；不能将路径检查当作素材内容或剪映兼容验证。']
    elif args.out:
        out = Path(args.out).resolve()
        # Exclusive creation protects the source plan, existing drafts and media.
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream:
            stream.write(editor.json.dumps(plan, ensure_ascii=False, indent=2))
        report.update(status='new_plan_written', output_plan=str(out))
        report['instructions'] = ['依次运行editor.py validate和build，仅新建版本；保留素材来源sidecar。',
                                  '已有加密/手工工程的重新链接和版本兼容另行核验，不自动修改已有草稿。']
        if any(item.get('mask', {}).get('rendering') == 'red_coverage_v1' for item in plan['clips']):
            report['instructions'].append('矩形羽化模式已保留；目标机须有已核11.6运行时及原矩形缓存，重新build会在新草稿内创建派生资源。此操作只迁移计划，不搬已有草稿。')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', help='Source edit-plan.json; never modified')
    parser.add_argument('--map', action='append', default=[], help='Explicit old-root=new-root; longest matching root wins')
    parser.add_argument('--out', help='New plan path; refuse any existing file, and write only when all media exist')
    args = parser.parse_args()
    try:
        result = execute(args)
        editor.emit(result)
        return 2 if result['missing_media'] or not result['environment']['ready'] else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_modified': False, 'draft_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
