"""One whitelist and resource layout shared by the CLI and external Agent adapters."""
from pathlib import Path
import re
import sys
import unicodedata

RUNTIME = Path(__file__).resolve().parent / 'runtime'
_EDITOR = {
    'doctor': 'Diagnose the local runtime without opening Jianying.',
    'capabilities': 'List admitted native resources and parameters.',
    'inspect': 'Probe local media and extract frames.',
    'reference': 'Extract reference frames and audio candidates.',
    'validate': 'Validate an edit plan and media dependencies.',
    'build': 'Build a new editable draft; refuses existing output.',
    'publish': 'Copy a generated draft to the active draft root and check registration.',
    'refresh-registration': 'Recreate an owned generated draft to trigger registration.',
    'native-preview': 'Render selected low-resolution native frames without the desktop UI.',
    'admit-matting': 'Register an externally reviewed existing mask cache; never computes or rates a cutout.',
}
_SCRIPTS = {
    'practice-compare': ('practice_compare.py', 'Render bounded local side-by-side review of two explicit video ranges with optional one-sided audio, input identities, and no automatic aesthetic judgment.'),
    'mesh-avatar': ('mesh_avatar.py', 'Inspect bounded local source-video frames, create identity-preserving Mesh project and exact per-variant ImageGen input assignments, then build/validate/pack and render transparent avatar media for Jianying.'),
    'layout-tracks': ('layout_tracks.py', 'Create a separate plan reusing safe non-overlapping overlay video tracks, preserving active layer order and protected groups; source unchanged.'),
    'preview-sequence': ('preview_sequence.py', 'Create a bounded low-resolution review video from native Jianying frames, optionally with one explicit audio selection; not a final export or full timeline mix.'),
    'music-edit': ('music_edit.py', 'Prepare bounded local media, call the configured native audio/video model through OpenAI-compatible or Gemini APIs, and compile musical/action anchors into validated Jianying or Resolve plans; analyze and review send selected media and consume API usage.'),
    'resolve-workflow': ('resolve_workflow.py', 'Validate/prepare bounded Resolve plans, create/save/reopen native projects and render verified NVIDIA H264/AAC outputs; no silent effect loss.'),
    'adobe-workflow': ('adobe_workflow.py', 'Validate/prepare bounded PR+AE plans, create native AE projects and lossless shots, then hand off standard XML to Premiere; no QE.'),
    'shot-exchange': ('shot_exchange.py', 'Prepare/build a bounded Blender shot, validate its return, or export plain silent OTIO cuts for Kdenlive.'),
    'beats': ('beats.py', 'Detect offline candidate beats from an explicitly selected local audio interval.'),
    'beat-apply': ('beat_apply.py', 'Create a new plan with explicit every-N candidate-beat cuts, or map candidates to a bounded native audio-mark plan with --mark-audio; never overwrites input.'),
    'denoise-audio': ('offline_denoise.py', 'Create aligned dry/FFT-denoised PCM excerpts with explicit noise-floor/reduction; preserve originals.'),
    'reverse': ('reverse_media.py', 'Create bounded reversed video source media with explicit reverse/mute audio policy.'),
    'migrate': ('migrate.py', 'Diagnose and explicitly map external media paths.'),
    'transcribe': ('transcribe.py', 'Transcribe offline with caller-supplied Whisper weights; optional validated model word timestamps.'),
    'separate': ('separate.py', 'Separate audio using caller-supplied local Demucs weights.'),
    'ducking': ('ducking.py', 'Create a new plan with a measured music volume envelope.'),
    'captions': ('captions.py', 'Map transcript/SRT into editable captions; optional timed full-group karaoke highlights, with source/timeline mapping.'),
    'tracking': ('tracking.py', 'Track a selected ROI offline; reports lost tracking.'),
    'tracking-apply': ('tracking_apply.py', 'Apply an admitted trajectory as editable image or bounded text position keys.'),
    'stock': ('stock.py', 'Explicit online stock search/download with provenance.'),
    'templates': ('templates.py', 'Read the external template index.'),
    'verify-templates': ('verify_templates.py', 'Verify template subsets against the current runtime.'),
    'borrow-template': ('template_borrow.py', 'Borrow only externally verified template subsets.'),
    'checks': ('run_checks.py', 'Run registered regression checks in fresh output directories.'),
    'requalify': ('requalify.py', 'After a Jianying update, render the version-gated self-check drafts on the measured and the new build; admit only subsets whose native frames match.'),
}
_GUIDES = {'editing': 'guides/editing.md', 'planning': 'guides/planning.md',
           'maintenance': 'guides/maintenance.md', 'capabilities': 'references/capabilities.md',
           'plan-format': 'references/plan-format.md', 'migration': 'references/migration.md'}
for _path in sorted((RUNTIME / 'references').rglob('*.md')):
    _relative = _path.relative_to(RUNTIME).as_posix()
    if _relative not in _GUIDES.values():
        _GUIDES['reference/' + _path.relative_to(RUNTIME / 'references').with_suffix('').as_posix()] = _relative


