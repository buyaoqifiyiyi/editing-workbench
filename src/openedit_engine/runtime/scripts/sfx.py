"""Procedural stereo cinema SFX synthesis & cut-aligned BGM micro-ducking keyframes (MIT DSP)."""
import argparse
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal

import file_hash
import runtime_core as core

SFX_TYPES = (
    'whoosh',
    'riser',
    'sub_drop',
    'impact',
    'katana_draw',
    'laser_sweep',
    'glitch_tick',
    'bass_swell',
)

_DEFAULT_PARAMS = {
    'whoosh': {'duration': 0.45, 'align': 'peak', 'peak_frac': 0.52},
    'riser': {'duration': 1.20, 'align': 'end', 'peak_frac': 0.97},
    'sub_drop': {'duration': 1.35, 'align': 'start', 'peak_frac': 0.02},
    'impact': {'duration': 0.65, 'align': 'peak', 'peak_frac': 0.03},
    'katana_draw': {'duration': 0.55, 'align': 'peak', 'peak_frac': 0.16},
    'laser_sweep': {'duration': 0.55, 'align': 'peak', 'peak_frac': 0.50},
    'glitch_tick': {'duration': 0.14, 'align': 'peak', 'peak_frac': 0.12},
    'bass_swell': {'duration': 0.90, 'align': 'end', 'peak_frac': 0.85},
}


def _db_to_lin(db):
    return float(10.0 ** (float(db) / 20.0))


def _lin_to_db(lin):
    return 20.0 * math.log10(max(float(lin), 1e-12))


def _edge_fade(n, sr, fade_in_s=0.004, fade_out_s=0.008):
    env = np.ones(n, dtype=np.float64)
    fin = min(n // 2, max(1, int(round(fade_in_s * sr)))) if fade_in_s > 0 else 0
    fout = min(n - fin, max(1, int(round(fade_out_s * sr)))) if fade_out_s > 0 else 0
    if fin > 0:
        env[:fin] *= 0.5 * (1.0 - np.cos(np.pi * np.arange(fin) / fin))
    if fout > 0:
        env[-fout:] *= 0.5 * (1.0 + np.cos(np.pi * np.arange(1, fout + 1) / fout))
    return env


def _pink_noise(rng, n):
    white = rng.standard_normal(n)
    b = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786], dtype=np.float64)
    a = np.array([1.0, -2.494956002, 2.017265875, -0.522189400], dtype=np.float64)
    pink = signal.lfilter(b, a, white)
    std = float(np.std(pink)) or 1.0
    return pink / std


