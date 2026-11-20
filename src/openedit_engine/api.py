"""One pure open-source whitelist, schemas, backend catalog, and BM25 knowledge retrieval."""
from collections import Counter
import math
from pathlib import Path
import re
import sys
import unicodedata

RUNTIME = Path(__file__).resolve().parent / 'runtime'

_CORE = {
    'doctor': 'Diagnose local open-source FilmCraft, EffectCraft, FFmpeg, GPU, and AI dependencies.',
    'capabilities': 'Describe open-source capabilities and optionally query live FilmCraft/EffectCraft commands.',
    'inspect': 'Probe local media and extract sample frames.',
    'reference': 'Extract reference frames and scene cuts via FFmpeg.',
    'normalize-media': 'Normalize VFR or exotic-codec video to CFR H.264/AAC or ProRes for FilmCraft ingest.',
    'validate': 'Validate an open edit-plan.json (v1/v2) and probe all local media dependencies.',
    'build': 'Compile an edit plan into EffectCraft ProRes 4444 overlays, a FilmCraft .fcproj project, optional previews, and optional verified export.',
    'preview': 'Render PNG review frames from an existing FilmCraft .fcproj project.',
    'export': 'Export an existing FilmCraft .fcproj project and verify output streams with ffprobe and strict ffmpeg decode.',
    'render-overlay': 'Build and verify a standalone EffectCraft .ecproj, RGBA PNG, and ProRes 4444 alpha .mov overlay.',
    'filmcraft-exec': 'Direct JSON command escape hatch to all 718 FilmCraft engine commands.',
    'effectcraft-exec': 'Direct JSON command and AE-style JSX script escape hatch to all 308 EffectCraft effects and 763 commands.',
}

_SCRIPTS = {
    'practice-compare': ('practice_compare.py', 'Render bounded side-by-side comparison video from two local ranges.'),
    'mesh-avatar': ('mesh_avatar.py', 'Build/pack bounded identity-preserving Mesh avatar transparent media.'),
    'layout-tracks': ('layout_tracks.py', 'Compact safe non-overlapping overlay video tracks while preserving layer order.'),
    'music-edit': ('music_edit.py', 'Prepare bounded media, run configured multimodal model analysis/review, and compile frame-clock open edit plans.'),
    'beats': ('beats.py', 'Detect offline candidate beats from a selected local audio interval using librosa.'),
    'beat-apply': ('beat_apply.py', 'Apply every-N candidate-beat cuts or beat marks into a new validated edit plan.'),
    'denoise-audio': ('offline_denoise.py', 'Create FFT-denoised PCM audio excerpts with explicit noise-floor settings.'),
    'reverse': ('reverse_media.py', 'Create bounded reversed video source media with explicit audio policy.'),
    'migrate': ('migrate.py', 'Diagnose and remap media root paths cleanly between devices or folders.'),
    'transcribe': ('transcribe.py', 'Offline ASR transcription with caller-supplied Whisper weights.'),
    'separate': ('separate.py', 'Offline stem separation using caller-supplied Demucs ONNX weights.'),
    'ducking': ('ducking.py', 'Generate measured music ducking volume keyframes from speech/vocals.'),
    'captions': ('captions.py', 'Map transcript or SRT cues into editable captions and optional karaoke highlights.'),
    'tracking': ('tracking.py', 'Offline single-object ROI tracking via OpenCV CSRT.'),
    'tracking-apply': ('tracking_apply.py', 'Apply tracked trajectory into position keyframes on an edit plan.'),
    'stock': ('stock.py', 'Explicit online stock media search/download with provenance.'),
    'checks': ('run_checks.py', 'Run registered open-source self-checks in isolated temporary directories.'),
}

_GUIDES = {
    'editing': 'guides/editing.md',
    'planning': 'guides/planning.md',
    'maintenance': 'guides/maintenance.md',
    'capabilities': 'references/capabilities.md',
    'plan-format': 'references/plan-format.md',
    'migration': 'references/migration.md',
}
for _path in sorted((RUNTIME / 'references').rglob('*.md')):
    _relative = _path.relative_to(RUNTIME).as_posix()
    if _relative not in _GUIDES.values():
        _GUIDES['reference/' + _path.relative_to(RUNTIME / 'references').with_suffix('').as_posix()] = _relative


def operations():
    return {**_CORE, **{name: value[1] for name, value in _SCRIPTS.items()}}


