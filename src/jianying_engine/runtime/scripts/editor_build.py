"""Draft assembly, readback verification, delivery documents, and live publication."""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

import file_hash
import local_font
import native_advanced
import native_hsl_channels
import native_mask_feather
import native_protocol
import native_text_alpha
import native_text_glow
import native_text_highlight


def text_segment(item, target, backend):
    import editor

    style = dict(item.get('style', {}))

    def rgb(color):
        return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))

    border_color = style.pop('border_color', '#000000')
    border_width = style.pop('border_width', 40)
    shadow = style.pop('shadow', None)
    if shadow is not None:
        shadow = dict(shadow)
        shadow['color'] = rgb(shadow.get('color', '#000000'))
    transform = {'transform_x': item['x'], 'transform_y': item['y'], **item.get('transform', {})}
    segment = backend.TextSegment(
        item['text'],
        target,
        style=backend.TextStyle(
            size=item['size'],
            color=rgb(item['color']),
            bold=item['bold'],
            align=1,
            auto_wrapping=style.pop('auto_wrapping', True),
            **style,
        ),
        clip_settings=backend.ClipSettings(**transform),
        border=backend.TextBorder(color=rgb(border_color), width=border_width) if item['border'] else None,
        shadow=backend.TextShadow(**shadow) if shadow is not None else None,
    )
    # 0.3.0 gives text a MediaSegment speed reference but never registers its speed material.
    # Text has no source time range; remove this orphan while keeping subsequent animation refs.
    segment.extra_material_refs.remove(segment.speed.global_id)
    for key in sorted(item.get('keyframes', []), key=lambda k: k['time']):
        segment.add_keyframe(getattr(backend.KeyframeProperty, key['property']), editor.us(key['time']), key['value'])
    return segment


def bind_text_font(material, item, default_font=None, local_assets=None, draft_root=None):
    import editor

    rich = json.loads(material['content'])
    native_text_highlight.apply(rich, item)
    if 'font_path' in item:
        if local_assets is None or draft_root is None:
            raise ValueError('Local font binding requires a verified asset and draft root')
        local_font.bind_material(material, local_assets[item['font_path']], draft_root)
        return material
    if 'font' in item:
        rich['styles'][0]['font'] = editor.native_font(item['font'])
    elif default_font is not None:
        material['font_path'] = default_font['path']
    material['content'] = json.dumps(rich, ensure_ascii=False)
    return material


