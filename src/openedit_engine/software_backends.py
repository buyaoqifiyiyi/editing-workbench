"""Open-source software backend catalog and routing helpers; no host or filesystem side effects."""
from copy import deepcopy


_ROLES = [
    {'id': 'editing', 'label': '剪辑与时间轴', 'description': '负责多轨时间轴剪辑、转场、图形字幕、调色与最终成片导出。'},
    {'id': 'effects', 'label': '动效与合成', 'description': '负责高级文字动画、图层样式、表达式、特效与透明通道叠层渲染。'},
    {'id': 'color', 'label': '调色', 'description': '负责 Lumetri 曲线、色轮、HSL、曝光与 LUT 调色。'},
    {'id': 'audio', 'label': '音频与混音', 'description': '负责多轨音频混音、响度计量与离线 AI 音频处理。'},
    {'id': 'media_prep', 'label': '媒体预处理与验收', 'description': '负责媒体探测、VFR 标准化转码、抽帧与严格全解码验收。'},
]

_CATALOG = [
    {
        'id': 'filmcraft', 'name': 'FilmCraft', 'short_name': 'FilmCraft',
        'license': 'MIT OR Apache-2.0',
        'repository': 'https://github.com/storytold/filmcraft',
        'roles': ['editing', 'color', 'audio'], 'admitted_roles': ['editing', 'color', 'audio'],
        'status': 'ready', 'status_label': '开源主剪辑内核',
        'summary': '纯 Rust 开源非线性剪辑与调色混音内核；明文 .fcproj 工程、无分辨率/轨数锁、MCP + 718 条命令、OTIO/FCPXML/H.264/ProRes/NVENC 导出。',
        'limitations': [
            '纯 Rust 解码器对手机 VFR 或异形封装建议先经 media-prep/ffmpeg 标准化；导出前需严格解码验收。',
            '无头导出已实测验证，人工 GUI 打开 .fcproj 微调与完整视听审片由使用者按需执行。',
        ],
        'guides': ['editing', 'capabilities', 'plan-format', 'reference/openedit'],
        'operations': ['doctor', 'capabilities', 'validate', 'build', 'preview', 'export', 'filmcraft-exec'],
        'single_supported': True,
        'routing': '通过 validate / build / preview / export 或 filmcraft-exec 驱动，生成明文 .fcproj 并严格校验导出成片。',
    },
    {
        'id': 'effectcraft', 'name': 'EffectCraft', 'short_name': 'EffectCraft',
        'license': 'MIT OR Apache-2.0',
        'repository': 'https://github.com/storytold/effectcraft',
        'roles': ['effects'], 'admitted_roles': ['effects'],
        'status': 'ready', 'status_label': '开源特效与动效内核',
        'summary': '纯 Rust 开源动效合成内核；明文 .ecproj 工程、308 种特效、9 种图层样式、表达式、文字动画器、3D 相机/灯光、ProRes 4444 (yuva444p12le) 透明通道导出。',
        'limitations': [
            '透明叠层统一使用 ProRes 4444 RGBA (.mov) 中间件（Phase 0 实测 WebM VP9 默认导出不含可用 Alpha）。',
        ],
        'guides': ['editing', 'capabilities', 'plan-format', 'reference/openedit'],
        'operations': ['render-overlay', 'effectcraft-exec', 'build'],
        'single_supported': True,
        'routing': '复杂动效与透明叠层经 render-overlay 或 build 内的 overlays 自动渲染为 ProRes 4444 (.mov) 并交由 FilmCraft 合成。',
    },
    {
        'id': 'ffmpeg', 'name': 'FFmpeg / FFprobe', 'short_name': 'FFmpeg',
        'license': 'LGPL/GPL',
        'repository': 'https://ffmpeg.org',
        'roles': ['media_prep'], 'admitted_roles': ['media_prep'],
        'status': 'ready', 'status_label': '媒体探测与预处理底座',
        'summary': '本地媒体元信息探测、VFR 恒定帧率标准化、帧提取与导出成品严格全解码核验。',
        'limitations': ['不替代主时间轴非线编排；与各离线 AI 工具协同运行。'],
        'guides': ['capabilities', 'reference/general-editing'],
        'operations': ['inspect', 'reference', 'normalize-media', 'practice-compare', 'reverse', 'denoise-audio'],
        'single_supported': True,
        'routing': '通过 inspect / normalize-media / reference 及导出后严格全解码调用。',
    },
]