def console_python():
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').is_file():
        python = python.with_name('python.exe')
    return str(python)


def command_for(operation, arguments):
    if not isinstance(operation, str) or operation not in operations():
        raise ValueError(f'Unknown operation: {operation}')
    if not isinstance(arguments, list) or any(not isinstance(arg, str) or '\0' in arg for arg in arguments):
        raise ValueError('arguments must be an array of strings without NUL')
    script, prefix = ('open_engine.py', [operation]) if operation in _CORE else (_SCRIPTS[operation][0], [])
    path = RUNTIME / 'scripts' / script
    if not path.is_file():
        raise ValueError(f'Installed engine is incomplete: missing {script}')
    return [console_python(), str(path), *prefix, *arguments]


def _str_param(flag, *, required=False, positional=False, choices=None, description=''):
    spec = {'type': 'string', 'required': required}
    if positional:
        spec['positional'] = True
    else:
        spec['flag'] = flag
    if choices is not None:
        spec['choices'] = tuple(choices)
    if description:
        spec['description'] = description
    return spec


def _num_param(flag, kind='number', *, required=False, choices=None, description=''):
    spec = {'flag': flag, 'type': kind, 'required': required}
    if choices is not None:
        spec['choices'] = tuple(choices)
    if description:
        spec['description'] = description
    return spec


def _bool_param(flag, description=''):
    spec = {'flag': flag, 'type': 'boolean', 'required': False}
    if description:
        spec['description'] = description
    return spec


def _list_param(flag, item_type='string', *, required=False, description=''):
    spec = {'flag': flag, 'type': f'{item_type}_list', 'required': required}
    if description:
        spec['description'] = description
    return spec


