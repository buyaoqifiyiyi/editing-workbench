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
    checks.append('clean project audit passed (0 overlaps, strict decode, audio QC, 0ms beat alignment, contact sheet)')

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
