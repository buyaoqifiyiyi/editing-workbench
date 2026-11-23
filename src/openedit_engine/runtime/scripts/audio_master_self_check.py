"""Two-pass EBU R128 loudness mastering & PCM QC regression check."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import audio_master
import runtime_core as core


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = audio_master.main([str(x) for x in arguments])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def _make_fixtures(root):
    ffmpeg = core.tool('ffmpeg')
    quiet = root / 'quiet.wav'
    clipped = root / 'clipped.wav'
    silent = root / 'silent.wav'
    no_audio = root / 'no_audio.png'

    # 3.5s steady -26 LUFS multi-tone speech-like harmonic complex so EBU R128 gating integrates cleanly.
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'aevalsrc=0.05*(sin(2*PI*220*t)+0.5*sin(2*PI*440*t)+0.3*sin(2*PI*880*t)):s=48000:d=3.5',
        '-ac', '2', '-c:a', 'pcm_s16le', str(quiet),
    ])
    # Deliberately hot/clipped fixture (+6 dB overdrive clamped to full scale by pcm_s16le).
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'aevalsrc=2.0*sin(2*PI*330*t):s=48000:d=3.5',
        '-ac', '2', '-c:a', 'pcm_s16le', str(clipped),
    ])
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=2.0',
        '-c:a', 'pcm_s16le', str(silent),
    ])
    core.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=black:s=160x90', '-frames:v', '1', str(no_audio),
    ])
    return quiet, clipped, silent, no_audio


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    quiet, clipped, silent, no_audio = _make_fixtures(root)

    # 1. Measure-only on quiet fixture
    m_out = root / 'measure-quiet'
    code, m_rep = _run_main(['--input', quiet, '--out', m_out, '--preset', 'streaming', '--measure-only'])
    assert code == 0 and m_rep['status'] == 'passed' and m_rep['mode'] == 'measure_only', m_rep
    assert m_rep['output_media'] is None and not (m_out / 'mastered.wav').exists()
    assert m_rep['before']['integrated_lufs'] < -20.0, m_rep['before']
    assert m_rep['pass_qc'] is False
    checks.append(f'measure-only: quiet={m_rep["before"]["integrated_lufs"]} LUFS flagged pass_qc=False without WAV')

    # 2. Two-pass streaming (-14 LUFS) on quiet fixture
    s_out = root / 'master-streaming'
    code, s_rep = _run_main(['--input', quiet, '--out', s_out, '--preset', 'streaming'])
    assert code == 0 and s_rep['status'] == 'passed' and s_rep['pass_qc'] is True, s_rep
    assert Path(s_rep['output_media']['path']).is_file()
    assert abs(s_rep['after']['integrated_lufs'] - (-14.0)) <= 1.2, s_rep['after']
    assert s_rep['after']['true_peak_dbtp'] <= -0.7, s_rep['after']
    assert s_rep['after']['pcm']['full_scale_clipped_samples'] == 0, s_rep['after']
    checks.append(
        f'streaming (-14 LUFS): {m_rep["before"]["integrated_lufs"]} -> '
        f'{s_rep["after"]["integrated_lufs"]} LUFS (TP={s_rep["after"]["true_peak_dbtp"]} dBTP)'
    )

    # 3. Hot/clipped fixture reduced under podcast (-16 LUFS, -1.5 dBTP) with zero post-master clipping
    p_out = root / 'master-podcast'
    code, p_rep = _run_main(['--input', clipped, '--out', p_out, '--preset', 'podcast', '--channels', 'mono', '--sample-rate', '44100'])
    assert code == 0 and p_rep['status'] == 'passed' and p_rep['pass_qc'] is True, p_rep
    assert p_rep['before']['pcm']['full_scale_clipped_samples'] > 0
    assert p_rep['after']['pcm']['full_scale_clipped_samples'] == 0
    assert p_rep['after']['pcm']['channels'] == 1 and p_rep['after']['pcm']['sample_rate'] == 44100
    assert abs(p_rep['after']['integrated_lufs'] - (-16.0)) <= 1.2, p_rep['after']
    assert p_rep['after']['true_peak_dbtp'] <= -1.2, p_rep['after']
    checks.append(
        f'podcast hot-signal attenuate: clipped={p_rep["before"]["pcm"]["full_scale_clipped_samples"]}->0, '
        f'LUFS={p_rep["after"]["integrated_lufs"]}, TP={p_rep["after"]["true_peak_dbtp"]}'
    )

    # 4. Convolution reverb (FFmpeg afir) + sidechain ducking (FFmpeg sidechaincompress)
    key_wav = root / 'voice_key.wav'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', "aevalsrc='0.45*sin(2*PI*300*t)*gt(mod(t,1.0),0.2)*lt(mod(t,1.0),0.7)':s=48000:d=2.2",
        '-ac', '2', '-c:a', 'pcm_s16le', str(key_wav),
    ])
    fx_out = root / 'master-fx'
    code_fx, rep_fx = _run_main([
        '--input', quiet, '--out', fx_out, '--preset', 'streaming',
        '--reverb-ir', 'hall', '--reverb-wet', '0.25',
        '--sidechain', key_wav, '--sidechain-ratio', '8.0',
    ])
    assert code_fx == 0 and rep_fx['status'] == 'passed' and rep_fx['pass_qc'] is True, rep_fx
    assert rep_fx['after']['pcm']['full_scale_clipped_samples'] == 0, rep_fx
    assert rep_fx['fx']['reverb']['ir'] == 'preset:hall' and rep_fx['fx']['sidechain']['ratio'] == 8.0, rep_fx
    checks.append(
        f'afir hall reverb + sidechaincompress: LUFS={rep_fx["after"]["integrated_lufs"]}, '
        f'TP={rep_fx["after"]["true_peak_dbtp"]}, 0 clipped'
    )

    # 5. Silent & invalid refusals
    sq_out = root / 'measure-silent'
    code_sq, rep_sq = _run_main(['--input', silent, '--out', sq_out, '--measure-only'])
    assert code_sq == 0 and rep_sq['pass_qc'] is False and rep_sq['before']['integrated_lufs'] is None, rep_sq
    checks.append('measure-only silent: returned finite JSON with pass_qc=False')

    refusals = {
        'silent master refused': ['--input', silent, '--out', root / 'r1'],
        'existing out': ['--input', quiet, '--out', s_out],
        'no audio stream': ['--input', no_audio, '--out', root / 'r2'],
        'bad target lufs': ['--input', quiet, '--out', root / 'r3', '--target-lufs', '-2'],
        'bad reverb ir': ['--input', quiet, '--out', root / 'r4', '--reverb-ir', 'nonexistent_preset'],
    }
    for label, arguments in refusals.items():
        code_r, rep_r = _run_main(arguments)
        assert code_r == 1 and rep_r['status'] == 'failed' and rep_r.get('error'), (label, rep_r)
        checks.append(f'refused: {label}')
    for rdir in ('r1', 'r2', 'r3', 'r4'):
        assert not (root / rdir).exists(), rdir

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'audio-master-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