def srt_time(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'


def provenance(plan):
    """Include selected assets only; private/local originals remain unclassified."""
    import editor

    records = []
    for material in dict.fromkeys(x['path'] for group in ('clips', 'audio') for x in plan[group]):
        path = Path(material)
        sidecar = path.with_name(path.name + '.provenance.json')
        if sidecar.is_file():
            item = editor.read_json(sidecar)
            if (item.get('license_verification') != 'agent_review_recorded' or not item.get('review_note')
                    or item.get('sha256') != file_hash.sha256(path)):
                raise ValueError(f'Material provenance missing review or checksum mismatch: {path.name}')
            records.append(item)
        else:
            records.append({'local_path': str(path), 'license_verification': 'not_recorded',
                            'attribution': '', 'rights_clearance': 'not_verified'})
    return records


def source_delivery(records):
    lines = ['# 素材来源与授权记录', '',
             '来源记录与核查备注不构成完整权利担保；画面中人物、商标及最终用途仍需核对。', '']
    for item in records:
        lines += [f'## {Path(item["local_path"]).name}', '',
                  f'- 核查状态：{item["license_verification"]}',
                  f'- 来源/署名：{item.get("attribution") or "未登记；不推断为免费素材"}',
                  f'- 核查备注：{item.get("review_note", "未记录")}',
                  f'- SHA256：{item.get("sha256", "未记录")}', '']
        if item.get('license') == 'by-sa':
            lines += ['- CC BY-SA：使用者已记录相同方式分享安排，需按核查备注执行。', '']
    return '\n'.join(lines) + '\n'


def delivery(plan, path, warnings):
    lines = [f'# {plan["name"]} 剪辑交付', '',
             ('已生成剪映原生草稿；仍有下列素材或剪辑操作缺口。' if plan['missing_assets'] or plan.get('jianying_tasks') else
              '剪辑已完成：已生成剪映原生草稿，并通过计划校验、草稿结构与原生参数回读。'),
             '导出和最终检查由用户完成，不属于 Skill 的未完成项。', '',
             f'- 草稿目录：`{path}`', '- 计划：`edit-plan.json`', '- 字幕：`字幕.srt`',
             '- 验证：`verification.json`（结构检查，不代表应用兼容）',
             '- 素材来源与署名：`素材来源.md`、`素材来源.json`（未登记的素材不会被标为免费）',
             '- 原生效果与待办：`原生效果.md`（效果参数保留在草稿，未烘焙素材）', '',
             '## 时间轴', '', '| 时间范围（秒） | 轨道 | 素材 | 源入点 | 变速 | 选择依据 |',
             '|---|---|---|---|---|---|']
    for c in sorted(plan['clips'], key=lambda x: (x['start'], x['track'])):
        reason = c.get('reason', '').replace('|', '\\|').replace('\n', ' ')
        material = Path(c['path']).name.replace('|', '\\|')
        lines.append(f'| {c["start"]:.3f}–{c["start"]+c["duration"]:.3f} | {c["track"]} | {material} | {c["source_in"]:.3f} | {c["speed"]} | {reason} |')
    lines += ['', '## 取舍与素材缺口', ''] + [f'- {x}' for x in plan['notes']]
    lines += [f'- 缺少：{x}' for x in plan['missing_assets']] if plan['missing_assets'] else ['- 未记录素材缺口。']
    if plan.get('composites'):
        lines += ['', '## 原生复合片段', ''] + [
            f'- {s["name"]}：' + (f'{s["start"]:.3f}–{s["start"] + s["duration"]:.3f} 秒' if 'start' in s else '完整时间轴')
            + f'，内部轨道 {", ".join(s["tracks"])}。'
            for s in plan['composites']
        ]
    if plan.get('jianying_tasks'):
        lines += ['', '## 剪映内仍待完成', ''] + [
            f'- {t["operation"]}：轨道 {t["track"]}，{t["start"]:.3f}–{t["start"]+t["duration"]:.3f} 秒。{t["instructions"]}'
            for t in plan['jianying_tasks']]
    for clip in plan['clips']:
        primary = clip.get('grading', {}).get('primary_wheel')
        if primary:
            axis, selection = next(iter(primary.items()))
            channel, value = next(iter(selection.items()))
            lines += ['', '## 主色轮受限子集', '',
                      f'- {axis}/{channel}：{value}；11.7 静态 RGB PNG，使用安装内 v1 色轮资源。',
                      '- 仅已测单轴及固定值；界面参数编辑、保存重开和连续视听仍需确认。']
        if native_hsl_channels.requested(clip.get('grading')):
            name = next(iter(clip['grading']))
            spec = clip['grading'][name]
            titles = {'saturation': '饱和度', 'lightness': '明度', 'hue': '色相'}
            detail = '；'.join(f'{native_hsl_channels.CHANNELS[name][1]}{titles[k]}：{spec[k]}' for k in ('hue', 'saturation', 'lightness') if k in spec)
            lines += ['', '## 其他HSL子集', '', f'- {detail}；仅静态PNG受限子集。',
                      '- 邻近颜色也可能改变，不等于语义主体颜色隔离。']
        if 'green_hsl' in clip.get('grading', {}):
            green = clip['grading']['green_hsl']
            control = '明度' if 'lightness' in green else '色相' if 'hue' in green else '饱和度'
            lines += ['', '## 绿色 HSL 子集', '',
                      f'- 绿色{control}：{next(iter(green.values()))}；参数见原生效果.md。',
                      '- 绿色通道也会影响邻近黄色和青色，实际构图与颜色由当前工程单独检查。']
        if 'blue_hsl' in clip.get('grading', {}):
            blue = clip['grading']['blue_hsl']
            control = '明度' if 'lightness' in blue else '色相' if 'hue' in blue else '饱和度'
            value = next(iter(blue.values()))
            lines += ['', '## 蓝色 HSL 子集', '',
                      f'- 蓝色{control}：{value}；静图或已准入静音视频子集，参数见原生效果.md。',
                      '- 蓝色通道也会影响邻近青色和部分紫色，实际构图与颜色由当前工程单独检查。']
    local_fonts = [item['font_path'] for item in plan['captions'] if 'font_path' in item]
    if local_fonts:
        lines += ['', '## 本地字体依赖', '',
                  f'- 已复制 {len(set(local_fonts))} 份静态 TTF/OTF 到草稿 `Resources/local-fonts/`。',
                  '- 字体文件是用户提供的本地依赖；字体版权、嵌入与商业使用许可由使用者核对。',
                  '- 当前只准入静态横排文字，未验证动画、特效、重点词、透明度和竖排组合。']
    lines += ['', '## Skill 检查', '', '- 素材探测、计划校验、草稿结构及原生参数回读：通过。',
              '', '## 用户负责的最终步骤', '',
              '- 在剪映打开工程，检查构图、资源显示、参数可调、声音接缝与节奏，保存重开并导出。',
              '- 当前未执行这些用户步骤；结构检查不代表已验证实际渲染或完成成片导出。']
    if warnings:
        lines += ['', '## 用户检查重点', ''] + [f'- {x}' for x in warnings]
    return '\n'.join(lines) + '\n'


def native_delivery(plan):
    lines = ['# 剪映原生效果与待办', '',
             '效果由剪映渲染，Skill 检查草稿参数和引用；用户负责最终确认参数可调与视觉呈现，并导出。',
             '原始素材保持引用；没有外部预渲染、没有用生成的视频替换效果。', '']
    for group in ('clips', 'captions', 'audio', 'effects', 'filters'):
        for item in plan[group]:
            settings = ({key: item[key] for key in ('animations', 'effects', 'filters', 'mix_mode', 'chroma', 'mask', 'grading', 'matting_cache', 'background', 'style', 'transform', 'keyframes', 'motion_easing', 'speed_curve', 'text_curve', 'font', 'font_path', 'glow', 'highlights', 'volume', 'fade_in', 'fade_out', 'denoise', 'loudness', 'beat_marks')
                         if key in item} if group in ('clips', 'captions', 'audio') else
                        {key: item[key] for key in ('name', 'kind', 'params', 'intensity', 'keyframes') if key in item})
            if settings:
                lines += [f'## {item["track"]} · {item["start"]:.3f}–{item["start"]+item["duration"]:.3f} 秒',
                          '', '```json', json.dumps(settings, ensure_ascii=False, indent=2), '```', '']
    if plan.get('composites'):
        lines += ['## 后台原生复合配置', '', '```json', json.dumps({'composites': plan['composites']}, ensure_ascii=False, indent=2), '```', '']
    lines += ['## 剪映内待办（尚未执行）', '']
    lines += [f'- {t["operation"]} · {t["track"]} · {t["start"]} 秒：{t["instructions"]}'
              for t in plan['jianying_tasks']] or ['- 未记录智能抠像/自动跟踪待办。']
    return '\n'.join(lines) + '\n'


def verify_native(content, plan, draft_root=None, local_assets=None):
    """Check actual material references, parameter values and animation timing after serialization."""
    import editor

    backend = editor.load_backend()
    user_data = editor.configured_user_data()
    animation_cache = (Path(user_data) / 'Cache' / 'effect') if user_data else None
    local_assets = local_font.inspect_plan(plan) if local_assets is None else local_assets
    if local_assets and draft_root is None:
        raise ValueError('Local font verification requires the draft root')
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    for track in content['tracks']:
        for segment in track['segments']:
            if segment['material_id'] not in materials or any(r not in materials for r in segment.get('extra_material_refs', [])):
                raise ValueError('Draft readback contains an unresolved material reference')
    segments = {(track['name'], s['target_timerange']['start'], s['target_timerange']['duration']): s
                for track in content['tracks'] for s in track['segments']}
    counts = {}
    glow_count = native_text_glow.verify(content, plan, None)
    if glow_count:
        counts['text_glows'] = glow_count

    def equal(actual, expected, label):
        if isinstance(expected, float):
            valid = isinstance(actual, (int, float)) and math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-8)
        else:
            valid = actual == expected
        if not valid:
            raise ValueError(f'Native draft readback mismatch: {label}')

    def check_effect(actual, item):
        if item.get('keyframes'):
            expected = native_advanced.blur_material(item, None)
            equal(actual.get('effect_id'), expected['effect_id'], 'native 11.5 blur effect id')
            equal(actual.get('resource_id'), expected['resource_id'], 'native 11.5 blur resource id')
            return  # The extension validator checks every resource/parameter field.
        member, params = editor.effect_params(backend, item)
        equal(actual.get('effect_id'), member.value.effect_id, 'effect id')
        definitions = member.value.params
        equal(len(actual.get('adjust_params', [])), len(definitions), 'effect parameter count')
        for p, supplied, actual_param in zip(definitions, params, actual.get('adjust_params', [])):
            expected = p.default_value if supplied is None else p.min_value + (p.max_value - p.min_value) * supplied / 100
            equal(actual_param.get('name'), p.name, 'effect parameter name')
            equal(actual_param.get('value'), float(expected), 'effect parameter value')

    for group in ('clips', 'captions', 'effects', 'filters'):
        for item in plan[group]:
            segment = segments[(item['track'], editor.us(item['start']), editor.us(item['duration']))]
            refs = [materials[r] for r in segment.get('extra_material_refs', []) if r in materials]
            if group in ('effects', 'filters'):
                actual = materials.get(segment['material_id'], {})
                if group == 'effects':
                    check_effect(actual, item)
                    equal(actual.get('apply_target_type'), 2, 'standalone effect scope')
                else:
                    equal(actual.get('effect_id'), editor.native_enum(backend, 'filters', item['name'], True).value.effect_id, 'track filter id')
                    equal(actual.get('value'), item['intensity'] / 100.0, 'track filter intensity')
                counts['track_' + group] = counts.get('track_' + group, 0) + 1
                continue
            requested = [a for a in item['animations']
                         if native_advanced.animation_bundle(animation_cache,
                                                             editor.planned_animation_id(backend, group, a)) is not None]
            actual_animations = [a for r in refs for a in r.get('animations', [])]
            equal(len(actual_animations), len(requested), 'animation count')
            durations = {a['stage']: editor.us(a['duration']) for a in requested if 'duration' in a}
            for a in requested:
                stage = a['stage']
                kind = ('video_' if group == 'clips' else 'text_') + stage
                member = editor.native_enum(backend, kind, a['name'], True)
                actual = next((v for v in actual_animations if v['type'] == {'intro': 'in', 'outro': 'out', 'group': 'group', 'loop': 'loop'}[stage]), {})
                start = editor.us(item['duration']) - durations[stage] if stage == 'outro' else durations.get('intro', 0) if stage == 'loop' else 0
                duration = (editor.us(item['duration']) - durations.get('intro', 0) - durations.get('outro', 0)
                            if stage == 'loop' else durations[stage])
                for key, value in (('id', member.value.effect_id), ('start', start), ('duration', duration)):
                    equal(actual.get(key), value, 'animation ' + key)
            if requested:
                counts[group + '_animations'] = counts.get(group + '_animations', 0) + len(requested)
            if group == 'captions':
                material = materials[segment['material_id']]
                default_font = editor.default_text_font()
                local_asset = local_assets.get(item.get('font_path'))
                expected_path = (local_font.path_for(draft_root, local_asset).as_posix()
                                 if local_asset else
                                 default_font['path'] if ('font' not in item and default_font) else None)
                equal(material.get('font_path'), expected_path, 'default or local text font path')
                if any(material.get(k) for k in ('font_id',)):
                    raise ValueError('Unrequested top-level font metadata')
                rich = json.loads(material['content'])
                actual_fonts = [s.get('font') for s in rich['styles'] if 'font' in s]
                expected_font = (local_font.expected_font(local_asset, draft_root) if local_asset else
                                 editor.native_font(item['font']) if 'font' in item else None)
                equal(actual_fonts, [expected_font] if expected_font else [], 'text font identity/path')
                if expected_font:
                    counts['local_fonts' if local_asset else 'text_fonts'] = (
                        counts.get('local_fonts' if local_asset else 'text_fonts', 0) + 1)
                if 'text_curve' in item:
                    material = materials[segment['material_id']]
                    equal(material.get('text_curve'), {'enable': True, 'angle': item['text_curve']['angle']}, 'text curve')
                    expected = editor.text_segment(item, backend.Timerange(editor.us(item['start']), editor.us(item['duration'])), backend)
                    equal(material.get('check_flag'), expected.export_material()['check_flag'] | 128, 'text curve style flags')
                    counts['text_curves'] = counts.get('text_curves', 0) + 1
                expected = editor.text_segment(item, backend.Timerange(editor.us(item['start']), editor.us(item['duration'])), backend)
                expected_material = editor.bind_text_font(expected.export_material(), item, local_assets=local_assets,
                                                          draft_root=draft_root)
                equal(material.get('content'), expected_material.get('content'), 'text content/styles')
                if 'highlights' in item:
                    counts['caption_highlights'] = counts.get('caption_highlights', 0) + len(item['highlights'])
                if (item.get('style') or item.get('transform') or item.get('keyframes') or 'font' in item
                        or 'font_path' in item or 'highlights' in item):
                    expected = editor.text_segment(item, backend.Timerange(editor.us(item['start']), editor.us(item['duration'])), backend)
                    material = materials[segment['material_id']]
                    expected_material = editor.bind_text_font(expected.export_material(), item, local_assets=local_assets,
                                                              draft_root=draft_root)
                    for key in ('content', 'global_alpha', 'typesetting', 'letter_spacing', 'line_spacing'):
                        equal(material.get(key), expected_material.get(key), 'text ' + key)
                    exported = expected.export_json()
                    native_text_alpha.adapt(exported, item)
                    native_advanced.apply_motion_curve(exported, item)
                    native_advanced.verify_motion_curves(segment, item)
                    for key in ('clip', 'common_keyframes', 'uniform_scale'):
                        actual_value, expected_value = segment.get(key), exported.get(key)
                        if key == 'common_keyframes':
                            def without_ids(value):
                                if isinstance(value, dict):
                                    return {k: without_ids(v) for k, v in value.items() if k not in ('id', 'keyframe_list_id')}
                                return [without_ids(v) for v in value] if isinstance(value, list) else value
                            actual_value, expected_value = without_ids(actual_value), without_ids(expected_value)
                        equal(actual_value, expected_value, 'text ' + key)
                    counts['text_design'] = counts.get('text_design', 0) + 1
                continue
            for item_effect in item['effects']:
                effect_id = editor.native_enum(backend, item_effect['kind'] + '_effects', item_effect['name'], True).value.effect_id
                actual = next((r for r in refs if r.get('effect_id') == effect_id and r.get('type') in ('video_effect', 'face_effect')), {})
                check_effect(actual, item_effect)
                counts['clip_effects'] = counts.get('clip_effects', 0) + 1
            for filter_ in item['filters']:
                effect_id = editor.native_enum(backend, 'filters', filter_['name'], True).value.effect_id
                actual = next((r for r in refs if r.get('effect_id') == effect_id and r.get('type') == 'filter'), {})
                equal(actual.get('value'), filter_['intensity'] / 100.0, 'clip filter intensity')
                counts['clip_filters'] = counts.get('clip_filters', 0) + 1
            for key, resource_type in (('mix_mode', 'mix_mode'), ('chroma', 'chroma'), ('mask', 'mask'), ('background', 'canvas_' + item.get('background', {}).get('type', ''))):
                if key not in item:
                    continue
                if key == 'background':
                    resource_type = 'canvas_blur' if item[key]['type'] == 'blur' else 'canvas_color'
                actual = next((r for r in refs if r.get('type') == resource_type), {})
                if not actual:
                    raise ValueError('Native material reference missing: ' + key)
                counts[key] = counts.get(key, 0) + 1
                if key == 'mix_mode':
                    equal(actual.get('effect_id'), editor.native_enum(backend, 'mix_modes', item[key]['name'], True).value.effect_id, 'mix mode id')
                elif key == 'chroma':
                    equal(actual.get('color'), item[key]['color'].lower(), 'chroma color')
                    for name in ('intensity', 'shadow', 'edge_smooth', 'spill'):
                        equal(actual.get(name + '_value'), item[key][name] / 100.0, 'chroma ' + name)
                elif key == 'background':
                    name = 'blur' if item[key]['type'] == 'blur' else 'color'
                    equal(actual.get(name), item[key][name].lower() if name == 'color' else item[key][name], 'background ' + name)
                else:
                    m = item[key]
                    metadata = editor.native_enum(backend, 'masks', m['name'], True).value
                    resource = (native_advanced.templates()['mask']['resource_id'] if m['name'] == '矩形' else
                                native_advanced.cached_material('common_mask', None)['resource_id'] if m['name'] == '圆形' else metadata.resource_id)
                    equal(actual.get('resource_id'), resource, 'mask resource')
                    source = materials[segment['material_id']]
                    width, height = source['width'], source['height']
                    mask_width = m.get('rect_width', m['size']) if m['name'] == '矩形' else m['size'] * height * metadata.default_aspect_ratio / width
                    for k, value in (('height', m['size']), ('rotation', m['rotation']), ('invert', m['invert']),
                                     ('feather', m['feather'] / 100), ('roundCorner', m.get('round_corner', 0) / 100),
                                     ('centerX', m['center_x'] / (width / 2)), ('centerY', m['center_y'] / (height / 2)),
                                     ('width', mask_width), ('aspectRatio', metadata.default_aspect_ratio)):
                        equal(actual.get('config', {}).get(k), value, 'mask ' + k)
    return {'mode': 'jianying_native_parameters', 'parameter_readback': 'passed',
            'counts': counts, 'prerendered_effects': False, 'resources_and_rendering': 'unverified',
            'experimental_masks': counts.get('mask', 0), 'pending_jianying_tasks': len(plan.get('jianying_tasks', []))}