def _swept_filter(x, sr, freqs, *, btype='bandpass', q=1.8, order=2):
    """Apply time-varying Butterworth filter continuously via carried state `zi` across 128-sample blocks."""
    n = len(x)
    out = np.empty(n, dtype=np.float64)
    block = 128
    nyq = 0.5 * sr
    sos0 = None
    zi = None
    for i in range(0, n, block):
        end = min(n, i + block)
        fc = float(np.clip(freqs[min(i + (end - i) // 2, n - 1)], 24.0, nyq * 0.88))
        if btype == 'bandpass':
            bw = max(fc / max(q, 0.4), 18.0)
            lo = max(20.0, fc - 0.5 * bw)
            hi = min(nyq * 0.92, fc + 0.5 * bw)
            if hi <= lo * 1.05:
                hi = min(nyq * 0.94, lo * 1.15)
            sos = signal.butter(order, [lo / nyq, hi / nyq], btype='bandpass', output='sos')
        else:
            sos = signal.butter(order, fc / nyq, btype=btype, output='sos')
        if zi is None:
            zi = signal.sosfilt_zi(sos) * x[0]
            sos0 = sos
        elif sos.shape != sos0.shape:
            zi = signal.sosfilt_zi(sos) * out[max(0, i - 1)]
            sos0 = sos
        out[i:end], zi = signal.sosfilt(sos, x[i:end], zi=zi)
    return out


def _synth_mono_cue(cue_type, dur, sr, cue, rng):
    n = max(64, int(round(dur * sr)))
    t = np.arange(n, dtype=np.float64) / sr
    u = t / max(dur, 1e-6)

    if cue_type == 'whoosh':
        peak_u = float(cue.get('peak_frac', 0.52))
        f0 = float(cue.get('freq_start', 240.0))
        f_pk = float(cue.get('freq_peak', 2300.0))
        f1 = float(cue.get('freq_end', 380.0))
        env = np.where(u <= peak_u, np.power(np.clip(u / max(peak_u, 1e-3), 0.0, 1.0), 1.9),
                       np.power(np.clip((1.0 - u) / max(1.0 - peak_u, 1e-3), 0.0, 1.0), 2.3))
        freqs = np.where(u <= peak_u,
                         f0 + (f_pk - f0) * np.sin(0.5 * np.pi * u / max(peak_u, 1e-3)),
                         f1 + (f_pk - f1) * np.cos(0.5 * np.pi * (u - peak_u) / max(1.0 - peak_u, 1e-3)))
        air = _swept_filter(_pink_noise(rng, n) * 0.8 + rng.standard_normal(n) * 0.25, sr, freqs, btype='bandpass', q=1.5)
        body_env = np.exp(-0.5 * ((u - peak_u) / 0.14) ** 2)
        body = np.sin(2.0 * np.pi * (82.0 - 34.0 * u) * t) * body_env * 0.42
        sig = (air * 0.85 * env + body) * _edge_fade(n, sr, 0.006, 0.012)

    elif cue_type == 'riser':
        f0 = float(cue.get('freq_start', 110.0))
        f1 = float(cue.get('freq_end', 880.0))
        freq_curve = f0 * np.power(max(f1 / max(f0, 20.0), 1.01), np.power(u, 1.35))
        phase = 2.0 * np.pi * np.cumsum(freq_curve) / sr
        tone = 0.55 * np.sin(phase) + 0.30 * np.sin(1.498 * phase) + 0.18 * np.tanh(1.6 * np.sin(2.0 * phase))
        noise_freqs = 260.0 * np.power(24.0, np.power(u, 1.25))
        noise = _swept_filter(rng.standard_normal(n), sr, noise_freqs, btype='highpass', order=2)
        trem_freq = 5.0 + 13.0 * u
        trem = 0.78 + 0.22 * np.sin(2.0 * np.pi * np.cumsum(trem_freq) / sr)
        env = np.power(u, 2.15) * trem
        sig = (tone * 0.52 + noise * 0.48) * env * _edge_fade(n, sr, 0.015, 0.004)

    elif cue_type == 'sub_drop':
        f0 = float(cue.get('freq_start', 98.0))
        f1 = float(cue.get('freq_end', 29.0))
        freq = f1 + (f0 - f1) * np.exp(-t / 0.085)
        phase = 2.0 * np.pi * np.cumsum(freq) / sr
        sub = np.tanh(1.35 * (np.sin(phase) + 0.20 * np.sin(2.0 * phase)))
        transient = _swept_filter(rng.standard_normal(n), sr, np.full(n, 320.0), btype='lowpass') * np.exp(-t / 0.018) * 0.65
        env = np.exp(-t / max(dur * 0.42, 0.15)) * (1.0 - np.exp(-t / 0.0025))
        sig = (sub * env * 0.90 + transient) * _edge_fade(n, sr, 0.001, 0.025)

    elif cue_type == 'impact':
        f0 = float(cue.get('freq_start', 155.0))
        f1 = float(cue.get('freq_end', 38.0))
        freq = f1 + (f0 - f1) * np.exp(-t / 0.048)
        phase = 2.0 * np.pi * np.cumsum(freq) / sr
        punch = np.tanh(1.8 * np.sin(phase)) * np.exp(-t / max(dur * 0.28, 0.06))
        crack = rng.standard_normal(n) * np.exp(-t / 0.014)
        crack = _swept_filter(crack, sr, np.linspace(2400.0, 750.0, n), btype='bandpass', q=0.9)
        boom = _swept_filter(_pink_noise(rng, n), sr, np.linspace(380.0, 95.0, n), btype='lowpass') * np.exp(-t / max(dur * 0.36, 0.10))
        sig = (punch * 0.65 + crack * 0.55 + boom * 0.50) * _edge_fade(n, sr, 0.001, 0.020)

    elif cue_type == 'katana_draw':
        f0 = float(cue.get('freq_start', 1750.0))
        f1 = float(cue.get('freq_end', 5400.0))
        scrape = _swept_filter(rng.standard_normal(n), sr, f0 + (f1 - f0) * np.power(u, 0.65), btype='bandpass', q=3.2)
        scrape *= np.exp(-np.maximum(0.0, t - 0.03) / 0.11)
        f_ring = 1920.0 + 480.0 * np.exp(-t / 0.05)
        p1 = 2.0 * np.pi * np.cumsum(f_ring) / sr
        ring = (0.45 * np.sin(p1 + 0.6 * np.sin(2.73 * p1))
                + 0.32 * np.sin(1.618 * p1)
                + 0.22 * np.sin(2.414 * p1)) * np.exp(-t / max(dur * 0.44, 0.12))
        air = _swept_filter(rng.standard_normal(n), sr, np.linspace(900.0, 4200.0, n), btype='bandpass', q=1.4)
        air *= np.exp(-0.5 * ((t - 0.035) / 0.032) ** 2)
        sig = (scrape * 0.42 + ring * 0.55 + air * 0.38) * _edge_fade(n, sr, 0.002, 0.015)

    elif cue_type == 'laser_sweep':
        f0 = float(cue.get('freq_start', 320.0))
        f_pk = float(cue.get('freq_peak', 3600.0))
        f1 = float(cue.get('freq_end', 520.0))
        freq = np.where(u <= 0.48,
                        f0 * np.power(max(f_pk / max(f0, 40.0), 1.05), u / 0.48),
                        f_pk * np.power(max(f1 / max(f_pk, 40.0), 0.1), (u - 0.48) / 0.52))
        phase = 2.0 * np.pi * np.cumsum(freq) / sr
        carrier = np.tanh(2.1 * (np.sin(phase) + 0.45 * np.sin(2.0 * phase + 0.8 * np.sin(0.5 * phase))))
        shimmer = _swept_filter(rng.standard_normal(n), sr, freq * 1.35, btype='bandpass', q=2.8)
        env = np.sin(np.pi * u) ** 0.75
        sig = (carrier * 0.58 + shimmer * 0.42) * env * _edge_fade(n, sr, 0.004, 0.010)

    elif cue_type == 'glitch_tick':
        base_f = float(cue.get('freq_start', 2100.0))
        step_hz = np.where((np.floor(u * 6.0) % 2) == 0, base_f, base_f * 1.49)
        phase = 2.0 * np.pi * np.cumsum(step_hz) / sr
        fm = np.sin(phase + 2.4 * np.sin(phase * 0.43))
        grit = _swept_filter(rng.standard_normal(n), sr, np.linspace(4800.0, 2200.0, n), btype='highpass')
        gate = np.where((np.floor(u * 11.0) % 3) != 2, 1.0, 0.15)
        env = np.exp(-t / max(dur * 0.35, 0.025)) * gate
        sig = (fm * 0.60 + grit * 0.45) * env * _edge_fade(n, sr, 0.001, 0.005)

    elif cue_type == 'bass_swell':
        f0 = float(cue.get('freq_start', 48.0))
        f1 = float(cue.get('freq_end', 68.0))
        freq = f0 + (f1 - f0) * np.power(u, 1.5)
        phase = 2.0 * np.pi * np.cumsum(freq) / sr
        warm = np.tanh(1.3 * (0.72 * np.sin(phase) + 0.34 * np.sin(1.5 * phase) + 0.20 * np.sin(2.0 * phase)))
        air = _swept_filter(_pink_noise(rng, n), sr, 140.0 + 900.0 * np.power(u, 1.9), btype='lowpass')
        env = np.power(u, 1.85) * np.where(u < 0.88, 1.0, np.clip((1.0 - u) / 0.12, 0.0, 1.0) ** 0.7)
        sig = (warm * 0.72 + air * 0.35) * env * _edge_fade(n, sr, 0.015, 0.010)

    else:
        raise ValueError(f'Unsupported SFX cue type: {cue_type!r}')

    peak = float(np.max(np.abs(sig)))
    if peak > 1e-8:
        sig = sig / peak
    return sig


def _resolve_cue_timing(cue, idx):
    ctype = cue.get('type')
    if ctype not in _DEFAULT_PARAMS:
        raise ValueError(f'cues[{idx}].type must be one of {SFX_TYPES}, got {ctype!r}')
    defaults = _DEFAULT_PARAMS[ctype]
    dur = float(cue.get('duration', defaults['duration']))
    if not math.isfinite(dur) or dur <= 0.02 or dur > 60.0:
        raise ValueError(f'cues[{idx}].duration must be within (0.02, 60.0], got {dur}')
    peak_frac = float(cue.get('peak_frac', defaults['peak_frac']))
    if not 0.0 <= peak_frac <= 1.0:
        raise ValueError(f'cues[{idx}].peak_frac must be in [0, 1]')

    if 'start' in cue and cue['start'] is not None:
        start = float(cue['start'])
        align = 'start'
        anchor = start
    else:
        anchor_raw = cue.get('peak_time', cue.get('time', cue.get('at')))
        if anchor_raw is None:
            raise ValueError(f'cues[{idx}] requires start, time, at, or peak_time')
        anchor = float(anchor_raw)
        align = str(cue.get('align', defaults['align']))
        if align == 'start':
            start = anchor
        elif align == 'end':
            start = anchor - dur
        elif align == 'peak':
            start = anchor - peak_frac * dur
        else:
            raise ValueError(f'cues[{idx}].align must be start|peak|end, got {align!r}')
    if not math.isfinite(start):
        raise ValueError(f'cues[{idx}] resolved to non-finite start time')
    return ctype, start, dur, peak_frac, align, anchor


def _build_duck_keyframes(duck_specs, total_dur):
    """Build strictly increasing clip-local gain keyframes starting at t=0.0."""
    ramps = []
    for i, ev in enumerate(duck_specs):
        duck_db = float(ev.get('duck_db', -2.5))
        if not -24.0 <= duck_db <= 0.0:
            raise ValueError(f'duck[{i}].duck_db must be in [-24, 0], got {duck_db}')
        attack = max(0.01, float(ev.get('attack', 0.06)))
        release = max(0.02, float(ev.get('release', 0.16)))
        if 'start' in ev and 'end' in ev:
            s_t, e_t = float(ev['start']), float(ev['end'])
        else:
            anchor = float(ev.get('time', ev.get('at', ev.get('peak_time'))))
            hold = max(0.02, float(ev.get('hold', 0.10)))
            pre = float(ev.get('pre', 0.02))
            s_t, e_t = anchor - pre, anchor + hold
        if e_t <= s_t or e_t <= 0.0 or s_t >= total_dur:
            continue
        ramps.append([max(0.0, s_t), min(total_dur - 0.001, e_t), _db_to_lin(duck_db), attack, release])

    ramps.sort(key=lambda r: r[0])
    # Merge adjacent/overlapping duck intervals so ramps never invert time order.
    merged = []
    for s_t, e_t, gain, atk, rel in ramps:
        if merged and s_t - atk <= merged[-1][1] + merged[-1][4] + 0.015:
            prev = merged[-1]
            prev[1] = max(prev[1], e_t)
            prev[2] = min(prev[2], gain)
            prev[4] = max(prev[4], rel)
        else:
            merged.append([s_t, e_t, gain, atk, rel])

    max_kft = max(0.0005, total_dur - 0.0001)
    pts = [(0.0, 1.0)]
    for s_t, e_t, gain, atk, rel in merged:
        t0 = max(0.0, min(max_kft, s_t - atk))
        t1 = max(0.0, min(max_kft, s_t))
        t2 = max(0.0, min(max_kft, e_t))
        t3 = max(0.0, min(max_kft, e_t + rel))
        if t0 > 0.002:
            pts.append((t0, 1.0))
        pts.append((t1, gain))
        pts.append((t2, gain))
        if t3 > t2 + 0.002:
            pts.append((t3, 1.0))

    keys = []
    last_us = -1
    for t_val, val in sorted(pts, key=lambda p: p[0]):
        t_clamped = min(max(0.0, t_val), max_kft)
        u = core.us(t_clamped)
        if u == last_us:
            keys[-1] = {'time': round(t_clamped, 6), 'value': round(float(val), 6)}
            continue
        keys.append({'time': round(t_clamped, 6), 'value': round(float(val), 6)})
        last_us = u
    if keys and keys[0]['time'] != 0.0:
        keys.insert(0, {'time': 0.0, 'value': 1.0})
    core.validate_parameter_keys(keys, total_dur, 'bgm_ducking', 0, 10)
    return keys


def generate_sfx(args):
    cues_path = Path(args.cues).expanduser().resolve()
    if not cues_path.is_file():
        raise ValueError(f'Cues file does not exist: {cues_path}')
    total_dur = core.number(args.duration, 'duration', 0.1, 3600.0)
    sr = int(args.sample_rate)
    if not 16000 <= sr <= 96000:
        raise ValueError(f'sample_rate must be in [16000, 96000], got {sr}')
    peak_db = core.number(args.master_peak_db, 'master_peak_db', -18.0, -0.1)

    payload = core.read_json(cues_path)
    if isinstance(payload, list):
        cues = payload
        explicit_ducks = []
    elif isinstance(payload, dict):
        cues = payload.get('cues')
        explicit_ducks = list(payload.get('duck_events', []))
    else:
        raise ValueError('--cues JSON must be a cue array or {"cues": [...]}')
    if not isinstance(cues, list) or not cues:
        raise ValueError('cues must be a non-empty array')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    total_samples = max(1, int(round(total_dur * sr)))
    mix = np.zeros((total_samples, 2), dtype=np.float64)
    placed_cues = []
    cue_ducks = []
    rng = np.random.default_rng(int(args.seed))

    for idx, cue in enumerate(cues):
        if not isinstance(cue, dict):
            raise ValueError(f'cues[{idx}] must be a JSON object')
        ctype, start, dur, peak_frac, align, anchor = _resolve_cue_timing(cue, idx)
        gain_lin = float(cue['gain']) if 'gain' in cue else _db_to_lin(cue.get('gain_db', 0.0))
        if not math.isfinite(gain_lin) or gain_lin < 0.0 or gain_lin > 4.0:
            raise ValueError(f'cues[{idx}] gain out of safe range [0, 4.0]: {gain_lin}')

        pan_val = float(cue.get('pan', 0.0))
        pan0 = float(cue.get('pan_start', pan_val))
        pan1 = float(cue.get('pan_end', pan_val))
        if not (-1.0 <= pan0 <= 1.0 and -1.0 <= pan1 <= 1.0):
            raise ValueError(f'cues[{idx}] pan must be in [-1.0, 1.0]')

        mono = _synth_mono_cue(ctype, dur, sr, cue, rng) * gain_lin
        theta = (np.linspace(pan0, pan1, len(mono)) + 1.0) * (0.25 * np.pi)
        stereo = np.column_stack((mono * np.cos(theta), mono * np.sin(theta)))

        s_idx = int(round(start * sr))
        e_idx = s_idx + len(stereo)
        src_s = max(0, -s_idx)
        dst_s = max(0, s_idx)
        dst_e = min(total_samples, e_idx)
        clipped_bounds = s_idx < 0 or e_idx > total_samples
        if dst_e > dst_s:
            chunk = stereo[src_s:src_s + (dst_e - dst_s)].copy()
            if src_s > 0:
                chunk *= _edge_fade(len(chunk), sr, 0.005, 0.0)[:, None]
            if e_idx > total_samples:
                chunk *= _edge_fade(len(chunk), sr, 0.0, 0.010)[:, None]
            mix[dst_s:dst_e] += chunk

        placed_cues.append({
            'index': idx,
            'name': cue.get('name', f'{ctype}_{idx + 1}'),
            'type': ctype,
            'align': align,
            'anchor_time': round(anchor, 4),
            'start': round(start, 4),
            'duration': round(dur, 4),
            'peak_time': round(start + peak_frac * dur, 4),
            'gain_db': round(_lin_to_db(gain_lin), 2) if gain_lin > 0 else -120.0,
            'pan': [round(pan0, 2), round(pan1, 2)],
            'rendered': dst_e > dst_s,
            'trimmed_to_timeline': clipped_bounds and (dst_e > dst_s),
        })
        if 'duck_db' in cue and cue['duck_db'] is not None:
            hit_t = start + peak_frac * dur
            cue_ducks.append({
                'time': hit_t,
                'pre': float(cue.get('duck_pre', 0.025)),
                'hold': float(cue.get('duck_hold', min(0.18, dur * 0.45))),
                'attack': float(cue.get('duck_attack', 0.055)),
                'release': float(cue.get('duck_release', 0.14)),
                'duck_db': float(cue['duck_db']),
            })

    raw_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    target_lin = _db_to_lin(peak_db)
    # Soft-knee limiter above 0.72 so transients stay punchy while peaks fit cleanly under target headroom.
    thresh = min(0.72, target_lin * 0.88)
    limited = mix.copy()
    if raw_peak > thresh and raw_peak > 0.0:
        mag = np.abs(limited)
        over = mag > thresh
        knee_span = max(0.05, 1.0 - thresh)
        limited[over] = np.sign(limited[over]) * (thresh + knee_span * np.tanh((mag[over] - thresh) / knee_span))
    lim_peak = float(np.max(np.abs(limited))) if limited.size else 0.0
    scale_gain = min(1.0, target_lin / lim_peak) if lim_peak > 1e-8 else 1.0
    final = limited * scale_gain

    pcm16 = np.clip(np.round(final * 32767.0), -32767, 32767).astype('<i2')
    out_dir = core.new_directory(out_cand)
    out_wav = out_dir / 'sfx_bed.wav'
    with wave.open(str(out_wav), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm16.tobytes())

    duck_keys = _build_duck_keyframes(explicit_ducks + cue_ducks, total_dur)
    duck_payload = {
        'duration': round(total_dur, 6),
        'keyframe_count': len(duck_keys),
        'keyframes': duck_keys,
    }
    duck_default = out_dir / 'bgm_duck_keyframes.json'
    core.write_json(duck_default, duck_payload)
    extra_duck = None
    if args.duck_out:
        extra_duck = Path(args.duck_out).expanduser().resolve()
        extra_duck.parent.mkdir(parents=True, exist_ok=True)
        core.write_json(extra_duck, duck_payload)

    post_peak = float(np.max(np.abs(pcm16)) / 32768.0) if pcm16.size else 0.0
    post_rms = float(np.sqrt(np.mean((pcm16.astype(np.float64) / 32768.0) ** 2))) if pcm16.size else 0.0
    clipped_cnt = int(np.count_nonzero((pcm16 == 32767) | (pcm16 == -32768)))
    report = {
        'status': 'passed',
        'operation': 'sfx',
        'sample_rate': sr,
        'channels': 2,
        'duration': round(total_dur, 6),
        'frames': total_samples,
        'cue_count': len(placed_cues),
        'cues': placed_cues,
        'audio_stats': {
            'raw_peak_linear': round(raw_peak, 4),
            'limiter_scale': round(scale_gain, 6),
            'sample_peak_dbfs': round(_lin_to_db(post_peak), 2) if post_peak > 0 else -120.0,
            'rms_dbfs': round(_lin_to_db(post_rms), 2) if post_rms > 0 else -120.0,
            'full_scale_clipped_samples': clipped_cnt,
        },
        'output_wav': {
            'path': str(out_wav),
            'bytes': out_wav.stat().st_size,
            'sha256': file_hash.sha256(out_wav),
        },
        'duck_keyframes': {
            'path': str(extra_duck or duck_default),
            'default_path': str(duck_default),
            'count': len(duck_keys),
        },
    }
    core.write_json(out_dir / 'sfx-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--cues', required=True, help='JSON file with cue array or {"cues": [...], "duck_events": [...]}')
    parser.add_argument('--duration', type=float, required=True, help='Output timeline bed duration in seconds')
    parser.add_argument('--out', required=True, help='New output directory for sfx_bed.wav, sfx-report.json, and bgm_duck_keyframes.json')
    parser.add_argument('--sample-rate', type=int, default=48000, help='WAV sample rate (default 48000)')
    parser.add_argument('--master-peak-db', type=float, default=-1.5, help='Ceiling sample peak in dBFS in [-18, -0.1] (default -1.5)')
    parser.add_argument('--duck-out', default=None, help='Optional extra output path for BGM ducking keyframes JSON')
    parser.add_argument('--seed', type=int, default=42, help='Deterministic noise generator seed (default 42)')
    args = parser.parse_args(argv)
    try:
        report = generate_sfx(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