OPERATION_SCHEMAS = {
    'doctor': {'params': {}},
    'capabilities': {'params': {
        'query': _str_param('--query', description='Filter live FilmCraft commands and EffectCraft effects'),
        'live': _bool_param('--live', 'Probe live FilmCraft/EffectCraft CLI binaries'),
    }},
    'inspect': {'params': {
        'input': _str_param('--input', required=True, description='Source file or media folder'),
        'out': _str_param('--out', required=True, description='New probe output directory'),
        'frames': _num_param('--frames', 'integer', description='Frames per file in [1, 12]'),
        'max_files': _num_param('--max-files', 'integer', description='Max scanned media files in [1, 1000]'),
    }},
    'reference': {'params': {
        'input': _str_param('--input', required=True, description='Reference video path'),
        'out': _str_param('--out', required=True, description='New reference output directory'),
        'start': _num_param('--start'),
        'duration': _num_param('--duration'),
        'threshold': _num_param('--threshold'),
        'max_frames': _num_param('--max-frames', 'integer', description='Integer in [2, 96]'),
    }},
    'normalize-media': {'params': {
        'input': _str_param('--input', required=True),
        'out': _str_param('--out', required=True),
        'fps': _num_param('--fps'),
        'width': _num_param('--width', 'integer'),
        'height': _num_param('--height', 'integer'),
        'codec': _str_param('--codec', choices=('h264', 'prores')),
        'crf': _num_param('--crf', 'integer'),
    }},
    'validate': {'params': {
        'plan': _str_param('--plan', required=True, description='Edit plan JSON path'),
    }},
    'build': {'params': {
        'plan': _str_param('--plan', required=True, description='Edit plan JSON path'),
        'out': _str_param('--out', required=True, description='New project output directory'),
        'export': _str_param('--export', description='Optional final rendered media path (.mp4/.mov)'),
        'preview_times': _list_param('--preview-times', 'number', description='Optional preview timestamps in seconds'),
        'hardware_encoding': _str_param('--hardware-encoding', choices=('off', 'auto')),
        'export_otio': _str_param('--export-otio', description='Optional OTIO output path'),
        'export_fcpxml': _str_param('--export-fcpxml', description='Optional FCPXML output path'),
    }},
    'preview': {'params': {
        'project': _str_param('--project', required=True, description='Path to .fcproj file'),
        'out': _str_param('--out', required=True, description='New preview directory'),
        'times': _list_param('--times', 'number', required=True, description='Timeline timestamps in seconds'),
        'max_side': _num_param('--max-side', 'integer', description='0 for full resolution'),
    }},
    'export': {'params': {
        'project': _str_param('--project', required=True, description='Path to .fcproj file'),
        'out': _str_param('--out', required=True, description='New output media path'),
        'format': _str_param('--format', choices=('h264', 'hevc', 'prores', 'wav', 'aac')),
        'crf': _num_param('--crf', 'integer'),
        'hardware_encoding': _str_param('--hardware-encoding', choices=('off', 'auto')),
        'no_burn_captions': _bool_param('--no-burn-captions'),
        'no_audio': _bool_param('--no-audio'),
    }},
    'render-overlay': {'params': {
        'spec': _str_param('--spec', required=True, description='JSON file path or inline EffectCraft spec JSON'),
        'out': _str_param('--out', required=True, description='New overlay output directory'),
        'no_video': _bool_param('--no-video', 'Skip ProRes 4444 MOV export and emit PNG/ecproj only'),
        'gpu': _bool_param('--gpu', 'Enable EffectCraft GPU flag during render'),
    }},
    'filmcraft-exec': {'params': {
        'commands': _str_param('--commands', required=True, description='JSON array [{id, params}] or file path'),
        'project': _str_param('--project'),
        'save_as': _str_param('--save-as'),
    }},
    'effectcraft-exec': {'params': {
        'commands': _str_param('--commands', description='JSON array [{command, params}] or file path'),
        'script': _str_param('--script', description='AE-style JS source string or .jsx path'),
        'project': _str_param('--project'),
        'save_as': _str_param('--save-as'),
    }},
    'captions': {'params': {
        'plan': _str_param(None, required=True, positional=True, description='Input edit plan JSON path'),
        'transcript': _str_param('--transcript', description='Transcript JSON path (mutually exclusive with srt)'),
        'srt': _str_param('--srt', description='Plain-text UTF-8 SRT path (mutually exclusive with transcript)'),
        'timebase': _str_param('--timebase', choices=('source', 'timeline')),
        'group': _str_param('--group', choices=('clips', 'audio')),
        'index': _list_param('--index', 'integer', required=True, description='One or more item indices'),
        'out': _str_param('--out', required=True, description='New output plan JSON path'),
        'track': _str_param('--track'),
        'karaoke_mode': _str_param('--karaoke-mode', choices=('active', 'cumulative')),
        'karaoke_color': _str_param('--karaoke-color'),
        'karaoke_size': _num_param('--karaoke-size'),
        'karaoke_max_chars': _num_param('--karaoke-max-chars', 'integer'),
        'karaoke_max_words': _num_param('--karaoke-max-words', 'integer'),
        'karaoke_max_gap': _num_param('--karaoke-max-gap'),
        'karaoke_joiner': _str_param('--karaoke-joiner'),
        'level': _str_param('--level', choices=('segment', 'word')),
    }},
    'music-edit': {
        'params': {'config': _str_param('--config', description='Optional omni config JSON path')},
        'subcommands': {
            'doctor': {},
            'prepare': {
                'request': _str_param('--request', required=True),
                'out': _str_param('--out', required=True),
                'video_profile': _str_param('--video-profile', choices=('proxy', 'review')),
                'video_fps': _num_param('--video-fps', 'integer', choices=(1, 8)),
                'frame_times': _list_param('--frame-times', 'number'),
            },
            'analyze': {'bundle': _str_param('--bundle', required=True)},
            'review': {'bundle': _str_param('--bundle', required=True)},
            'compile': {
                'bundle': _str_param('--bundle', required=True),
                'out': _str_param('--out', required=True),
                'backend': _str_param('--backend', choices=('openedit', 'filmcraft')),
                'width': _num_param('--width', 'integer'),
                'height': _num_param('--height', 'integer'),
                'beats': _str_param('--beats'),
                'snap_window': _num_param('--snap-window'),
            },
        },
    },
    'practice-compare': {'params': {
        'left': _str_param('--left', required=True),
        'right': _str_param('--right', required=True),
        'left_start': _num_param('--left-start'),
        'right_start': _num_param('--right-start'),
        'duration': _num_param('--duration', required=True),
        'fps': _num_param('--fps', 'integer', choices=(15, 30)),
        'audio': _str_param('--audio', required=True, choices=('none', 'left', 'right')),
        'goal': _str_param('--goal', required=True),
        'change': _str_param('--change', required=True),
        'out': _str_param('--out', required=True),
    }},
    'beats': {'params': {
        'input': _str_param('--input', required=True),
        'start': _num_param('--start'),
        'duration': _num_param('--duration', required=True),
        'out': _str_param('--out', required=True),
    }},
    'beat-apply': {'params': {
        'plan': _str_param('--plan', required=True),
        'beats': _str_param('--beats', required=True),
        'every': _num_param('--every', 'integer'),
        'mark_audio': _bool_param('--mark-audio'),
        'out': _str_param('--out', required=True),
    }},
    'ducking': {'params': {
        'plan': _str_param(None, required=True, positional=True, description='Input edit plan JSON path'),
        'vocals': _str_param('--vocals'),
        'speech_json': _str_param('--speech-json'),
        'out': _str_param('--out', required=True),
        'track': _str_param('--track'),
        'duck_db': _num_param('--duck-db'),
        'attack': _num_param('--attack'),
        'release': _num_param('--release'),
        'floor_margin_db': _num_param('--floor-margin-db'),
        'threshold_floor_db': _num_param('--threshold-floor-db'),
        'min_speech': _num_param('--min-speech'),
        'merge_gap': _num_param('--merge-gap'),
    }},
    'checks': {'params': {
        'only': _list_param('--only', 'string'),
        'keep': _str_param('--keep'),
        'timeout': _num_param('--timeout', 'integer'),
        'demucs_model': _str_param('--demucs-model'),
        'whisper_model': _str_param('--whisper-model'),
    }},
}


