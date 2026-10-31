"""Transparent overlay video: probe/validate admission and native alpha compositing.

Synthetic media only. PNG-in-MOV and QuickTime Animation must composite exactly over the
background (straight alpha, source offset, timeline start, scale); VP9 alpha and ProRes 4444
must be refused at validate; opaque H.264 and palette GIF must not be mistaken for alpha.
No UI, no publish to the live draft root.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace as Args

from PIL import Image

import editor
import native_preview

W, H, FPS = 1280, 720, 30
BASE = (32, 80, 160)
RED = (255, 0, 0)
HALF_RED = (144, 40, 80)  # red at alpha 0.5 over BASE; 127/128 encoder rounding fits the tolerance
WHITE = (255, 255, 255)
TOLERANCE = 3
# opaque red box left, 50% red box right, white box top-centre only from source second 1
DRAW = ("drawbox=x=128:y=360:w=384:h=240:color=red@1.0:t=fill:replace=1,"
        "drawbox=x=768:y=360:w=384:h=240:color=red@0.5:t=fill:replace=1,"
        "drawbox=x=512:y=72:w=256:h=144:color=white@1.0:t=fill:replace=1:enable='gte(t,1)'")
ADMITTED = {'png': ('mov', ['-c:v', 'png', '-pix_fmt', 'rgba']),
            'qtrle': ('mov', ['-c:v', 'qtrle', '-pix_fmt', 'argb'])}
REFUSED = {'vp9': ('webm', ['-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-b:v', '0', '-crf', '30']),
           'prores4444': ('mov', ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le'])}
# 320x180 preview coordinates
P_RED, P_HALF, P_TOP, P_BG = (80, 120), (240, 120), (160, 36), (160, 110)
P_SCALED_RED = (120, 105)  # centre of the opaque box after scale 0.5 about the canvas centre


def encode(root, name, ext, args, seconds=3):
    out = root / f'{name}.{ext}'
    src = f'color=c=black@0.0:s={W}x{H}:r={FPS}:d={seconds},format=rgba,{DRAW}'
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', src, *args, '-an', str(out)])
    return out


def plan(root, name, overlay, **clip):
    if not (root / 'base.png').exists():
        Image.new('RGB', (W, H), BASE).save(root / 'base.png')
    item = {'path': str(overlay), 'start': 0, 'duration': 2, 'track': 'overlay', **clip}
    data = {'schema_version': 1, 'name': f'透明视频-{name}', 'mode': 'script',
            'canvas': {'width': W, 'height': H, 'fps': FPS},
            'clips': [{'path': str(root / 'base.png'), 'start': 0, 'duration': 3}, item],
            'layer_order': ['video', 'overlay']}
    path = root / f'plan-{name}.json'
    editor.write_json(path, data)
    return path


def near(actual, expected):
    return all(abs(a - b) <= TOLERANCE for a, b in zip(actual, expected))


def expect_pixels(preview_result, expected):
    """expected: list (one per requested time) of {point: rgb}."""
    seen = []
    for frame, wanted in zip(preview_result['frames'], expected):
        img = Image.open(frame['path']).convert('RGB')
        for point, rgb in wanted.items():
            got = img.getpixel(point)
            seen.append({'time_us': frame['timestamp_us'], 'point': point, 'got': got, 'want': rgb})
            if not near(got, rgb):
                raise AssertionError(f'native pixel {point} at {frame["timestamp_us"]}us: {got} != {rgb}')
    if len(preview_result['frames']) != len(expected):
        raise AssertionError('native preview returned the wrong number of frames')
    return seen


def check(out, install):
    root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    report = {'probe': {}, 'refused': {}, 'native': {}}
    media = {name: encode(root, name, ext, args) for name, (ext, args) in {**ADMITTED, **REFUSED}.items()}
    media['h264'] = encode(root, 'h264', 'mp4', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p'])
    media['gif'] = encode(root, 'palette', 'gif', ['-c:v', 'gif'], seconds=1)
    for name, path in media.items():
        info = editor.probe(path)
        report['probe'][name] = {k: info.get(k) for k in ('kind', 'codec', 'pix_fmt', 'alpha')}
        if info.get('alpha') is not (name in ADMITTED or name in REFUSED):
            raise AssertionError(f'{name}: alpha detection {info.get("alpha")} from {info.get("pix_fmt")}')
    for name in REFUSED:
        try:
            editor.validate_plan(plan(root, name, media[name]))
        except ValueError as exc:
            if 'not admitted' not in str(exc):
                raise
            report['refused'][name] = str(exc)
        else:
            raise AssertionError(f'{name} transparent video must be refused at validate')
    editor.validate_plan(plan(root, 'h264', media['h264']))
    editor.validate_plan(plan(root, 'gif', media['gif'], duration=0.9))

    for name in ADMITTED:
        # overlay enters at timeline 0.5 from source 0.8: the white box (source >= 1) shows from timeline 0.7
        timed = plan(root, name + '-timed', media[name], start=0.5, source_in=0.8, duration=2)
        editor.validate_plan(timed)
        built = editor.build_draft(Args(plan=str(timed), out=str(root / f'draft-{name}-timed')))
        result = native_preview.preview(install, Path(built['draft']) / 'draft_content.json',
                                        root / f'preview-{name}-timed', [0.3, 0.6, 1.2])
        before = {P_RED: BASE, P_HALF: BASE, P_TOP: BASE, P_BG: BASE}
        early = {P_RED: RED, P_HALF: HALF_RED, P_TOP: BASE, P_BG: BASE}
        late = {P_RED: RED, P_HALF: HALF_RED, P_TOP: WHITE, P_BG: BASE}
        report['native'][name + '-timed'] = expect_pixels(result, [before, early, late])

        scaled = plan(root, name + '-scaled', media[name],
                      transform={'scale_x': 0.5, 'scale_y': 0.5})
        built = editor.build_draft(Args(plan=str(scaled), out=str(root / f'draft-{name}-scaled')))
        result = native_preview.preview(install, Path(built['draft']) / 'draft_content.json',
                                        root / f'preview-{name}-scaled', [1.5])
        report['native'][name + '-scaled'] = expect_pixels(result, [{P_SCALED_RED: RED, P_RED: BASE, P_BG: BASE}])
    report['status'] = 'passed'
    (root / 'alpha-video-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--install', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.out, args.install)
    editor.emit({'status': result['status'], 'probe': result['probe'], 'refused': sorted(result['refused']),
                 'native_cases': sorted(result['native'])})


if __name__ == '__main__':
    main()
