"""Microsecond-boundary regression: frame-rate times must validate and build identically."""
import argparse
from pathlib import Path
from types import SimpleNamespace
import tempfile

import editor


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    movie, audio = root / 'movie.mp4', root / 'audio.wav'
    ffmpeg = editor.tool('ffmpeg')
    editor.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-f', 'lavfi',
                '-i', 'testsrc=s=320x240:r=30:d=12', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', movie])
    editor.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-f', 'lavfi',
                '-i', 'sine=frequency=330:duration=12', '-c:a', 'pcm_s16le', audio])
    # 0.7633758 + 5.4599835 rounds to 6223359us as a sum but 6223360us as us(start)+us(duration):
    # the old check accepted it and pyJianYingDraft then raised SegmentOverlap.
    a, d = 0.7633758, 5.4599835
    clips = [{'path': movie.name, 'start': 0, 'duration': a},
             {'path': movie.name, 'start': a, 'duration': d, 'source_in': 1 / 30},
             {'path': movie.name, 'start': a + d, 'duration': 7 / 30, 'source_in': 6}]
    total = a + d + 7 / 30
    plan = {'schema_version': 1, 'name': 'timing-check', 'mode': 'script',
            'canvas': {'width': 320, 'height': 240, 'fps': 30}, 'clips': clips,
            # Half-speed audio of 1/30-based length: backend used to recompute round(source/speed).
            'audio': [{'path': audio.name, 'start': 1 / 30, 'duration': 41 / 30, 'speed': 0.5},
                      {'path': audio.name, 'start': 1 / 30 + 41 / 30, 'duration': total - 42 / 30, 'source_in': 1}],
            'captions': [{'text': '第一行\n\n第二行', 'start': 1 / 3, 'duration': 2 / 3}]}
    path = root / 'edit-plan.json'
    editor.write_json(path, plan)
    validated, _, _ = editor.validate_plan(path)
    for item in validated['clips'] + validated['audio'] + validated['captions']:
        assert editor.us(item['start']) + editor.us(item['duration']) == editor.us(item['start'] + item['duration'])
    result = editor.build_draft(SimpleNamespace(plan=str(path), out=str(root / 'draft')))
    srt = (root / 'draft' / '字幕.srt').read_text(encoding='utf-8')
    if '\n\n第二行' in srt:
        raise AssertionError('Blank caption line split the SRT cue')
    rebuilt, _, _ = editor.validate_plan(root / 'draft' / 'edit-plan.json')
    if rebuilt['clips'] != validated['clips'] or rebuilt['audio'] != validated['audio']:
        raise AssertionError('Snapping is not idempotent across build/publish revalidation')
    overlap = dict(plan, clips=[dict(clips[0], duration=a + 0.0000015), *clips[1:]])
    editor.write_json(root / 'overlap.json', overlap)
    try:
        editor.validate_plan(root / 'overlap.json')
    except ValueError as exc:
        if 'Overlapping' not in str(exc):
            raise
    else:
        raise AssertionError('1us overlap must be rejected before build')
    return result['verification']['duration_seconds']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', help='New directory for evidence; never overwritten')
    args = parser.parse_args()
    if args.work:
        print({'timing_check': 'passed', 'duration_seconds': check(Path(args.work).resolve())})
    else:
        with tempfile.TemporaryDirectory() as directory:
            print({'timing_check': 'passed', 'duration_seconds': check(Path(directory) / 'case')})


if __name__ == '__main__':
    main()
