"""Offline integration check using synthetic media; never opens Jianying."""
import argparse
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
from unittest.mock import patch

import editor


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    movie, image, audio = root / '镜头 测试.mp4', root / '静帧.png', root / '声音.wav'
    ffmpeg = editor.tool('ffmpeg')
    editor.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=red:s=320x240:r=30:d=2',
                '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=30:d=2',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
                '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
                '-map', '[v]', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-t', '4', movie])
    editor.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=green:s=320x240', '-frames:v', '1', image])
    editor.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'sine=frequency=220:duration=6', '-c:a', 'pcm_s16le', audio])
    plan = {'schema_version': 1, 'name': '中文脚本仿剪-v1', 'mode': 'hybrid',
            'canvas': {'width': 320, 'height': 240, 'fps': 30},
            'clips': [
                {'path': movie.name, 'start': 0, 'duration': 2, 'volume': 1,
                 'transition': {'name': '叠化', 'duration': 0.2},
                 'keyframes': [{'property': 'uniform_scale', 'time': 0, 'value': 1},
                               {'property': 'uniform_scale', 'time': 2, 'value': 1.1}]},
                {'path': movie.name, 'start': 2, 'duration': 1, 'source_in': 1, 'speed': 2,
                 'keyframes': [{'property': 'position_x', 'time': 0, 'value': -0.4},
                               {'property': 'position_x', 'time': 0.75, 'value': 0.4}]},
                {'path': image.name, 'start': 3, 'duration': 1},
                {'path': image.name, 'start': 1.5, 'duration': 1, 'track': 'overlay',
                 'transform': {'scale_x': 0.5, 'scale_y': 0.5, 'transform_x': 0.5}}],
            'audio': [{'path': audio.name, 'start': 0, 'duration': 4, 'fade_in': 0.2, 'fade_out': 0.3}],
            'captions': [{'text': '中文字幕第一句', 'start': 0.1, 'duration': 1.7},
                         {'text': '变速和静帧', 'start': 2.1, 'duration': 1.6},
                         {'text': '标题', 'track': 'titles', 'start': 0, 'duration': 1, 'y': 0}],
            'notes': ['合成素材，仅验证技术结构'], 'missing_assets': []}
    plan_path = root / 'edit-plan.json'
    editor.write_json(plan_path, plan)
    original_hash = editor.hashlib.sha256(movie.read_bytes()).hexdigest()
    checks = []
    normalized, probes, warnings = editor.validate_plan(plan_path)
    assert normalized['clips'][0]['path'] == str(movie.resolve()) and len(probes) == 3
    checks.append('relative paths, Unicode and spaces')
    destination = root / 'draft-v1'
    result = editor.build_draft(SimpleNamespace(plan=str(plan_path), out=str(destination)))
    assert result['verification']['segments'] == 8 and result['verification']['duration_seconds'] == 4
    assert isinstance(result['verification']['native_effects'], dict)
    content = editor.read_json(destination / 'draft_content.json')
    video = [s for t in content['tracks'] if t['type'] == 'video' for s in t['segments']]
    fast = next(s for s in video if s['target_timerange']['start'] == 2_000_000)
    assert fast['speed'] == 2 and fast['source_timerange'] == {'start': 1_000_000, 'duration': 2_000_000}
    assert [p['time_offset'] for p in fast['common_keyframes'][0]['keyframe_list']] == [1_000_000, 2_500_000]
    corrupt_motion = copy.deepcopy(content)
    corrupt_fast = next(s for t in corrupt_motion['tracks'] if t['name'] == 'video'
                        for s in t['segments'] if s['target_timerange']['start'] == 2_000_000)
    corrupt_fast['common_keyframes'][0]['keyframe_list'][0]['time_offset'] = 0
    try:
        editor.verify_structure(corrupt_motion, normalized)
    except ValueError as exc:
        assert 'source-time motion' in str(exc)
    else:
        raise AssertionError('Video local-time keyframes must fail readback')
    checks.append('nonzero source in and speed two motion timing; corrupted timestamps rejected')
    first = next(s for s in video if s['target_timerange']['start'] == 0)
    assert first['common_keyframes'] and content['materials']['transitions']
    assert '中文字幕第一句' in (destination / '字幕.srt').read_text(encoding='utf-8')
    assert '标题' not in (destination / '字幕.srt').read_text(encoding='utf-8')
    assert editor.read_json(destination / 'verification.json')['jianying_open'] == 'unverified'
    assert result['verification']['editing_status'] == 'complete'
    assert result['verification']['final_check_owner'] == result['verification']['export_owner'] == 'user'
    assert result['verification']['native_export'] == 'unverified'
    checks.append('new editable multi-track draft, trims, speed, still image, keyframes, transition, fades and subtitles')
    reference = editor.analyze_reference(SimpleNamespace(input=str(movie), out=str(root / 'reference'),
                                                        start=0, duration=120, threshold=0.3, max_frames=8))
    analysis = editor.read_json(reference['analysis'])
    assert analysis['entire_video_analyzed'] and any(abs(t - 2) < 0.1 for t in analysis['cut_candidates'])
    assert len(analysis['frames']) == 8 and Path(analysis['audio']).is_file()
    assert abs(editor.probe(analysis['audio'])['duration'] - 4) < 0.1
    assert analysis['semantic_analysis'] == 'not_performed'
    window = editor.analyze_reference(SimpleNamespace(input=str(movie), out=str(root / 'reference-window'),
                                                     start=1, duration=2, threshold=0.3, max_frames=4))
    assert not window['entire_video_analyzed']
    w = editor.read_json(window['analysis'])
    assert w['audio_time_origin'] == 1 and any(abs(t - 2) < 0.1 for t in w['cut_candidates'])
    checks.append('reference scene candidates, bounded frames, audio extraction and partial-window timestamps')
    inspection = editor.inspect_media(SimpleNamespace(input=str(root), out=str(root / 'inventory'), frames=2, max_files=20))
    assert inspection['status'] == 'success'
    checks.append('media inventory and thumbnails')
    root_copy = root / 'fake-app-drafts'
    root_copy.mkdir()
    published = editor.publish_draft(SimpleNamespace(draft=str(destination), draft_root=str(root_copy), offline_copy=True))
    assert published['global_index_modified'] is False
    assert editor.read_json(Path(published['draft']) / 'draft_meta_info.json')['draft_fold_path'] == published['draft']
    checks.append('safe draft-directory copy without homepage index mutation')
    fake_local = root / 'local-appdata'
    user_data = fake_local / 'JianyingPro' / 'User Data'
    setting = user_data / 'Config' / 'globalSetting'
    active = root / '自定义 草稿目录'
    active.mkdir()
    setting.parent.mkdir(parents=True)
    setting.write_text('[General]\ncurrentCustomDraftPath=' + str(active).replace('\\', '\\\\') + '\n', encoding='utf-8')
    index_path = user_data / 'Projects' / 'com.lveditor.draft' / 'root_meta_info.json'
    index_path.parent.mkdir(parents=True)
    existing = {'draft_id': 'existing', 'draft_fold_path': str(root / 'existing')}
    index_data = {'all_draft_store': [existing], 'draft_ids': 91}
    editor.write_json(index_path, index_data)
    with patch.dict(editor.os.environ, {'LOCALAPPDATA': str(fake_local)}):
        assert Path(editor.draft_location()['active_draft_root']) == active.resolve()
        checks.append('Qt custom draft directory with Unicode and escaped Windows separators')
        try:
            editor.publish_draft(SimpleNamespace(draft=str(destination), draft_root=str(index_path.parent)))
        except ValueError as exc:
            assert 'configured draft root' in str(exc)
        else:
            raise AssertionError('Wrong default directory accepted for live publication')
        checks.append('wrong default draft directory rejected before copying')
        live_destination = active / plan['name']
        def native_registration():
            until = time.monotonic() + 10
            while not live_destination.is_dir() and time.monotonic() < until:
                time.sleep(0.02)
            index_data['all_draft_store'].append({'draft_id': 'native-assigned-id', 'draft_fold_path': str(live_destination)})
            editor.write_json(index_path, index_data)
        native_writer = threading.Thread(target=native_registration)
        native_writer.start()
        live_result = editor.publish_draft(SimpleNamespace(draft=str(destination), draft_root=None, wait_seconds=3))
        native_writer.join()
        assert live_result['draft'] == str(live_destination.resolve())
        assert live_result['registration']['draft_id'] == 'native-assigned-id' and live_result['restart_required'] is False
        assert editor.read_json(index_path) == index_data and index_data['all_draft_store'][0] == existing
        checks.append('default publication uses active directory and waits for native registration without index writes')
        assert editor.wait_for_registration(root / 'absent', str(index_path), 0)['status'] == 'unverified'
        checks.append('missing native registration remains unverified')
        setting.write_text('[General]\ncurrentCustomDraftPath=' + str(root / 'missing-custom') + '\n', encoding='utf-8')
        try:
            editor.draft_location()
        except ValueError:
            pass
        else:
            raise AssertionError('Unavailable custom root silently fell back to default')
        checks.append('unavailable custom directory rejected without default fallback')
    for action in (lambda: editor.build_draft(SimpleNamespace(plan=str(plan_path), out=str(destination))),
                   lambda: editor.publish_draft(SimpleNamespace(draft=str(destination), draft_root=str(root_copy), offline_copy=True))):
        try:
            action()
        except FileExistsError:
            pass
        else:
            raise AssertionError('Existing output was not rejected')
    checks.append('existing draft/output overwrite rejected')
    for label, change in [
        ('source range', lambda p: p['clips'][1].update(source_in=3)),
        ('same-track overlap', lambda p: p['clips'][1].update(start=1)),
        ('main video gap', lambda p: p['clips'][1].update(start=2.5)),
        ('unknown field', lambda p: p['clips'][0].update(fake_effect=True)),
        ('missing media', lambda p: p['clips'][0].update(path='absent.mp4')),
        ('illegal project name', lambda p: p.update(name='../original')),
        ('duplicate keyframe', lambda p: p['clips'][0]['keyframes'].append(dict(p['clips'][0]['keyframes'][0]))),
        ('oversized fade', lambda p: p['audio'][0].update(fade_in=4, fade_out=1)),
        ('wrong audio stream', lambda p: p['audio'][0].update(path=movie.name)),
        ('nonfinite speed', lambda p: p['clips'][1].update(speed=float('nan'))),
    ]:
        invalid = copy.deepcopy(plan)
        change(invalid)
        editor.write_json(plan_path, invalid)
        try:
            editor.validate_plan(plan_path)
        except ValueError:
            checks.append(label + ' rejected')
        else:
            raise AssertionError(label + ' accepted')
    editor.write_json(plan_path, plan)
    content_path = destination / 'draft_content.json'
    content_path.write_text(content_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    try:
        editor.publish_draft(SimpleNamespace(draft=str(destination), draft_root=str(root_copy), offline_copy=True))
    except ValueError:
        checks.append('modified draft rejected for publish')
    else:
        raise AssertionError('Modified draft was published')
    assert editor.hashlib.sha256(movie.read_bytes()).hexdigest() == original_hash
    checks.append('source media unchanged')
    report = {'status': 'passed', 'checks': checks, 'checks_count': len(checks),
              'jianying_ui': 'not_tested', 'native_export': 'not_tested',
              'root': str(root), 'doctor': editor.doctor()}
    editor.write_json(root / 'self-check.json', report)
    return report


if __name__ == '__main__':
    if hasattr(editor.sys.stdout, 'reconfigure'):
        editor.sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', help='New directory for inspectable synthetic evidence; never overwritten')
    args = parser.parse_args()
    if args.work:
        editor.emit(check(Path(args.work).resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix='jianying-skill-check-') as temp:
            editor.emit(check(Path(temp) / 'check'))