def _export_param_dict(params):
    out = {}
    for name, spec in params.items():
        copied = dict(spec)
        if 'choices' in copied:
            copied['choices'] = list(copied['choices'])
        out[name] = copied
    return out


def _export_schema(schema):
    exported = {}
    if 'params' in schema:
        exported['params'] = _export_param_dict(schema['params'])
    if 'subcommands' in schema:
        exported['subcommands'] = {sub: _export_param_dict(sub_params)
                                   for sub, sub_params in schema['subcommands'].items()}
    return exported


def operation_schemas(operation=None):
    """Return structured parameter metadata for validated operations."""
    if operation is None:
        return {name: _export_schema(schema) for name, schema in OPERATION_SCHEMAS.items()}
    if not isinstance(operation, str) or operation not in operations():
        raise ValueError(f'Unknown operation: {operation}')
    schema = OPERATION_SCHEMAS.get(operation)
    return _export_schema(schema) if schema is not None else None


def _format_scalar(name, value, spec):
    kind = spec['type']
    if kind == 'string':
        if not isinstance(value, str) or '\0' in value:
            raise ValueError(f'Parameter {name} must be a string without NUL')
        text = value
    elif kind == 'integer':
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f'Parameter {name} must be an integer')
        text = str(value)
    elif kind == 'number':
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f'Parameter {name} must be a finite number')
        text = str(value)
    else:
        raise ValueError(f'Unsupported scalar parameter type: {kind}')
    if 'choices' in spec and value not in spec['choices']:
        raise ValueError(f'Parameter {name} must be one of {list(spec["choices"])}')
    return text


def _compile_param_specs(values, specs):
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise ValueError(f'Unknown parameters: {", ".join(unknown)}')
    positionals, flagged = [], []
    for name, spec in specs.items():
        present = name in values and values[name] is not None
        if not present:
            if spec.get('required'):
                raise ValueError(f'Missing required parameter: {name}')
            continue
        val = values[name]
        kind = spec['type']
        if spec.get('positional'):
            positionals.append(_format_scalar(name, val, spec))
        elif kind == 'boolean':
            if not isinstance(val, bool):
                raise ValueError(f'Parameter {name} must be a boolean')
            if val:
                flagged.append(spec['flag'])
        elif kind.endswith('_list'):
            item_kind = kind[:-5]
            if not isinstance(val, list) or not val:
                raise ValueError(f'Parameter {name} must be a non-empty list')
            item_spec = {'type': item_kind}
            if 'choices' in spec:
                item_spec['choices'] = spec['choices']
            flagged.append(spec['flag'])
            flagged.extend(_format_scalar(name, item, item_spec) for item in val)
        else:
            text = _format_scalar(name, val, spec)
            if text.startswith('-'):
                flagged.append(f"{spec['flag']}={text}")
            else:
                flagged.extend([spec['flag'], text])
    return [*positionals, *flagged]


