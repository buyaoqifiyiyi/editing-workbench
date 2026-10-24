"""Software routing metadata; execution still uses the engine's existing whitelist.

Product roles describe placement; admitted_roles alone describe executable integrations.
All public functions return new values and have no host or filesystem side effects.
"""
from copy import deepcopy


_ROLES = [
    {'id': 'editing', 'label': '剪辑', 'description': '选择一个负责时间轴剪辑的软件。'},
    {'id': 'effects', 'label': '特效与合成', 'description': '选择负责镜头加工与合成的软件。'},
    {'id': 'color', 'label': '调色', 'description': '选择负责专业调色的软件；未适配功能暂不可选。'},
    {'id': 'audio', 'label': '音频', 'description': '选择负责工程声音的软件；内部音频工具可按任务调用。'},
]

_CATALOG = [
    {
        'id': 'resolve', 'name': 'DaVinci Resolve', 'short_name': '达芬奇',
        'roles': ['editing', 'effects', 'color', 'audio'], 'admitted_roles': ['editing'],
        'status': 'limited', 'status_label': '受限接入',
        'summary': '30fps顺序剪切、静态变换、立体声混音与原生字幕；支持校验后回填AE镜头。',
        'limitations': ['只覆盖单视频轨、30fps恒定帧率、已准入尺寸与时长；字幕为无样式SRT轨，关键帧、多层视频、Fusion和调色未准入。',
                        '运行前核对 doctor 与宿主连接；已有非空或已保存用户工程会阻断执行。'],
        'guides': ['reference/resolve-workflow'], 'operations': ['resolve-workflow'],
        'single_supported': True,
        'routing': '使用独立 backend=resolve 计划与 resolve-workflow validate/prepare/run/verify；AE镜头经return-ae另存回填计划后执行。',
    },
    {
        'id': 'jianying', 'name': '剪映专业版', 'short_name': '剪映',
        'roles': ['editing'], 'admitted_roles': ['editing'],
        'status': 'limited', 'status_label': '持续维护 · 按子集接入',
        'summary': '新建或续接剪映任务，保留可编辑原生参数；能力以主表为准。',
        'limitations': ['效果、资源、调色和声音仅限 capabilities 已准入子集，缓存、版本与资源身份须逐任务核实。',
                        '调色仅开放主表列明的HSL、静态三点L/RGB曲线及局部圆等受限组合；未准入组合拒绝。',
                        '保留原素材、草稿、配置与原生资源引用；加密已有工程不解密、不覆盖，按既有续接边界执行。',
                        '结构回读或原生静帧不代表保存重开、连续视听或最终导出通过。'],
        'guides': ['editing', 'capabilities', 'plan-format'],
        'operations': ['doctor', 'capabilities', 'validate', 'build', 'publish', 'native-preview',
                       'denoise-audio', 'transcribe', 'separate', 'beats'],
        'single_supported': True,
        'routing': '沿用剪映既有 validate/build/publish 路径；先核对现有项目与当前能力主表，不迁移旧环境。',
    },
    {
        'id': 'after-effects', 'name': 'Adobe After Effects', 'short_name': 'AE',
        'roles': ['effects'], 'admitted_roles': ['effects'],
        'status': 'limited', 'status_label': '局部加工接入',
        'summary': '可编辑AEP与无损镜头；原生文字动画、视频线性运动与明确凸多边形前景遮罩。',
        'limitations': ['当前使用 backend=pr-ae 的有界计划格式；最多8个连续恒速视频、30秒与准入画布/整数帧率。',
                        '文字仅整层线性动画；前景遮罩仅3–32点源像素凸多边形，可羽化/反转，源层透明度须恒为1；不接自动人物分割/跟踪、逐字动画、任意插件或Dynamic Link。',
                        '原生图层留在AEP，中间渲染素材不携带可编辑图层；软件间回填由当轮组合按对应主剪辑指南处理。',
                        '运行前核对AE可执行文件路径与宿主占用；路径缺失时使用 run-ae --ae-root 显式指定，不要求全套Adobe配置。恢复对话框等可能阻断执行。'],
        'guides': ['reference/adobe-workflow'], 'operations': ['adobe-workflow'],
        'single_supported': True,
        'routing': '独立调用 adobe-workflow validate/prepare/run-ae/verify；只生成AE工程与镜头素材，不要求主剪辑软件或导入XML。',
    },
    {
        'id': 'premiere', 'name': 'Adobe Premiere Pro', 'short_name': 'PR',
        'roles': ['editing', 'effects', 'color', 'audio'], 'admitted_roles': [],
        'status': 'unavailable', 'status_label': '尚未适配',
        'summary': '计划适配；现有XML交接保留，原生自动化与保存重开稳定性待重新准入。',
        'limitations': ['尚无已准入的公共自动导入/保存链；不能因XML已生成推断原生工程完成。'],
        'guides': ['reference/adobe-workflow'], 'operations': [], 'single_supported': False,
    },
]


