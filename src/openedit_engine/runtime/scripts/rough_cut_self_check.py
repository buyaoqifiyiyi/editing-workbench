"""One-command rough-cut regression check: auto-cut + optional Whisper/karaoke caption stitching & refusals."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import rough_cut
import runtime_core as core
import runtime_paths


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rough_cut.main([str(x) for x in arguments])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def _make_speech_video(root):
    ffmpeg = core.tool('ffmpeg')
    wav = root / 'speech.wav'
    vid = root / 'talk.mp4'
    silent = root / 'silent.mp4'
    # Two clear active segments [0.5..1.8] and [3.0..4.5] separated by silence in 5s video.
    expr = (
        "'0.35*(sin(2*PI*220*t)+0.4*sin(2*PI*440*t))*"
        "(between(t,0.5,1.8)+between(t,3.0,4.5))'"
    )
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', f'aevalsrc={expr}:s=16000:d=5.0',
        '-c:a', 'pcm_s16le', str(wav),
    ])
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=24:d=5.0',
        '-i', str(wav),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(vid),
    ])
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=navy:s=320x180:r=24:d=2.0',
        '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono:d=2.0',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(silent),
    ])
    return wav, vid, silent


def check(root, whisper_model=None, vad_model=None):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    wav, vid, silent = _make_speech_video(root)

    # 1. --skip-captions end-to-end
    out_skip = root / 'skip-captions'
    code, rep_skip = _run_main([
        '--input', vid, '--out', out_skip, '--detector', 'ffmpeg',
        '--threshold', '-30', '--skip-captions',
    ])
    assert code == 0 and rep_skip['status'] == 'passed' and rep_skip['skip_captions'] is True, rep_skip
    assert rep_skip['kept_clips'] == 2 and Path(rep_skip['plan_path']).is_file()
    core.validate_plan(rep_skip['plan_path'])
    checks.append(f'skip-captions: kept {rep_skip["kept_clips"]} clips ({rep_skip["kept_duration"]}s) & validated plan')

    # 2. Boundary-straddling word cue stitching (mocked deterministic ASR output -> segment & karaoke modes)
    fake_tr = {
        'audio': {'seconds': 5.0},
        'params': {'word_timestamps': True},
        'segments': [
            {
                'id': 0,
                'start': 0.52,
                'end': 3.55,
                'text': '开源剪辑全流程',
                'words': [
                    {'word': '开源', 'start': 0.55, 'end': 0.95, 'probability': 0.99},
                    {'word': '剪辑', 'start': 1.00, 'end': 1.45, 'probability': 0.98},
                    {'word': '全', 'start': 3.10, 'end': 3.30, 'probability': 0.97},
                    {'word': '流程', 'start': 3.32, 'end': 3.55, 'probability': 0.96},
                ],
            }
        ],
    }
    plan_for_stitch = core.read_json(Path(rep_skip['plan_path']))
    cues_plain, total_u = rough_cut._build_rough_cut_captions(
        plan_for_stitch, fake_tr, track='captions', karaoke_mode=None,
        karaoke_color='#FFD54F', max_chars=12, caption_size=48.0, caption_y=-0.7,
    )
    assert total_u == 4 and len(cues_plain) == 2 and ''.join(c['text'] for c in cues_plain) == '开源剪辑全流程', cues_plain
    cues_karaoke, _ = rough_cut._build_rough_cut_captions(
        plan_for_stitch, fake_tr, track='captions', karaoke_mode='active',
        karaoke_color='#FFD54F', max_chars=12, caption_size=48.0, caption_y=-0.7,
    )
    assert len(cues_karaoke) >= 4 and sum('highlights' in c for c in cues_karaoke) == 4, cues_karaoke
    plan_k = dict(plan_for_stitch, captions=cues_karaoke, layer_order=[*plan_for_stitch['layer_order'], 'captions'])
    k_path = root / 'karaoke-stitched.json'
    core.write_json(k_path, plan_k)
    core.validate_plan(k_path)
    checks.append(f'clip-clamped word stitching: plain={len(cues_plain)} cues, karaoke={len(cues_karaoke)} states validated')

    # 3. Full auto-cut -> Whisper transcription -> plan when local whisper model exists
    w_model = whisper_model or runtime_paths.discover_model('whisper')
    if w_model is not None and Path(w_model).is_dir():
        full_out = root / 'full-whisper'
        vad_args = ['--vad-model', vad_model] if vad_model and Path(vad_model).is_file() else []
        with patch('auto_cut.run_auto_cut', wraps=rough_cut.auto_cut.run_auto_cut):
            code_f, rep_f = _run_main([
                '--input', vid, '--out', full_out, '--whisper-model', w_model,
                '--language', 'auto', '--karaoke-mode', 'active', *vad_args,
            ])
        assert code_f == 0 and rep_f['status'] == 'passed' and Path(rep_f['plan_path']).is_file(), rep_f
        core.validate_plan(rep_f['plan_path'])
        checks.append(f'full rough-cut with Whisper: kept={rep_f["kept_clips"]}, report written & plan validated')

    # 4. Pure silence & invalid input refusals
    sil_out = root / 'silent-out'
    code_s, rep_s = _run_main(['--input', silent, '--out', sil_out, '--detector', 'ffmpeg'])
    assert code_s == 0 and rep_s['plan_path'] is None and rep_s['captions_count'] == 0, rep_s

    for label, arguments in (
        ('audio-only rejected', ['--input', wav, '--out', root / 'r1', '--skip-captions']),
        ('existing out', ['--input', vid, '--out', out_skip, '--skip-captions']),
    ):
        code_r, rep_r = _run_main(arguments)
        assert code_r == 1 and rep_r['status'] == 'failed' and rep_r.get('error'), (label, rep_r)
        checks.append(f'refused: {label}')
    assert not (root / 'r1').exists()

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'rough-cut-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--whisper-model', type=Path, default=None)
    parser.add_argument('--vad-model', type=Path, default=None)
    args = parser.parse_args()
    core.emit(check(
        args.out.resolve(),
        whisper_model=args.whisper_model.resolve() if args.whisper_model else None,
        vad_model=args.vad_model.resolve() if args.vad_model else None,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
