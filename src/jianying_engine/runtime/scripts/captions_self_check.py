"""Synthetic caption import checks; no model inference, desktop use or publication."""
import argparse
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile

import captions
import editor


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    video, audio = root / 'video.mp4', root / 'voice.wav'
    for path, source, options in (
        (video, 'color=c=blue:s=160x90:r=30:d=8', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']),
        (audio, 'sine=frequency=330:duration=8', ['-c:a', 'pcm_s16le'])):
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-f', 'lavfi', '-i', source, *options, path])
    base = {'schema_version': 1, 'name': '字幕导入自检', 'mode': 'script',
            'canvas': {'width': 160, 'height': 90, 'fps': 30},
            'clips': [{'path': 'video.mp4', 'start': 0, 'duration': 4, 'source_in': 2, 'speed': 1.5}],
            'audio': [{'path': 'voice.wav', 'start': 1, 'duration': 2, 'source_in': 2, 'speed': 2}],
            'captions': [{'text': '已存在', 'start': 3.5, 'duration': 0.4, 'track': 'old'}],
            'layer_order': ['voice', 'video', 'old']}
    segments = [{'id': 0, 'start': 3, 'end': 4.5, 'text': ' 原文，不改。 '},
                {'id': 1, 'start': 5, 'end': 6.5, 'text': '第二句！'}]
    plan_path, transcript = root / 'input.json', root / 'transcript.json'
    report_path = root / 'transcript-report.json'

    def fixture(plan=None, cues=None, source=None):
        cues = copy.deepcopy(segments if cues is None else cues)
        source = source or video
        editor.write_json(plan_path, base if plan is None else plan)
        editor.write_json(transcript, {'segments': cues, 'textAll': ''.join(c['text'] for c in cues)})
        editor.write_json(report_path, {'status': 'passed', 'input': str(source), 'segments': cues,
                                      'audio': {'bytes': source.stat().st_size}})

    def args(name, group='clips', index=0, track='captions'):
        return SimpleNamespace(plan=str(plan_path), transcript=str(transcript), group=group,
                               index=index, track=track, out=str(root / name / 'plan.json'))

    def hashes():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (plan_path, transcript, report_path, video, audio) if p.exists()}

    def reject(label, expected, options=None):
        options = options or args('reject-' + str(len(checks)))
        before = hashes()
        try:
            captions.execute(options)
        except (ValueError, OSError) as exc:
            assert expected.lower() in str(exc).lower(), (label, str(exc))
        else:
            raise AssertionError(label + ' was accepted')
        assert hashes() == before
        assert not Path(options.out).exists()
        checks.append(label)

    fixture()
    before = hashes()
    request = args('imported')
    result = captions.execute(request)
    assert result['imported_captions'] == 2 and hashes() == before
    plan, _, _ = editor.validate_plan(request.out)
    added = plan['captions'][1:]
    assert [(c['start'], c['duration'], c['text']) for c in added] == [
        (0.666667, 1.0, segments[0]['text']), (2.0, 1.0, segments[1]['text'])]
    saved = editor.read_json(request.out)
    assert saved['captions'][0] == base['captions'][0]
    assert saved['layer_order'] == ['voice', 'video', 'old', 'captions']
    assert Path(saved['clips'][0]['path']) == video and Path(saved['audio'][0]['path']) == audio
    built = editor.build_draft(SimpleNamespace(plan=request.out, out=str(root / 'draft')))
    assert built['verification']['draft_structure'] == 'passed'
    content = editor.read_json(root / 'draft/draft_content.json')
    text_track = next(t for t in content['tracks'] if t.get('name') == 'captions')
    assert [s['target_timerange'] for s in text_track['segments']] == [
        {'start': 666667, 'duration': 1000000}, {'start': 2000000, 'duration': 1000000}]
    texts = {t['id']: editor.json.loads(t['content'])['text'] for t in content['materials']['texts']}
    assert [texts[s['material_id']] for s in text_track['segments']] == [s['text'] for s in segments]
    checks.append('video speed/source offset mapping, original text, existing captions, layer order, relocation and build readback')
    before_output = Path(request.out).read_bytes()
    try:
        captions.execute(request)
    except FileExistsError:
        pass
    else:
        raise AssertionError('existing output overwritten')
    assert Path(request.out).read_bytes() == before_output and hashes() == before
    checks.append('existing output is never overwritten')
    fixture(cues=segments[:1], source=audio)
    audio_request = args('audio-imported', group='audio')
    captions.execute(audio_request)
    audio_plan, _, _ = editor.validate_plan(audio_request.out)
    cue = audio_plan['captions'][-1]
    assert (cue['start'], cue['duration'], cue['text']) == (1.5, 0.75, segments[0]['text'])
    assert editor.build_draft(SimpleNamespace(plan=audio_request.out, out=str(root / 'audio-draft')))['verification']['draft_structure'] == 'passed'
    checks.append('audio speed/source offset/timeline offset mapping validates and builds')

    for name, duration, cues, expected_duration in (
        ('decimal-full-phrase', 0.7, [{'start': 0.1, 'end': 0.8, 'text': '完整句'}], 700000),
        ('decimal-next-phrase', 0.2, [{'start': 0.1, 'end': 0.2, 'text': '内部句'},
                                      {'start': 0.3, 'end': 0.4, 'text': '范围外句'}], 100000)):
        decimal_plan = copy.deepcopy(base)
        decimal_plan['clips'][0].update(source_in=0.1, duration=duration, speed=1)
        decimal_plan['audio'] = []
        decimal_plan['captions'] = []
        decimal_plan['layer_order'] = ['video']
        fixture(plan=decimal_plan, cues=cues)
        request = args(name)
        assert captions.execute(request)['imported_captions'] == 1
        checked, _, _ = editor.validate_plan(request.out)
        assert checked['captions'][0]['start'] == 0
        assert editor.us(checked['captions'][0]['duration']) == expected_duration
        draft = root / (name + '-draft')
        assert editor.build_draft(SimpleNamespace(plan=request.out, out=str(draft)))['verification']['draft_structure'] == 'passed'
        native = editor.read_json(draft / 'draft_content.json')
        track = next(t for t in native['tracks'] if t.get('name') == 'captions')
        assert len(track['segments']) == 1
        assert track['segments'][0]['target_timerange'] == {'start': 0, 'duration': expected_duration}
        checks.append(name + ': integer microsecond boundaries validate, build and read back')

    fixture(); report_path.unlink()
    reject('missing provenance refused', 'Missing transcript-report')
    fixture(source=audio)
    reject('different source refused', 'does not match')
    fixture(); report = editor.read_json(report_path); report['input'] = 'video.mp4'; editor.write_json(report_path, report)
    reject('ambiguous relative report source refused', 'must be absolute')
    fixture(); report = editor.read_json(report_path); report['segments'][0]['text'] = 'changed'; editor.write_json(report_path, report)
    reject('transcript/report mismatch refused', 'do not match')
    fixture(); report = editor.read_json(report_path); report['audio']['bytes'] += 1; editor.write_json(report_path, report)
    reject('changed source size refused', 'size changed')
    for label, cues, expected in (
        ('partial phrase refused', [dict(segments[0], start=1)], 'partially cut'),
        ('no intersection refused', [dict(segments[0], start=0, end=1)], 'No complete'),
        ('inverted segment refused', [dict(segments[0], start=5, end=4)], 'after start'),
        ('nonfinite segment refused', [dict(segments[0], start=float('nan'))], 'non-finite'),
        ('overlapping segments refused', [segments[0], dict(segments[1], start=4)], 'overlap'),
        ('empty text refused', [dict(segments[0], text=' ')], 'nonempty')):
        fixture(cues=cues); reject(label, expected)
    for label, change, expected in (
        ('nonlinear speed refused', lambda p: p['clips'][0].update(speed_curve=[]), 'speed_curve'),
        ('zero speed refused', lambda p: p['clips'][0].update(speed=0), 'supported range'),
        ('existing same-track overlap refused', lambda p: p['captions'].append({'text': '冲突', 'start': 1, 'duration': 1}), 'Overlapping'),
        ('beyond main picture refused', lambda p: p['audio'][0].update(start=3), 'extends past main'),
        ('composite plan refused', lambda p: p.update(composites=[{'name': 'g', 'tracks': ['video']}]), 'composites')):
        changed = copy.deepcopy(base); change(changed); fixture(plan=changed); reject(label, expected)
    fixture(); reject('invalid index refused', 'out of range', args('bad-index', index=10))
    fixture(); reject('media/text track name collision refused', 'different media types', args('track-collision', track='video'))
    # External/corrected subtitles have an explicit timebase, independent of ASR reports.
    fixture()
    srt = root / 'corrected.srt'
    srt_text = '1\r\n00:00:03,000 --> 00:00:04,500\r\n 人工校正。 \r\n第二行\r\n\r\n2\r\n00:00:05,000 --> 00:00:06,500\r\n结尾！\r\n'
    srt.write_bytes(b'\xef\xbb\xbf' + srt_text.encode('utf-8'))
    srt_before = srt.read_bytes()
    source_request = args('srt-source')
    source_request.transcript = None
    source_request.srt = str(srt)
    source_request.timebase = 'source'
    result = captions.execute(source_request)
    assert result['input_format'] == 'srt' and 'not automatically verified' in result['source_identity']
    saved = editor.read_json(source_request.out)
    assert [(c['start'], c['duration'], c['text']) for c in saved['captions'][1:]] == [
        (0.666667, 1.0, ' 人工校正。 \n第二行'), (2.0, 1.0, '结尾！')]
    assert srt.read_bytes() == srt_before
    draft = root / 'srt-source-draft'
    assert editor.build_draft(SimpleNamespace(plan=source_request.out, out=str(draft)))['verification']['draft_structure'] == 'passed'
    native = editor.read_json(draft / 'draft_content.json')
    text_track = next(t for t in native['tracks'] if t.get('name') == 'captions')
    texts = {t['id']: editor.json.loads(t['content'])['text'] for t in native['materials']['texts']}
    assert [texts[s['material_id']] for s in text_track['segments']] == [' 人工校正。 \n第二行', '结尾！']
    checks.append('UTF-8 BOM/CRLF multiline corrected SRT maps source offsets/speed and builds editable text unchanged')

    timeline_request = copy.copy(source_request)
    timeline_request.out = str(root / 'srt-timeline/plan.json')
    timeline_request.timebase = 'timeline'
    timeline_request.group = 'audio'
    srt.write_text('1\n00:00:01,250 --> 00:00:02,000\n时间轴字幕\n', encoding='utf-8')
    assert captions.execute(timeline_request)['timebase'] == 'timeline'
    cue = editor.read_json(timeline_request.out)['captions'][-1]
    assert (cue['start'], cue['duration']) == (1.25, 0.75)
    checks.append('timeline SRT retains final timestamps despite selected clip source offset and speed')

    for label, content, expected in (
        ('SRT empty refused', '', 'no cues'),
        ('SRT invalid clock refused', '1\n00:60:00,000 --> 00:00:02,000\n字幕', 'expected HH'),
        ('SRT unnumbered refused', '00:00:01,000 --> 00:00:02,000\n字幕', 'numbered'),
        ('SRT trailing junk refused', '1\n00:00:01,000 --> 00:00:02,000\n字幕\n\njunk', 'numbered'),
        ('SRT missing separator refused', '1\n00:00:03,000 --> 00:00:04,000\n甲\n2\n00:00:05,000 --> 00:00:06,000\n乙', 'blank line'),
        ('SRT duplicate numbers refused', '1\n00:00:01,000 --> 00:00:02,000\n甲\n\n1\n00:00:02,000 --> 00:00:03,000\n乙', 'increasing'),
        ('SRT overlap refused', '1\n00:00:03,000 --> 00:00:04,000\n甲\n\n2\n00:00:03,500 --> 00:00:05,000\n乙', 'overlap'),
        ('SRT reverse range refused', '1\n00:00:04,000 --> 00:00:03,000\n字幕', 'after start'),
        ('SRT partial source cue refused', '1\n00:00:01,500 --> 00:00:03,000\n半句', 'partially cut'),
        ('SRT style markup refused', '1\n00:00:03,000 --> 00:00:04,000\n<i>字幕</i>', 'markup'),
        ('SRT empty text refused', '1\n00:00:03,000 --> 00:00:04,000\n ', 'nonempty'),
        ('SRT NUL refused', '1\n00:00:03,000 --> 00:00:04,000\n字\x00幕', 'without NUL')):
        srt.write_text(content, encoding='utf-8')
        options = copy.copy(source_request)
        options.out = str(root / ('srt-reject-' + str(len(checks))) / 'plan.json')
        before_srt = srt.read_bytes()
        reject(label, expected, options)
        assert srt.read_bytes() == before_srt
    srt.write_text('1\n00:00:03,000 --> 00:00:04,000\n字幕', encoding='utf-8')
    for label, timebase, transcript_value, expected in (
        ('SRT ambiguous timebase refused', None, None, 'explicit --timebase'),
        ('SRT and transcript together refused', 'source', str(transcript), 'exactly one')):
        options = copy.copy(source_request)
        options.out = str(root / ('srt-options-' + str(len(checks))) / 'plan.json')
        options.timebase, options.transcript = timebase, transcript_value
        reject(label, expected, options)
    existing_output = Path(source_request.out).read_bytes()
    try:
        captions.execute(source_request)
    except FileExistsError:
        pass
    else:
        raise AssertionError('SRT output overwritten')
    assert Path(source_request.out).read_bytes() == existing_output
    checks.append('SRT existing output refused and preserved')

    # Batch source mapping: reordered footage, repeated source range, speed changes and silence.
    batch = copy.deepcopy(base)
    batch['clips'] = [
        {'path': 'video.mp4', 'start': 0, 'duration': 2, 'source_in': 4},
        {'path': 'video.mp4', 'start': 2, 'duration': 2, 'source_in': 0, 'speed': 2},
        {'path': 'video.mp4', 'start': 4, 'duration': 1, 'source_in': 4, 'speed': 2},
        {'path': 'video.mp4', 'start': 5, 'duration': 1, 'source_in': 6}]
    batch['audio'] = []
    batch['layer_order'] = ['video', 'old']
    batch_cues = [{'start': .5, 'end': 1.5, 'text': '前段原文。'},
                  {'start': 4.5, 'end': 5.5, 'text': '后段\n第二行'}]
    fixture(plan=batch, cues=batch_cues)
    batch_request = args('batch-transcript', index=[2, 0, 1, 3])
    before = hashes()
    batch_result = captions.execute(batch_request)
    assert batch_result['selected_indices'] == [2, 0, 1, 3] and batch_result['selected_index'] is None
    assert [row['imported_captions'] for row in batch_result['mappings']] == [1, 1, 1, 0]
    saved = editor.read_json(batch_request.out)
    expected = [(.5, 1., '后段\n第二行'), (2.25, .5, '前段原文。'), (4.25, .5, '后段\n第二行')]
    assert [(c['start'], c['duration'], c['text']) for c in saved['captions'][1:]] == expected
    assert saved['captions'][0] == batch['captions'][0] and hashes() == before
    draft = root / 'batch-draft'
    assert editor.build_draft(SimpleNamespace(plan=batch_request.out, out=str(draft)))['verification']['draft_structure'] == 'passed'
    native = editor.read_json(draft / 'draft_content.json')
    text_track = next(t for t in native['tracks'] if t.get('name') == 'captions')
    texts = {t['id']: editor.json.loads(t['content'])['text'] for t in native['materials']['texts']}
    assert [(s['target_timerange']['start'], s['target_timerange']['duration'], texts[s['material_id']])
            for s in text_track['segments']] == [(editor.us(a), editor.us(d), text) for a, d, text in expected]
    checks.append('batch transcript reorders/repeats source cues, maps fixed speeds, allows silent selections and builds exact native text/timing')
    offline = root / 'batch-offline'
    offline.mkdir()
    assert Path(editor.publish_draft(SimpleNamespace(draft=str(draft), draft_root=str(offline), offline_copy=True))['draft']).is_dir()
    checks.append('batch caption draft passes existing offline publication without source changes')

    srt.write_text('1\n00:00:00,500 --> 00:00:01,500\n前段原文。\n\n2\n00:00:04,500 --> 00:00:05,500\n后段\n第二行\n', encoding='utf-8-sig')
    batch_srt = copy.copy(batch_request)
    batch_srt.transcript, batch_srt.srt, batch_srt.timebase = None, str(srt), 'source'
    batch_srt.out = str(root / 'batch-srt/plan.json')
    before_srt = srt.read_bytes()
    assert captions.execute(batch_srt)['imported_captions'] == 3
    assert [(c['start'], c['duration'], c['text']) for c in editor.read_json(batch_srt.out)['captions'][1:]] == expected
    assert srt.read_bytes() == before_srt and hashes() == before
    checks.append('one source-time SRT maps into multiple reordered/repeated clips with original text preserved')

    batch_audio = copy.deepcopy(batch)
    batch_audio['clips'] = [{'path': 'video.mp4', 'start': 0, 'duration': 6}]
    batch_audio['audio'] = [
        {'path': 'voice.wav', 'track': 'voice', 'start': 0, 'duration': 1, 'source_in': 4, 'speed': 2},
        {'path': 'voice.wav', 'track': 'voice', 'start': 2, 'duration': 2, 'source_in': 0, 'speed': 2}]
    batch_audio['layer_order'] = ['voice', 'video', 'old']
    fixture(plan=batch_audio, cues=batch_cues, source=audio)
    audio_request = args('batch-audio', group='audio', index=[1, 0])
    assert captions.execute(audio_request)['imported_captions'] == 2
    assert [(c['start'], c['duration']) for c in editor.read_json(audio_request.out)['captions'][1:]] == [(.25, .5), (2.25, .5)]
    assert editor.build_draft(SimpleNamespace(plan=audio_request.out, out=str(root / 'batch-audio-draft')))['verification']['draft_structure'] == 'passed'
    checks.append('batch audio-source transcript maps across disjoint clips and validates/builds')

    other_video = root / 'other.mp4'
    other_video.write_bytes(video.read_bytes())
    different = copy.deepcopy(batch)
    different['clips'] = different['clips'][:2]
    different['clips'][1]['path'] = 'other.mp4'
    fixture(plan=different, cues=batch_cues)
    srt.write_text('1\n00:00:00,500 --> 00:00:01,000\n时间轴一\n\n2\n00:00:02,500 --> 00:00:03,000\n时间轴二\n', encoding='utf-8')
    timeline = copy.copy(batch_srt)
    timeline.index, timeline.timebase, timeline.out = [1, 0], 'timeline', str(root / 'batch-timeline/plan.json')
    assert captions.execute(timeline)['imported_captions'] == 2
    assert [(c['start'], c['duration']) for c in editor.read_json(timeline.out)['captions'][1:]] == [(.5, .5), (2.5, .5)]
    checks.append('timeline SRT maps disjoint selections from different media without changing final timestamps')
    source_bad = copy.copy(timeline)
    source_bad.timebase, source_bad.out = 'source', str(root / 'batch-source-mismatch/plan.json')
    reject('source-time SRT cannot guess association across different media', 'same media', source_bad)

    for label, indices, expected_error in (
        ('duplicate selected indices refused', [0, 0], 'Duplicate'),
        ('empty selected indices refused', [], 'nonnegative'),
        ('boolean selected index refused', [True], 'nonnegative'),
        ('later selected index out of range refused', [0, 99], 'out of range')):
        fixture(plan=batch, cues=batch_cues)
        reject(label, expected_error, args('batch-bad-' + str(len(checks)), index=indices))
    fixture(plan=batch, cues=[batch_cues[0], {'start': 5.5, 'end': 6.5, 'text': '跨界完整原文'}])
    reject('late batch partial phrase failure leaves no output plan', 'partially cut', args('batch-partial', index=[1, 0]))
    curved = copy.deepcopy(batch)
    curved['clips'][1]['speed_curve'] = []
    fixture(plan=curved, cues=batch_cues)
    reject('later nonlinear clip refused atomically', 'speed_curve', args('batch-curve', index=[0, 1]))
    overlap = copy.deepcopy(batch)
    overlap['clips'][1]['start'] = 1
    fixture(plan=overlap, cues=batch_cues)
    reject('overlapping selected timeline ranges refused', 'Selected clips overlap', args('batch-overlap', index=[0, 1]))
    fixture(plan=batch, cues=batch_cues[:1])
    reject('all selected clips without complete cues refused', 'No complete', args('batch-empty', index=[2, 3]))
    collision = copy.deepcopy(batch)
    collision['captions'].append({'text': '已有同轨冲突', 'start': .75, 'duration': .2})
    fixture(plan=collision, cues=batch_cues)
    reject('batch same-track conflict refused before output creation', 'Overlapping', args('batch-collision', index=[0, 1, 2]))
    fixture(plan=batch, cues=batch_cues)
    old_bytes = Path(batch_request.out).read_bytes()
    try:
        captions.execute(batch_request)
    except FileExistsError:
        pass
    else:
        raise AssertionError('Batch output overwritten')
    assert Path(batch_request.out).read_bytes() == old_bytes
    checks.append('batch existing output preserved')

    word_segments = [
        {'start': .5, 'end': 1.5, 'text': ' 前段原文', 'words': [
            {'word': ' 前段', 'start': .5, 'end': 1., 'probability': .9},
            {'word': '原文', 'start': 1., 'end': 1.5, 'probability': .8}]},
        {'start': 4.5, 'end': 5.5, 'text': '后段文本', 'words': [
            {'word': '后段', 'start': 4.5, 'end': 5., 'probability': .9},
            {'word': '文本', 'start': 5., 'end': 5.5, 'probability': .8}]}]
    def word_fixture(plan=None, rows=None):
        fixture(plan=batch if plan is None else plan, cues=word_segments if rows is None else rows)
        report = editor.read_json(report_path)
        report['params'] = {'word_timestamps': True, 'level': 'word'}
        report['audio'].update(seconds=8, sha256=hashlib.sha256(video.read_bytes()).hexdigest())
        editor.write_json(report_path, report)

    word_fixture()
    word_request = args('word-batch', index=[1, 0, 2])
    word_request.level = 'word'
    before = hashes()
    word_result = captions.execute(word_request)
    assert word_result['level'] == 'word' and word_result['imported_captions'] == 6
    assert 'SHA256' in word_result['source_identity'] and hashes() == before
    word_plan = editor.read_json(word_request.out)
    expected_words = [(.5, .5, '后段'), (1., .5, '文本'), (2.25, .25, ' 前段'),
                      (2.5, .25, '原文'), (4.25, .25, '后段'), (4.5, .25, '文本')]
    assert [(c['start'], c['duration'], c['text']) for c in word_plan['captions'][1:]] == expected_words
    word_draft = root / 'word-draft'
    assert editor.build_draft(SimpleNamespace(plan=word_request.out, out=str(word_draft)))['verification']['draft_structure'] == 'passed'
    native = editor.read_json(word_draft / 'draft_content.json')
    text_track = next(t for t in native['tracks'] if t.get('name') == 'captions')
    texts = {t['id']: editor.json.loads(t['content'])['text'] for t in native['materials']['texts']}
    assert [(s['target_timerange']['start'], s['target_timerange']['duration'], texts[s['material_id']])
            for s in text_track['segments']] == [(editor.us(a), editor.us(d), text) for a, d, text in expected_words]
    checks.append('word-level transcript maps repeated/reordered/speed-adjusted clips and builds exact editable word text/timing')
    word_offline = root / 'word-offline'
    word_offline.mkdir()
    assert Path(editor.publish_draft(SimpleNamespace(draft=str(word_draft), draft_root=str(word_offline), offline_copy=True))['draft']).is_dir()
    checks.append('word-level native draft passes existing offline publish')
    split_plan = copy.deepcopy(batch)
    split_plan['clips'] = [{'path': 'video.mp4', 'start': 0, 'duration': 1, 'source_in': 4},
                           {'path': 'video.mp4', 'start': 1, 'duration': 1, 'source_in': 5}]
    split_plan['captions'], split_plan['layer_order'] = [], ['video']
    word_fixture(plan=split_plan)
    split_request = args('split-word-boundaries', index=[0, 1])
    split_request.level = 'word'
    assert captions.execute(split_request)['imported_captions'] == 2
    assert [(c['start'], c['text']) for c in editor.read_json(split_request.out)['captions']] == [(.5, '后段'), (1., '文本')]
    checks.append('word boundaries allow a clip cut within its parent sentence without inventing partial-word time')
    reject('segment mode still refuses the same half sentence', 'partially cut', args('segment-split', index=[0, 1]))
    split_plan['clips'][0]['duration'] = .75
    split_plan['clips'][1].update(start=.75, duration=1.25, source_in=4.75)
    word_fixture(plan=split_plan)
    partial = args('partial-word', index=[0, 1])
    partial.level = 'word'
    reject('cut inside a model word refused', 'partially cut', partial)
    for label, change, expected_error in (
        ('missing word-mode evidence refused', lambda r: r.pop('params'), 'word-timestamps'),
        ('missing word source hash refused', lambda r: r['audio'].pop('sha256'), 'SHA256'),
        ('changed word source hash refused', lambda r: r['audio'].update(sha256='0' * 64), 'SHA256')):
        word_fixture()
        report = editor.read_json(report_path)
        change(report)
        editor.write_json(report_path, report)
        options = args('word-report-' + str(len(checks)), index=[0, 1])
        options.level = 'word'
        reject(label, expected_error, options)
    for label, change, expected_error in (
        ('word text changed refused', lambda rows: rows[0]['words'][0].update(word='改写'), 'reconstruct'),
        ('zero word duration refused', lambda rows: rows[0]['words'][0].update(end=.5), 'nonpositive'),
        ('overlapping word times refused', lambda rows: rows[0]['words'][1].update(start=.75), 'overlap'),
        ('missing words refused', lambda rows: rows[0].pop('words'), 'nonempty')):
        rows = copy.deepcopy(word_segments)
        change(rows)
        word_fixture(rows=rows)
        options = args('word-bad-' + str(len(checks)), index=[0, 1])
        options.level = 'word'
        reject(label, expected_error, options)
    word_fixture()
    srt_word = copy.copy(batch_srt)
    srt_word.out, srt_word.level = str(root / 'srt-word-masquerade/plan.json'), 'word'
    reject('SRT cannot masquerade as model word evidence', 'not SRT', srt_word)
    old_word_output = Path(word_request.out).read_bytes()
    try:
        captions.execute(word_request)
    except FileExistsError:
        pass
    else:
        raise AssertionError('Word plan overwritten')
    assert Path(word_request.out).read_bytes() == old_word_output
    checks.append('word plan existing output preserved')

    report = {'status': 'passed', 'checks_count': len(checks), 'checks': checks,
              'inputs_preserved': True, 'model_inference': False, 'ui_operations': 0,
              'validation': 'editor validate, build and native draft JSON readback; no playback/export'}
    editor.write_json(root / 'captions-self-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    options = parser.parse_args()
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    if options.out:
        editor.emit(check(Path(options.out).resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-captions-check-') as temporary:
            editor.emit(check(Path(temporary) / 'check'))
