"""Pure karaoke compilation checks; no model, native worker or media required."""
import argparse
import copy
from pathlib import Path
import tempfile

import editor
import karaoke


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    def verify(label, condition):
        assert condition, label
        checks.append(label)

    def reject(label, units=None, **options):
        candidate = copy.deepcopy(words if units is None else units)
        before = copy.deepcopy(candidate)
        try:
            karaoke.compile_cues(candidate, **options)
        except ValueError:
            pass
        else:
            raise AssertionError(label + ' was accepted')
        verify(label, candidate == before)

    words = [{'text': '剪辑', 'start': 1, 'end': 1.4, 'group': 0},
             {'text': '很好', 'start': 1.4, 'end': 1.9, 'group': 0},
             {'text': '用', 'start': 2.1, 'end': 2.5, 'group': 0}]
    original = copy.deepcopy(words)
    active = karaoke.compile_cues(words)
    cumulative = karaoke.compile_cues(words, mode='cumulative')
    verify('input preserved', words == original)
    verify('same sentence remains visible', [c['text'] for c in active] == ['剪辑很好用'] * 4)
    verify('word timing and internal silence only', [(c['start'], c['end']) for c in active] ==
           [(1, 1.4), (1.4, 1.9), (1.9, 2.1), (2.1, 2.5)])
    verify('active follows original word ranges', [c.get('highlights') for c in active] ==
           [[{'start': 0, 'end': 2, 'color': '#FFD54F'}],
            [{'start': 2, 'end': 4, 'color': '#FFD54F'}], None,
            [{'start': 4, 'end': 5, 'color': '#FFD54F'}]])
    verify('cumulative retains previous range in silence', [c['highlights'][0]['end'] for c in cumulative] == [2, 4, 4, 5])
    verify('cumulative begins at first character', all(c['highlights'][0]['start'] == 0 for c in cumulative))
    verify('native compatible plain style', all(c['style'] == {'auto_wrapping': False} and
           c['color'] == '#FFFFFF' and c['bold'] is False for c in active + cumulative))
    active[0]['style']['auto_wrapping'] = True
    active[0]['highlights'][0]['end'] = 1
    verify('output objects are independent', active[1]['style'] == {'auto_wrapping': False} and
           active[1]['highlights'][0]['end'] == 4)
    ascii_words = [{'text': ' hello', 'start': 0, 'end': .2}, {'text': 'world ', 'start': .2, 'end': .4}]
    ascii_copy = copy.deepcopy(ascii_words)
    joined = karaoke.compile_cues(ascii_words, joiner='-', max_chars=20, color='#aabbcc')
    verify('raw whitespace and explicit joiner preserved', joined[0]['text'] == ' hello-world ' and ascii_words == ascii_copy)
    verify('joiner outside active word range', joined[1]['highlights'] == [{'start': 7, 'end': 13, 'color': '#AABBCC'}])
    verify('max words splits displays', [c['text'] for c in karaoke.compile_cues(words, max_words=1)] == ['剪辑', '很好', '用'])
    verify('max chars splits displays', [c['text'] for c in karaoke.compile_cues(words, max_chars=4)] == ['剪辑很好', '剪辑很好', '用'])
    verify('joiner counts against length limit', [c['text'] for c in karaoke.compile_cues(ascii_words, joiner='-', max_chars=12)] == [' hello', 'world '])
    grouped = copy.deepcopy(words)
    grouped[2]['group'] = 1
    verify('original segment boundary splits display', [c['text'] for c in karaoke.compile_cues(grouped)] == ['剪辑很好', '剪辑很好', '用'])
    verify('large internal gap splits display', [c['text'] for c in karaoke.compile_cues(words, max_gap=.1)] == ['剪辑很好', '剪辑很好', '用'])
    verify('gap equal to limit stays in display', len(karaoke.compile_cues(words, max_gap=.2)) == 4)
    verify('zero gap still allows touching words', len(karaoke.compile_cues(words[:2], max_gap=0)) == 2)
    precise = karaoke.compile_cues([{'text': '准', 'start': .1000001, 'end': .2000001}])
    verify('microsecond clock', precise[0]['start'] == .1 and precise[0]['end'] == .2)
    for label, value in [('empty list', []), ('nonlist', {}), ('nonobject', [None])]:
        reject(label, value)
    for text in ('', ' \t ', '\x00词', '两\n行', '两\r行'):
        reject('invalid word ' + repr(text), [{'text': text, 'start': 0, 'end': 1}])
    reject('overlong single word', [{'text': '一' * 13, 'start': 0, 'end': 1}])
    for field in ('start', 'end'):
        for value in (True, None, '1', float('nan'), float('inf'), -1):
            unit = {'text': '词', 'start': 0, 'end': 1}
            unit[field] = value
            reject('invalid ' + field + ' ' + repr(value), [unit])
    for a, b in ((1, 1), (1, 0), (0, .0000001)):
        reject('nonpositive microsecond duration ' + repr((a, b)), [{'text': '词', 'start': a, 'end': b}])
    reject('overlap', [words[1], words[0]])
    reject('submicrosecond raw overlap', [{'text': '甲', 'start': 0, 'end': .2000002},
                                        {'text': '乙', 'start': .2000001, 'end': .4}])
    for group in (True, -1, None, '0', .5):
        reject('invalid group ' + repr(group), [{'text': '词', 'start': 0, 'end': 1, 'group': group}])
    for mode in ('', 'word', None, True):
        reject('invalid mode ' + repr(mode), mode=mode)
    for color in ('red', '#FFF', '#GG1234', None):
        reject('invalid color ' + repr(color), color=color)
    for option, values in (('max_chars', (True, 0, 81, 2.0)), ('max_words', (False, 0, 31, 2.0)),
                           ('max_gap', (True, -.1, 10.1, float('inf'), float('nan'))),
                           ('joiner', (None, '\n', '\x00', ' ' * 81))):
        for value in values:
            reject('invalid ' + option + ' ' + repr(value), **{option: value})
    editor.write_json(root / 'active.json', karaoke.compile_cues(words))
    editor.write_json(root / 'cumulative.json', cumulative)
    result = {'status': 'passed', 'checks': len(checks), 'passed': checks,
              'native_preview': 'not run; pure function only', 'input_modified': False}
    editor.write_json(root / 'verification.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    if args.out:
        result = check(Path(args.out).resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-karaoke-') as temporary:
            result = check(Path(temporary) / 'checks')
    print(editor.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