def arguments_from_params(operation, params):
    """Compile validated structured params into safe CLI argument tokens."""
    if not isinstance(operation, str) or operation not in operations():
        raise ValueError(f'Unknown operation: {operation}')
    if operation not in OPERATION_SCHEMAS:
        raise ValueError(f'No structured parameter schema for operation: {operation}; pass CLI arguments')
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    schema = OPERATION_SCHEMAS[operation]
    if 'subcommands' in schema:
        subcommand = params.get('subcommand')
        if not isinstance(subcommand, str) or subcommand not in schema['subcommands']:
            raise ValueError(f'Parameter subcommand must be one of {list(schema["subcommands"])}')
        top_specs = schema.get('params', {})
        sub_specs = schema['subcommands'][subcommand]
        allowed = {'subcommand', *top_specs, *sub_specs}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f'Unknown parameters for {operation} {subcommand}: {", ".join(unknown)}')
        top_values = {k: params[k] for k in top_specs if k in params}
        sub_values = {k: params[k] for k in sub_specs if k in params}
        tokens = _compile_param_specs(top_values, top_specs)
        tokens.append(subcommand)
        tokens.extend(_compile_param_specs(sub_values, sub_specs))
        return tokens
    return _compile_param_specs(params, schema.get('params', {}))


def paths():
    return {
        'python': console_python(),
        'runtime': str(RUNTIME),
        'guides': {name: str(RUNTIME / relative) for name, relative in _GUIDES.items()},
    }


def backends(detect=True):
    """Return open-source software backend catalog and optional read-only local installation detection."""
    from . import software_backends, software_detection
    availability = software_detection.detect_backends() if detect else None
    return {
        'roles': software_backends.roles(),
        'backends': software_backends.catalog(availability),
        'detected': bool(detect),
        'read_only': True,
        'scope': 'open-source runtime discovery (FilmCraft, EffectCraft, FFmpeg); no proprietary hosts',
    }


def read_guide(name):
    if not isinstance(name, str) or name not in _GUIDES:
        raise ValueError(f'Unknown guide: {name}; choose from {", ".join(_GUIDES)}')
    return (RUNTIME / _GUIDES[name]).read_text(encoding='utf-8-sig')


_KNOWLEDGE_SYNONYMS = (
    (('剧情', '故事', '叙事', '因果', '人物动机'), ('剧情', '故事', '叙事', '人物', '目标', '障碍', '因果')),
    (('平淡', '太平', '节奏', '拖沓', '卡点', '拍点'), ('节奏', '反差', '重音', '动作', '停留', '拍点')),
    (('转场', '接管', '换场', '镜头连接', '画面切换'), ('连接', '转场', '接管', '遮挡', '锚点', '画面')),
    (('小窗', '分屏', '画中画', '蒙版'), ('分层', '分屏', '窗口', '蒙版', '主体', '接管')),
    (('对白', '台词', '人声', '声音', '说话'), ('对白', '人声', '声音', '原声', '分离', '语音')),
    (('音画', '声画', '不同步', '对不上', '口型'), ('声画', '音画', '同步', '源时间', '对白', '口型')),
    (('j-cut', 'l-cut', 'j剪', 'l剪', '声桥', '声音先入'), ('声桥', '声音', '先入', '延续', '画面', '边界')),
    (('音乐', '配乐', '背景乐', 'bgm'), ('音乐', '配乐', 'bgm', '乐句', '重音', '声音')),
    (('字幕', '文字', '排版', '标题'), ('字幕', '文字', '排版', '阅读', '标题', '字体')),
    (('动效', '特效', '透明', 'overlay', 'effectcraft'), ('动效', '特效', '透明', 'prores', 'effectcraft')),
    (('配方', 'recipe', '标题卡', '下三分之一', '闪白', '遮幅', '进度条'), ('配方', 'recipe', '标题卡', '下三分之一', 'effectcraft', 'overlays')),
    (('绿幕', '抠像', '色键', 'ultra_key', 'color_key'), ('绿幕', '抠像', '色键', 'ultra_key', 'color_key')),
    (('蒙版', 'mask', '嵌套', 'composites'), ('蒙版', 'mask', '嵌套', 'composites')),
    (('调色', 'lumetri', '曲线', '色轮', 'hsl'), ('调色', 'lumetri', 'hsl', '曲线', '色轮')),
    (('混合模式', 'blend', '滤色', '正片叠底', '叠加'), ('混合模式', 'blend', '滤色', '正片叠底')),
    (('变速', '倒放', 'speed'), ('变速', '倒放', 'speed')),
    (('跟踪', '追踪', 'roi', 'tracking'), ('跟踪', '轨迹', 'roi', 'tracking')),
    (('降噪', '响度', '闪避', 'ducking', 'denoise'), ('降噪', '响度', '闪避', 'ducking', 'denoise')),
    (('重点词', '逐词高亮', 'karaoke', 'highlights'), ('重点词', 'highlights', '逐词', 'karaoke')),
    (('互操作', 'otio', 'fcpxml', 'aaf', 'filmcraft'), ('otio', 'fcpxml', 'aaf', 'filmcraft')),
    (('预览', '审片', '导出', '验收'), ('预览', '审片', '导出', '验证', 'decode')),
)
_KNOWLEDGE_STOP = frozenset(('如何', '怎么', '这个', '那个', '一个', '我们', '需要', '可以', '应该', '一下', '问题', '什么'))
_ANCHOR_RE = re.compile(r'^<a\s+id=["\']([^"\']+)["\']\s*>\s*</a>\s*$')

