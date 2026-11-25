"""Self-check for openedit audit: plan stats, overlap detection, strict decode, loudness QC, rhythm alignment, and contact sheet."""
import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import wave

import numpy as np
from PIL import Image

import audit_project
import file_hash
import runtime_core as core


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = audit_project.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _write_tone_wav(path, sr=48000, dur=2.0, amp=0.22):
    n = int(round(sr * dur))
    t = np.arange(n, dtype=np.float64) / sr
    sig = amp * np.sin(2.0 * np.pi * 440.0 * t)
    stereo = np.column_stack((sig, sig))
    pcm = np.clip(np.round(stereo * 32767.0), -32767, 32767).astype('<i2')
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    media_dir = root / 'media'
    media_dir.mkdir()
    src_a = media_dir / 'clip_a.mp4'
    src_b = media_dir / 'clip_b.mp4'
    bgm = media_dir / 'bgm.wav'
    export_mp4 = root / 'export.mp4'
    ffmpeg = core.tool('ffmpeg')

    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=crimson:s=320x180:r=30:d=2',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(src_a),
    ])
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=navy:s=320x180:r=30:d=2',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(src_b),
    ])
    _write_tone_wav(bgm, sr=48000, dur=2.0, amp=0.216)
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=2',
        '-i', str(bgm),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-shortest', str(export_mp4),
    ])

    struct_dir = root / 'struct'
    struct_dir.mkdir()
    core.write_json(struct_dir / 'structure.json', {
        'status': 'passed',
        'operation': 'music-structure',
        'source': {'path': str(bgm.resolve()), 'sha256': file_hash.sha256(bgm)},
        'start_seconds': 0.0,
        'duration': 2.0,
        'bpm': 120.0,
        'beats': [0.5, 1.0, 1.5],
        'downbeats': [1.0],
        'drop_points': [{'time': 1.0, 'type': 'chorus_drop'}],
        'boundaries': [1.0],
        'segments': [],
    })

    clean_plan = root / 'clean-plan.json'
    core.write_json(clean_plan, {
        'schema_version': 1,
        'name': 'audit-clean',
        'canvas': {'width': 320, 'height': 180, 'fps': 30},
        'clips': [
            {'path': str(src_a), 'start': 0.0, 'source_in': 0.0, 'duration': 1.0, 'volume': 0},
            {'path': str(src_b), 'start': 1.0, 'source_in': 0.2, 'duration': 1.0, 'volume': 0},
        ],
        'audio': [
            {'path': str(bgm), 'track': 'music', 'start': 0.0, 'source_in': 0.0, 'duration': 2.0, 'volume': 1.0},
        ],
    })

    out_clean = root / 'audit-clean'
    code_ok, rep_ok = _run_main([
        '--plan', clean_plan, '--out', out_clean, '--export', export_mp4,
        '--beats', struct_dir, '--contact-sheet', '--sheet-frames', 6,
    ])
    assert code_ok == 0 and rep_ok['status'] == 'passed', rep_ok
    assert rep_ok['source_overlaps_count'] == 0
    assert rep_ok['strict_decode']['export'] == 'passed'
    assert rep_ok['audio_qc']['pass_qc'] is True and rep_ok['audio_qc']['pcm']['full_scale_clipped_samples'] == 0
    assert rep_ok['rhythm_alignment']['median_delta_ms'] == 0.0
    sheet_path = Path(rep_ok['contact_sheet']['path'])
    assert sheet_path.is_file()
    with Image.open(sheet_path) as im:
        assert im.size[0] >= 400 and im.size[1] >= 100
    assert rep_ok['visual_qc']['sampled_frames'] == 6 and rep_ok['visual_qc']['pass_qc'] is True, rep_ok['visual_qc']
    assert 'max_adjacent_oklab_delta' in rep_ok['visual_qc']
    assert rep_ok['pacing_qc']['clip_count'] == 2 and rep_ok['pacing_qc']['asl_seconds'] == 1.0, rep_ok['pacing_qc']
    assert rep_ok['motion_qc'] and rep_ok['motion_qc']['dynamic_ratio'] >= 0.7 and rep_ok['motion_qc']['frozen_segments'] == [], rep_ok['motion_qc']
    checks.append('clean project audit passed (0 overlaps, strict decode, audio QC, visual_qc, pacing_qc, motion_qc, 0ms beat alignment, contact sheet)')

    code_mr, rep_mr = _run_main([
        '--plan', clean_plan, '--out', root / 'audit-motion-review',
        '--export', export_mp4, '--motion-review',
    ])
    assert code_mr == 0 and rep_mr['status'] == 'passed', rep_mr
    assert rep_mr['motion_review']['video']['strict_decode'] == 'passed'
    assert Path(rep_mr['motion_review']['video']['path']).is_file()
    strips_p = Path(rep_mr['motion_review']['cut_strips']['path'])
    assert strips_p.is_file()
    with Image.open(strips_p) as sim:
        assert sim.size[0] >= 600 and sim.size[1] >= 120
    checks.append('--motion-review generated 360p/15fps preview and cut_strips.jpg cleanly')

    static_plan = root / 'static-plan.json'
    core.write_json(static_plan, {
        'schema_version': 1, 'name': 'audit-static', 'canvas': {'width': 320, 'height': 180, 'fps': 30},
        'clips': [{'path': str(src_a), 'start': 0.0, 'source_in': 0.0, 'duration': 2.0, 'volume': 0}],
    })
    code_st, rep_st = _run_main(['--plan', static_plan, '--out', root / 'audit-static', '--export', src_a])
    assert code_st == 0 and rep_st['status'] == 'passed', rep_st
    assert len(rep_st['motion_qc']['frozen_segments']) >= 1 and rep_st['motion_qc']['dynamic_ratio'] <= 0.25, rep_st['motion_qc']
    checks.append('motion_qc detected static frozen segment deterministically')

    dup_plan = root / 'dup-plan.json'
    core.write_json(dup_plan, {
        'schema_version': 1,
        'name': 'audit-dup',
        'canvas': {'width': 320, 'height': 180, 'fps': 30},
        'clips': [
            {'path': str(src_a), 'start': 0.0, 'source_in': 0.0, 'duration': 1.0, 'volume': 0},
            {'path': str(src_a), 'start': 1.0, 'source_in': 0.5, 'duration': 1.0, 'volume': 0},
        ],
        'audio': [
            {'path': str(bgm), 'track': 'music', 'start': 0.0, 'source_in': 0.0, 'duration': 2.0, 'volume': 1.0},
        ],
    })
    out_dup = root / 'audit-dup'
    code_dup, rep_dup = _run_main(['--plan', dup_plan, '--out', out_dup])
    assert code_dup == 0 and rep_dup['status'] == 'attention', rep_dup
    assert rep_dup['source_overlaps_count'] == 1, rep_dup
    checks.append('duplicate source interval overlap detected cleanly as attention')

    white_mp4 = root / 'white_blowout.mp4'
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=white:s=320x180:r=30:d=1',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(white_mp4),
    ])
    code_blow, rep_blow = _run_main([
        '--plan', clean_plan, '--out', root / 'audit-blowout',
        '--export', white_mp4, '--visual-qc', '--sheet-frames', 4,
    ])
    assert code_blow == 0 and rep_blow['status'] == 'attention'
    assert rep_blow['visual_qc']['blowout_frames'] and not rep_blow['visual_qc']['pass_qc'], rep_blow['visual_qc']
    checks.append('visual_qc flagged highlight blowout cleanly')

    consola = Path('C:/Windows/Fonts/consola.ttf')
    if consola.is_file():
        tofu_plan = root / 'tofu-plan.json'
        core.write_json(tofu_plan, {
            'schema_version': 1, 'name': 'audit-tofu', 'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': str(src_a), 'start': 0.0, 'source_in': 0.0, 'duration': 1.0, 'volume': 0}],
            'captions': [{'text': 'PLAY ▶', 'start': 0.0, 'duration': 1.0}],
        })
        code_tofu, rep_tofu = _run_main(['--plan', tofu_plan, '--out', root / 'audit-tofu', '--font-check', str(consola)])
        assert code_tofu == 0 and rep_tofu['status'] == 'attention'
        assert rep_tofu['visual_qc']['font_glyphs']['missing'], rep_tofu['visual_qc']
        checks.append('font glyph check caught missing U+25B6 symbol cleanly')

    code_bad, rep_bad = _run_main(['--plan', clean_plan, '--out', out_clean])
    assert code_bad == 1 and rep_bad['status'] == 'failed'
    checks.append('existing out directory refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'audit-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
