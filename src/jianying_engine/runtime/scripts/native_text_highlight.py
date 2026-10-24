"""Static same-caption color spans; native UTF-16 ranges limited to measured BMP text."""
import copy
import json
import unicodedata
from pathlib import Path
import file_hash


def runtime(install=None):
    import editor
    spec = json.loads((Path(__file__).resolve().parents[1] / 'assets/text-highlights-v1.json').read_text(encoding='utf-8'))
    install = Path(install) if install is not None else editor.jianying_install()
    if not install or install.name != spec['runtime_version']:
        raise ValueError('Text highlights require measured Jianying ' + spec['runtime_version'])
    files = dict(spec['runtime_sha256'])
    files[spec['font_relative']] = spec['font_sha256']
    for relative, digest in files.items():
        if not file_hash.matches(install / relative, digest):
            raise ValueError('Text highlight runtime/font identity changed: ' + relative)


def validate(item, plan):
    import editor
    values = item['highlights']
    if not isinstance(values, list) or not values:
        raise ValueError('highlights requires a nonempty list of explicit start/end/color spans')
    style = item.get('style', {})
    if (plan.get('composites') or not isinstance(style, dict) or style.get('auto_wrapping') is not False or
            set(style) - {'auto_wrapping', 'alpha'} or item.get('transform') or
            any(item.get(k) for k in ('animations', 'motion_easing', 'font', 'glow', 'text_curve')) or
            any(k['property'] != 'alpha' for k in item.get('keyframes', []))):
        raise ValueError('Highlights require standalone single-line default-font text, auto_wrapping=false and optional opacity only')
    if 'alpha' in style:
        editor.number(style['alpha'], 'highlight style.alpha', 0, 1)
    if 'alpha' in style or item.get('keyframes'):
        if item['color'].upper() != '#FFFFFF' or item['bold']:
            raise ValueError('Colored-text opacity requires white base text and bold=false')
    multiline = '\n' in item['text']
    if multiline:
        lines = item['text'].split('\n')
        if (len(lines) != 2 or any(not line.strip() for line in lines) or style != {'auto_wrapping': False} or
                item.get('keyframes') or item['color'].upper() != '#FFFFFF' or item['bold']):
            raise ValueError('Multiline highlights require exactly two nonblank static white-base nonbold lines, no opacity or keys')
    for c in item['text']:
        if multiline and c == '\n':
            continue
        n = ord(c)
        punctuation = (0x3000 <= n <= 0x303f or 0xff00 <= n <= 0xffef) and unicodedata.category(c)[0] in ('P', 'S', 'Z')
        if unicodedata.combining(c) or not (0x20 <= n <= 0x7e or 0x4e00 <= n <= 0x9fff or punctuation):
            raise ValueError('Highlights admit Chinese/ASCII/fullwidth punctuation only; emoji, surrogates, combining text and line breaks are unverified')
    for span in values:
        editor.fields(span, {'start', 'end', 'color'}, 'highlight', ('start', 'end', 'color'))
        if (type(span['start']) is not int or type(span['end']) is not int or
                not 0 <= span['start'] < span['end'] <= len(item['text'])):
            raise ValueError('Highlight indices require integer 0 <= start < end <= character count, end exclusive')
        if '\n' in item['text'][span['start']:span['end']]:
            raise ValueError('A highlight range cannot include a line break; use one explicit range per line')
        if not isinstance(span['color'], str) or not editor.re.fullmatch(r'#[0-9A-Fa-f]{6}', span['color']):
            raise ValueError('Highlight color must be #RRGGBB')
        span['color'] = span['color'].upper()
    values.sort(key=lambda s: s['start'])
    if any(a['end'] > b['start'] for a, b in zip(values, values[1:])):
        raise ValueError('Highlight ranges must not overlap')
    if editor.default_text_font() is None:
        raise ValueError('Highlights require the installed measured default text font')
    runtime()


def apply(rich, item):
    if 'highlights' not in item:
        return
    base = rich['styles'][0]
    spans = []
    cursor = 0
    def append(start, end, color=None):
        if start == end:
            return
        style = copy.deepcopy(base)
        style['range'] = [start, end]
        if color is not None:
            style['fill']['content']['solid']['color'] = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        spans.append(style)
    for spec in item['highlights']:
        append(cursor, spec['start'])
        append(spec['start'], spec['end'], spec['color'])
        cursor = spec['end']
    append(cursor, len(item['text']))
    rich['styles'] = spans


def preview_check(content, root, install=None):
    import editor
    def multiple(value):
        if isinstance(value, dict):
            if value.get('type') in ('text', 'subtitle') and isinstance(value.get('content'), str):
                rich = json.loads(value['content'])
                if len(rich.get('styles', [])) > 1:
                    return True
            return any(multiple(v) for v in value.values())
        return isinstance(value, list) and any(multiple(v) for v in value)
    path = Path(root) / 'edit-plan.json'
    declared = path.is_file() and any('highlights' in item for item in editor.read_json(path).get('captions', []))
    if multiple(content) or declared:
        runtime(install)
        if not path.is_file():
            raise ValueError('Native multi-style text preview requires its formal edit-plan.json')
        plan, _, _ = editor.validate_plan(path)
        editor.verify_structure(content, plan)