_CAPABILITY_TOPIC_ANCHORS = (
    ('filmcraft-editing', ('剪辑', '时间轴', '多轨', '转场', 'filmcraft')),
    ('effectcraft-overlays', ('动效', '特效', '透明', 'prores', 'effectcraft', 'overlay', '配方', 'recipe', '标题卡', '下三分之一', '闪白', '遮幅', '进度条')),
    ('filmcraft-grading', ('调色', 'lumetri', 'hsl', '曲线', 'lut')),
    ('filmcraft-blend', ('混合模式', 'blend', 'mix_mode', '绿幕', '抠像', '色键', 'ultra_key', 'color_key', '蒙版', 'mask', '嵌套', 'composites')),
    ('text-highlights', ('重点词', 'highlights', '逐词高亮', 'karaoke', '字幕')),
    ('media-prep', ('vfr', '转码', 'normalize', 'ffmpeg')),
    ('transcription', ('转写', 'whisper', 'transcribe')),
    ('separation', ('人声分离', 'separate', 'demucs', '分离人声')),
    ('ducking', ('闪避', 'ducking')),
    ('offline-denoise', ('降噪', 'denoise')),
    ('beat-editing', ('按拍切镜', 'beats', 'beat-apply')),
    ('offline-tracking', ('单目标跟踪', 'tracking')),
)


