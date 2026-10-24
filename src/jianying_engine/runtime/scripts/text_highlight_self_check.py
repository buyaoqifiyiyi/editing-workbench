"""Native same-caption highlighting contract and formal round-trip checks, no UI."""
import argparse,copy,json
from pathlib import Path
from types import SimpleNamespace as Args
from unittest.mock import patch
from PIL import Image
import editor
import native_text_highlight as highlight


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    Image.new('RGB', (1280, 720), (20, 40, 90)).save(root / 'base.png')
    plan = {'schema_version': 1, 'name': '同句重点词测试', 'mode': 'script',
            'canvas': {'width': 1280, 'height': 720, 'fps': 30},
            'clips': [{'path': str(root / 'base.png'), 'start': 0, 'duration': 2}],
            'captions': [{'text': '甲乙丙丁戊己庚辛', 'start': .2, 'duration': 1.5, 'size': 14, 'border': False,
                          'style': {'auto_wrapping': False},
                          'highlights': [{'start': 6, 'end': 8, 'color': '#00ff00'}, {'start': 2, 'end': 5, 'color': '#ff0000'}]}]}
    path = root / 'plan.json'; editor.write_json(path, plan); frozen = path.read_bytes()
    normalized, _, _ = editor.validate_plan(path)
    result = editor.build_draft(Args(plan=str(path), out=str(root / 'draft')))
    assert result['verification']['native_effects']['counts']['caption_highlights'] == 2
    content = editor.read_json(root / 'draft/draft_content.json')
    rich = json.loads(content['materials']['texts'][0]['content'])
    assert rich['text'] == plan['captions'][0]['text']
    assert [s['range'] for s in rich['styles']] == [[0, 2], [2, 5], [5, 6], [6, 8]]
    assert rich['styles'][1]['fill']['content']['solid']['color'] == [1., 0., 0.]
    assert rich['styles'][3]['fill']['content']['solid']['color'] == [0., 1., 0.]
    assert len(content['materials']['texts']) == 1
    (root / 'archive').mkdir()
    published = editor.publish_draft(Args(draft=str(root / 'draft'), draft_root=str(root / 'archive'), offline_copy=True, user_data=None))
    editor.verify_structure(editor.read_json(Path(published['draft']) / 'draft_content.json'), normalized)
    checks = ['same native text material, original text and segment clock preserved through build/verify/publish',
              'normalized sorted disjoint BMP ranges form a complete nonoverlapping style partition']
    def reject(label, callback):
        try:
            callback()
        except (ValueError, TypeError):
            checks.append(label)
        else:
            raise AssertionError('Expected rejection: ' + label)
    def invalid(label, mutate):
        candidate = copy.deepcopy(plan); mutate(candidate)
        editor.write_json(root / 'invalid.json', candidate)
        reject(label, lambda: editor.validate_plan(root / 'invalid.json'))
    for values in (None, [], {}, [{'start': True, 'end': 3, 'color': '#FF0000'}],
                   [{'start': 2., 'end': 3, 'color': '#FF0000'}], [{'start': 2, 'end': False, 'color': '#FF0000'}],
                   [{'start': -1, 'end': 3, 'color': '#FF0000'}], [{'start': 3, 'end': 3, 'color': '#FF0000'}],
                   [{'start': 2, 'end': 9, 'color': '#FF0000'}], [{'start': 2, 'end': 3, 'color': '#FF000080'}],
                   [{'start': 2, 'end': 3, 'color': '#FF0000', 'font': 'fake'}],
                   [{'start': 1, 'end': 4, 'color': '#FF0000'}, {'start': 3, 'end': 5, 'color': '#00FF00'}]):
        invalid('invalid highlights ' + str(values), lambda p, v=values: p['captions'][0].update(highlights=v))
    for key, value in [('text', '甲😀乙丙丁戊己庚辛'), ('text', '甲乙\r\n丙丁戊己庚辛'), ('text', 'Ae\u0301文字测试123'),
                       ('text', '甲\u302a乙丙丁戊己庚辛'), ('text', '甲\u302f乙丙丁戊己庚辛'), ('text', '甲\uff00乙丙丁戊己庚辛'),
                       ('style', {'auto_wrapping': True}), ('style', {'auto_wrapping': False, 'vertical': True}),
                       ('transform', {'alpha': .5}), ('keyframes', [{'property': 'position_x', 'time': 0, 'value': 0}]),
                       ('text_curve', {'angle': 20}), ('font', '励字志向黑简_特粗')]:
        invalid('unsupported ' + key + '=' + str(value), lambda p, k=key, v=value: p['captions'][0].update({k: v}))
    invalid('composites excluded', lambda p: p.update(composites=[{'name': 'group', 'tracks': ['video', 'captions']}]))
    for label, mutate in [
        ('original text', lambda r: r.update(text='甲乙坏丁戊己庚辛')),
        ('range', lambda r: r['styles'][1].update(range=[2, 4])),
        ('color', lambda r: r['styles'][1]['fill']['content']['solid'].update(color=[0., 0., 1.])),
        ('font injection', lambda r: r['styles'][1].update(font={'path': 'fake'})),
        ('gap', lambda r: r['styles'].pop(0)),
        ('duplicate style', lambda r: r['styles'].append(copy.deepcopy(r['styles'][0]))),
        ('unrequested size', lambda r: r['styles'][1].update(size=33))]:
        bad = copy.deepcopy(content); altered = json.loads(bad['materials']['texts'][0]['content']); mutate(altered)
        bad['materials']['texts'][0]['content'] = json.dumps(altered, ensure_ascii=False)
        reject('readback ' + label, lambda: editor.verify_structure(bad, normalized))
        reject('preview ' + label, lambda: highlight.preview_check(bad, root / 'draft'))
    # The old plain-caption path must also reject an unrequested additional rich style.
    plain = copy.deepcopy(plan); plain['captions'][0].pop('highlights')
    editor.write_json(root / 'plain.json', plain)
    p, _, _ = editor.validate_plan(root / 'plain.json')
    reject('unrequested highlight on plain caption', lambda: editor.verify_structure(content, p))
    with patch.object(editor, 'default_text_font', return_value=None):
        reject('missing default font', lambda: editor.validate_plan(path))
    with patch.object(editor, 'jianying_install', return_value=root):
        reject('changed runtime version', lambda: editor.validate_plan(path))
    with patch.object(highlight.file_hash, 'matches', return_value=False):
        reject('changed runtime/font identity', lambda: editor.validate_plan(path))
    reject('multi-style preview missing formal plan', lambda: highlight.preview_check(content, root))
    bad = copy.deepcopy(normalized['captions'][0]); bad['text'] = '甲\ud800乙丙丁戊己庚辛'
    reject('isolated surrogate', lambda: highlight.validate(bad, normalized))
    two=copy.deepcopy(plan);two['captions'][0].update(text='甲乙丙丁\n戊己庚辛',
        highlights=[{'start':0,'end':4,'color':'#FF0000'},{'start':5,'end':9,'color':'#00FF00'}])
    for border in (False,True):
        p=copy.deepcopy(two);p['name']+='-two-'+str(border);p['captions'][0]['border']=border
        path2=root/('two-'+str(border)+'.json');editor.write_json(path2,p);before=path2.read_bytes();n,_,_=editor.validate_plan(path2)
        built=editor.build_draft(Args(plan=str(path2),out=str(root/('two-'+str(border)+'-draft'))))
        c=editor.read_json(Path(built['draft'])/'draft_content.json');rich2=json.loads(c['materials']['texts'][0]['content'])
        assert len(c['materials']['texts'])==1 and rich2['text']==p['captions'][0]['text']
        assert [s['range'] for s in rich2['styles']]==[[0,4],[4,5],[5,9]]
        pub=editor.publish_draft(Args(draft=built['draft'],draft_root=str(root/'archive'),offline_copy=True,user_data=None))
        editor.verify_structure(editor.read_json(Path(pub['draft'])/'draft_content.json'),n)
        highlight.preview_check(c,Path(built['draft']),editor.jianying_install());assert path2.read_bytes()==before
        checks.append('formal two-line build/readback/publish/preview border='+str(border))
        for label,mutate in [('newline removed',lambda r:r.update(text=r['text'].replace('\n',''))),
                            ('second line shifted',lambda r:r['styles'][2].update(range=[4,8])),
                            ('missing newline style',lambda r:r['styles'].pop(1))]:
            bad=copy.deepcopy(c);rr=json.loads(bad['materials']['texts'][0]['content']);mutate(rr)
            bad['materials']['texts'][0]['content']=json.dumps(rr,ensure_ascii=False)
            reject('two-line readback '+label,lambda:editor.verify_structure(bad,n))
            reject('two-line preview '+label,lambda:highlight.preview_check(bad,Path(built['draft'])))
    for label,change in [
        ('three lines',{'text':'甲乙\n丙丁\n戊己庚辛'}),('empty line',{'text':'甲乙丙丁\n'}),
        ('leading empty line',{'text':'\n甲乙丙丁戊己庚辛'}),('CR separator',{'text':'甲乙丙丁\r戊己庚辛'}),
        ('Unicode line separator',{'text':'甲乙丙丁\u2028戊己庚辛'}),('Unicode paragraph separator',{'text':'甲乙丙丁\u2029戊己庚辛'}),
        ('tab',{'text':'甲乙丙\t\n戊己庚辛'}),
        ('whitespace-only line',{'text':'甲乙丙丁\n    '}),('CRLF',{'text':'甲乙丙丁\r\n戊己庚辛'}),
        ('cross-line span',{'highlights':[{'start':2,'end':7,'color':'#FF0000'}]}),
        ('newline-only span',{'highlights':[{'start':4,'end':5,'color':'#FF0000'}]}),
        ('bold',{'bold':True}),('base color',{'color':'#FF0000'}),
        ('static opacity',{'style':{'auto_wrapping':False,'alpha':.5}}),
        ('dynamic opacity',{'keyframes':[{'property':'alpha','time':0,'value':0},{'property':'alpha','time':1,'value':1}]}),
        ('emoji',{'text':'甲乙😀丁\n戊己庚辛'})]:
        p=copy.deepcopy(two);p['captions'][0].update(change);editor.write_json(root/'two-invalid.json',p)
        reject('two-line '+label,lambda:editor.validate_plan(root/'two-invalid.json'))
    assert path.read_bytes() == frozen
    report = {'status': 'passed', 'check_count': len(checks), 'checks': checks, 'plan_unchanged': True,
              'UI_save_reopen_export': 'unverified'}
    editor.write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--out', type=Path, required=True)
    print(json.dumps(check(parser.parse_args().out.resolve()), ensure_ascii=True, indent=2))
