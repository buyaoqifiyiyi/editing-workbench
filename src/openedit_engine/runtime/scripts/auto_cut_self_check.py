"""Offline auto-cut regression: Silero VAD v5 context window, FFmpeg silencedetect fallback, plan validation, and refusals."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import wave

import numpy as np

import auto_cut
import runtime_core as core


def _write_wav(path, mono, rate=16000):
    pcm = np.clip(mono, -1.0, 1.0)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm * 32767.0).astype('<i2').tobytes())
    return path


def _make_speech_fixture(root, *, seconds=6.0, rate=16000):
    """6s video/audio fixture with two modulated formant bursts at [0.8, 2.0] and [3.8, 5.0] and silence around them."""
    rng = np.random.default_rng(42)
    count = int(seconds * rate)
    t = np.arange(count, dtype=np.float32) / rate
    audio = np.zeros(count, dtype=np.float32)
    for start_s, end_s in ((0.8, 2.0), (3.8, 5.0)):
        i0, i1 = int(start_s * rate), int(end_s * rate)
        seg_t = t[i0:i1] - start_s
        # Syllabic amplitude modulation + multi-harmonic vocal tract signal
        env = 0.55 + 0.45 * np.sin(2 * np.pi * 4.2 * seg_t)
        f0 = 155.0 + 25.0 * np.sin(2 * np.pi * 2.1 * seg_t)
        voice = (
            0.35 * np.sin(2 * np.pi * f0 * seg_t)
            + 0.25 * np.sin(2 * np.pi * 510.0 * seg_t)
            + 0.18 * np.sin(2 * np.pi * 1180.0 * seg_t)
            + 0.06 * rng.normal(0.0, 1.0, i1 - i0)
        )
        audio[i0:i1] = (voice * env).astype(np.float32)

    wav_speech = _write_wav(root / 'speech.wav', audio, rate)
    wav_silent = _write_wav(root / 'silence.wav', np.zeros(int(2.0 * rate), dtype=np.float32), rate)

    vid_speech = root / 'talking.mp4'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=30:d={seconds}',
        '-i', str(wav_speech),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(vid_speech),
    ])

    vid_silent = root / 'silent.mp4'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=30:d=2',
        '-i', str(wav_silent),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(vid_silent),
    ])

    vid_no_audio = root / 'muted.mp4'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=navy:s=320x180:r=30:d=1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-an', str(vid_no_audio),
    ])
    return wav_speech, vid_speech, vid_silent, vid_no_audio


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = auto_cut.main([str(x) for x in arguments])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def check(root, vad_model=None):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    wav_speech, vid_speech, vid_silent, vid_no_audio = _make_speech_fixture(root)

    # 1. Pure-unit postprocess check
    merged = auto_cut.merge_speech_spans([(0.8, 1.4), (1.5, 2.0), (2.2, 2.3), (3.8, 5.0)], 6.0,
                                         min_speech=0.25, min_silence=0.45, pad=0.1)
    assert len(merged) == 2 and abs(merged[0][0] - 0.7) < 1e-6 and abs(merged[0][1] - 2.1) < 1e-6, merged
    checks.append('postprocess: dropped short blip and merged short-gap spans with padding')

    # 2. FFmpeg silencedetect fallback (always available without ONNX)
    ff_out = root / 'ff-out'
    code, ff_report = _run_main([
        '--input', vid_speech, '--out', ff_out, '--detector', 'ffmpeg',
        '--threshold', '-32', '--min-silence', '0.4', '--pad', '0.1', '--fade', '0.02',
    ])
    assert code == 0 and ff_report['status'] == 'passed' and ff_report['detector_used'] == 'ffmpeg', ff_report
    assert ff_report['kept_count'] == 2, ff_report
    assert ff_report['removed_duration'] >= 2.5, ff_report
    assert Path(ff_report['plan_path']).is_file()
    norm, _, _ = core.validate_plan(ff_report['plan_path'])
    assert len(norm['clips']) == 2 and norm['clips'][0]['volume'] == 0.0 and len(norm['audio']) == 2
    assert norm['audio'][0]['fade_in'] > 0 and norm['audio'][0]['fade_out'] > 0
    checks.append(f'ffmpeg fallback: kept {ff_report["kept_count"]} segments ({ff_report["kept_duration"]}s / 6s), plan validated')

    # 3. fade=0 mode keeps linked audio on video clips
    nofade_out = root / 'nofade-out'
    code, nf_report = _run_main(['--input', vid_speech, '--out', nofade_out, '--detector', 'ffmpeg', '--fade', '0'])
    assert code == 0 and nf_report['status'] == 'passed'
    nf_plan = core.read_json(nf_report['plan_path'])
    assert nf_plan['audio'] == [] and all(c['volume'] == 1.0 for c in nf_plan['clips'])
    checks.append('fade=0 mode: audio stays linked on video clips without extra audio[] entries')

    # 4. Audio-only input produces cuts.json with audio_clips and plan_path=None
    aud_out = root / 'audio-out'
    code, aud_report = _run_main(['--input', wav_speech, '--out', aud_out, '--detector', 'ffmpeg'])
    assert code == 0 and aud_report['status'] == 'passed' and aud_report['plan_path'] is None
    assert len(aud_report['audio_clips']) == 2
    checks.append('audio-only input: wrote cuts.json audio_clips without creating invalid video plan')

    # 5. Pure silence must yield 0 kept segments and no empty edit-plan.json
    sil_ff_out = root / 'silent-ff-out'
    code, sil_ff = _run_main(['--input', vid_silent, '--out', sil_ff_out, '--detector', 'ffmpeg'])
    assert code == 0 and sil_ff['kept_count'] == 0 and sil_ff['plan_path'] is None, sil_ff
    checks.append('silence (ffmpeg): yielded 0 kept segments and omitted invalid edit-plan.json')

    # 6. Silero VAD v5 check when model weights + onnxruntime are present
    silero_info = None
    if vad_model is not None:
        sil_out = root / 'silero-out'
        code, sl_report = _run_main([
            '--input', vid_speech, '--out', sil_out, '--detector', 'silero',
            '--vad-model', vad_model, '--threshold', '0.35', '--min-silence', '0.4', '--pad', '0.1',
        ])
        assert code == 0 and sl_report['status'] == 'passed' and sl_report['detector_used'] == 'silero', sl_report
        assert sl_report['kept_count'] >= 1 and sl_report['removed_duration'] >= 1.0, sl_report
        assert Path(sl_report['plan_path']).is_file()
        core.validate_plan(sl_report['plan_path'])
        assert len(sl_report['vad_model']['sha256']) == 64

        sil_quiet = root / 'silero-silent-out'
        code_q, sl_quiet = _run_main([
            '--input', vid_silent, '--out', sil_quiet, '--detector', 'silero', '--vad-model', vad_model,
        ])
        assert code_q == 0 and sl_quiet['kept_count'] == 0 and sl_quiet['plan_path'] is None, sl_quiet
        silero_info = {
            'kept_count': sl_report['kept_count'],
            'kept_duration': sl_report['kept_duration'],
            'removed_duration': sl_report['removed_duration'],
        }
        checks.append(f'silero vad v5: kept {sl_report["kept_count"]} segments ({sl_report["kept_duration"]}s), silence yielded 0')

    # 7. Refusals
    refusals = {
        'existing out': ['--input', vid_speech, '--out', ff_out, '--detector', 'ffmpeg'],
        'missing input': ['--input', root / 'missing.mp4', '--out', root / 'r1', '--detector', 'ffmpeg'],
        'video without audio': ['--input', vid_no_audio, '--out', root / 'r2', '--detector', 'ffmpeg'],
        'bad threshold for silero': ['--input', vid_speech, '--out', root / 'r3', '--detector', 'silero', '--threshold', '1.5'],
        'vad-model with ffmpeg': ['--input', vid_speech, '--out', root / 'r4', '--detector', 'ffmpeg', '--vad-model', wav_speech],
        'missing silero model': ['--input', vid_speech, '--out', root / 'r5', '--detector', 'silero'],
    }
    for label, arguments in refusals.items():
        saved = {}
        if label == 'missing silero model':
            for k in ('OPENEDIT_VAD_MODEL', 'OPENEDIT_MODELS_DIR', 'OPENEDIT_NO_AUTO_MODELS'):
                saved[k] = os.environ.pop(k, None)
            os.environ['OPENEDIT_NO_AUTO_MODELS'] = '1'
        try:
            code, refused = _run_main(arguments)
        finally:
            if label == 'missing silero model':
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        assert code == 1 and refused['status'] == 'failed' and refused.get('error'), (label, refused)
        checks.append(f'refused: {label} -> {refused["error"][:80]}')
    for name in ('r1', 'r2', 'r3', 'r4', 'r5'):
        assert not (root / name).exists(), name

    result = {
        'status': 'passed',
        'checks': checks,
        'checks_count': len(checks),
        'silero_vad_tested': silero_info is not None,
        'silero_measurements': silero_info,
        'ffmpeg_measurements': {
            'kept_count': ff_report['kept_count'],
            'kept_duration': ff_report['kept_duration'],
            'removed_duration': ff_report['removed_duration'],
        },
        'visual_audio_review': 'unverified',
    }
    core.write_json(root / 'auto-cut-check.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--vad-model', type=Path, default=None, help='Optional Silero VAD ONNX model path')
    args = parser.parse_args()
    if hasattr(core.sys.stdout, 'reconfigure'):
        core.sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    vad_model = args.vad_model.resolve() if args.vad_model else None
    if vad_model is not None and not vad_model.is_file():
        core.emit({'status': 'failed', 'error': f'VAD model is not a file: {vad_model}'})
        return 1
    report = check(args.out.resolve(), vad_model=vad_model)
    core.emit(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