def verify_structure(content, plan, physical_root=None, final_root=None):
    import editor

    if 'layer_order' in plan or plan.get('composites'):
        if content.get('free_render_index_mode_on') is not True or content.get('render_index_track_mode_on') is not False:
            raise ValueError('Explicit layer order requires free render index mode')
    outer_tracks = len(content['tracks'])
    pre_counts = native_protocol.verify_pre_expand(content, plan)
    if plan.get('composites'):
        content = native_advanced.expanded_content(content, plan)
    expected = [(kind, item) for group, kind in (('clips', 'video'), ('audio', 'audio'), ('captions', 'text'),
                                              ('effects', 'effect'), ('filters', 'filter')) for item in plan.get(group, [])]
    actual = [(track['type'], segment) for track in content['tracks'] for segment in track['segments']]
    if len(actual) != len(expected):
        raise ValueError('Draft readback segment count mismatch')

    def ranges(entries):
        return sorted((kind, editor.us(x['start']), editor.us(x['duration'])) for kind, x in entries)

    actual_ranges = sorted((kind, segment['target_timerange']['start'], segment['target_timerange']['duration'])
                           for kind, segment in actual)
    if actual_ranges != ranges(expected):
        raise ValueError('Draft readback timeline range mismatch')
    expected_duration = max(editor.us(x['start']) + editor.us(x['duration']) for x in plan['clips'] if x['track'] == 'video')
    if content['duration'] != expected_duration or content['canvas_config']['width'] != plan['canvas']['width'] or content['canvas_config']['height'] != plan['canvas']['height']:
        raise ValueError('Draft readback canvas/duration mismatch')
    if 'layer_order' in plan:
        if [track['name'] for track in content['tracks']] != plan['layer_order']:
            raise ValueError('Draft readback layer order mismatch')
        for index, track in enumerate(content['tracks']):
            if any(s.get('render_index') != index for s in track['segments']):
                raise ValueError('Draft readback render order mismatch')
    local_assets = local_font.inspect_plan(plan)
    path_root = final_root or physical_root
    file_root = physical_root or path_root
    native = editor.verify_native(content, plan, path_root, local_assets)
    if local_assets:
        native['local_fonts'] = local_font.verify_bindings(
            content, plan, path_root, local_assets, file_root=file_root)
    native_protocol.verify_post_expand(
        content, plan, native, pre_counts, physical_root, final_root, editor.measure_loudness
    )
    materials = {m['id']: m for values in content['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    by_track = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
    for item in plan['clips'] + plan['audio']:
        segment = by_track[item['track'], editor.us(item['start'])]
        source = materials.get(segment['material_id'], {})
        if source.get('path') != item['path']:
            raise ValueError('Draft readback source path mismatch')
        native_advanced.verify_speed(segment, item, content)
        if item in plan['clips'] and item.get('keyframes'):
            native_advanced.verify_motion_curves(segment, item)
            ratios = (plan['canvas']['width'] / source['width'], plan['canvas']['height'] / source['height'])
            base = max(ratios) / min(ratios) if item['fit'] == 'cover' else 1
            expected_motion = {}
            backend = editor.load_backend()
            for key in item['keyframes']:
                prop = 'scale_x' if key['property'] == 'uniform_scale' else key['property']
                native_property = backend.KeyframeProperty[prop].value
                value = key['value'] * base if 'scale' in key['property'] else key['value']
                expected_motion.setdefault(native_property, []).append((editor.us(item['source_in'] + key['time'] * item['speed']), [value]))
            actual_motion = {g['property_type']: [(p['time_offset'], p['values']) for p in g['keyframe_list']]
                             for g in segment['common_keyframes'] if g['property_type'] in expected_motion}
            if actual_motion != {prop: sorted(points) for prop, points in expected_motion.items()}:
                raise ValueError('Video source-time motion keyframe readback mismatch')
        if Path(item['path']).suffix.lower() not in editor.IMAGE_EXT and segment['source_timerange'] != {
                'start': editor.us(item['source_in']), 'duration': editor.us(item['duration'] * item['speed'])}:
            raise ValueError('Draft readback source range mismatch')
    return {'status': 'structure_checked', 'duration_seconds': expected_duration / 1_000_000,
            'editing_status': 'partial' if plan['missing_assets'] or plan.get('jianying_tasks') else 'complete',
            'final_check_owner': 'user', 'export_owner': 'user',
            'segments': len(actual), 'tracks': len(content['tracks']), 'outer_tracks': outer_tracks, 'media_probe': 'passed',
            'plan_validation': 'passed', 'draft_structure': 'passed',
            'jianying_open': 'unverified', 'save_reopen': 'unverified',
            'visual_audio_review': 'unverified', 'native_export': 'unverified', 'native_effects': native}


def build_draft(args):
    import editor

    plan, probes, warnings = editor.validate_plan(args.plan, editor.configured_user_data())
    sources = editor.provenance(plan)
    backend = editor.load_backend()
    animation_drops = editor.drop_unavailable_animations(plan, backend, editor.configured_user_data())
    destination = Path(args.out).resolve()
    if destination.exists():
        raise FileExistsError(f'Output already exists; choose a new draft version: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jianying-build-', dir=destination.parent, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        script = backend.DraftFolder(str(root)).create_draft(plan['name'], **plan['canvas'], allow_replace=False)
        project = root / plan['name']
        local_assets = local_font.inspect_plan(plan)
        local_font.copy_assets(local_assets, project)
        track_kinds = {}
        for group, kind in (('audio', backend.TrackType.audio), ('clips', backend.TrackType.video),
                            ('captions', backend.TrackType.text), ('effects', backend.TrackType.effect),
                            ('filters', backend.TrackType.filter)):
            names = list(dict.fromkeys(x['track'] for x in plan[group]))
            if group == 'clips':
                names = ['video'] + [name for name in names if name != 'video']
            track_kinds.update((name, kind) for name in names)
        script.append_tracks([backend.TrackSpec(track_kinds[name], name)
                              for name in plan.get('layer_order', list(track_kinds))])
        for group, kind in (('audio', backend.TrackType.audio), ('clips', backend.TrackType.video), ('captions', backend.TrackType.text)):
            for item in sorted(plan[group], key=lambda x: x['start']):
                target = backend.Timerange(editor.us(item['start']), editor.us(item['duration']))
                if group == 'captions':
                    segment = editor.text_segment(item, target, backend)
                    editor.apply_animations(segment, item, backend, text=True)
                else:
                    source = backend.Timerange(editor.us(item['source_in']), editor.us(item['duration'] * item['speed']))
                    if group == 'audio':
                        # Passing speed with source_timerange makes 0.3.0 recompute the target as
                        # round(source/speed), which can drift 1us; derive speed from both ranges like video.
                        segment = backend.AudioSegment(item['path'], target, source_timerange=source, volume=item['volume'])
                        for key in item.get('keyframes', []):
                            segment.add_keyframe(editor.us(item['source_in'] + key['time'] * item['speed']), key['value'])
                        if item['fade_in'] or item['fade_out']:
                            segment.add_fade(editor.us(item['fade_in']), editor.us(item['fade_out']))
                    else:
                        info = probes[item['path']]
                        ratios = (plan['canvas']['width'] / info['width'], plan['canvas']['height'] / info['height'])
                        base = max(ratios) / min(ratios) if item['fit'] == 'cover' else 1
                        transform = dict(item['transform'])
                        for axis in ('scale_x', 'scale_y'):
                            transform[axis] = transform.get(axis, 1) * base
                        segment = backend.VideoSegment(
                            item['path'], target,
                            source_timerange=None if info['kind'] == 'image' else source,
                            volume=item['volume'], clip_settings=backend.ClipSettings(**transform),
                        )
                        for key in sorted(item['keyframes'], key=lambda x: x['time']):
                            value = key['value'] * base if 'scale' in key['property'] else key['value']
                            key_time = item['source_in'] + key['time'] * item['speed']
                            segment.add_keyframe(getattr(backend.KeyframeProperty, key['property']), editor.us(key_time), value)
                        if 'transition' in item:
                            tr = item['transition']
                            segment.add_transition(backend.TransitionType[tr['name']], duration=editor.us(tr['duration']))
                        editor.apply_native(segment, item, backend)
                script.add_segment(segment, item['track'])
        for group, kind in (('effects', backend.TrackType.effect), ('filters', backend.TrackType.filter)):
            for item in sorted(plan[group], key=lambda x: x['start']):
                target = backend.Timerange(editor.us(item['start']), editor.us(item['duration']))
                if group == 'effects':
                    member, params = editor.effect_params(backend, item)
                    script.add_effect(member, target, item['track'], params=params)
                else:
                    script.add_filter(editor.native_enum(backend, 'filters', item['name'], True), target,
                                      item['track'], intensity=item['intensity'])
        draft_id = str(uuid.uuid4()).upper()
        script.content['id'] = draft_id
        script.save()
        content = editor.read_json(project / 'draft_content.json')
        text_materials = {m['id']: m for m in content['materials']['texts']}
        text_segments = {(t['name'], s['target_timerange']['start']): s for t in content['tracks'] for s in t['segments']}
        default_font = editor.default_text_font()
        if any('font' not in item and 'font_path' not in item for item in plan['captions']) and default_font is None:
            warnings.append('No installed Jianying default font found; text may show placeholder glyphs '
                            'until Jianying repairs the draft itself.')
        for item in plan['captions']:
            segment = text_segments[item['track'], editor.us(item['start'])]
            editor.bind_text_font(text_materials[segment['material_id']], item, default_font,
                                  local_assets=local_assets, draft_root=destination)
            native_text_alpha.adapt(segment, item)
        native_protocol.apply_post_save(content, plan, project, destination, editor.measure_loudness)
        media_library = editor.register_media_library(content, (project, destination))
        has_animations = any(item.get('animations') for item in plan['clips'] + plan['captions'])
        animation_paths = native_advanced.bind_animation_paths(content, editor.configured_user_data() if has_animations else None)
        dropped_animations = animation_drops or animation_paths['dropped']
        if dropped_animations:
            warnings.append('Animation bundle is absent from the local effect cache for ' +
                            ', '.join(f"{d['name']}({d['id']})" for d in dropped_animations) +
                            '; it was dropped from the draft. Jianying draws no animation whose resource is '
                            'missing: it marks the segment as lost and keeps looking for it while the draft '
                            'opens, which is slow and needs network. Measured on 2026-10-16 with 11.6.')
        if animation_paths['unverified']:
            warnings.append('Animation bundle could not be resolved for ' +
                            ', '.join(sorted(set(animation_paths['unverified']))) +
                            ': no local Jianying user data, so the app must download the resource on first open.')
        native_advanced.write_sidecars(content, project, destination, editor.write_json)
        editor.write_json(project / 'draft_content.json', content)
        status = editor.verify_structure(content, plan, project, destination)
        native_advanced.verify_sidecars(content, project, destination, editor.read_json)
        meta = editor.read_json(project / 'draft_meta_info.json')
        meta.update(draft_id=draft_id, draft_name=plan['name'], draft_fold_path=str(destination),
                    draft_root_path=str(destination.parent), tm_duration=content['duration'],
                    tm_draft_create=int(time.time() * 1_000_000), tm_draft_modified=int(time.time() * 1_000_000))
        for group in meta.get('draft_materials', []):
            if group.get('type') == 0:
                group['value'] = media_library
        editor.write_json(project / 'draft_meta_info.json', meta)
        editor.write_json(project / 'edit-plan.json', plan)
        editor.write_json(project / 'verification.json', status)
        editor.write_json(project / '素材来源.json', {'schema_version': 1, 'materials': sources})
        (project / '素材来源.md').write_text(editor.source_delivery(sources), encoding='utf-8')
        (project / '原生效果.md').write_text(editor.native_delivery(plan), encoding='utf-8')
        captions = sorted((x for x in plan['captions'] if x['track'] == 'captions'), key=lambda x: x['start'])
        srt = '\n\n'.join(f'{i+1}\n{editor.srt_time(c["start"])} --> {editor.srt_time(c["start"]+c["duration"])}\n'
                           + re.sub(r'\n\s*\n', '\n', c['text'].replace('\r\n', '\n').replace('\r', '\n')).strip('\n')
                           for i, c in enumerate(captions))
        (project / '字幕.srt').write_text(srt + ('\n' if srt else ''), encoding='utf-8')
        (project / '剪辑交付.md').write_text(editor.delivery(plan, destination, warnings), encoding='utf-8')
        editor.write_json(project / '_skill_manifest.json', {
            'generator': 'jianying-editor', 'version': 2, 'backend': 'pyJianYingDraft==0.3.0',
            'draft_content_sha256': hashlib.sha256((project / 'draft_content.json').read_bytes()).hexdigest(),
            'local_fonts': local_font.manifest(local_assets)})
        project.rename(destination)
    return {'draft': str(destination), 'delivery': str(destination / '剪辑交付.md'),
            'verification': status, 'warnings': warnings, 'missing_assets': plan['missing_assets'],
            'animation_paths': animation_paths, 'local_fonts': local_font.manifest(local_assets)}


def publish_draft(args):
    import editor

    source = Path(args.draft).resolve()
    editor.require_plaintext_draft(source / 'draft_content.json', 'to publish this draft')
    settings = editor.draft_location()
    requested_root = getattr(args, 'draft_root', None) or settings['active_draft_root']
    if not requested_root:
        raise ValueError('No configured draft root found; specify a confirmed --draft-root')
    root = Path(requested_root).resolve()
    offline = getattr(args, 'offline_copy', False)
    if not offline and settings['active_draft_root'] and root != Path(settings['active_draft_root']).resolve():
        raise ValueError(f"Destination differs from Jianying configured draft root: {settings['active_draft_root']}. Use that directory for live publication, or --offline-copy for an archive.")
    wait_seconds = editor.number(getattr(args, 'wait_seconds', 5), 'wait-seconds', 0, 30)
    manifest = editor.read_json(source / '_skill_manifest.json')
    if manifest.get('generator') != 'jianying-editor' or hashlib.sha256((source / 'draft_content.json').read_bytes()).hexdigest() != manifest['draft_content_sha256']:
        raise ValueError('Only an unchanged draft generated by this Skill can be published')
    plan, _, warnings = editor.validate_plan(source / 'edit-plan.json', editor.configured_user_data(), mask_resource_root=source)
    editor.provenance(plan)
    content = editor.read_json(source / 'draft_content.json')
    editor.verify_structure(content, plan, source, source)
    local_assets = local_font.inspect_plan(plan)
    native_advanced.verify_sidecars(content, source, source, editor.read_json)
    if not root.is_dir():
        raise ValueError('Confirmed draft root must already exist')
    if root == source or source in root.parents:
        raise ValueError('Draft root cannot be inside source draft')
    destination = root / plan['name']
    if destination.exists():
        raise FileExistsError('Draft name already exists; build a new named version')
    with tempfile.TemporaryDirectory(prefix='.jianying-copy-', dir=root, ignore_cleanup_errors=True) as temporary:
        staged = Path(temporary) / 'draft'
        shutil.copytree(source, staged)
        if local_assets:
            local_font.rebase(content, plan, destination, local_assets)
        if any(native_mask_feather.requested(item) for item in plan['clips']):
            native_mask_feather.relocate(content, plan, source, destination)
        editor.verify_structure(content, plan, staged, destination)
        meta = editor.read_json(staged / 'draft_meta_info.json')
        meta.update(draft_fold_path=str(destination), draft_root_path=str(root))
        editor.write_json(staged / 'draft_meta_info.json', meta)
        native_advanced.write_sidecars(content, staged, destination, editor.write_json)
        native_advanced.verify_sidecars(content, staged, destination, editor.read_json)
        editor.write_json(staged / 'draft_content.json', content)
        manifest['draft_content_sha256'] = hashlib.sha256((staged / 'draft_content.json').read_bytes()).hexdigest()
        editor.write_json(staged / '_skill_manifest.json', manifest)
        (staged / '剪辑交付.md').write_text(editor.delivery(plan, destination, warnings), encoding='utf-8')
        (staged / '原生效果.md').write_text(editor.native_delivery(plan), encoding='utf-8')
        staged.rename(destination)
    registration = editor.wait_for_registration(destination, settings['homepage_index'], 0 if offline else wait_seconds)
    return {'draft': str(destination), 'copied': True, 'global_index_modified': False,
            'configured_draft_root': settings['active_draft_root'], 'registration': registration,
            'homepage_visibility': 'pending_ui_verification' if registration['status'] == 'registered' else 'unverified',
            'jianying_open': 'unverified', 'restart_required': False if registration['status'] == 'registered' else None}