def catalog(availability=None):
    """Return open-source backend metadata, optionally enriched with local availability."""
    entries = deepcopy(_CATALOG)
    if availability is not None:
        for entry in entries:
            entry['availability'] = deepcopy(availability[entry['id']])
            entry['selectable'] = bool(entry['single_supported'] and entry['availability']['selectable'])
            entry['role_availability'] = {}
            for role in entry['roles']:
                admitted = role in entry['admitted_roles']
                selectable = bool(admitted and entry['selectable'])
                if not entry['availability']['selectable']:
                    label = entry['availability']['label']
                    reason = entry['availability']['reason']
                elif not admitted:
                    label, reason = '未接入', '当前引擎尚未开放该角色。'
                else:
                    label, reason = '可用', '本机已就绪，完整开放子集可用。'
                entry['role_availability'][role] = {'selectable': selectable, 'label': label, 'reason': reason}
    return entries


def require_available(selection, entries):
    """Validate that every selected open-source backend is available on this machine."""
    if selection is None:
        return None
    selection = normalize_selection(selection)
    selected = ([selection['single_backend']] if selection['mode'] == 'single' else
                list(dict.fromkeys(item for values in selection['roles'].values() for item in values)))
    by_id = {entry['id']: entry for entry in entries}
    for backend_id in selected:
        entry = by_id[backend_id]
        if not entry.get('selectable', False):
            reason = entry.get('availability', {}).get('reason') or '当前不可用'
            raise ValueError(f'{entry["name"]}：{reason}。请检查本机安装。')
    return selection


def roles():
    return deepcopy(_ROLES)


def default_selection():
    """Default open-source production workflow: FilmCraft + EffectCraft + FFmpeg."""
    return {
        'mode': 'workflow',
        'roles': {
            'editing': ['filmcraft'],
            'effects': ['effectcraft'],
            'color': ['filmcraft'],
            'audio': ['filmcraft'],
            'media_prep': ['ffmpeg'],
        },
        'single_backend': None,
    }


def normalize_selection(value):
    """Validate open-source backend selection choices without executing anything."""
    if not isinstance(value, dict):
        raise ValueError('软件选择必须是对象')
    if set(value) - {'mode', 'roles', 'single_backend'}:
        raise ValueError('软件选择包含未知字段')
    mode = value.get('mode')
    if mode not in ('workflow', 'single'):
        raise ValueError('mode 必须是 workflow 或 single')
    selected_roles = value.get('roles', {})
    if not isinstance(selected_roles, dict):
        raise ValueError('roles 必须是对象')
    role_ids = [role['id'] for role in _ROLES]
    if set(selected_roles) - set(role_ids):
        raise ValueError('roles 包含未知角色')
    by_id = {backend['id']: backend for backend in _CATALOG}
    normalized_roles = {}
    for role in role_ids:
        entries = selected_roles.get(role, [])
        if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
            raise ValueError(f'{role} 必须是软件ID数组')
        if len(entries) != len(set(entries)):
            raise ValueError(f'{role} 不允许重复软件')
        if role == 'editing' and len(entries) > 1:
            raise ValueError('剪辑角色最多选择一个软件')
        for backend_id in entries:
            if backend_id not in by_id:
                raise ValueError(f'未知软件: {backend_id}')
            if role not in by_id[backend_id]['admitted_roles']:
                raise ValueError(f'{backend_id} 不支持 {role} 角色')
        normalized_roles[role] = list(entries)
    single = value.get('single_backend')
    if single is not None:
        if not isinstance(single, str) or single not in by_id:
            raise ValueError('single_backend 必须是已知软件ID或null')
        if not by_id[single]['single_supported']:
            raise ValueError(f'{single} 不支持单独调用')
    if mode == 'single':
        if single is None:
            raise ValueError('单独调用必须选择一个软件')
        normalized_roles = {role: [] for role in role_ids}
    else:
        if single is not None:
            raise ValueError('组合模式的 single_backend 必须为null')
        if not any(normalized_roles.values()):
            raise ValueError('组合模式至少选择一个角色')
    return {'mode': mode, 'roles': normalized_roles, 'single_backend': single}


def selection_prompt(selection):
    """Describe active open-source backend routing in concise Chinese."""
    if selection is None:
        selection = default_selection()
    else:
        selection = normalize_selection(selection)
    by_id = {backend['id']: backend for backend in _CATALOG}
    if selection['mode'] == 'single':
        backend_id = selection['single_backend']
        return f"当前单独调用开源后端：{by_id[backend_id]['name']}。按计划或直传命令执行并验证产物。"
    parts = [f"{role['label']}={by_id[bid]['name']}"
             for role in _ROLES for bid in selection['roles'].get(role['id'], [])]
    return '当前开源工作流组合：' + '、'.join(parts) + '。'
