"""Pure FFmpeg two-pass EBU R128 loudness mastering & PCM audio QC (no extra dependencies)."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

import file_hash
import runtime_core as core

PRESETS = {
    'streaming': {'target_lufs': -14.0, 'true_peak': -1.0, 'lra': 11.0, 'note': 'Douyin / Bilibili / YouTube streaming'},
    'podcast': {'target_lufs': -16.0, 'true_peak': -1.5, 'lra': 9.0, 'note': 'Voiceover / podcast / spoken word'},
    'broadcast': {'target_lufs': -23.0, 'true_peak': -1.0, 'lra': 7.0, 'note': 'EBU R128 / ITU broadcast standard'},
}
REVERB_PRESETS = {
    'room': {'rt60': 0.45, 'lpf_hz': 6500.0, 'predelay_ms': 8.0, 'seed': 101},
    'hall': {'rt60': 1.35, 'lpf_hz': 4800.0, 'predelay_ms': 18.0, 'seed': 202},
    'cathedral': {'rt60': 2.40, 'lpf_hz': 3800.0, 'predelay_ms': 28.0, 'seed': 303},
    'tunnel': {'rt60': 1.65, 'lpf_hz': 2800.0, 'predelay_ms': 22.0, 'seed': 404},
    'plate': {'rt60': 0.95, 'lpf_hz': 8200.0, 'predelay_ms': 4.0, 'seed': 505},
}
_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_JSON_BLOCK_RE = re.compile(r'\{[^{}]*"input_i"[^{}]*\}', re.DOTALL)


def _synthesize_room_ir(preset_name, sample_rate, out_wav):
    """Synthesize deterministic stereo room impulse response WAV for FFmpeg afir."""
    cfg = REVERB_PRESETS[preset_name]
    dur_s = min(3.5, cfg['rt60'] * 1.4)
    n = max(1024, int(round(dur_s * sample_rate)))
    rng = np.random.default_rng(cfg['seed'])
    decay_k = 6.907755 / max(0.05, cfg['rt60'])
    t = np.arange(n, dtype=np.float64) / sample_rate
    env = np.exp(-decay_k * t)
    predelay = int(round(cfg['predelay_ms'] * 0.001 * sample_rate))
    stereo = np.zeros((n, 2), dtype=np.float64)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    roll = 1.0 / np.sqrt(1.0 + (freqs / cfg['lpf_hz']) ** 2)
    for ch in range(2):
        noise = rng.normal(0.0, 1.0, size=n) * env
        damped = np.fft.irfft(np.fft.rfft(noise) * roll, n=n)
        ir = np.zeros(n, dtype=np.float64)
        ir[0] = 1.0
        if predelay < n:
            tail = damped[predelay:]
            ir[predelay:] += 0.25 * tail / (np.max(np.abs(tail)) + 1e-12)
        for k in range(1, 6):
            tap = min(n - 1, predelay + int((0.007 * k + 0.002 * ch) * sample_rate))
            ir[tap] += ((-1.0) ** k) * (0.28 / k)
        stereo[:, ch] = np.clip(ir, -1.0, 1.0)
    pcm = (stereo * 32767.0).astype('<i2')
    with wave.open(str(out_wav), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return out_wav


def _apply_audio_fx(
    source,
    work_dir,
    *,
    sample_rate,
    channels,
    src_duration,
    reverb_ir=None,
    reverb_wet=0.18,
    sidechain=None,
    sc_threshold=0.05,
    sc_ratio=6.0,
    sc_attack=15.0,
    sc_release=220.0,
):
    """Apply optional FFmpeg sidechaincompress ducking and/or afir convolution reverb before loudnorm."""
    ffmpeg = core.tool('ffmpeg')
    cur = Path(source)
    layout = 'mono' if int(channels) == 1 else 'stereo'
    fx_meta = {}

    if sidechain is not None:
        sc_path = Path(os.path.expanduser(os.path.expandvars(str(sidechain)))).resolve()
        if not sc_path.is_file():
            raise ValueError(f'Sidechain trigger file does not exist: {sc_path}')
        sc_info = core.probe(sc_path)
        if not sc_info.get('has_audio'):
            raise ValueError(f'Sidechain trigger file has no audio stream: {sc_path}')
        sc_dur = max(0.2, float(src_duration or sc_info.get('duration') or 1.0))
        sc_out = Path(work_dir) / 'sidechained.wav'
        fc = (
            f'[0:a:0]aformat=sample_rates={sample_rate}:channel_layouts={layout}[main];'
            f'[1:a:0]aformat=sample_rates={sample_rate}:channel_layouts={layout},'
            f'apad,atrim=0:{sc_dur:.6f}[key];'
            f'[main][key]sidechaincompress=threshold={sc_threshold}:ratio={sc_ratio}'
            f':attack={sc_attack}:release={sc_release}[out]'
        )
        _run_ffmpeg_capture([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file,pipe',
            '-i', str(cur), '-i', str(sc_path),
            '-filter_complex', fc, '-map', '[out]',
            '-c:a', 'pcm_f32le', str(sc_out),
        ])
        cur = sc_out
        fx_meta['sidechain'] = {
            'path': str(sc_path),
            'threshold': sc_threshold,
            'ratio': sc_ratio,
            'attack_ms': sc_attack,
            'release_ms': sc_release,
        }

    if reverb_ir is not None:
        raw_ir = str(reverb_ir).strip()
        if raw_ir.lower() in REVERB_PRESETS:
            preset_key = raw_ir.lower()
            ir_wav = _synthesize_room_ir(preset_key, sample_rate, Path(work_dir) / f'ir_{preset_key}.wav')
            ir_label = f'preset:{preset_key}'
        else:
            ir_wav = Path(os.path.expanduser(os.path.expandvars(raw_ir))).resolve()
            if not ir_wav.is_file():
                raise ValueError(f'Reverb IR WAV not found and not a preset {tuple(REVERB_PRESETS)}: {raw_ir}')
            if not core.probe(ir_wav).get('has_audio'):
                raise ValueError(f'Reverb IR file has no audio stream: {ir_wav}')
            ir_label = str(ir_wav)
        dry_gain = round(max(0.0, 1.0 - reverb_wet), 4)
        wet_gain = round(min(1.0, max(0.0, reverb_wet)), 4)
        rev_out = Path(work_dir) / 'reverb.wav'
        fc = (
            f'[0:a:0]aformat=sample_rates={sample_rate}:channel_layouts={layout},asplit=2[dry][toir];'
            f'[1:a:0]aformat=sample_rates={sample_rate}:channel_layouts={layout}[ir];'
            f'[toir][ir]afir=gtype=peak[wet];'
            f"[dry][wet]amix=inputs=2:weights='{dry_gain} {wet_gain}':duration=first:normalize=0[out]"
        )
        _run_ffmpeg_capture([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file,pipe',
            '-i', str(cur), '-i', str(ir_wav),
            '-filter_complex', fc, '-map', '[out]',
            '-c:a', 'pcm_f32le', str(rev_out),
        ])
        cur = rev_out
        fx_meta['reverb'] = {'ir': ir_label, 'wet': reverb_wet, 'dry': dry_gain}

    return cur, fx_meta


def _parse_loudnorm_float(value):
    if isinstance(value, (int, float)) and type(value) is not bool:
        val = float(value)
        return val if math.isfinite(val) else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('-inf', 'inf', '+inf', 'nan', ''):
            return None
        try:
            val = float(text)
            return val if math.isfinite(val) else None
        except ValueError:
            return None
    return None


def _extract_loudnorm_json(stderr_text):
    matches = _JSON_BLOCK_RE.findall(stderr_text or '')
    if not matches:
        raise ValueError(f'FFmpeg loudnorm emitted no metrics JSON; tail={stderr_text[-800:] if stderr_text else ""}')
    return json.loads(matches[-1])


def _run_ffmpeg_capture(args, *, timeout=600):
    proc = subprocess.run(
        [str(x) for x in args],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        creationflags=_NO_WIN,
    )
    if proc.returncode != 0:
        raise ValueError(f'{Path(str(args[0])).name} failed: {proc.stderr[-2000:]}')
    return proc


def measure_pcm_stats(media_path, *, channels=None, sample_rate=None):
    """Decode local media audio stream into PCM16 and measure peak, clipping, RMS, and DC offset."""
    ffmpeg = core.tool('ffmpeg')
    with tempfile.TemporaryDirectory(prefix='oe-pcm-qc-') as tmp:
        wav_path = Path(tmp) / 'probe.wav'
        cmd = [
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file,pipe', '-i', str(media_path), '-map', '0:a:0', '-vn', '-sn', '-dn',
        ]
        if channels is not None:
            cmd.extend(['-ac', str(channels)])
        if sample_rate is not None:
            cmd.extend(['-ar', str(sample_rate)])
        cmd.extend(['-c:a', 'pcm_s16le', '-map_metadata', '-1', str(wav_path)])
        core.run(cmd, timeout=300)
        with wave.open(str(wav_path), 'rb') as handle:
            rate = handle.getframerate()
            ch = handle.getnchannels()
            nframes = handle.getnframes()
            if nframes <= 0:
                raise ValueError(f'{Path(media_path).name} decoded to 0 audio frames')
            raw = np.frombuffer(handle.readframes(nframes), dtype='<i2')
    samples = raw.astype(np.float64) / 32768.0
    abs_samples = np.abs(samples)
    peak_lin = float(abs_samples.max()) if samples.size else 0.0
    rms_lin = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    clipped = int(np.count_nonzero((raw == 32767) | (raw == -32768)))
    dc_offset = float(np.mean(samples)) if samples.size else 0.0
    return {
        'sample_rate': rate,
        'channels': ch,
        'frames': nframes,
        'duration': round(nframes / rate, 4),
        'sample_peak': round(peak_lin, 6),
        'sample_peak_dbfs': round(20.0 * math.log10(peak_lin), 2) if peak_lin > 0 else -120.0,
        'rms_dbfs': round(20.0 * math.log10(rms_lin), 2) if rms_lin > 0 else -120.0,
        'dc_offset': round(dc_offset, 6),
        'full_scale_clipped_samples': clipped,
        'total_channel_samples': int(samples.size),
    }


def _ebur_prefix(*, channels=None, sample_rate=None, pre_filter=None):
    parts = []
    if channels is not None or sample_rate is not None:
        fmt_opts = []
        if sample_rate is not None:
            fmt_opts.append(f'sample_rates={int(sample_rate)}')
        if channels is not None:
            layout = 'mono' if int(channels) == 1 else 'stereo'
            fmt_opts.append(f'channel_layouts={layout}')
        parts.append('aformat=' + ':'.join(fmt_opts))
    if pre_filter:
        parts.append(pre_filter)
    return ','.join(parts)


def _measure_ebur128(media_path, *, i_target, tp_target, lra_target, channels=None, sample_rate=None, pre_filter=None):
    ffmpeg = core.tool('ffmpeg')
    af_loud = f'loudnorm=I={i_target}:TP={tp_target}:LRA={lra_target}:print_format=json'
    prefix = _ebur_prefix(channels=channels, sample_rate=sample_rate, pre_filter=pre_filter)
    af = f'{prefix},{af_loud}' if prefix else af_loud
    cmd = [
        ffmpeg, '-hide_banner', '-nostdin', '-protocol_whitelist', 'file,pipe',
        '-i', str(media_path), '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-af', af, '-f', 'null', '-',
    ]
    proc = _run_ffmpeg_capture(cmd, timeout=600)
    raw_json = _extract_loudnorm_json(proc.stderr)
    return {
        'integrated_lufs': _parse_loudnorm_float(raw_json.get('input_i')),
        'true_peak_dbtp': _parse_loudnorm_float(raw_json.get('input_tp')),
        'lra_lu': _parse_loudnorm_float(raw_json.get('input_lra')),
        'threshold_lufs': _parse_loudnorm_float(raw_json.get('input_thresh')),
        'target_offset': _parse_loudnorm_float(raw_json.get('target_offset')),
        'raw': raw_json,
    }


def run_audio_master(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')

    preset_cfg = PRESETS[args.preset]
    target_i = core.number(args.target_lufs, 'target_lufs', -40.0, -5.0) if args.target_lufs is not None else preset_cfg['target_lufs']
    target_tp = core.number(args.true_peak, 'true_peak', -9.0, 0.0) if args.true_peak is not None else preset_cfg['true_peak']
    target_lra = core.number(args.lra, 'lra', 1.0, 20.0) if args.lra is not None else preset_cfg['lra']
    sample_rate = int(args.sample_rate)

    info = core.probe(source)
    if not info.get('has_audio'):
        raise ValueError(f'audio-master requires a local file with an audio stream: {source}')

    src_ch = int(info.get('channels') or 2)
    channels = (1 if src_ch == 1 else 2) if args.channels == 'auto' else (1 if args.channels == 'mono' else 2)

    reverb_ir = getattr(args, 'reverb_ir', None)
    reverb_wet = core.number(getattr(args, 'reverb_wet', 0.18), 'reverb_wet', 0.0, 1.0)
    sidechain = getattr(args, 'sidechain', None)
    sc_threshold = core.number(getattr(args, 'sidechain_threshold', 0.05), 'sidechain_threshold', 0.001, 1.0)
    sc_ratio = core.number(getattr(args, 'sidechain_ratio', 6.0), 'sidechain_ratio', 1.0, 20.0)
    sc_attack = core.number(getattr(args, 'sidechain_attack', 15.0), 'sidechain_attack', 0.1, 2000.0)
    sc_release = core.number(getattr(args, 'sidechain_release', 220.0), 'sidechain_release', 0.1, 5000.0)
    has_fx = bool(reverb_ir is not None or sidechain is not None)
    if args.measure_only and has_fx:
        raise ValueError('--measure-only cannot be combined with --reverb-ir or --sidechain')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    pcm_before = measure_pcm_stats(source, channels=channels, sample_rate=sample_rate)
    ebur_before = _measure_ebur128(source, i_target=target_i, tp_target=target_tp, lra_target=target_lra,
                                   channels=channels, sample_rate=sample_rate)

    before_i = ebur_before['integrated_lufs']
    ultra_quiet = before_i is None or before_i < -70.0
    if ultra_quiet and not args.measure_only:
        raise ValueError(
            f'Input audio is silent or ultra-quiet ({ebur_before["raw"].get("input_i")} LUFS < -70); '
            'refusing linear loudnorm gain boost (pass --measure-only to inspect)'
        )

    out_wav = out_cand / 'mastered.wav'
    norm_type = None
    ebur_after = None
    pcm_after = None
    fx_meta = {}
    warnings = []

    if pcm_before['full_scale_clipped_samples'] > 0:
        warnings.append(f'Input already contains {pcm_before["full_scale_clipped_samples"]} full-scale clipped samples.')
    if abs(pcm_before['dc_offset']) > 0.02:
        warnings.append(f'Input has noticeable DC offset ({pcm_before["dc_offset"]:+.4f}).')

    if not args.measure_only:
        with tempfile.TemporaryDirectory(prefix='oe-audio-fx-') as fx_tmp:
            work_source, fx_meta = _apply_audio_fx(
                source, fx_tmp,
                sample_rate=sample_rate, channels=channels,
                src_duration=info.get('duration') or pcm_before['duration'],
                reverb_ir=reverb_ir, reverb_wet=reverb_wet,
                sidechain=sidechain, sc_threshold=sc_threshold, sc_ratio=sc_ratio,
                sc_attack=sc_attack, sc_release=sc_release,
            )
            work_ebur = ebur_before if not fx_meta else _measure_ebur128(
                work_source, i_target=target_i, tp_target=target_tp, lra_target=target_lra,
                channels=channels, sample_rate=sample_rate,
            )
            work_i = work_ebur['integrated_lufs']
            pre_filter = None
            pass2_measured = work_ebur
            if work_i is not None and work_i > -0.5:
                pad_db = math.ceil(work_i) + 6
                pre_filter = f'volume=-{pad_db}dB'
                pass2_measured = _measure_ebur128(
                    work_source, i_target=target_i, tp_target=target_tp, lra_target=target_lra,
                    channels=channels, sample_rate=sample_rate, pre_filter=pre_filter,
                )
                warnings.append(f'Hot input ({work_i:+.2f} LUFS) pre-attenuated by -{pad_db} dB before two-pass loudnorm.')

            out = core.new_directory(out_cand)
            out_wav = out / 'mastered.wav'
            raw_b = pass2_measured['raw']
            prefix = _ebur_prefix(channels=channels, sample_rate=sample_rate, pre_filter=pre_filter)
            af_loud = (
                f'loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}'
                f':measured_I={raw_b["input_i"]}:measured_TP={raw_b["input_tp"]}'
                f':measured_LRA={raw_b["input_lra"]}:measured_thresh={raw_b["input_thresh"]}'
                f':offset={raw_b["target_offset"]}:linear=true:print_format=json'
            )
            af_pass2 = f'{prefix},{af_loud}' if prefix else af_loud
            ffmpeg = core.tool('ffmpeg')
            cmd2 = [
                ffmpeg, '-hide_banner', '-nostdin', '-n', '-protocol_whitelist', 'file,pipe',
                '-i', str(work_source), '-map', '0:a:0', '-vn', '-sn', '-dn',
                '-af', af_pass2, '-ar', str(sample_rate), '-ac', str(channels),
                '-c:a', 'pcm_s16le', '-map_metadata', '-1', '-map_chapters', '-1', str(out_wav),
            ]
            proc2 = _run_ffmpeg_capture(cmd2, timeout=600)
            try:
                pass2_json = _extract_loudnorm_json(proc2.stderr)
                norm_type = pass2_json.get('normalization_type')
            except ValueError:
                pass

        if not out_wav.is_file():
            raise RuntimeError(f'FFmpeg created no mastered WAV: {out_wav}')

        pcm_after = measure_pcm_stats(out_wav)
        ebur_after = _measure_ebur128(out_wav, i_target=target_i, tp_target=target_tp, lra_target=target_lra)

        after_i = ebur_after['integrated_lufs']
        after_tp = ebur_after['true_peak_dbtp']
        qc_i_ok = after_i is not None and abs(after_i - target_i) <= 1.5
        qc_tp_ok = after_tp is not None and after_tp <= target_tp + 0.3
        qc_clip_ok = pcm_after['full_scale_clipped_samples'] == 0
        pass_qc = bool(qc_i_ok and qc_tp_ok and qc_clip_ok)
        if not qc_i_ok:
            warnings.append(f'Post-master LUFS ({after_i}) deviated >1.5 LU from target {target_i}.')
        if not qc_tp_ok:
            warnings.append(f'Post-master true peak ({after_tp} dBTP) exceeded limit {target_tp}.')
        if not qc_clip_ok:
            warnings.append(f'Post-master WAV has {pcm_after["full_scale_clipped_samples"]} clipped samples.')
    else:
        out = core.new_directory(out_cand)
        pass_qc = bool(
            not ultra_quiet
            and ebur_before['true_peak_dbtp'] is not None
            and ebur_before['true_peak_dbtp'] <= target_tp + 0.3
            and abs(before_i - target_i) <= 1.5
            and pcm_before['full_scale_clipped_samples'] == 0
        )
        if ultra_quiet:
            warnings.append('Input integrated loudness is below -70 LUFS (near-silent).')

    report = {
        'status': 'passed',
        'operation': 'audio-master',
        'mode': 'measure_only' if args.measure_only else 'two_pass_linear_master',
        'preset': args.preset,
        'fx': fx_meta or None,
        'targets': {
            'integrated_lufs': target_i,
            'true_peak_dbtp': target_tp,
            'lra_lu': target_lra,
            'sample_rate': sample_rate,
            'channels': channels,
        },
        'input': {
            'path': str(source),
            'sha256': file_hash.sha256(source),
            'duration': info.get('duration'),
            'source_sample_rate': info.get('sample_rate'),
            'source_channels': info.get('channels'),
        },
        'before': {
            'integrated_lufs': before_i,
            'true_peak_dbtp': ebur_before['true_peak_dbtp'],
            'lra_lu': ebur_before['lra_lu'],
            'threshold_lufs': ebur_before['threshold_lufs'],
            'target_offset': ebur_before['target_offset'],
            'pcm': pcm_before,
        },
        'after': None if args.measure_only else {
            'integrated_lufs': ebur_after['integrated_lufs'],
            'true_peak_dbtp': ebur_after['true_peak_dbtp'],
            'lra_lu': ebur_after['lra_lu'],
            'threshold_lufs': ebur_after['threshold_lufs'],
            'normalization_type': norm_type,
            'pcm': pcm_after,
        },
        'output_media': None if args.measure_only else {
            'path': str(out_wav),
            'bytes': out_wav.stat().st_size,
            'sha256': file_hash.sha256(out_wav),
        },
        'pass_qc': pass_qc,
        'warnings': warnings,
        'boundaries': [
            'Two-pass FFmpeg EBU R128 loudnorm measures source first and applies linear gain + true-peak limiter to PCM16 WAV.',
            'Very short clips (< 3 s) or sparse speech bursts can measure wider EBU R128 gating error than steady dialogue.',
            'Loudness normalization does not replace perceptual listening QC for EQ, sibilance, or background noise.',
        ],
    }
    core.write_json(out / 'loudness-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local audio or video file with an audio stream')
    parser.add_argument('--out', required=True, help='New output directory for mastered.wav and loudness-report.json')
    parser.add_argument('--preset', choices=tuple(PRESETS), default='streaming', help='Target preset: streaming (-14 LUFS), podcast (-16 LUFS), broadcast (-23 LUFS)')
    parser.add_argument('--target-lufs', type=float, default=None, help='Override target integrated loudness LUFS in [-40, -5]')
    parser.add_argument('--true-peak', type=float, default=None, help='Override maximum true peak dBTP in [-9, 0]')
    parser.add_argument('--lra', type=float, default=None, help='Override loudness range target in [1, 20]')
    parser.add_argument('--sample-rate', type=int, choices=(44100, 48000), default=48000, help='Output WAV sample rate (default 48000)')
    parser.add_argument('--channels', choices=('auto', 'mono', 'stereo'), default='auto', help='Output channel layout (default auto)')
    parser.add_argument('--reverb-ir', default=None, help='Optional room preset (room|hall|cathedral|tunnel|plate) or local impulse WAV path for FFmpeg afir')
    parser.add_argument('--reverb-wet', type=float, default=0.18, help='Reverb wet mix in [0, 1] when --reverb-ir is set (default 0.18)')
    parser.add_argument('--sidechain', default=None, help='Optional voice/SFX key WAV to sidechain-duck this audio via FFmpeg sidechaincompress')
    parser.add_argument('--sidechain-threshold', type=float, default=0.05, help='Sidechain threshold in [0.001, 1.0] (default 0.05)')
    parser.add_argument('--sidechain-ratio', type=float, default=6.0, help='Sidechain compression ratio in [1, 20] (default 6.0)')
    parser.add_argument('--sidechain-attack', type=float, default=15.0, help='Sidechain attack ms in [0.1, 2000] (default 15)')
    parser.add_argument('--sidechain-release', type=float, default=220.0, help='Sidechain release ms in [0.1, 5000] (default 220)')
    parser.add_argument('--measure-only', action='store_true', help='Measure loudness & clipping QC without rendering mastered.wav')
    args = parser.parse_args(argv)
    try:
        report = run_audio_master(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
