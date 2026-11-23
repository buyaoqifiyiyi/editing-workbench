"""Regression check for shot_search (SigLIP 2 ONNX bilingual shot retrieval + histogram fallback)."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import cv2
import numpy as np

import runtime_core as core
import runtime_paths
import shot_search


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = shot_search.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_three_scene_video(mp4_path, fps=24, w=256, h=144):
    raw_dir = mp4_path.parent / '_shot_frames'
    raw_dir.mkdir(exist_ok=True)
    idx = 0
    # Shot 0 (0.0..1.0s): Bright crimson red fire flash with diagonal white slash
    for _ in range(24):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:, :] = (25, 35, 225)  # BGR red
        cv2.line(img, (20, 120), (235, 20), (255, 255, 255), 8)
        cv2.circle(img, (128, 72), 34, (60, 170, 255), -1)
        cv2.imwrite(str(raw_dir / f'{idx:04d}.png'), img)
        idx += 1
    # Shot 1 (1.0..2.0s): Deep blue ocean waves
    for i in range(24):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:60, :] = (210, 130, 50)
        img[60:, :] = (180, 75, 15)
        cv2.line(img, (0, 62 + (i % 4)), (w, 62 + (i % 4)), (245, 235, 210), 3)
        cv2.imwrite(str(raw_dir / f'{idx:04d}.png'), img)
        idx += 1
    # Shot 2 (2.0..3.0s): Lush bright green forest meadow
    for _ in range(24):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:, :] = (40, 195, 55)  # BGR green
        cv2.circle(img, (80, 50), 28, (30, 130, 25), -1)
        cv2.circle(img, (180, 80), 36, (45, 150, 35), -1)
        cv2.imwrite(str(raw_dir / f'{idx:04d}.png'), img)
        idx += 1
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', str(fps), '-i', str(raw_dir / '%04d.png'),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(mp4_path),
    ], timeout=60)
    return mp4_path


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    clip = _make_three_scene_video(root / 'three_scenes.mp4')
    cache = root / 'cache'

    # 1. Deterministic histogram fallback
    h1_out = root / 'hist-red'
    ch1, rh1 = _run_main([
        '--input', clip, '--query', 'red flame', '--out', h1_out,
        '--index-cache', cache, '--backend', 'histogram', '--detector', 'stride', '--stride', '1.0',
    ])
    assert ch1 == 0 and rh1['status'] == 'passed' and rh1['indexed_shots'] == 3, rh1
    assert rh1['matches'][0]['mid_time'] < 1.0, rh1['matches']
    assert Path(rh1['matches'][0]['thumbnail']).is_file()

    ch2, rh2 = _run_main([
        '--input', clip, '--query', 'green forest', '--out', root / 'hist-green',
        '--index-cache', cache, '--backend', 'histogram', '--detector', 'stride', '--stride', '1.0',
    ])
    assert ch2 == 0 and rh2['cache_hits'] == 1 and rh2['matches'][0]['mid_time'] > 2.0, rh2['matches']
    checks.append('histogram fallback: cold/warm cache & color queries (red->shot0, green->shot2)')

    # 2. Live SigLIP 2 ONNX bilingual shot retrieval when available
    if runtime_paths.discover_model('siglip2') is not None:
        s1_out = root / 'siglip-en'
        cs1, rs1 = _run_main([
            '--input', clip, '--query', 'bright red fire explosion flash',
            '--out', s1_out, '--index-cache', cache, '--backend', 'siglip2', '--gpu',
        ])
        assert cs1 == 0 and rs1['backend'] == 'siglip2_onnx' and rs1['indexed_shots'] >= 3, rs1
        assert rs1['matches'][0]['mid_time'] < 1.0, rs1['matches']

        cs2, rs2 = _run_main([
            '--input', clip, '--query', '蓝色大海波浪',
            '--out', root / 'siglip-zh', '--index-cache', cache, '--backend', 'siglip2', '--gpu',
        ])
        assert cs2 == 0 and rs2['cache_hits'] == 1, rs2
        assert 0.9 <= rs2['matches'][0]['mid_time'] <= 2.1, rs2['matches']
        checks.append(
            f'SigLIP 2 ONNX ({rs1["provider"]}): EN red explosion & CN 蓝色大海 hit exact shots with warm cache'
        )
    else:
        checks.append('SigLIP 2 ONNX skipped: weights not installed')

    # 3. Verify --with-motion attaches entry/exit optical-flow metadata to matched shots
    m_out = root / 'hist-motion'
    cm, rm = _run_main([
        '--input', clip, '--query', 'red flame', '--out', m_out,
        '--index-cache', cache, '--backend', 'histogram', '--detector', 'stride', '--stride', '1.0',
        '--top-k', '2', '--with-motion',
    ])
    assert cm == 0 and rm['with_motion'] is True and len(rm['matches']) == 2, rm
    for item in rm['matches']:
        mot = item.get('motion')
        assert isinstance(mot, dict) and {'entry_direction', 'exit_direction', 'zoom', 'peak_speed', 'action_peaks'} <= set(mot), item
    checks.append('shot-search --with-motion attached optical-flow entry/exit motion & zoom metadata')

    # 4. Refusal on existing out
    cb, rb = _run_main(['--input', clip, '--query', 'red', '--out', h1_out])
    assert cb == 1 and rb['status'] == 'failed'
    checks.append('existing out refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'shot-search-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