def catalog(availability=None):
    """Return product placement separately from admission and local availability."""
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
                    label, reason = '尚未适配', '当前工作台尚未适配此软件的该功能。'
                else:
                    label, reason = '可选', '本机已安装，当前功能已按子集接入。'
                entry['role_availability'][role] = {'selectable': selectable,
                                                   'label': label, 'reason': reason}
    return entries


def require_available(selection, entries):
    """Reject a new selection or execution while retaining saved historical choices."""
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
            raise ValueError(f'{entry["name"]}：{reason}。请重新选择软件后端。')
    return selection


def roles():
    return deepcopy(_ROLES)


def default_selection():
    """Initial choice for new sessions only; never a legacy-session migration."""
    return {'mode': 'workflow', 'roles': {'editing': ['resolve'], 'effects': ['after-effects'],
                                        'color': [], 'audio': []}, 'single_backend': None}


def normalize_selection(value):
    """Validate routing choices without changing them or invoking software.

    Omitted role lists become empty; single mode validates supplied fields but
    discards all workflow roles. Legacy None is handled by selection_prompt only.
    """
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
                raise ValueError(f'{backend_id} 尚未准入 {role} 角色')
        normalized_roles[role] = list(entries)
    single = value.get('single_backend')
    if single is not None:
        if not isinstance(single, str) or single not in by_id:
            raise ValueError('single_backend 必须是已知软件ID或null')
        if not by_id[single]['single_supported']:
            raise ValueError(f'{single} 尚未准入单独调用')
    if mode == 'single':
        if single is None:
            raise ValueError('单独调用必须选择一个软件')
        normalized_roles = {role: [] for role in role_ids}
    else:
        if single is not None:
            raise ValueError('组合模式的 single_backend 必须为null')
        if not any(normalized_roles.values()):
            raise ValueError('组合模式至少选择一个已准入角色')
    return {'mode': mode, 'roles': normalized_roles, 'single_backend': single}


def selection_prompt(selection):
    """Describe the selected context; this grants no new execution operations."""
    if selection is None:
        return ('当前会话未绑定软件选择：沿用此会话已有工程、用户选择与历史执行路径；'
                '不应用新会话默认组合，不自动迁移、覆盖工程或改写原配置。')
    selection = normalize_selection(selection)
    by_id = {backend['id']: backend for backend in _CATALOG}
    if selection['mode'] == 'single':
        backend_ids = [selection['single_backend']]
        lines = [f"本次为单独调用：{by_id[backend_ids[0]]['name']}。只处理用户本次输入需要的该后端任务；无需主剪辑链。"]
    else:
        labels = {role['id']: role['label'] for role in _ROLES}
        assignments = [f"{labels[role]}：{'、'.join(by_id[item]['short_name'] for item in values)}"
                       for role, values in selection['roles'].items() if values]
        backend_ids = list(dict.fromkeys(item for values in selection['roles'].values() for item in values))
        lines = ['当前软件组合：' + '；'.join(assignments) + '。',
                 '仅按本次任务使用所选模块，不要求每回合运行全部软件；未选择的后端不隐性执行。']
    lines.append('先按用户目标读取planning；调用仍走已有CLI/MCP白名单与准入校验。接入状态不代表当前安装、授权或宿主连接可用，执行前按该指南核对环境。')
    for backend_id in backend_ids:
        backend = by_id[backend_id]
        lines.extend([f"{backend['name']}（{backend['status_label']}）：{backend['summary']}",
                      '指南：' + '、'.join(backend['guides']) + '。',
                      '现有操作：' + '、'.join(backend['operations']) + '。', backend['routing']])
        lines.extend('限制：' + item for item in backend['limitations'])
    lines.append('组合只交接有明确契约的素材与计划引用，保留来源和各宿主原生参数；不承诺任意跨软件原生数据互通。不得新增付费服务、自动安装或绕过准入。分别报告实际完成的结构、原生输出、保存重开、视听与导出层级。')
    lines.append('音频降噪、转写、分离和节拍分析是工作台内部工具，按需调用denoise-audio/transcribe/separate/beats；不另列软件后端，不强制创建视频工程。可选依赖与模型按任务核实，不自动下载。')
    return '\n'.join(lines)
