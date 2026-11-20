"""Exercise practice comparison with an isolated, locally generated A/B fixture."""
import argparse
from pathlib import Path
from PIL import Image
import subprocess
import sys

import editor
import file_hash
import practice_compare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    options = parser.parse_args(argv)
    output = editor.new_directory(options.out)
    fixture = output / 'fixture.mp4'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=1',
                '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
                '-map', '[v]', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', fixture])
    before = file_hash.sha256(fixture)
    args = argparse.Namespace(left=str(fixture), right=str(fixture), left_start=0, right_start=1,
                              duration=1, fps=15, audio='right', goal='对照', change='两段不同颜色',
                              out=str(output / 'result'))
    result = practice_compare.compare(args)
    picture = output / 'first.png'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-nostdin', '-n',
                '-i', result['comparison'], '-frames:v', '1', picture])
    with Image.open(picture) as image:
        left = image.convert('RGB').getpixel((160, 90))
        right = image.convert('RGB').getpixel((480, 90))
    assert left[0] > left[2] + 150 and right[2] > right[0] + 150
    assert result['frames'] == 15 and result['audio'] == 'right'
    assert result['human_watching'] == 'not_performed' and file_hash.sha256(fixture) == before
    try:
        practice_compare.compare(args)
        raise AssertionError('Existing output was not rejected')
    except ValueError as exc:
        assert 'already exists' in str(exc)
    args.out = str(output / 'bad-range')
    args.left_start = 1.5
    try:
        practice_compare.compare(args)
        raise AssertionError('Out-of-range input was not rejected')
    except ValueError as exc:
        assert 'exceeds media duration' in str(exc)
    assert not Path(args.out).exists()
    editor.emit({'status': 'passed', 'comparison': result['comparison'], 'frames': result['frames'],
                 'side_assignment': 'red-left/blue-right', 'audio': result['audio'],
                 'overwrite_and_range_rejected': True, 'input_unchanged': True,
                 'human_watching': 'not_performed'})
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        sys.exit(main())
    except (OSError, ValueError, AssertionError, subprocess.TimeoutExpired) as exc:
        editor.emit({'status': 'failed', 'error': str(exc)})
        sys.exit(1)