def _tokenize_text(text):
    normalized = unicodedata.normalize('NFKC', text).casefold()
    tokens = []
    for tok in re.findall(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', normalized):
        tokens.append(tok)
        if '-' in tok:
            tokens.extend(part for part in tok.split('-') if part)
    for run in re.findall(r'[\u3400-\u9fff]+', normalized):
        if 2 <= len(run) <= 6:
            tokens.append(run)
        for idx in range(len(run) - 1):
            tokens.append(run[idx:idx + 2])
    return normalized, tokens


def _knowledge_terms(query):
    normalized, tokens = _tokenize_text(query)
    terms, seen = [], set()
    for term in tokens:
        if term in _KNOWLEDGE_STOP or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    groups = [targets for triggers, targets in _KNOWLEDGE_SYNONYMS
              if any(trigger in normalized for trigger in triggers)]
    return normalized, terms, groups


def _knowledge_match(term, text):
    if re.fullmatch(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', term):
        return re.search(r'(?<![a-z0-9_])' + re.escape(term) + r'(?![a-z0-9_])', text) is not None
    return term in text


def _parse_guide_sections(name, lines):
    sections, anchors_by_id = [], {}
    heading, start, raw_head, section_anchor, body = name, 1, name, None, []
    inline_anchors = []
    pending_anchor, fenced, include_fence = None, False, False
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith('```'):
            pending_anchor = None
            if fenced:
                fenced, include_fence = False, False
            else:
                fenced = True
                language = stripped[3:].strip().casefold()
                include_fence = language in ('text', 'plaintext', 'json', 'jsonc') and not (
                    name == 'reference/creative-knowledge' and heading == '如何调用')
            continue
        if fenced and not include_fence:
            continue
        anchor_match = None if fenced else _ANCHOR_RE.match(stripped)
        if anchor_match:
            pending_anchor = anchor_match.group(1)
            continue
        match = None if fenced else re.match(r'^(#{1,3})\s+(.+?)\s*$', line)
        if match:
            if body or section_anchor:
                sections.append({
                    'guide': name, 'heading': heading, 'start': start,
                    'raw_head': raw_head, 'anchor': section_anchor,
                    'inline_anchors': inline_anchors, 'body': body,
                })
            heading, start, raw_head = match.group(2), line_number, stripped
            section_anchor = pending_anchor
            pending_anchor = None
            inline_anchors = []
            body = []
            if section_anchor:
                anchors_by_id[section_anchor] = {
                    'guide': name, 'anchor': section_anchor, 'section': heading, 'line': line_number,
                }
        else:
            if pending_anchor:
                inline_anchors.append((line_number, pending_anchor))
                anchors_by_id[pending_anchor] = {
                    'guide': name, 'anchor': pending_anchor, 'section': heading, 'line': line_number,
                }
                pending_anchor = None
            body.append((line_number, line))
    if body or section_anchor:
        sections.append({
            'guide': name, 'heading': heading, 'start': start,
            'raw_head': raw_head, 'anchor': section_anchor,
            'inline_anchors': inline_anchors, 'body': body,
        })
    return sections, anchors_by_id


def _build_section_context(section, hit_line, hit_text):
    body = section['body']
    hit_idx = next((idx for idx, (n, _) in enumerate(body) if n == hit_line), None)
    if hit_idx is None:
        snippet = hit_text
    else:
        window = []
        for _, raw in body[max(0, hit_idx - 1):hit_idx + 4]:
            text = raw.strip()
            if not text or text.startswith(('```', '|---')) or _ANCHOR_RE.match(text):
                continue
            window.append(text)
        snippet = '\n'.join(window) if window else hit_text
    prefixed = f"[{section['heading']}] {snippet}" if snippet != section['heading'] else snippet
    return prefixed[:900]


def _matching_capability_hints(query_norm, section_norm, capability_index):
    if not capability_index:
        return []
    matched, seen = [], set()
    for text_scope in (query_norm, section_norm):
        for anchor_id, triggers in _CAPABILITY_TOPIC_ANCHORS:
            if anchor_id in seen or anchor_id not in capability_index:
                continue
            if any(_knowledge_match(trigger, text_scope) for trigger in triggers):
                seen.add(anchor_id)
                matched.append(dict(capability_index[anchor_id]))
                if len(matched) == 3:
                    return matched
    return matched


def search_knowledge(query, limit=5):
    """Search registered bundled Markdown using section BM25, anchors, and capability hints."""
    if not isinstance(query, str) or not query.strip() or len(query) > 256:
        raise ValueError('query must be non-empty text of at most 256 characters')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('limit must be an integer from 1 to 20')
    normalized, terms, groups = _knowledge_terms(query.strip())
    if not terms and not groups:
        return {'query': query, 'results': [], 'scope': 'bundled editing guides only'}

    allowed = RUNTIME.resolve()
    all_sections, capability_index = [], {}
    for name in _GUIDES:
        path = RUNTIME / _GUIDES[name]
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(allowed) or path.is_symlink() or path.stat().st_size > 262144:
                continue
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            continue
        parsed, anchors = _parse_guide_sections(name, lines)
        all_sections.extend(parsed)
        if name == 'capabilities':
            capability_index.update(anchors)

    if not all_sections:
        return {'query': query, 'results': [], 'scope': 'bundled editing guides only'}

    query_syn_terms, seen_syn = [], set(terms)
    for group in groups:
        for candidate in group:
            _, syn_tokens = _tokenize_text(candidate)
            for tok in syn_tokens:
                if tok not in seen_syn:
                    seen_syn.add(tok)
                    query_syn_terms.append(tok)

    df = Counter()
    total_body_len, total_head_len = 0, 0
    for sec in all_sections:
        head_norm, head_tokens = _tokenize_text(sec['heading'])
        body_norm, body_tokens = _tokenize_text('\n'.join(s for _, s in sec['body']))
        sec['head_norm'], sec['body_norm'] = head_norm, body_norm
        sec['head_counts'], sec['body_counts'] = Counter(head_tokens), Counter(body_tokens)
        sec['head_len'], sec['body_len'] = max(1, len(head_tokens)), max(1, len(body_tokens))
        total_head_len += sec['head_len']
        total_body_len += sec['body_len']
        for tok in set(head_tokens) | set(body_tokens):
            df[tok] += 1

    num_docs = len(all_sections)
    avg_body_len = max(1.0, total_body_len / num_docs)
    avg_head_len = max(1.0, total_head_len / num_docs)
    k1, b = 1.2, 0.35

    def _idf(tok):
        doc_freq = df.get(tok, 0)
        return math.log(1.0 + (num_docs - doc_freq + 0.5) / (doc_freq + 0.5))

    found = []
    for sec in all_sections:
        name, head_norm = sec['guide'], sec['head_norm']
        head_counts, body_counts = sec['head_counts'], sec['body_counts']
        direct_hits = sum(1 for t in terms if head_counts.get(t, 0) or body_counts.get(t, 0))
        guide_name_hits = sum(1 for t in terms if len(t) >= 3 and _knowledge_match(t, name.casefold()))
        syn_hits = max((sum(1 for candidate in group if candidate in head_norm or candidate in sec['body_norm'])
                        for group in groups), default=0)
        if not (direct_hits or guide_name_hits or syn_hits >= 2):
            continue

        score = 0.0
        for tok in terms:
            idf = _idf(tok)
            tf_b, tf_h = body_counts.get(tok, 0), head_counts.get(tok, 0)
            if tf_b:
                score += idf * (tf_b * (k1 + 1.0)) / (tf_b + k1 * (1.0 - b + b * (sec['body_len'] / avg_body_len)))
            if tf_h:
                score += 2.2 * idf * (tf_h * (k1 + 1.0)) / (tf_h + k1 * (1.0 - b + b * (sec['head_len'] / avg_head_len)))
        if direct_hits and terms:
            score += 1.5 * (direct_hits / len(terms))
        for tok in query_syn_terms:
            idf = _idf(tok)
            tf_b, tf_h = body_counts.get(tok, 0), head_counts.get(tok, 0)
            if tf_b:
                score += 0.35 * idf * (tf_b * (k1 + 1.0)) / (tf_b + k1 * (1.0 - b + b * (sec['body_len'] / avg_body_len)))
            if tf_h:
                score += 0.8 * idf * (tf_h * (k1 + 1.0)) / (tf_h + k1 * (1.0 - b + b * (sec['head_len'] / avg_head_len)))
        for group in groups:
            score += min(4.0, sum(1.5 if candidate in head_norm else 0.7 if candidate in sec['body_norm'] else 0.0
                                  for candidate in group))
        if len(normalized) >= 2 and _knowledge_match(normalized, head_norm):
            score += 6.0
        if guide_name_hits:
            score += 5.0 * min(2, guide_name_hits)
        if name == 'reference/creative-knowledge-sources' and not any(
                marker in normalized for marker in ('来源', '出处', '证据', 'source', 'license')):
            score -= 4.0
        if score < 1.2:
            continue
        if name.startswith('reference/creative-knowledge') and not (
                name == 'reference/creative-knowledge' and sec['heading'] == '如何调用'):
            score += 1.5

        body = sec['body']
        excerpt_line = next(
            ((n, s.strip()) for n, s in body
             if s.strip() and not s.lstrip().startswith(('```', '|---')) and not _ANCHOR_RE.match(s.strip())
             and (any(_knowledge_match(t, unicodedata.normalize('NFKC', s).casefold()) for t in terms)
                  or any(candidate in unicodedata.normalize('NFKC', s).casefold()
                         for group in groups for candidate in group))),
            None,
        )
        if excerpt_line is None:
            excerpt_line = next(
                ((n, s.strip()) for n, s in body if s.strip() and not _ANCHOR_RE.match(s.strip())),
                (sec['start'], sec['raw_head']),
            )
        hit_line, hit_text = excerpt_line
        nearest_inline = max((aid for ln, aid in sec['inline_anchors'] if ln <= hit_line), default=None)
        anchor = nearest_inline or sec['anchor']
        context = _build_section_context(sec, hit_line, hit_text)
        hints = ([] if name == 'capabilities'
                 else _matching_capability_hints(normalized, f"{head_norm}\n{context.casefold()}", capability_index))
        found.append({
            'guide': name,
            'section': sec['heading'],
            'section_start_line': sec['start'],
            'line': hit_line,
            'anchor': anchor,
            'excerpt': hit_text[:320],
            'context': context,
            'capability_hints': hints,
            '_score': score,
        })

    found.sort(key=lambda item: (-item['_score'], item['guide'], item['line']))
    results, per_guide = [], {}
    for item in found:
        if per_guide.get(item['guide'], 0) >= 2:
            continue
        item.pop('_score')
        results.append(item)
        per_guide[item['guide']] = per_guide.get(item['guide'], 0) + 1
        if len(results) == limit:
            break
    return {'query': query, 'results': results,
            'scope': 'bundled editing guides only; results are leads, not proof of capability or quality'}
