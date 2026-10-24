"""Inspect verified template dependencies and rebuild selected timing/motion in an ordinary edit plan.

No raw track import, original media extraction, UI automation or draft publication.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import sys
import tempfile
import zipfile

import templates
import editor


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, obj):
    """Atomic replace through a same-directory temporary file; readers never see a partial JSON."""
    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            output.write(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def source_path(source, root):
    """Same locator rule as templates.verify: per-source root override, '\\' separators accepted."""
    return Path(source.get('root', root)) / source['path'].replace('\\', '/')


def load(project, root):
    """Return the first source whose bytes match the indexed SHA256.

    A locator escaping its root is refused (never read) and recorded like any other unusable
    alias, so a valid alternate source can still be used.
    """
    failures = []
    for source in project['sources']:
        source_root = Path(source.get('root', root))
        path = source_path(source, root).resolve()
        if not path.is_relative_to(source_root.resolve()):
            failures.append({'source': source, 'status': 'refused', 'error': 'Source locator escapes the source root'})
            continue
        try:
            if source['kind'] == 'folder':
                raw = path.read_bytes()
            else:
                with zipfile.ZipFile(path) as archive:
                    raw = archive.read(next(i for i in archive.infolist()
                                            if templates.decoded_name(i) == source['member']))
            if hashlib.sha256(raw).hexdigest() != project['sha256']:
                failures.append({'source': source, 'status': 'changed'})
                continue
            return json.loads(raw.decode('utf-8-sig')), source
        except (OSError, StopIteration, zipfile.BadZipFile, RuntimeError) as error:
            failures.append({'source': source, 'status': 'unavailable', 'error': str(error) or 'ZIP member not found'})
    raise ValueError('No unchanged template source: ' + json.dumps(failures, ensure_ascii=False))


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from strings(value)


def tables(data):
    materials, keys, segments = {}, {}, {}
    for category, items in (data.get('materials') or {}).items():
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get('id'):
                    entry = {'category': category, 'data': item}
                    if item['id'] not in materials:
                        materials[item['id']] = entry
                    elif entry != {k: v for k, v in materials[item['id']].items() if k != 'alternatives'}:
                        alternatives = materials[item['id']].setdefault('alternatives', [])
                        if entry not in alternatives:
                            alternatives.append(entry)
    for items in (data.get('keyframes') or {}).values():
        if isinstance(items, list):
            for key in items:
                ref = key['id']
                if ref not in keys:
                    keys[ref] = key
                elif keys[ref] != key:
                    if 'conflicting_variants' not in keys[ref]:
                        keys[ref] = {'id': ref, 'conflicting_variants': [keys[ref]]}
                    if key not in keys[ref]['conflicting_variants']:
                        keys[ref]['conflicting_variants'].append(key)
    for ti, track in enumerate(data.get('tracks') or []):
        for si, segment in enumerate(track.get('segments') or []):
            segments[f'{ti}:{si}'] = (track, segment)
    return materials, keys, segments


def dependencies(segment, materials):
    pending = [segment.get('material_id')] + (segment.get('extra_material_refs') or [])
    result, missing = {}, []
    while pending:
        ref = pending.pop()
        if not ref or ref in result or ref in missing:
            continue
        if ref not in materials:
            missing.append(ref)
            continue
        result[ref] = materials[ref]
        pending.extend(s for s in strings(materials[ref]) if s in materials and s not in result)
    return [entry for m in result.values() for entry in
            [{k: v for k, v in m.items() if k != 'alternatives'}] + m.get('alternatives', [])], missing


def resource_paths(obj, field=''):
    if isinstance(obj, dict):
        for key, value in obj.items():
            label = field + key
            if isinstance(value, str) and value and (key == 'path' or key.endswith('_path')):
                yield label, value
            yield from resource_paths(value, label + '.')
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from resource_paths(value, field + str(index) + '.')


def resource_locator(value, source, root, archive_names):
    root = Path(source.get('root', root))
    normalized = value.replace('\\', '/')
    relative = re.sub(r'^##_draftpath_placeholder_[^#]+_##/*', '', normalized)
    if re.match(r'^https?://', normalized):
        return {'status': 'remote', 'original': value}
    if Path(value).is_absolute() and Path(value).is_file():
        return {'status': 'exists_as_written', 'original': value}
    if source['kind'] == 'folder':
        package = source_path(source, root).resolve().parent
        candidate = (package / relative).resolve()
        if candidate.is_relative_to(package) and candidate.exists():
            return {'status': 'package_exact', 'original': value, 'path': str(candidate)}
    else:
        # A draft at the ZIP root has an empty prefix; never build a member starting with '/'.
        prefix = posixpath.dirname(source['member'].replace('\\', '/'))
        member = posixpath.join(prefix, relative) if prefix else relative
        if member in archive_names:
            return {'status': 'zip_entry', 'original': value, 'member': member}
    return {'status': 'unresolved', 'original': value}


def report(data, project, source, selections=None, root=None):
    materials, keys, segments = tables(data)
    rows = []
    archive_names = set()
    if root:
        root = Path(source.get('root', root))
    if root and source['kind'] != 'folder':
        with zipfile.ZipFile(source_path(source, root)) as archive:
            archive_names = {templates.decoded_name(i) for i in archive.infolist()}
    for locator, (track, segment) in segments.items():
        if selections is not None and locator not in selections:
            continue
        deps, missing = dependencies(segment, materials)
        refs = segment.get('keyframe_refs') or []
        rows.append({'segment': locator, 'track_type': track['type'], 'track_id': track.get('id'),
                     'original': segment, 'dependencies': deps, 'missing_material_refs': missing,
                     'ambiguous_material_refs': sorted({m['data']['id'] for m in deps
                                                        if materials[m['data']['id']].get('alternatives')}),
                     'legacy_keyframes': [keys[r] for r in refs if r in keys],
                     'missing_keyframe_refs': [r for r in refs if r not in keys],
                     'resource_paths': [{'material_id': m['data']['id'], 'field': field,
                                         **resource_locator(value, source, root, archive_names)}
                                        for m in deps for field, value in resource_paths(m['data'])]
                                       if root else []})
    return {'template_id': project['id'], 'title': project['title'], 'sha256': project['sha256'],
            'verified_source': source, 'canvas': data.get('canvas_config'), 'fps': data.get('fps'),
            'evidence': {'source_hash': 'unchanged', 'dependency_structure': 'read',
                         'ui_compatibility': 'unverified', 'audio_listening': 'unverified'},
            'segments': rows}


def finite(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if minimum is not None and value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return value


def motion(segment, keys, basis, duration):
    """Use explicitly selected time semantics; never infer it from a timestamp fitting a range."""
    if basis not in ('local', 'source_relative', 'source_absolute'):
        raise ValueError('time_basis must be local, source_relative or source_absolute')
    old_duration = finite(segment['target_timerange']['duration'] / 1e6, 'original duration', 0.000001)
    old_speed = finite(segment.get('speed', 1), 'original speed', 0.000001)
    source_in = (segment.get('source_timerange') or {}).get('start', 0) / 1e6
    def retime(microseconds):
        time = finite(microseconds, 'keyframe timestamp') / 1e6
        if basis != 'local':
            time = (time - (source_in if basis == 'source_absolute' else 0)) / old_speed
        if time < -0.000001 or time > old_duration + 0.000001:
            raise ValueError('Keyframe outside selected time basis; inspect source ranges, do not guess')
        return round(max(0, min(old_duration, time)) / old_duration * duration, 6)
    points = []
    refs = segment.get('keyframe_refs') or []
    if refs and segment.get('common_keyframes'):
        raise ValueError('Mixed modern/legacy motion needs explicit interpretation')
    for ref in refs:
        if ref not in keys:
            raise ValueError('Missing legacy keyframe: ' + ref)
        key = keys[ref]
        if key.get('conflicting_variants'):
            raise ValueError('Ambiguous legacy keyframe ID: ' + ref)
        if key.get('graph'):
            raise ValueError('Legacy graph is not a validated linear motion source')
        flags = key.get('flags')
        if not isinstance(flags, int):
            raise ValueError('Missing legacy property flags')
        # Low four bits are observed position/scale/rotation/alpha selections in this library.
        values = {}
        if flags & 1:
            values.update(position_x=key['position']['x'], position_y=key['position']['y'])
        if flags & 2:
            values.update(scale_x=key['scale']['x'], scale_y=key['scale']['y'])
        if flags & 4:
            values['rotation'] = key['rotation']
        if flags & 8:
            if key.get('alpha') is None:
                raise ValueError('Legacy alpha channel has no scalar alpha value; inspect the frame, do not infer text opacity')
            values['alpha'] = key['alpha']
        for prop, value in values.items():
            points.append({'property': prop, 'time': retime(key['time_offset']),
                           'value': finite(value, prop)})
    if refs and not points:
        raise ValueError('Legacy frames contain no supported motion channels')
    names = {'KFTypePositionX': 'position_x', 'KFTypePositionY': 'position_y',
             'KFTypeScaleX': 'scale_x', 'KFTypeScaleY': 'scale_y',
             'KFTypeRotation': 'rotation', 'KFTypeAlpha': 'alpha', 'UNIFORM_SCALE': 'uniform_scale'}
    for group in segment.get('common_keyframes') or []:
        prop = names.get(group.get('property_type'))
        if not prop:
            raise ValueError('Unsupported modern motion property: ' + str(group.get('property_type')))
        for key in group.get('keyframe_list') or []:
            if len(key.get('values') or []) != 1:
                raise ValueError('Expected a scalar modern keyframe')
            points.append({'property': prop, 'time': retime(key['time_offset']),
                           'value': finite(key['values'][0], prop)})
    points.sort(key=lambda p: (p['property'], p['time']))
    seen = set()
    for point in points:
        identity = (point['property'], round(point['time'] * 1e6))
        if identity in seen:
            raise ValueError('Duplicate motion point after duration adaptation')
        seen.add(identity)
    return points


def adapt(data, project, inspection, mapping, base, mapping_dir):
    materials, keys, segments = tables(data)
    allowed = {'segment', 'path', 'text', 'start', 'duration', 'source_in', 'speed', 'volume',
               'track', 'fit', 'time_basis', 'motion_mode', 'transform', 'size', 'color', 'bold',
               'border', 'x', 'y', 'style', 'fade_in', 'fade_out', 'native'}
    editor.fields(mapping, {'name', 'canvas', 'selections', 'allow_canvas_change'}, 'mapping', ('selections',))
    if not isinstance(mapping['selections'], list) or not mapping['selections']:
        raise ValueError('Select at least one segment')
    if base is None:
        editor.fields(mapping, set(mapping), 'mapping', ('name', 'canvas'))
        base = {'schema_version': 1, 'name': mapping['name'], 'mode': 'script',
                'canvas': mapping['canvas'], 'clips': [], 'notes': []}
    plan = copy.deepcopy(base)
    if 'canvas' in mapping and mapping['canvas'] != plan['canvas']:
        raise ValueError('With --base-plan use its canvas; change canvas in a separate original plan revision')
    if mapping.get('name'):
        plan['name'] = mapping['name']
    source_canvas = data['canvas_config']
    if (plan['canvas']['width'], plan['canvas']['height']) != (source_canvas['width'], source_canvas['height']):
        if mapping.get('allow_canvas_change') is not True:
            raise ValueError('Changed canvas requires allow_canvas_change=true and design review')
    added, omissions = [], []
    for row in mapping['selections']:
        editor.fields(row, allowed, 'selection', ('segment', 'start', 'duration', 'track'))
        if row['segment'] not in segments:
            raise ValueError('Unknown segment locator: ' + row['segment'])
        track, segment = segments[row['segment']]
        shared = {'segment', 'start', 'duration', 'track'}
        per_type = {
            'video': {'path', 'source_in', 'speed', 'volume', 'fit', 'time_basis', 'motion_mode', 'transform', 'native'},
            'audio': {'path', 'source_in', 'speed', 'volume', 'fade_in', 'fade_out'},
            'text': {'text', 'size', 'color', 'bold', 'border', 'x', 'y', 'style',
                     'time_basis', 'motion_mode', 'transform', 'native'}}
        if track['type'] not in per_type:
            raise ValueError('Only video/audio/text selections can be rebuilt')
        editor.fields(row, shared | per_type[track['type']], 'selection for ' + track['type'])
        duration = finite(row['duration'], 'duration', 0.000001)
        item = {k: row[k] for k in ('start', 'duration', 'track')}
        finite(item['start'], 'start', 0)
        if track['type'] in ('video', 'text'):
            if row.get('motion_mode') != 'linear_rebuild':
                raise ValueError('Visual selection requires motion_mode=linear_rebuild; full effects are not transplanted')
            item['keyframes'] = motion(segment, keys, row.get('time_basis'), duration)
            clip = segment.get('clip') or {}
            item['transform'] = {'scale_x': (clip.get('scale') or {}).get('x', 1),
                                 'scale_y': (clip.get('scale') or {}).get('y', 1),
                                 'rotation': clip.get('rotation', 0), 'alpha': clip.get('alpha', 1),
                                 'flip_horizontal': (clip.get('flip') or {}).get('horizontal', False),
                                 'flip_vertical': (clip.get('flip') or {}).get('vertical', False)}
            if track['type'] == 'video':
                item['transform'].update(transform_x=(clip.get('transform') or {}).get('x', 0),
                                         transform_y=(clip.get('transform') or {}).get('y', 0))
            item['transform'].update(row.get('transform', {}))
        if track['type'] in ('video', 'audio'):
            if not row.get('path'):
                raise ValueError('Replacement media path required; template media are not silently reused')
            path = Path(row['path'])
            item.update(path=str((mapping_dir / path).resolve()), source_in=row.get('source_in', 0),
                        speed=row.get('speed', 1), volume=row.get('volume', 0 if track['type'] == 'video' else 1))
            if track['type'] == 'video':
                if row.get('fit') not in ('contain', 'cover'):
                    raise ValueError('Choose replacement fit explicitly')
                item['fit'] = row['fit']
                group = 'clips'
            else:
                item.update(fade_in=row.get('fade_in', 0), fade_out=row.get('fade_out', 0))
                if segment.get('keyframe_refs') or segment.get('common_keyframes'):
                    raise ValueError('Audio automation is not supported; choose another segment or design sound independently')
                group = 'audio'
        elif track['type'] == 'text':
            editor.fields(row, allowed, 'text selection', ('text', 'size'))
            item.update(text=row['text'], size=row['size'],
                        x=row.get('x', ((segment.get('clip') or {}).get('transform') or {}).get('x', 0)),
                        y=row.get('y', ((segment.get('clip') or {}).get('transform') or {}).get('y', 0)))
            for k in ('color', 'bold', 'border', 'style'):
                if k in row:
                    item[k] = row[k]
            if any(k['property'] == 'alpha' for k in item['keyframes']):
                raise ValueError('Text alpha motion unsupported; inspect and reconstruct with supported text animations')
            group = 'captions'
        else:
            raise ValueError('Select video/audio/text for rebuilding; other dependencies stay in the research report')
        native = row.get('native', {})
        native_fields = {'effects', 'filters', 'animations', 'transition', 'mix_mode', 'chroma', 'background', 'mask'}
        editor.fields(native, native_fields if track['type'] == 'video' else
                      {'animations'} if track['type'] == 'text' else set(), 'native reconstruction')
        if native.get('mask') and native['mask'].get('name') != '矩形':
            raise ValueError('Non-rectangle masks remain suspended research')
        item.update(copy.deepcopy(native))
        plan.setdefault(group, []).append(item)
        detail = next(r for r in inspection['segments'] if r['segment'] == row['segment'])
        omitted = {'segment': row['segment'], 'not_transplanted': [m['category'] for m in detail['dependencies']],
                   'scope': 'Only timing and explicit replacement settings; visual selections also rebuild linear motion. '
                            'Original crop, effects, animation presets, masks, algorithms, grading, font and other fields are not copied.',
                   'missing_material_refs': detail['missing_material_refs'],
                   'ambiguous_material_refs': detail['ambiguous_material_refs'],
                   'native_reconstructed_from_explicit_settings': native}
        omissions.append(omitted)
        added.append({'segment': row['segment'], 'group': group, 'index': len(plan[group]) - 1,
                      'keyframe_points': len(item.get('keyframes', [])),
                      'time_basis': row.get('time_basis'), 'motion_mode': row.get('motion_mode'),
                      'original_source_range': segment.get('source_timerange'),
                      'original_target_range': segment['target_timerange']})
        plan.setdefault('notes', []).append(f"借鉴 {project['id']} {row['segment']}：重新选择素材/内容与时长；"
                                           '画面运动按所选时间基准线性重建；未迁移原裁切、资源效果、算法、字体或缓动。')
    if 'layer_order' in plan:
        for group in ('clips', 'audio', 'captions'):
            for item in plan.get(group, []):
                if item.get('track') not in plan['layer_order']:
                    plan['layer_order'].append(item['track'])
    return plan, {'source': {'id': project['id'], 'sha256': project['sha256']},
                  'added': added, 'omissions': omissions, 'plan_validation': 'pending',
                  'ui_compatibility': 'unverified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--mapping', type=Path, help='Replacement and timing choices; without it only inspect')
    parser.add_argument('--base-plan', type=Path, help='Append to a validated original plan, saved as a new version')
    parser.add_argument('--out', type=Path, required=True, help='New output directory; never overwritten')
    args = parser.parse_args()
    index = templates.read_index()
    matches = templates.search(index['projects'], project_id=args.id)
    if not matches:
        raise ValueError('Unknown template ID')
    root = args.source_root or Path(index['source_root'])
    data, source = load(matches[0], root)
    mapping = read(args.mapping) if args.mapping else None
    if args.base_plan and not mapping:
        raise ValueError('--base-plan requires --mapping')
    inspection = report(data, matches[0], source,
                        {r['segment'] for r in mapping['selections']} if mapping else None, root)
    plan = adaptation = None
    if mapping:
        import verify_templates
        verify_templates.require_admission(matches[0], mapping['selections'])
        base = editor.validate_plan(args.base_plan)[0] if args.base_plan else None
        plan, adaptation = adapt(data, matches[0], inspection, mapping, base, args.mapping.resolve().parent)
    args.out.mkdir(parents=True, exist_ok=False)
    write(args.out / '拆解报告.json', inspection)
    if plan:
        write(args.out / '借鉴记录.json', adaptation)
        plan_path = args.out / 'edit-plan.json'
        write(plan_path, plan)
        # Existing generator remains the only schema/media/timeline validation authority.
        try:
            validated, probes, warnings = editor.validate_plan(plan_path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            adaptation['plan_validation'] = 'failed'
            adaptation['error'] = str(error)
            write(args.out / '借鉴记录.json', adaptation)
            raise
        adaptation['plan_validation'] = 'passed'
        adaptation['warnings'] = warnings
        write(args.out / '借鉴记录.json', adaptation)
    print(json.dumps({'out': str(args.out.resolve()), 'segments_inspected': len(inspection['segments']),
                      'adaptation': adaptation}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
