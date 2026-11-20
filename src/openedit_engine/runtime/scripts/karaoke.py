"""Compile original timed words into editable, sentence-stable color cues."""
import re

import editor


def compile_cues(units, mode='active', color='#FFD54F', max_chars=12,
                 max_words=6, joiner='', max_gap=1.0):
    """Use word boundaries only; no character timing or text rewriting is inferred.

    Each display group stays on screen from its first word to its last word.
    Internal silence clears active highlighting or retains cumulative highlighting.
    Returned start/end times use the engine's integer-microsecond clock.
    """
    if mode not in ('active', 'cumulative'):
        raise ValueError('Karaoke mode must be active|cumulative')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', color):
        raise ValueError('Karaoke color must be #RRGGBB')
    for value, label, high in ((max_chars, 'max_chars', 80), (max_words, 'max_words', 30)):
        if type(value) is not int or not 1 <= value <= high:
            raise ValueError(f'{label} must be an integer in [1, {high}]')
    gap_limit = editor.us(editor.number(max_gap, 'max_gap', 0, 10))
    if not isinstance(joiner, str) or any(c in joiner for c in '\x00\r\n') or len(joiner) > 80:
        raise ValueError('joiner must be single-line text without NUL, at most 80 characters')
    if not isinstance(units, list) or not units:
        raise ValueError('Karaoke requires a nonempty list of timed words')

    groups, current, previous_end, previous_raw_end = [], [], None, None
    length = 0
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            raise ValueError('Every karaoke word must be an object')
        text = unit.get('text')
        if (not isinstance(text, str) or not text.strip() or
                any(c in text for c in '\x00\r\n')):
            raise ValueError('Karaoke words require nonblank original single-line text without NUL')
        if len(text) > max_chars:
            raise ValueError('A karaoke word exceeds max_chars; character timing cannot be inferred')
        group = unit.get('group')
        if 'group' in unit and (type(group) is not int or group < 0):
            raise ValueError('Karaoke group must be a nonnegative original segment index')
        # Safe integer microseconds, also consumable by JavaScript/JSON clients.
        a = editor.number(unit.get('start'), f'word {index} start', 0, (2**53 - 1) / 1e6)
        b = editor.number(unit.get('end'), f'word {index} end', 0, (2**53 - 1) / 1e6)
        start, end = editor.us(a), editor.us(b)
        if b <= a or end <= start:
            raise ValueError('Karaoke word end must be after start by at least one microsecond')
        if previous_end is not None and (a < previous_raw_end or start < previous_end):
            raise ValueError('Karaoke words overlap or are not source-clock ordered')
        if current and (group != current[-1]['group'] or start - previous_end > gap_limit or
                        len(current) >= max_words or length + len(joiner) + len(text) > max_chars):
            groups.append(current)
            current, length = [], 0
        offset = length + (len(joiner) if current else 0)
        current.append({'text': text, 'start': start, 'end': end, 'group': group,
                        'span_start': offset, 'span_end': offset + len(text)})
        length = offset + len(text)
        previous_end, previous_raw_end = end, b
    groups.append(current)

    cues = []
    for group in groups:
        text = joiner.join(unit['text'] for unit in group)
        for index, unit in enumerate(group):
            span = {'start': 0 if mode == 'cumulative' else unit['span_start'],
                    'end': unit['span_end'], 'color': color.upper()}
            cue = {'start': unit['start'] / 1e6, 'end': unit['end'] / 1e6, 'text': text,
                   'style': {'auto_wrapping': False}, 'color': '#FFFFFF', 'bold': False,
                   'highlights': [span]}
            cues.append(cue)
            if index + 1 < len(group) and unit['end'] < group[index + 1]['start']:
                silence = {'start': unit['end'] / 1e6, 'end': group[index + 1]['start'] / 1e6,
                           'text': text, 'style': {'auto_wrapping': False},
                           'color': '#FFFFFF', 'bold': False}
                if mode == 'cumulative':
                    silence['highlights'] = [dict(span)]
                cues.append(silence)
    return cues
