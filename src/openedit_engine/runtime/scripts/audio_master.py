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
_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_JSON_BLOCK_RE = re.compile(r'\{[^{}]*"input_i"[^{}]*\}', re.DOTALL)


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

    out = core.new_directory(out_cand)
    out_wav = out / 'mastered.wav'
    norm_type = None
    ebur_after = None
    pcm_after = None
    warnings = []

    if pcm_before['full_scale_clipped_samples'] > 0:
        warnings.append(f'Input already contains {pcm_before["full_scale_clipped_samples"]} full-scale clipped samples.')
    if abs(pcm_before['dc_offset']) > 0.02:
        warnings.append(f'Input has noticeable DC offset ({pcm_before["dc_offset"]:+.4f}).')

    if not args.measure_only:
        # Two-pass EBU R128 loudnorm: FFmpeg requires measured_I in [-99, 0], so ultra-hot signals (> -0.5 LUFS)
        # get a fixed linear headroom pad before both measurement and linear Pass 2.
        pre_filter = None
        pass2_measured = ebur_before
        if before_i is not None and before_i > -0.5:
            pad_db = math.ceil(before_i) + 6
            pre_filter = f'volume=-{pad_db}dB'
            pass2_measured = _measure_ebur128(
                source, i_target=target_i, tp_target=target_tp, lra_target=target_lra,
                channels=channels, sample_rate=sample_rate, pre_filter=pre_filter,
            )
            warnings.append(f'Hot input ({before_i:+.2f} LUFS) pre-attenuated by -{pad_db} dB before two-pass loudnorm.')

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
            '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
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