def operations():
    return {**_EDITOR, **{name: value[1] for name, value in _SCRIPTS.items()}}


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
    script, prefix = ('editor.py', [operation]) if operation in _EDITOR else (_SCRIPTS[operation][0], [])
    path = RUNTIME / 'scripts' / script
    if not path.is_file():
        raise ValueError(f'Installed engine is incomplete: missing {script}')
    return [console_python(), str(path), *prefix, *arguments]


def paths():
    return {'python': console_python(), 'runtime': str(RUNTIME),
            'guides': {name: str(RUNTIME / relative) for name, relative in _GUIDES.items()}}


def read_guide(name):
    if not isinstance(name, str) or name not in _GUIDES:
        raise ValueError(f'Unknown guide: {name}; choose from {", ".join(_GUIDES)}')
    return (RUNTIME / _GUIDES[name]).read_text(encoding='utf-8-sig')


# Synonyms describe common editing questions, not a score of creative quality.
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
    (('模板', '预设', '迁移'), ('模板', '参数', '迁移', '来源', '准入')),
    (('素材', '参考', '教程', '来源'), ('素材', '参考', '教程', '来源', '证据', '观察')),
    (('看过', '审片', '预览', '试听', '验收'), ('预览', '试听', '审片', '观看', '证据', '验证')),
)
_KNOWLEDGE_STOP = frozenset(('如何', '怎么', '这个', '那个', '一个', '我们', '需要', '可以', '应该', '一下', '问题', '什么'))


def _knowledge_terms(query):
    query = unicodedata.normalize('NFKC', query).casefold()
    terms = set(re.findall(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', query))
    for run in re.findall(r'[\u3400-\u9fff]+', query):
        if 2 <= len(run) <= 6:
            terms.add(run)
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    terms -= _KNOWLEDGE_STOP
    groups = [targets for triggers, targets in _KNOWLEDGE_SYNONYMS
              if any(term in query for term in triggers)]
    return query, terms, groups


def _knowledge_match(term, text):
    if re.fullmatch(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', term):
        return re.search(r'(?<![a-z0-9_])' + re.escape(term) + r'(?![a-z0-9_])', text) is not None
    return term in text


def search_knowledge(query, limit=5):
    """Search only registered bundled Markdown; return guide names and source lines."""
    if not isinstance(query, str) or not query.strip() or len(query) > 256:
        raise ValueError('query must be non-empty text of at most 256 characters')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('limit must be an integer from 1 to 20')
    normalized, terms, groups = _knowledge_terms(query.strip())
    if not terms and not groups:
        return {'query': query, 'results': [], 'scope': 'bundled editing guides only'}
    found = []
    names = list(_GUIDES)
    allowed = RUNTIME.resolve()
    for name in names:
        path = RUNTIME / _GUIDES[name]
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(allowed) or path.is_symlink() or path.stat().st_size > 262144:
                continue
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            continue
        sections = []
        heading, start, body, fenced, include_fence = name, 1, [], False, False
        for line_number, line in enumerate(lines, 1):
            if line.lstrip().startswith('```'):
                if fenced:
                    fenced, include_fence = False, False
                else:
                    fenced = True
                    language = line.lstrip()[3:].strip().casefold()
                    include_fence = language in ('text', 'plaintext', 'json', 'jsonc') and not (
                        name == 'reference/creative-knowledge' and heading == '如何调用')
                continue
            if fenced and not include_fence:
                continue
            match = None if fenced else re.match(r'^(#{1,3})\s+(.+?)\s*$', line)
            if match:
                if body:
                    sections.append((heading, start, body))
                heading, start, body = match.group(2), line_number, []
            else:
                body.append((line_number, line))
        if body:
            sections.append((heading, start, body))
        for heading, start, body in sections:
            head = unicodedata.normalize('NFKC', heading).casefold()
            text = unicodedata.normalize('NFKC', '\n'.join(line for _, line in body)).casefold()
            score = sum(6 if _knowledge_match(term, head) else 3 if _knowledge_match(term, text) else 0 for term in terms)
            for group in groups:
                score += min(4, sum(2 if term in head else 1 if term in text else 0 for term in group))
            if len(normalized) >= 2 and _knowledge_match(normalized, head):
                score += 8
            if any(len(term) >= 3 and _knowledge_match(term, name.casefold()) for term in terms):
                score += 7
            if name == 'reference/creative-knowledge-sources' and not any(
                    term in normalized for term in ('来源', '出处', '证据', 'source', 'license')):
                score -= 4
            if score < 3:
                continue
            if name.startswith('reference/creative-knowledge'):
                score += 2
            excerpt_line = next(((n, s.strip()) for n, s in body
                                 if s.strip() and not s.lstrip().startswith(('```', '|---'))
                                 and (any(_knowledge_match(term, unicodedata.normalize('NFKC', s).casefold()) for term in terms)
                                      or any(term in s for group in groups for term in group))), None)
            if excerpt_line is None:
                excerpt_line = next(((n, s.strip()) for n, s in body if s.strip()), (start, heading))
            found.append({'guide': name, 'section': heading, 'line': excerpt_line[0],
                          'excerpt': excerpt_line[1][:320], '_score': score})
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
