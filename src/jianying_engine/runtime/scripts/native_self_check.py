"""Native editable parameter regression. Uses generated source media, never prerenders effects."""
import argparse
import copy
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as Args

import editor


def long_path(path):
    """Extension-length form for the copied cache tree, whose deepest key is ~120 chars.
    Without it an evidence directory longer than ~140 chars fails with WinError 206."""
    path = Path(path)
    return path if os.name != 'nt' or str(path).startswith('\\\\?\\') else Path('\\\\?\\' + str(path))


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    source = root / '绿幕 源素材.mp4'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=green:s=320x240:r=30:d=4',
                '-vf', 'drawbox=x=120:y=60:w=80:h=120:color=red:t=fill',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', source])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    backend = editor.load_backend()
    plan = {'schema_version': 1, 'name': '剪映原生效果测试-v1', 'mode': 'script',
        'canvas': {'width': 320, 'height': 240, 'fps': 30},
        'clips': [
            {'path': str(source), 'start': 0, 'duration': 4, 'background': {'type': 'blur', 'blur': 0.375},
             'filters': [{'name': 'KE1', 'intensity': 34}],
             'effects': [{'name': '_1998', 'params': {'effects_adjust_filter': 25, 'effects_adjust_texture': 90}}]},
            {'path': str(source), 'start': 0, 'duration': 4, 'track': '画中画',
             'transform': {'scale_x': 0.35, 'scale_y': 0.35, 'transform_x': 0.5, 'transform_y': -0.4},
             'animations': [{'stage': 'intro', 'name': '动感放大', 'duration': 0.5},
                            {'stage': 'outro', 'name': '向上滑动', 'duration': 0.4}],
             'effects': [{'kind': 'character', 'name': 'BOOM', 'params': {'effects_adjust_speed': 20}}]},
            {'path': str(source), 'start': 0, 'duration': 4, 'track': '圆形画中画-实验',
             'transform': {'scale_x': 0.3, 'scale_y': 0.3},
             'mask': {'name': '圆形', 'experimental': True, 'center_x': 10, 'center_y': -4,
                      'size': 0.7, 'feather': 0},
             'animations': [{'stage': 'group', 'name': '三分割', 'duration': 4}]}],
        'captions': [{'text': '原生字幕动画测试', 'start': 0, 'duration': 4,
                      'animations': [{'stage': 'loop', 'name': 'VHS'},
                                     {'stage': 'outro', 'name': '右上弹出', 'duration': 0.5},
                                     {'stage': 'intro', 'name': '渐显', 'duration': 0.4}]}],
        'effects': [{'name': '_70s', 'start': 1, 'duration': 1.2, 'track': '特效轨',
                     'params': {'effects_adjust_speed': 40}}],
        'filters': [{'name': 'ABG', 'start': 0, 'duration': 4, 'track': '滤镜轨', 'intensity': 60}],
        'jianying_tasks': [{'operation': 'smart_cutout', 'track': '画中画', 'start': 0, 'duration': 4,
                           'instructions': '在剪映内选择智能抠像；测试仅记录待办，未执行。'},
                          {'operation': 'tracking', 'track': '画中画', 'start': 0, 'duration': 4,
                           'instructions': '在剪映内指定跟踪主体；测试仅记录待办，未执行。'}],
        'notes': ['合成源素材，仅验证原生参数，不评价特效观感'], 'missing_assets': []}
    plan_path = root / 'edit-plan.json'
    editor.write_json(plan_path, plan)
    normalized, _, warnings = editor.validate_plan(plan_path)
    assert normalized['allow_vip_effects'] is True
    draft = root / '原生草稿'
    result = editor.build_draft(Args(plan=str(plan_path), out=str(draft)))
    assert result['verification']['segments'] == 6
    counts = result['verification']['native_effects']['counts']
    assert counts == {'clip_effects': 2, 'clip_filters': 1, 'background': 1, 'clips_animations': 3,
                      'mask': 1, 'captions_animations': 3,
                      'track_effects': 1, 'track_filters': 1}, counts
    content = editor.read_json(draft / 'draft_content.json')
    materials = content['materials']
    assert all(v['path'] == str(source.resolve()) for v in materials['videos'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert not result['verification']['native_effects']['prerendered_effects']
    assert result['verification']['native_effects']['pending_jianying_tasks'] == 2
    assert result['verification']['editing_status'] == 'partial'
    assert result['verification']['final_check_owner'] == result['verification']['export_owner'] == 'user'
    complete_plan = copy.deepcopy(normalized)
    complete_plan['jianying_tasks'] = []
    assert editor.verify_structure(content, complete_plan)['editing_status'] == 'complete'
    complete_plan['missing_assets'] = ['必需的配音尚未提供']
    assert editor.verify_structure(content, complete_plan)['editing_status'] == 'partial'
    assert '剪映内仍待完成' in (draft / '剪辑交付.md').read_text(encoding='utf-8')
    assert 'smart_cutout' in (draft / '原生效果.md').read_text(encoding='utf-8')
    text_anims = next(a['animations'] for a in materials['material_animations']
                      if len(a['animations']) == 3)
    loop = next(a for a in text_anims if a['type'] == 'loop')
    assert loop['start'] == 400000 and loop['duration'] == 3100000
    cache = Path(editor.native_user_data()) / 'Cache' / 'effect'
    shipped = [animation for a in materials['material_animations'] for animation in a['animations']]
    assert shipped and all(editor.native_advanced.animation_bundle(cache, x['id']) for x in shipped), \
        [x['id'] for x in shipped]
    checks = ['native filters/effects, independent tracks, animations, background and admitted mask geometry',
              'original source paths and bytes preserved; no baked effect video',
              'text loop starts after intro and ends before outro',
              'every shipped animation resolves to a local effect bundle the app itself caches',
              'smart cutout/tracking explicitly pending']
    corrupt = copy.deepcopy(content)
    corrupt['materials']['canvases'][0]['blur'] = 0.999
    try:
        editor.verify_structure(corrupt, normalized)
    except ValueError as exc:
        assert 'background' in str(exc)
    else:
        raise AssertionError('Tampered native parameter must fail readback')
    checks.append('serialized native parameter tampering rejected')
    rejected = []
    def reject(label, mutate):
        candidate = copy.deepcopy(plan)
        mutate(candidate)
        path = root / 'invalid.json'
        editor.write_json(path, candidate)
        try:
            editor.validate_plan(path)
        except (ValueError, TypeError) as exc:
            rejected.append({'case': label, 'reason': str(exc)})
        else:
            raise AssertionError('Expected rejection: ' + label)
    reject('unknown filter', lambda p: p['clips'][0]['filters'][0].update(name='虚构滤镜'))
    reject('filter intensity beyond range', lambda p: p['filters'][0].update(intensity=101))
    reject('unknown effect parameter', lambda p: p['clips'][0]['effects'][0]['params'].update(fake=50))
    reject('nonfinite effect parameter', lambda p: p['effects'][0]['params'].update(effects_adjust_speed=float('nan')))
    reject('effect parameter beyond range', lambda p: p['effects'][0]['params'].update(effects_adjust_speed=-1))
    reject('duplicate filter', lambda p: p['clips'][0]['filters'].append(copy.deepcopy(p['clips'][0]['filters'][0])))
    reject('animation overlaps', lambda p: p['clips'][1]['animations'][0].update(duration=3.8))
    reject('duplicate animation stage', lambda p: p['clips'][1]['animations'].append(copy.deepcopy(p['clips'][1]['animations'][0])))
    reject('group conflicts with intro', lambda p: p['clips'][2]['animations'].append({'stage': 'intro', 'name': '动感放大', 'duration': 0.2}))
    reject('text loop duration must be implicit', lambda p: p['captions'][0]['animations'][0].update(duration=2))
    reject('mix mode on base track', lambda p: p['clips'][0].update(mix_mode='滤色'))
    reject('unknown mix mode', lambda p: p['clips'][1].update(mix_mode='不存在'))
    reject('unadmitted chroma', lambda p: p['clips'][1].update(chroma={'color': '#00FF00FF'}))
    reject('unadmitted rectangle feather', lambda p: p['clips'][2].update(mask={'name': '矩形', 'experimental': True, 'feather': 20}))
    reject('unadmitted feather animation', lambda p: p['clips'][2].update(mask={'name': '矩形', 'experimental': True,
           'keyframes': [{'property': 'feather', 'time': 0, 'value': 0}, {'property': 'feather', 'time': 1, 'value': 20}]}))
    reject('nonboolean VIP policy', lambda p: p.update(allow_vip_effects='true'))
    reject('mask acknowledgement missing', lambda p: p['clips'][2]['mask'].update(experimental=False))
    for shape in ('线性', '镜面', '星形', '爱心'):
        reject('staged mask ' + shape, lambda p, shape=shape: p['clips'][2]['mask'].update(name=shape))
    reject('rectangle-only mask parameter', lambda p: p['clips'][2]['mask'].update(rect_width=0.5))
    reject('background on overlay', lambda p: p['clips'][1].update(background={'type': 'blur'}))
    reject('effect extends past timeline', lambda p: p['effects'][0].update(duration=10))
    reject('same effect track overlap', lambda p: p['effects'].append(copy.deepcopy(p['effects'][0])))
    reject('mixed track media type', lambda p: p['filters'][0].update(track='画中画'))
    reject('native task outside clip', lambda p: p['jianying_tasks'][0].update(start=3, duration=2))
    reject('pretend operation not supported', lambda p: p['jianying_tasks'][0].update(operation='auto_magic'))
    vip = next(x for x in backend.FilterType if x.value.is_vip)
    reject('VIP filter when explicitly disabled', lambda p: (p.update(allow_vip_effects=False),
           p['clips'][0]['filters'][0].update(name=vip.name)))
    member_plan = copy.deepcopy(plan)
    member_plan['clips'][0]['filters'][0]['name'] = vip.name
    member_plan['clips'][1]['animations'][0]['name'] = '九宫格'
    member_plan['captions'][0]['animations'][0]['name'] = '环形滚动'
    editor.write_json(root / 'member-plan.json', member_plan)
    member_result = editor.build_draft(Args(plan=str(root / 'member-plan.json'), out=str(root / 'member-draft')))
    assert editor.read_json(root / 'member-draft' / 'edit-plan.json')['allow_vip_effects'] is True
    assert member_result['verification']['native_effects']['parameter_readback'] == 'passed'
    checks.append('VIP filter and video/text animations build by default; explicit opt-out enforced')
    plain = copy.deepcopy(plan)
    plain['clips'][0]['background'] = {'type': 'color', 'color': '#AABBCCFF'}
    plain['clips'][2]['mask'] = {'name': '矩形', 'experimental': True, 'size': 0.6,
                               'rect_width': 0.7, 'round_corner': 35, 'invert': True}
    editor.write_json(root / 'rectangle-plan.json', plain)
    editor.build_draft(Args(plan=str(root / 'rectangle-plan.json'), out=str(root / 'rectangle-draft')))
    checks.append('native rectangle/corner/inverted mask and color background parameter readback')
    rect_content = editor.read_json(root / 'rectangle-draft' / 'draft_content.json')
    rect_plan = editor.read_json(root / 'rectangle-draft' / 'edit-plan.json')
    assert Path(rect_content['materials']['common_mask'][0]['path']).is_dir()
    for field, value in (('path', ''), ('resource_id', 'wrong'), ('category', 'adjust')):
        tampered = copy.deepcopy(rect_content)
        tampered['materials']['common_mask'][0][field] = value
        try:
            editor.verify_structure(tampered, rect_plan)
        except ValueError:
            pass
        else:
            raise AssertionError('Rectangle resource tampering accepted: ' + field)
    checks.append('rectangle cache path bound; empty path, resource and category tampering rejected')
    spec = editor.read_json(editor.SKILL_ROOT / 'assets/native-extensions-11.5.json')['rectangle_mask']
    fake_user_data = long_path(root / 'rectangle-resource-control')
    cache = fake_user_data / spec['cache_relative']
    for relative in spec['file_sha256']:
        destination = cache / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((editor.native_user_data() / spec['cache_relative'] / relative).read_bytes())
    editor.native_advanced.cached_material('rectangle_mask', fake_user_data)
    (cache / 'config.json').write_text('{}', encoding='utf-8')
    for target in (fake_user_data, root / 'missing-mask-cache'):
        try:
            editor.native_advanced.cached_material('rectangle_mask', target)
        except ValueError:
            pass
        else:
            raise AssertionError('Missing or changed rectangle cache accepted')
    checks.append('rectangle resource byte identity and absent cache rejected without shared-cache mutation')
    published_root = root / 'fake-app-drafts'
    published_root.mkdir()
    published = editor.publish_draft(Args(draft=str(draft), draft_root=str(published_root), offline_copy=True))
    assert (Path(published['draft']) / '原生效果.md').is_file()
    checks.append('native parameter and handoff documentation preserved on publish')
    designed = copy.deepcopy(plan)
    designed['layer_order'] = ['video', 'captions', '画中画', '圆形画中画-实验', '特效轨', '滤镜轨']
    designed['captions'][0].update(style={'alpha': 0.8, 'italic': True, 'vertical': True,
        'letter_spacing': 4, 'auto_wrapping': False, 'border_color': '#FF3355', 'border_width': 12,
        'shadow': {'color': '#2299FF', 'diffuse': 0, 'distance': 8, 'angle': 0}},
        transform={'rotation': -8, 'scale_x': 1.2, 'scale_y': 1.2},
        keyframes=[{'property': 'position_x', 'time': 0, 'value': -0.7},
                   {'property': 'position_x', 'time': 0.3, 'value': -0.3}])
    editor.write_json(root / 'designed-plan.json', designed)
    advanced = editor.build_draft(Args(plan=str(root / 'designed-plan.json'), out=str(root / 'designed-draft')))
    advanced_content = editor.read_json(root / 'designed-draft' / 'draft_content.json')
    assert [t['name'] for t in advanced_content['tracks']] == designed['layer_order']
    text_material = advanced_content['materials']['texts'][0]
    assert text_material['global_alpha'] == 0.8 and text_material['typesetting'] == 1
    import json
    style_data = json.loads(text_material['content'])['styles'][0]
    assert style_data['shadows'][0]['distance'] == 8 and style_data['shadows'][0]['diffuse'] == 0
    assert style_data['strokes'][0]['content']['solid']['color'][0] == 1
    assert advanced['verification']['native_effects']['counts']['text_design'] == 1
    corrupt_text = copy.deepcopy(advanced_content)
    corrupt_text['materials']['texts'][0]['global_alpha'] = 0.1
    try:
        editor.verify_structure(corrupt_text, editor.read_json(root / 'designed-draft' / 'edit-plan.json'))
    except ValueError as exc:
        assert 'text' in str(exc)
    else:
        raise AssertionError('Text style tampering must fail readback')
    corrupt_order = copy.deepcopy(advanced_content)
    corrupt_order['tracks'][1], corrupt_order['tracks'][2] = corrupt_order['tracks'][2], corrupt_order['tracks'][1]
    try:
        editor.verify_structure(corrupt_order, editor.read_json(root / 'designed-draft' / 'edit-plan.json'))
    except ValueError as exc:
        assert 'order' in str(exc)
    else:
        raise AssertionError('Mixed media layer tampering must fail readback')
    checks.append('native text style/shadow/rotation/motion, mixed media layering and tampering detection')
    for field, bad in [('free_render_index_mode_on', False), ('render_index_track_mode_on', True)]:
        corrupt_mode = copy.deepcopy(advanced_content)
        corrupt_mode[field] = bad
        try:
            editor.verify_structure(corrupt_mode, editor.read_json(root / 'designed-draft' / 'edit-plan.json'))
        except ValueError as exc:
            assert 'render index mode' in str(exc)
        else:
            raise AssertionError('Layer mode tampering must fail readback')
        checks.append('explicit layer mode tampering rejected: ' + field)
    reject('partial layer order', lambda p: p.update(layer_order=['video']))
    reject('text alpha keyframe not supported', lambda p: p['captions'][0].update(
        keyframes=[{'property': 'alpha', 'time': 0, 'value': 1}]))
    reject('text shadow beyond range', lambda p: p['captions'][0].update(style={'shadow': {'distance': 101}}))
    dynamic = copy.deepcopy(plain)
    dynamic['name'] = '正式接入-动态蒙版与复合-v1'
    dynamic['clips'][2]['mask'].update(center_x=-60, center_y=0, rotation=0, keyframes=[
        {'property': 'center_x', 'time': 0, 'value': -60},
        {'property': 'center_x', 'time': 3.8, 'value': 60},
        {'property': 'center_y', 'time': 0, 'value': 0},
        {'property': 'center_y', 'time': 3.8, 'value': 20},
        {'property': 'rotation', 'time': 0, 'value': 0},
        {'property': 'rotation', 'time': 3.8, 'value': 30}])
    dynamic['clips'][2].update(source_in=0.2, start=0.1, duration=3.8)
    dynamic['clips'][2]['animations'][0]['duration'] = 3.8
    for point in dynamic['clips'][2]['mask']['keyframes']:
        if point['time']:
            point['time'] = 3.6
    dynamic['jianying_tasks'] = []
    dynamic['composites'] = [{'name': '原生视频复合', 'tracks': ['video', '画中画', '圆形画中画-实验']}]
    editor.write_json(root / 'dynamic-plan.json', dynamic)
    composite_root = root / 'composite-draft'
    composite_result = editor.build_draft(Args(plan=str(root / 'dynamic-plan.json'), out=str(composite_root)))
    composite_content = editor.read_json(composite_root / 'draft_content.json')
    composite_plan = editor.read_json(composite_root / 'edit-plan.json')
    assert composite_result['verification']['native_effects']['dynamic_rectangle_masks'] == 1
    assert composite_result['verification']['native_effects']['video_composites'] == 1
    nested = composite_content['materials']['drafts'][0]['draft']
    assert len(nested['tracks']) == 3
    mask_segment = nested['tracks'][2]['segments'][0]
    assert mask_segment['target_timerange']['start'] == 100000
    assert mask_segment['common_keyframes'][0]['keyframe_list'][0]['time_offset'] == 200000
    assert all(m['path'] == str(source.resolve()) for m in nested['materials']['videos'])
    corrupt_mask = copy.deepcopy(composite_content)
    corrupt_mask['materials']['drafts'][0]['draft']['tracks'][2]['segments'][0]['common_keyframes'][-1]['keyframe_list'][-1]['values'] = [99]
    try:
        editor.verify_structure(corrupt_mask, composite_plan)
    except ValueError as exc:
        assert 'Mask keyframe' in str(exc)
    else:
        raise AssertionError('Nested dynamic mask tampering must fail')
    composite_published = editor.publish_draft(Args(draft=str(composite_root), draft_root=str(published_root), offline_copy=True))
    published_path = Path(composite_published['draft'])
    relocated = editor.read_json(published_path / 'draft_content.json')
    editor.native_advanced.verify_sidecars(relocated, published_path, published_path, editor.read_json)
    assert editor.verify_structure(relocated, composite_plan)['native_effects']['video_composites'] == 1
    editor.native_advanced.verify_sidecars(composite_content, composite_root, composite_root, editor.read_json)
    checks += ['dynamic rectangle X/Y/rotation keyframes and three-track composite parameter readback',
               'nested mask tampering rejected; source media preserved',
               'composite sidecar paths relocated on publish; source draft unchanged']
    wrong_storage = copy.deepcopy(composite_content)
    nested_materials = wrong_storage['materials']['drafts'][0]['draft']['materials']
    moved = nested_materials['common_mask'].pop()
    nested_materials.setdefault('masks', []).append(moved)
    wrong_storage['materials']['common_mask'] = []
    try:
        editor.verify_structure(wrong_storage, composite_plan)
    except ValueError as exc:
        assert 'mask identity/enable' in str(exc)
    else:
        raise AssertionError('Legacy mask storage must fail readback')
    checks.append('rectangle moved back to legacy mask storage rejected')
    fast = copy.deepcopy(dynamic)
    fast['clips'][2].update(source_in=0.2, speed=2, duration=1.8)
    fast['clips'][2]['animations'][0]['duration'] = 1.8
    for point in fast['clips'][2]['mask']['keyframes']:
        if point['time']:
            point['time'] = 1.6
    editor.write_json(root / 'fast-mask-plan.json', fast)
    editor.build_draft(Args(plan=str(root / 'fast-mask-plan.json'), out=str(root / 'fast-mask-draft')))
    fast_content = editor.read_json(root / 'fast-mask-draft' / 'draft_content.json')
    fast_segment = fast_content['materials']['drafts'][0]['draft']['tracks'][2]['segments'][0]
    assert [p['time_offset'] for p in fast_segment['common_keyframes'][0]['keyframe_list']] == [200000, 3400000]
    checks.append('mask source-time mapping includes nonzero source in and speed two')
    multiple = copy.deepcopy(dynamic)
    multiple['clips'].append(dict(path=str(source), start=0, duration=4, track='另一路画面'))
    multiple['composites'] = [{'name': '背景组', 'tracks': ['video', '画中画']},
                              {'name': '前景组', 'tracks': ['圆形画中画-实验', '另一路画面']}]
    editor.write_json(root / 'multiple-plan.json', multiple)
    multi_result = editor.build_draft(Args(plan=str(root / 'multiple-plan.json'), out=str(root / 'multiple-draft')))
    assert multi_result['verification']['native_effects']['video_composites'] == 2
    multi_content = editor.read_json(root / 'multiple-draft' / 'draft_content.json')
    editor.native_advanced.verify_sidecars(multi_content, root / 'multiple-draft', root / 'multiple-draft', editor.read_json)
    checks.append('two disjoint video composites preserve tracks and sidecars')
    invalid_base = plan
    plan = dynamic
    reject('dynamic unmeasured mask', lambda p: p['clips'][2]['mask'].update(name='星形'))
    reject('mask keyframe time past clip', lambda p: p['clips'][2]['mask']['keyframes'][1].update(time=4))
    reject('mask keyframes duplicate or unordered', lambda p: p['clips'][2]['mask']['keyframes'][1].update(time=0))
    reject('mask source timestamps collide after slow speed mapping', lambda p: (
        p['clips'][2].update(speed=0.1), p['clips'][2]['mask']['keyframes'][1].update(time=0.000001)))
    reject('mask property unknown', lambda p: p['clips'][2]['mask']['keyframes'][0].update(property='perspective'))
    reject('mask initial point missing', lambda p: p['clips'][2]['mask']['keyframes'][0].update(time=0.1))
    reject('composite nonexistent track', lambda p: p['composites'][0]['tracks'].append('missing'))
    reject('composite order wrong', lambda p: p['composites'][0].update(tracks=['画中画', 'video']))
    reject('composite reused tracks', lambda p: p['composites'].append({'name': '重复', 'tracks': ['video', '画中画']}))
    reject('composite must begin with a video track', lambda p: p['composites'][0].update(tracks=['captions', 'video']))
    perspective = copy.deepcopy(dynamic)
    perspective['name'] = '正式接入-后台透视-v1'
    perspective['composites'][0]['perspective'] = {'keyframes': [
        {'property': 'up_left', 'time': 0, 'values': [-0.68, -0.71]},
        {'property': 'up_left', 'time': 0.833333, 'values': [-1, -1]}]}
    editor.write_json(root / 'perspective-plan.json', perspective)
    perspective_root = root / 'perspective-draft'
    perspective_result = editor.build_draft(Args(plan=str(root / 'perspective-plan.json'), out=str(perspective_root)))
    assert perspective_result['verification']['native_effects']['corner_pin_perspectives'] == 1
    perspective_content = editor.read_json(perspective_root / 'draft_content.json')
    perspective_plan = editor.read_json(perspective_root / 'edit-plan.json')
    corrupt_pin = copy.deepcopy(perspective_content)
    outer = next(s for t in corrupt_pin['tracks'] for s in t['segments'] if
                 any(g['property_type'] == 'KFTypeCornerPinUpLeft' for g in s.get('common_keyframes', [])))
    outer['common_keyframes'][0]['keyframe_list'][0]['values'] = [-1, -1]
    try:
        editor.verify_structure(corrupt_pin, perspective_plan)
    except ValueError as exc:
        assert 'Corner pin' in str(exc)
    else:
        raise AssertionError('Corner pin tampering must fail readback')
    pin_published = editor.publish_draft(Args(draft=str(perspective_root), draft_root=str(published_root), offline_copy=True))
    pin_path = Path(pin_published['draft'])
    editor.native_advanced.verify_sidecars(editor.read_json(pin_path / 'draft_content.json'), pin_path, pin_path, editor.read_json)
    checks.append('native editable composite left-upper corner pin generation, tampering rejection and publish paths')
    plan = perspective
    reject('unmeasured corner name', lambda p: p['composites'][0]['perspective']['keyframes'][0].update(property='up_right'))
    reject('corner values shape', lambda p: p['composites'][0]['perspective']['keyframes'][0].update(values=[0]))
    reject('corner missing zero point', lambda p: p['composites'][0]['perspective']['keyframes'][0].update(time=0.1))
    reject('corner duplicate time', lambda p: p['composites'][0]['perspective']['keyframes'][1].update(time=0))
    reject('corner beyond timeline', lambda p: p['composites'][0]['perspective']['keyframes'][1].update(time=5))
    plan = invalid_base
    report = {'status': 'passed', 'checks': checks, 'rejected_cases': rejected,
            'checks_count': len(checks) + len(rejected), 'verification': result['verification'],
            'draft': str(draft), 'composite_draft': str(composite_root), 'source_hash_unchanged': True,
            'jianying_ui': 'not_tested', 'native_export': 'not_tested'}
    editor.write_json(root / 'native-self-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.out:
        report = check(Path(args.out).resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-native-check-') as temporary:
            report = check(Path(temporary) / 'check')
    editor.emit(report)
