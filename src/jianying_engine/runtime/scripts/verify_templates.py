"""Background admission checks for template borrowing; temporary media/drafts are removed.

Tests current supported components, never promotes a whole template from a partial test.
"""
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import zipfile

import editor
import templates
import template_borrow as borrow

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = templates.INDEX.parent / 'template-validation.json'


def fingerprint():
    # Every local module that shapes generated drafts (editor imports the native_* helpers).
    # native_preview.py is excluded: only the native-preview command renders with it, never a build.
    digest = hashlib.sha256()
    for path in [ROOT / 'scripts' / n for n in ('editor.py', 'native_advanced.py', 'native_text_glow.py',
                                               'native_matting_cache.py', 'file_hash.py', 'templates.py', 'template_borrow.py',
                                               'verify_templates.py')]:
        digest.update(path.read_bytes())
    for path in sorted((ROOT / 'assets').glob('native*.json')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    backend_root = Path(editor.load_backend().__file__).parent
    for path in sorted(backend_root.rglob('*.py')):
        digest.update(path.relative_to(backend_root).as_posix().encode())
        digest.update(path.read_bytes())
    environment = {'backend': importlib.metadata.version('pyJianYingDraft'),
                   'jianying': editor.doctor()['jianying_installed'], 'implementation': digest.hexdigest()}
    return environment


def admission(project, registry=None, environment=None):
    registry = registry if registry is not None else borrow.read(REGISTRY) if REGISTRY.exists() else {}
    record = registry.get('projects', {}).get(project['id'])
    if not record:
        return {'status': 'not_tested', 'core_passed': 0}
    if record.get('source_sha256') != project['sha256']:
        return {'status': 'stale_source', 'core_passed': 0}
    current = environment or fingerprint()
    if registry.get('environment') != current or record.get('environment', registry.get('environment')) != current:
        return {'status': 'stale_environment', 'core_passed': 0}
    return {'status': record['status'], 'core_passed': sum(r['status'] == 'passed'
            for r in record['segments'].values()), 'summary': record['summary'],
            'full_template_rebuild': 'not_supported', 'ui_compatibility': 'unverified'}


def require_admission(project, selections):
    registry = borrow.read(REGISTRY) if REGISTRY.exists() else {}
    state = admission(project, registry)
    if state['status'] not in ('partial_verified', 'core_verified'):
        raise ValueError('Template has no current background admission: ' + state['status']
                         + '; run verify_templates.py --id ' + project['id'] + ' --out NEW_FOLDER --register')
    record = registry['projects'][project['id']]
    for row in selections:
        segment = record['segments'].get(row['segment'], {})
        if segment.get('status') != 'passed':
            raise ValueError('Segment is not admitted: ' + row['segment'] + '; ' + str(segment.get('reason')))
        if row.get('time_basis') and row['time_basis'] not in segment.get('time_bases', []):
            raise ValueError('Time basis is not verified for this segment: ' + row['segment'])


def blank(path, width=320, height=180):
    pixels = bytes([28, 24, 20]) * width * height
    header = b'BM' + struct.pack('<IHHI', 54 + len(pixels), 0, 0, 54)
    header += struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    path.write_bytes(header + pixels)


def material_file(material, source, source_root, cache):
    value = material.get('path', '')
    names = set()
    archive_path = borrow.source_path(source, source_root)
    if source['kind'] != 'folder':
        with zipfile.ZipFile(archive_path) as archive:
            names = {templates.decoded_name(i) for i in archive.infolist()}
    location = borrow.resource_locator(value, source, source_root, names)
    if location['status'] == 'exists_as_written':
        return Path(value)
    if location['status'] == 'package_exact':
        return Path(location['path'])
    if location['status'] == 'zip_entry':
        suffix = PurePosixPath(location['member']).suffix.lower()
        key = hashlib.sha256((str(archive_path) + '::' + location['member']).encode()).hexdigest()
        destination = cache / (key + suffix)
        if not destination.exists():
            with zipfile.ZipFile(archive_path) as archive:
                member = first((i for i in archive.infolist() if templates.decoded_name(i) == location['member']),
                               'ZIP member not found: ' + location['member'])
                with archive.open(member) as src, destination.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
        return destination
    raise ValueError('Original media cannot be resolved: ' + value)


def exact_enum(kind, material):
    backend = editor.load_backend()
    enum = getattr(backend, editor.NATIVE_TYPES[kind])
    candidates = []
    effect_id = str(material.get('effect_id') or material.get('id') or '')
    resource_id = str(material.get('resource_id') or '')
    for name, member in enum.__members__.items():
        if ((effect_id and effect_id == str(getattr(member.value, 'effect_id', '')))
            or (resource_id and resource_id == str(getattr(member.value, 'resource_id', '')))):
            candidates.append((name, member))
    if not candidates:
        raise ValueError('Resource has no exact current enum: ' + kind + '/' + str(material.get('name')))
    return candidates[0]


def native_setting(material, track_type, media):
    """Only maps known static fields with exact resource identities, no copied arbitrary IDs."""
    kind = material.get('type')
    if kind in ('video_effect', 'face_effect'):
        family = 'character' if kind == 'face_effect' else 'scene'
        name, member = exact_enum(family + '_effects', material)
        definitions = {p.name: p for p in member.value.params}
        params = {}
        for p in material.get('adjust_params', []):
            if p['name'] not in definitions:
                raise ValueError('Unknown effect parameter: ' + p['name'])
            definition = definitions[p['name']]
            if definition.max_value == definition.min_value:
                if p['value'] != definition.default_value:
                    raise ValueError('Non-default constant effect parameter')
            else:
                params[p['name']] = (p['value'] - definition.min_value) / (definition.max_value - definition.min_value) * 100
        return {'effects': [{'kind': family, 'name': name, 'params': params}]}
    if kind == 'filter':
        name, _ = exact_enum('filters', material)
        return {'filters': [{'name': name, 'intensity': material.get('value', 1) * 100}]}
    if kind == 'transition':
        name, _ = exact_enum('transitions', material)
        return {'transition': {'name': name, 'duration': material['duration'] / 1e6}}
    if kind in ('sticker_animation', 'material_animation') and material.get('animations'):
        animations = []
        for a in material['animations']:
            stage = {'in': 'intro', 'out': 'outro', 'group': 'group', 'loop': 'loop'}.get(a['type'])
            if not stage:
                raise ValueError('Unknown animation stage')
            name, _ = exact_enum(('text_' if track_type == 'text' else 'video_') + stage, a)
            setting = {'stage': stage, 'name': name}
            if stage != 'loop':
                setting['duration'] = a['duration'] / 1e6
            animations.append(setting)
        return {'animations': animations}
    if kind == 'video_animation':
        stage = {'in': 'intro', 'out': 'outro', 'group': 'group'}.get(material.get('category_name'))
        if not stage:
            raise ValueError('Legacy video animation stage is ambiguous')
        name, _ = exact_enum('video_' + stage, material)
        return {'animations': [{'stage': stage, 'name': name, 'duration': material['value'] / 1e6}]}
    if kind == 'mix_mode':
        name, _ = exact_enum('mix_modes', material)
        return {'mix_mode': name}
    if kind == 'chroma':
        return {'chroma': {'color': material['color'], **{k: material.get(k + '_value', 0) * 100
                for k in ('intensity', 'shadow', 'edge_smooth', 'spill')}}}
    if kind == 'mask':
        if material.get('name') != '矩形':
            raise ValueError('Suspended non-rectangle mask')
        c = material['config']
        return {'mask': {'name': '矩形', 'experimental': True, 'size': c['height'],
                         'rect_width': c['width'], 'center_x': c['centerX'] * media['width'] / 2,
                         'center_y': c['centerY'] * media['height'] / 2, 'rotation': c['rotation'],
                         'feather': c['feather'] * 100, 'invert': c['invert'],
                         'round_corner': c.get('roundCorner', 0) * 100}}
    raise ValueError('Not an admitted static native component: ' + str(kind))


def first(candidates, message):
    """First candidate, or a ValueError naming the failed check (never a bare StopIteration)."""
    for candidate in candidates:
        return candidate
    raise ValueError(message)


def independent_readback(content, plan):
    """Explicit checks (kept under python -O); each failure names the check and location."""
    mats = {m['id']: m for items in content['materials'].values() if isinstance(items, list)
            for m in items if isinstance(m, dict) and m.get('id')}
    lookup = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    motion_ids = []
    for t in content['tracks']:
        for s in t['segments']:
            where = f"track {t['name']} at {s['target_timerange']['start']}us"
            if s['material_id'] not in mats:
                raise ValueError('Missing primary native material: ' + where)
            missing = [r for r in s.get('extra_material_refs', []) if r not in mats]
            if missing:
                raise ValueError('Missing extra native material ' + json.dumps({'track': t['name'],
                    'start': s['target_timerange']['start'], 'refs': missing,
                    'settings': [x for group in ('clips', 'captions', 'audio') for x in plan.get(group, [])
                                 if x['track'] == t['name'] and editor.us(x['start']) == s['target_timerange']['start']]},
                    ensure_ascii=False))
            if s.get('keyframe_refs'):
                raise ValueError('Dangling legacy references: ' + where)
            motion_ids.append(s['id'])
            for g in s.get('common_keyframes', []):
                motion_ids.append(g['id'])
                motion_ids.extend(k['id'] for k in g['keyframe_list'])
    if len(motion_ids) != len(set(motion_ids)):
        raise ValueError('Duplicate native motion/segment IDs')
    for group in ('clips', 'captions', 'audio'):
        for item in plan.get(group, []):
            where = f"{group}/{item['track']} at {item['start']}s"
            s = lookup.get((item['track'], editor.us(item['start'])))
            if s is None:
                raise ValueError('Planned segment missing from draft: ' + where)
            if group != 'captions' and not abs(s['volume'] - item['volume']) < 1e-8:
                raise ValueError(f"Volume readback: {where} expected {item['volume']} got {s['volume']}")
            expected = {}
            for k in item.get('keyframes', []):
                prop = editor.load_backend().KeyframeProperty[k['property']].value
                stamp = k['time'] if group == 'captions' else item['source_in'] + k['time'] * item['speed']
                expected[prop, editor.us(stamp)] = k['value']
            actual = {(g['property_type'], k['time_offset']): k['values'][0]
                      for g in s.get('common_keyframes', []) for k in g['keyframe_list']}
            if len(expected) != len(actual):
                raise ValueError(f'Motion point count: {where} expected {len(expected)} got {len(actual)}')
            if not all(key in actual and abs(actual[key] - value) < 1e-8 for key, value in expected.items()):
                raise ValueError('Motion point values: ' + where)
            refs = [mats[r] for r in s.get('extra_material_refs', [])]
            if item.get('transition'):
                actual_tr = first((r for r in refs if r.get('type') == 'transition'),
                                  'Transition material missing from draft: ' + where)
                member = editor.native_enum(editor.load_backend(), 'transitions', item['transition']['name'])
                if actual_tr['effect_id'] != member.value.effect_id:
                    raise ValueError('Transition effect ID readback: ' + where)
                if actual_tr['duration'] != editor.us(item['transition']['duration']):
                    raise ValueError('Transition duration readback: ' + where)
            if group == 'audio' and (item['fade_in'] or item['fade_out']):
                fade = first((r for r in refs if r.get('type') == 'audio_fade'),
                             'Audio fade material missing from draft: ' + where)
                if fade['fade_in_duration'] != editor.us(item['fade_in']):
                    raise ValueError('Audio fade-in readback: ' + where)
                if fade['fade_out_duration'] != editor.us(item['fade_out']):
                    raise ValueError('Audio fade-out readback: ' + where)


def construct_unit(item, group):
    """Catch backend duration/parameter rejection before combining otherwise valid units."""
    backend = editor.load_backend()
    target = backend.Timerange(0, editor.us(item['duration']))
    if group == 'captions':
        segment = editor.text_segment(item, target, backend)
        editor.apply_animations(segment, item, backend, text=True)
        return
    source = backend.Timerange(editor.us(item['source_in']), editor.us(item['duration'] * item['speed']))
    if group == 'audio':
        segment = backend.AudioSegment(item['path'], target, source_timerange=source, volume=item['volume'])
        if item['fade_in'] or item['fade_out']:
            segment.add_fade(editor.us(item['fade_in']), editor.us(item['fade_out']))
    else:
        segment = backend.VideoSegment(item['path'], target,
                    source_timerange=None if Path(item['path']).suffix.lower() in editor.IMAGE_EXT else source,
                    volume=item['volume'], clip_settings=backend.ClipSettings(**item['transform']))
        for point in item['keyframes']:
            segment.add_keyframe(getattr(backend.KeyframeProperty, point['property']),
                                editor.us(item['source_in'] + point['time'] * item['speed']), point['value'])
        if item.get('transition'):
            segment.add_transition(backend.TransitionType[item['transition']['name']],
                                   duration=editor.us(item['transition']['duration']))
        editor.apply_native(segment, item, backend)


def verify_project(project, source_root, work, proof_dir):
    data, source = borrow.load(project, source_root)
    source_root = Path(source.get('root', source_root))
    inspection = borrow.report(data, project, source, root=source_root)
    materials, keys, segments = borrow.tables(data)
    cache = work / 'media'
    cache.mkdir()
    backdrop = work / 'background.bmp'
    blank(backdrop)
    canvas = {'width': int(data['canvas_config']['width']), 'height': int(data['canvas_config']['height']),
              'fps': int(round(data.get('fps') or 30))}
    units, segment_states, components, resources = [], {}, [], []
    probe_cache = {}
    def probe(path):
        path = str(path)
        if path not in probe_cache:
            probe_cache[path] = editor.probe(path)
        return probe_cache[path]
    for detail in inspection['segments']:
        locator = detail['segment']
        track, segment = segments[locator]
        typ = track['type']
        if typ not in ('video', 'audio', 'text'):
            segment_states[locator] = {'status': 'unsupported', 'reason': 'Track type ' + typ}
            continue
        original_duration = segment['target_timerange']['duration'] / 1e6
        row = {'segment': locator, 'start': 0, 'duration': original_duration,
               'track': 'sample', 'time_basis': 'local', 'motion_mode': 'linear_rebuild'}
        media = None
        try:
            if typ in ('video', 'audio'):
                primary = materials[segment['material_id']]
                if primary.get('alternatives'):
                    raise ValueError('Ambiguous primary media material')
                path = material_file(primary['data'], source, source_root, cache)
                media = probe(path)
                resources.append({'material_id': segment['material_id'], 'status': 'probed',
                                  'kind': media['kind'], 'bytes': path.stat().st_size})
                row.update(path=str(path), source_in=(segment.get('source_timerange') or {}).get('start', 0) / 1e6,
                           speed=segment.get('speed', 1), volume=segment.get('volume', 1))
                if typ == 'video':
                    row['fit'] = 'contain'
                    if media['kind'] == 'image':
                        row.update(source_in=0, speed=1)
                else:
                    row.pop('time_basis'); row.pop('motion_mode')
            else:
                row.update(text='文字参数后台检验', size=12)
            bases = ['local', 'source_relative', 'source_absolute']
            if typ in ('video', 'text') and (segment.get('keyframe_refs') or segment.get('common_keyframes')):
                original_source = (segment.get('source_timerange') or {}).get('start', 0)
                if original_source != 0 or segment.get('speed', 1) != 1:
                    raise ValueError('Time basis requires explicit source-format evidence; no automatic guess')
            mapping = {'name': 'component', 'canvas': canvas, 'selections': [row]}
            plan, _ = borrow.adapt(data, project, inspection, mapping, None, work)
            group = 'clips' if typ == 'video' else 'audio' if typ == 'audio' else 'captions'
            item = plan[group][0]
            units.append({'key': locator, 'group': group, 'item': item, 'kind': 'core'})
            segment_states[locator] = {'status': 'pending_build', 'time_bases': bases if typ != 'audio' else [],
                                       'scope': 'linear_motion_and_static_transform' if typ != 'audio' else 'constant_audio'}
        except (ValueError, KeyError, TypeError, OSError) as error:
            segment_states[locator] = {'status': 'blocked', 'reason': str(error)}
            if typ in ('video', 'audio') and media is None:
                resources.append({'material_id': segment.get('material_id'), 'status': 'failed', 'reason': str(error)})
        # Static source resources are tested independently, without promoting omitted source features.
        for dependency in detail['dependencies']:
            category, material = dependency['category'], dependency['data']
            if category not in ('video_effects', 'effects', 'material_animations', 'transitions',
                                'masks', 'chromas', 'mix_modes'):
                continue
            component = {'segment': locator, 'material_id': material['id'], 'category': category}
            try:
                if material['id'] in detail['ambiguous_material_refs']:
                    raise ValueError('Ambiguous material ID')
                native = native_setting(material, typ, media or {'width': 320, 'height': 180})
                if typ == 'audio' or (typ == 'text' and set(native) != {'animations'}):
                    raise ValueError('Resource does not apply to this supported track type')
                item = {'start': 0, 'duration': original_duration, 'track': 'sample', **native}
                group = 'captions' if typ == 'text' else 'clips'
                item.update(text='文字资源后台检验', size=12) if typ == 'text' else item.update(path=str(backdrop), fit='contain')
                component.update(status='pending_build', native=native)
                units.append({'key': str(len(components)), 'group': group, 'item': item, 'kind': 'native'})
            except (ValueError, KeyError, TypeError) as error:
                component.update(status='blocked', reason=str(error))
            components.append(component)
    # Validate individual units first to isolate rejected parameters without discarding other source parts.
    def state_of(unit):
        return segment_states[unit['key']] if unit['kind'] == 'core' else components[int(unit['key'])]
    accepted = []
    cursor = 0.0
    for unit in units:
        item = unit['item']
        # Backdrop first on the video track, then the unit in its own group (clips: after the backdrop).
        unit_plan = {'schema_version': 1, 'name': 'unit', 'mode': 'script', 'canvas': canvas,
                     'clips': [{'path': str(backdrop), 'start': 0, 'duration': item['duration'] + 0.01,
                                'track': 'video'}]}
        unit_plan.setdefault(unit['group'], []).append(item)
        unit_path = work / 'unit.json'
        borrow.write(unit_path, unit_plan)
        state = state_of(unit)
        try:
            checked, _, _ = editor.validate_plan(unit_path)
            group = unit['group']
            normalized = first((x for x in checked[group] if x['track'] == 'sample'),
                               'Validated unit plan lost the sample item: ' + group)
            construct_unit(normalized, group)
            normalized['start'] = round(cursor, 6)
            normalized['track'] = 'sample_' + group
            cursor = round(cursor + normalized['duration'] + 0.01, 6)
            unit['item'] = normalized
            accepted.append(unit)
        except (ValueError, KeyError, TypeError, OSError) as error:
            state.update(status='failed_validation', reason=str(error))
    plan = {'schema_version': 1, 'name': project['id'] + '-backend-test', 'mode': 'script', 'canvas': canvas,
            'clips': [{'path': str(backdrop), 'start': 0, 'duration': max(cursor, 0.1), 'track': 'video'}]}
    for unit in accepted:
        plan.setdefault(unit['group'], []).append(unit['item'])
    plan_path = work / 'edit-plan.json'
    borrow.write(plan_path, plan)
    built = 'no_components'
    proof = None
    if accepted:
        try:
            editor.build_draft(SimpleNamespace(plan=plan_path, out=work / 'draft'))
            checked = borrow.read(work / 'draft/edit-plan.json')
            content = borrow.read(work / 'draft/draft_content.json')
            independent_readback(content, checked)
            built = 'passed'
            for unit in accepted:
                state_of(unit)['status'] = 'passed'
            proof = {'plan_sha256': hashlib.sha256((work / 'draft/edit-plan.json').read_bytes()).hexdigest(),
                     'draft_sha256': hashlib.sha256((work / 'draft/draft_content.json').read_bytes()).hexdigest(),
                     'verification': borrow.read(work / 'draft/verification.json'),
                     'tested_parameters': [{**{k: u[k] for k in ('kind', 'key', 'group')},
                                            'item': u['item']} for u in accepted]}
        except (ValueError, KeyError, TypeError, OSError, AssertionError) as error:
            built = 'failed'
            for unit in accepted:
                state_of(unit).update(status='failed_build', reason=str(error))
    summary = {'segments': dict(Counter(r['status'] for r in segment_states.values())),
               'native_components': dict(Counter(r['status'] for r in components)),
               'resources': dict(Counter(r['status'] for r in resources))}
    passed = summary['segments'].get('passed', 0)
    result = {'source_sha256': project['sha256'], 'title': project['title'],
              'status': 'partial_verified' if passed else 'research_only', 'build': built,
              'segments': segment_states, 'native_components': components, 'summary': summary,
              'original_media_probes': resources, 'full_template_rebuild': 'not_supported',
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified',
              'omissions': 'Original crop, unsupported grading, font/templates/stickers, algorithms, '
                           'nonlinear/time-ambiguous motion and other unmapped fields are not rebuilt.'}
    borrow.write(proof_dir / (project['id'] + '.json'), {'result': result, 'proof': proof})
    return result


def new_project(path, member=None):
    if member:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(first((i for i in archive.infolist() if templates.decoded_name(i) == member),
                                     'ZIP member not found: ' + member))
        source = {'kind': 'zip', 'path': path.name, 'member': member, 'root': str(path.parent)}
    else:
        raw = path.read_bytes()
        source = {'kind': 'folder', 'path': path.name, 'root': str(path.parent)}
    data = json.loads(raw.decode('utf-8-sig'))
    digest = hashlib.sha256(raw).hexdigest()
    tracks = data.get('tracks', [])
    return {'id': 'TPL-' + digest[:12], 'title': data.get('name') or path.parent.name,
            'sha256': digest, 'sources': [source], 'canvas': data.get('canvas_config'),
            'fps': data.get('fps'), 'duration_seconds': data.get('duration', 0) / 1e6,
            'features': list(Counter(t['type'] for t in tracks)), 'effect_names': {}, 'animation_names': [],
            'evidence': {'structure': 'read', 'backend_rebuild': 'not_tested'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', action='append', help='Selected existing TPL ID; default all existing templates')
    parser.add_argument('--draft', type=Path, help='New draft_content.json, or ZIP when --member is supplied')
    parser.add_argument('--member', help='Exact decoded draft JSON member in a new ZIP')
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--out', type=Path, required=True, help='New report directory; temporary artifacts removed')
    parser.add_argument('--register', action='store_true', help='Record admission results and add new source to index')
    args = parser.parse_args()
    index = templates.read_index() if templates.INDEX.exists() or not args.draft else {
        'schema_version': 1, 'source_root': str(args.draft.resolve().parent), 'projects': [],
        'checked_date': time.strftime('%Y-%m-%d')}
    projects = [new_project(args.draft.resolve(), args.member)] if args.draft else index['projects']
    if args.id:
        projects = [p for p in projects if p['id'] in args.id]
        if set(args.id) != {p['id'] for p in projects}:
            raise ValueError('Unknown template ID')
    args.out.mkdir(parents=True, exist_ok=False)
    proof_dir = args.out / '参数证据'
    proof_dir.mkdir()
    environment = fingerprint()
    results = {}
    temp_parent = args.out / '临时测试'
    temp_parent.mkdir()
    started = time.time()
    try:
        for number, project in enumerate(projects, 1):
            root = args.source_root or Path(index['source_root'])
            with tempfile.TemporaryDirectory(prefix=project['id'] + '-', dir=temp_parent) as temp:
                try:
                    result = verify_project(project, root, Path(temp), proof_dir)
                except (ValueError, KeyError, TypeError, OSError, AssertionError) as error:
                    result = {'source_sha256': project['sha256'], 'title': project['title'], 'status': 'research_only',
                              'segments': {}, 'summary': {}, 'reason': str(error), 'build': 'failed'}
                result['environment'] = environment
                results[project['id']] = result
                borrow.write(args.out / '检验结果.json', {'environment': environment, 'projects': results})
                print(json.dumps({'progress': f'{number}/{len(projects)}', 'id': project['id'],
                                  'status': result['status'], 'summary': result.get('summary'),
                                  'reason': result.get('reason')}, ensure_ascii=False), flush=True)
    finally:
        # The only recursively removed path is this verified report folder's temporary child.
        if not (temp_parent.resolve().is_relative_to(args.out.resolve()) and temp_parent.name == '临时测试'):
            raise ValueError('Refusing to remove a temporary folder outside the report directory')
        shutil.rmtree(temp_parent)
    if fingerprint() != environment:
        raise ValueError('Implementation/environment changed during audit; results cannot be registered')
    if args.register:
        templates.INDEX.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        registry = borrow.read(REGISTRY) if REGISTRY.exists() else {'schema_version': 1, 'projects': {}}
        if registry.get('environment') != environment:
            # Keep old evidence while its per-project fingerprint continues to deny admission.
            for record in registry['projects'].values():
                record.setdefault('environment', registry.get('environment'))
        registry.update(environment=environment, checked_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        report_root=str(args.out.resolve()))
        registry['projects'].update(results)
        borrow.write(REGISTRY, registry)
        for project in projects:
            result = results[project['id']]
            existing = next((p for p in index['projects'] if p['id'] == project['id']), None)
            if existing is None:
                index['projects'].append(project)
                existing = project
            existing.setdefault('evidence', {})['backend_rebuild'] = result['status']
        borrow.write(templates.INDEX, index)
    summary = {'projects': len(results), 'states': dict(Counter(r['status'] for r in results.values())),
               'builds': dict(Counter(r['build'] for r in results.values())),
               'elapsed_seconds': round(time.time() - started, 1), 'temporary_files_removed': not temp_parent.exists(),
               'full_template_rebuild': 'not_supported', 'ui_compatibility': 'unverified'}
    borrow.write(args.out / '检验汇总.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
