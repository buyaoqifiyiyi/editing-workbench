"""Draft media library and Jianying settings decoding; synthetic media, never opens Jianying.

Each top-level local video, photo and audio must get one draft_meta_info media-library entry per
file with its material pointing at it (so Jianying lists the files in its media panel), and the
entries must survive an offline publish. currentCustomDraftPath must decode Qt INI escapes,
including \\xHHHH for a Chinese folder and an escaped hex digit right after it.
"""
import argparse
from pathlib import Path
from types import SimpleNamespace as Args

import editor


def qt_escape(text):
    """What QSettings writes without a codec: backslashes doubled, non-ASCII as \\xHHHH,
    and a hex digit right after such an escape escaped too."""
    out, after_hex = [], False
    for ch in text:
        if ch == '\\':
            out.append('\\\\'); after_hex = False
        elif ord(ch) > 0x7e or (after_hex and ch in '0123456789abcdefABCDEF'):
            out.append('\\x%x' % ord(ch)); after_hex = True
        else:
            out.append(ch); after_hex = False
    return ''.join(out)


def library_case(root):
    ffmpeg = editor.tool('ffmpeg')
    movie, still, sound = root / '镜头.mp4', root / '静帧.png', root / '配乐.wav'
    editor.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=s=320x240:r=30:d=3',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(movie)])
    editor.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=320x240', '-frames:v', '1', str(still)])
    editor.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=330:duration=4', '-c:a', 'pcm_s16le', str(sound)])
    plan = {'schema_version': 1, 'name': '素材库登记', 'mode': 'script',
            'canvas': {'width': 320, 'height': 240, 'fps': 30},
            'clips': [{'path': str(movie), 'start': 0, 'duration': 1},
                      {'path': str(still), 'start': 1, 'duration': 1},
                      {'path': str(movie), 'start': 2, 'duration': 1, 'source_in': 1.5}],
            'audio': [{'path': str(sound), 'start': 0, 'duration': 3, 'track': 'music'}]}
    plan_path = root / 'plan.json'
    editor.write_json(plan_path, plan)
    built = editor.build_draft(Args(plan=str(plan_path), out=str(root / 'draft')))
    result = {}
    for label, folder in (('build', Path(built['draft'])), ('publish', None)):
        if folder is None:
            (root / 'archive').mkdir()
            folder = Path(editor.publish_draft(Args(draft=built['draft'], draft_root=str(root / 'archive'),
                                                    offline_copy=True, user_data=None))['draft'])
        content = editor.read_json(folder / 'draft_content.json')
        meta = editor.read_json(folder / 'draft_meta_info.json')
        library = next(g['value'] for g in meta['draft_materials'] if g['type'] == 0)
        by_id = {e['id']: e for e in library}
        paths = {str(Path(e['file_Path']).resolve()).lower() for e in library}
        assert len(library) == len(by_id) == 3, library
        assert paths == {str(p.resolve()).lower() for p in (movie, still, sound)}, paths
        assert {e['metetype'] for e in library} == {'video', 'photo', 'music'}, library
        materials = content['materials']['videos'] + content['materials']['audios']
        assert materials and all(m['local_material_id'] in by_id for m in materials), materials
        for m in materials:
            assert str(Path(by_id[m['local_material_id']]['file_Path']).resolve()) == str(Path(m['path']).resolve())
        result[label] = sorted(by_id)
    assert result['build'] == result['publish'], 'library ids must survive publish'
    return {'entries': len(result['build']), 'materials_linked': True, 'publish_kept_ids': True}


def settings_case(root):
    folder = root / '草稿a目录'
    folder.mkdir()
    user = root / 'user'
    (user / 'Config').mkdir(parents=True)
    setting = user / 'Config' / 'globalSetting'
    utf8_codec = str(folder).replace('\\', '\\\\')  # Qt with a UTF-8 codec: only backslashes escaped
    forms = {'utf8_codec': utf8_codec, 'qt_escaped': qt_escape(str(folder)),
             'quoted': '"' + qt_escape(str(folder)) + '"', 'single_backslash_legacy': str(folder)}
    assert '\\x61' in forms['qt_escaped'], 'hex digit after an escape must itself be escaped in the fixture'
    for name, value in forms.items():
        setting.write_text('[General]\ncurrentCustomDraftPath=' + value + '\n', encoding='utf-8')
        got = editor.draft_location(user)['active_draft_root']
        assert got == str(folder.resolve()), (name, value, got)
    ascii_dir = root / 'plain'
    ascii_dir.mkdir()
    setting.write_text('[General]\ncurrentCustomDraftPath=' + str(ascii_dir).replace('\\', '\\\\') + '\n', encoding='utf-8')
    assert editor.draft_location(user)['active_draft_root'] == str(ascii_dir.resolve())
    return sorted(forms) + ['ascii_doubled']


def check(out):
    root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    return {'status': 'passed', 'library': library_case(root), 'settings_forms': settings_case(root)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    editor.emit(check(parser.parse_args().out))
