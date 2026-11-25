"""AE Graph-Editor cubic-bezier / damped-spring camera motion curves and cross-cut velocity compiler."""
import math

CURVE_PRESETS = {
    'expo_out': (0.16, 1.0, 0.30, 1.0),
    'expo_in': (0.70, 0.0, 0.84, 0.0),
    'snap_out': (0.06, 0.96, 0.22, 1.0),
    'snap_in': (0.78, 0.0, 0.94, 0.04),
    'cubic_io': (0.65, 0.0, 0.35, 1.0),
    'overshoot': None,
}

IN_PRESETS = frozenset({
    'none',
    'whip_in_left',
    'whip_in_right',
    'whip_in_up',
    'whip_in_down',
    'zoom_snap_in',
    'zoom_snap_out',
    'zoom_punch',
    'tilt_snap_cw',
    'tilt_snap_ccw',
})

OUT_PRESETS = frozenset({
    'none',
    'whip_out_left',
    'whip_out_right',
    'whip_out_up',
    'whip_out_down',
    'zoom_in_launch',
    'zoom_out_launch',
    'tilt_launch_cw',
    'tilt_launch_ccw',
})

DRIFT_MODES = frozenset({
    'none',
    'drift_push',
    'drift_pull',
    'pan_drift_left',
    'pan_drift_right',
    'beat_breathe',
})

ALL_SINGLE_PRESETS = (IN_PRESETS | OUT_PRESETS | DRIFT_MODES) - {'none'}

_ALLOWED_KEYS = frozenset({
    'preset', 'in_preset', 'out_preset', 'curve', 'intensity',
    'drift', 'drift_mode', 'settle_ratio', 'launch_ratio', 'overshoot', 'samples',
    'auto_overscan',
})

_CONFLICTING_KEYFRAME_PROPS = frozenset({
    'position_x', 'position_y', 'uniform_scale', 'scale_x', 'scale_y', 'rotation',
})


def _bezier_scalar(t, p1, p2):
    u = 1.0 - t
    return 3.0 * u * u * t * p1 + 3.0 * u * t * t * p2 + t * t * t


def _bezier_deriv(t, p1, p2):
    u = 1.0 - t
    return 3.0 * u * u * p1 + 6.0 * u * t * (p2 - p1) + 3.0 * t * t * (1.0 - p2)


def cubic_bezier_y(u, x1, y1, x2, y2):
    """Evaluate normalized 1D progress u in [0, 1] on cubic-bezier(x1, y1, x2, y2)."""
    x = max(0.0, min(1.0, float(u)))
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    t = x
    for _ in range(8):
        err = _bezier_scalar(t, x1, x2) - x
        if abs(err) < 1e-6:
            return _bezier_scalar(t, y1, y2)
        d = _bezier_deriv(t, x1, x2)
        if abs(d) < 1e-6:
            break
        t = max(0.0, min(1.0, t - err / d))
    lo, hi = 0.0, 1.0
    t = x
    for _ in range(20):
        bx = _bezier_scalar(t, x1, x2)
        if abs(bx - x) < 1e-6:
            break
        if bx < x:
            lo = t
        else:
            hi = t
        t = 0.5 * (lo + hi)
    return _bezier_scalar(t, y1, y2)


def eval_curve(u, curve='expo_out', *, overshoot=0.035):
    """Evaluate an AE Graph-Editor curve preset at normalized progress u in [0, 1]."""
    x = max(0.0, min(1.0, float(u)))
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if curve == 'overshoot':
        zeta = 0.58 if overshoot >= 0.06 else 0.72
        omega_n = 11.5
        omega_d = omega_n * math.sqrt(max(1e-6, 1.0 - zeta * zeta))
        raw = lambda tau: 1.0 - math.exp(-zeta * omega_n * tau) * (
            math.cos(omega_d * tau) + (zeta / math.sqrt(1.0 - zeta * zeta)) * math.sin(omega_d * tau)
        )
        end_v = raw(1.0)
        val = raw(x) / (end_v if abs(end_v) > 1e-6 else 1.0)
        peak = max(val - 1.0, 0.0)
        if peak > 0.0 and overshoot >= 0.0:
            scale = float(overshoot) / max(0.035, 0.095 if zeta < 0.65 else 0.038)
            val = 1.0 + (val - 1.0) * scale
        return val
    pts = CURVE_PRESETS.get(curve)
    if pts is None:
        raise ValueError(f'Unknown motion curve preset: {curve!r}')
    return cubic_bezier_y(x, *pts)


def sample_curve_values(t0, t1, v0, v1, *, curve='expo_out', steps=8, overshoot=0.035, round_digits=4):
    """Sample dense (time, value) pairs weighted toward steep velocity regions."""
    n = max(2, int(steps))
    span = float(t1) - float(t0)
    if span <= 1e-6:
        return [(round(float(t0), 6), round(float(v1), round_digits))]
    out = []
    out_bias = curve in ('expo_out', 'snap_out', 'overshoot')
    in_bias = curve in ('expo_in', 'snap_in')
    for idx in range(n + 1):
        lin = idx / n
        if out_bias:
            u = lin ** 1.45
        elif in_bias:
            u = 1.0 - (1.0 - lin) ** 1.45
        else:
            u = 0.5 - 0.5 * math.cos(math.pi * lin)
        val = float(v0) + (float(v1) - float(v0)) * eval_curve(u, curve=curve, overshoot=overshoot)
        out.append((round(float(t0) + u * span, 6), round(val, round_digits)))
    return out


def _check_num(val, label, low, high, default=None):
    if val is None:
        return default
    if type(val) not in (int, float) or not math.isfinite(val):
        raise ValueError(f'{label} must be a finite number')
    f = float(val)
    if f < low or f > high:
        raise ValueError(f'{label} must be within [{low}, {high}]')
    return f


def validate_camera_motion(spec, duration, label='clip', existing_keyframes=None):
    """Validate and normalize a clip/composite camera_motion configuration dict."""
    del duration
    clabel = f'{label}.camera_motion'
    if not isinstance(spec, dict) or not spec:
        raise ValueError(f'{clabel} must be a non-empty object')
    unknown = set(spec) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f'{clabel} unsupported field(s): {", ".join(sorted(unknown))}')

    in_p = spec.get('in_preset', 'none')
    out_p = spec.get('out_preset', 'none')
    drift_m = spec.get('drift_mode')

    if 'preset' in spec and spec['preset'] is not None:
        p = spec['preset']
        if not isinstance(p, str) or p not in ALL_SINGLE_PRESETS:
            raise ValueError(f'{clabel}.preset must be one of {sorted(ALL_SINGLE_PRESETS)}')
        if p in IN_PRESETS:
            if 'in_preset' in spec and in_p not in ('none', p):
                raise ValueError(f'{clabel}.preset conflicts with in_preset={in_p}')
            in_p = p
        elif p in OUT_PRESETS:
            if 'out_preset' in spec and out_p not in ('none', p):
                raise ValueError(f'{clabel}.preset conflicts with out_preset={out_p}')
            out_p = p
        elif p in DRIFT_MODES:
            if drift_m is not None and drift_m not in ('none', p):
                raise ValueError(f'{clabel}.preset conflicts with drift_mode={drift_m}')
            drift_m = p

    if not isinstance(in_p, str) or in_p not in IN_PRESETS:
        raise ValueError(f'{clabel}.in_preset must be one of {sorted(IN_PRESETS)}')
    if not isinstance(out_p, str) or out_p not in OUT_PRESETS:
        raise ValueError(f'{clabel}.out_preset must be one of {sorted(OUT_PRESETS)}')

    drift = _check_num(spec.get('drift'), f'{clabel}.drift', 0.0, 0.25, default=0.04)
    if drift_m is None:
        drift_m = 'drift_push' if drift > 0.0 else 'none'
    if not isinstance(drift_m, str) or drift_m not in DRIFT_MODES:
        raise ValueError(f'{clabel}.drift_mode must be one of {sorted(DRIFT_MODES)}')
    if drift_m == 'none':
        drift = 0.0

    if in_p == 'none' and out_p == 'none' and drift_m == 'none':
        raise ValueError(f'{clabel} must enable at least one of preset, in_preset, out_preset, or drift_mode')

    curve = spec.get('curve', 'expo_out')
    if not isinstance(curve, str) or curve not in CURVE_PRESETS:
        raise ValueError(f'{clabel}.curve must be one of {sorted(CURVE_PRESETS)}')

    settle_ratio = _check_num(spec.get('settle_ratio'), f'{clabel}.settle_ratio', 0.12, 0.65, default=0.34)
    launch_ratio = _check_num(spec.get('launch_ratio'), f'{clabel}.launch_ratio', 0.12, 0.65, default=0.28)
    if 'settle_ratio' in spec and 'launch_ratio' in spec and in_p != 'none' and out_p != 'none':
        if settle_ratio + launch_ratio > 0.96:
            raise ValueError(f'{clabel}: settle_ratio + launch_ratio must not exceed 0.96')

    raw_samples = spec.get('samples', 16)
    if type(raw_samples) is not int or not (6 <= raw_samples <= 40):
        raise ValueError(f'{clabel}.samples must be an integer in [6, 40]')

    intensity = _check_num(spec.get('intensity'), f'{clabel}.intensity', 0.1, 2.5, default=1.0)
    overshoot = _check_num(spec.get('overshoot'), f'{clabel}.overshoot', 0.0, 0.18, default=0.035)
    raw_overscan = spec.get('auto_overscan', True)
    if type(raw_overscan) is not bool:
        raise ValueError(f'{clabel}.auto_overscan must be a boolean')

    if isinstance(existing_keyframes, list):
        conflicts = sorted({kf.get('property') for kf in existing_keyframes if isinstance(kf, dict)} & _CONFLICTING_KEYFRAME_PROPS)
        if conflicts:
            raise ValueError(
                f'{label}: cannot combine camera_motion with explicit motion keyframes ({", ".join(conflicts)}); '
                'only alpha keyframes may coexist with camera_motion'
            )

    return {
        'in_preset': in_p,
        'out_preset': out_p,
        'drift_mode': drift_m,
        'curve': curve,
        'intensity': intensity,
        'drift': drift,
        'settle_ratio': settle_ratio,
        'launch_ratio': launch_ratio,
        'overshoot': overshoot,
        'samples': raw_samples,
        'auto_overscan': raw_overscan,
    }


def _matching_out_curve(in_curve):
    if in_curve == 'snap_out':
        return 'snap_in'
    if in_curve == 'cubic_io':
        return 'cubic_io'
    return 'expo_in'


def compile_camera_motion(spec, duration, *, base_transform=None):
    """Compile declarative camera_motion into exact dense FilmCraft-ready keyframe dictionaries."""
    cfg = validate_camera_motion(spec, duration, label='clip')
    dur = max(0.02, float(duration))
    base_tx = base_transform if isinstance(base_transform, dict) else {}
    base_x = float(base_tx.get('transform_x', 0.0))
    base_y = float(base_tx.get('transform_y', 0.0))
    base_scale = max(float(base_tx.get('scale_x', 1.0)), float(base_tx.get('scale_y', 1.0)))
    base_rot = float(base_tx.get('rotation', 0.0))

    in_p = cfg['in_preset']
    out_p = cfg['out_preset']
    drift_m = cfg['drift_mode']
    in_curve = cfg['curve']
    out_curve = _matching_out_curve(in_curve)
    amp = cfg['intensity']
    drift = cfg['drift']
    ov = cfg['overshoot']

    s_ratio = cfg['settle_ratio']
    l_ratio = cfg['launch_ratio']
    if in_p != 'none' and out_p != 'none' and (s_ratio + l_ratio > 0.90):
        tot = s_ratio + l_ratio
        s_ratio = s_ratio * 0.90 / tot
        l_ratio = l_ratio * 0.90 / tot

    t_settle = dur * s_ratio if in_p != 'none' else 0.0
    t_launch = dur * (1.0 - l_ratio) if out_p != 'none' else dur

    # Build time grid dense around steep entry/exit zones, rounded & deduplicated at microsecond resolution.
    times = {0.0, round(dur, 6)}
    n = cfg['samples']
    if t_settle > 1e-4:
        k_in = max(5, round(n * 0.48)) if out_p != 'none' else max(6, round(n * 0.72))
        for i in range(k_in + 1):
            u = (i / k_in) ** 1.45
            times.add(round(u * t_settle, 6))
    if t_launch < dur - 1e-4:
        k_out = max(5, round(n * 0.45)) if in_p != 'none' else max(6, round(n * 0.68))
        span_out = dur - t_launch
        for i in range(k_out + 1):
            u = 1.0 - (1.0 - i / k_out) ** 1.45
            times.add(round(t_launch + u * span_out, 6))
    # Ensure middle breathing samples so drift stays smooth between settle and launch.
    if t_launch - t_settle > 0.05:
        mid_n = 3 if drift_m != 'none' else 1
        for i in range(1, mid_n + 1):
            times.add(round(t_settle + (t_launch - t_settle) * (i / (mid_n + 1)), 6))

    sorted_times = []
    last_us = -1
    for t in sorted(times):
        t_us = max(0, min(round(dur * 1_000_000), round(t * 1_000_000)))
        if t_us <= last_us:
            continue
        last_us = t_us
        sorted_times.append(t_us / 1_000_000)

    whip_dx = 0.16 * amp
    whip_dy = 0.15 * amp
    rot_amp = 5.5 * amp
    zoom_in_step = 0.18 * amp
    zoom_out_step = 0.16 * amp
    zoom_punch_step = 0.22 * amp

    samples = []
    for t in sorted_times:
        dx, dy, ds, drot = 0.0, 0.0, 0.0, 0.0

        # Entry velocity impulse (settling from 0 -> t_settle)
        if in_p != 'none' and t < t_settle and t_settle > 1e-6:
            e = eval_curve(t / t_settle, in_curve, overshoot=ov)
            rem = 1.0 - e
            if in_p == 'whip_in_right':
                dx -= whip_dx * rem
            elif in_p == 'whip_in_left':
                dx += whip_dx * rem
            elif in_p == 'whip_in_down':
                dy -= whip_dy * rem
            elif in_p == 'whip_in_up':
                dy += whip_dy * rem
            elif in_p == 'zoom_snap_in':
                # Continuous forward zoom momentum across cut (dS/dt > 0).
                ds -= zoom_in_step * rem
            elif in_p == 'zoom_snap_out':
                # Continuous pull-back momentum across cut (dS/dt < 0).
                ds += zoom_out_step * rem
            elif in_p == 'zoom_punch':
                ds += zoom_punch_step * rem
            elif in_p == 'tilt_snap_cw':
                drot -= rot_amp * rem
            elif in_p == 'tilt_snap_ccw':
                drot += rot_amp * rem

        # Exit velocity launch (accelerating from t_launch -> dur)
        if out_p != 'none' and t > t_launch and (dur - t_launch) > 1e-6:
            e_out = eval_curve((t - t_launch) / (dur - t_launch), out_curve, overshoot=0.0)
            if out_p == 'whip_out_right':
                dx += whip_dx * e_out
            elif out_p == 'whip_out_left':
                dx -= whip_dx * e_out
            elif out_p == 'whip_out_down':
                dy += whip_dy * e_out
            elif out_p == 'whip_out_up':
                dy -= whip_dy * e_out
            elif out_p == 'zoom_in_launch':
                ds += zoom_in_step * e_out
            elif out_p == 'zoom_out_launch':
                ds -= zoom_out_step * e_out
            elif out_p == 'tilt_launch_cw':
                drot += rot_amp * e_out
            elif out_p == 'tilt_launch_ccw':
                drot -= rot_amp * e_out

        # Continuous breathing drift so frames never freeze statically between cuts.
        frac = t / dur
        if drift_m == 'drift_push':
            ds += drift * frac
        elif drift_m == 'drift_pull':
            ds += drift * (1.0 - frac)
        elif drift_m == 'pan_drift_left':
            dx -= 0.45 * drift * frac
        elif drift_m == 'pan_drift_right':
            dx += 0.45 * drift * frac
        elif drift_m == 'beat_breathe':
            ds += 0.55 * drift * frac + 0.45 * drift * math.sin(math.pi * frac)

        samples.append((t, base_x + dx, base_y + dy, base_scale + ds, base_rot + drot))

    if cfg['auto_overscan']:
        # Auto-overscan safety floor so pan/whip/rotation never reveal black borders on full-frame layers.
        max_abs_x = max(abs(item[1]) for item in samples)
        max_abs_y = max(abs(item[2]) for item in samples)
        max_abs_r = max(abs(item[4]) for item in samples)
        safe_min_scale = max(
            1.0,
            1.0 + max_abs_x + 0.015,
            1.0 + max_abs_y + 0.015,
            1.0 + math.sin(math.radians(min(45.0, max_abs_r))) * 1.15,
        )
        min_raw_scale = min(item[3] for item in samples)
        scale_lift = max(0.0, safe_min_scale - min_raw_scale)
    else:
        scale_lift = 0.0

    xs = [(t, round(px, 5)) for t, px, _, _, _ in samples]
    ys = [(t, round(py, 5)) for t, _, py, _, _ in samples]
    scs = [(t, round(sc + scale_lift, 5)) for t, _, _, sc, _ in samples]
    rots = [(t, round(rot, 4)) for t, _, _, _, rot in samples]

    out_kfs = []
    if max(v for _, v in xs) - min(v for _, v in xs) > 1e-4 or max(abs(v) for _, v in xs) > 1e-4:
        for t, v in xs:
            out_kfs.append({'property': 'position_x', 'time': t, 'value': v, 'interpolation': 'linear'})
    if max(v for _, v in ys) - min(v for _, v in ys) > 1e-4 or max(abs(v) for _, v in ys) > 1e-4:
        for t, v in ys:
            out_kfs.append({'property': 'position_y', 'time': t, 'value': v, 'interpolation': 'linear'})
    if max(v for _, v in scs) - min(v for _, v in scs) > 1e-4 or abs(scs[0][1] - base_scale) > 1e-4:
        for t, v in scs:
            out_kfs.append({'property': 'uniform_scale', 'time': t, 'value': v, 'interpolation': 'linear'})
    if max(v for _, v in rots) - min(v for _, v in rots) > 1e-4 or max(abs(v) for _, v in rots) > 1e-4:
        for t, v in rots:
            out_kfs.append({'property': 'rotation', 'time': t, 'value': v, 'interpolation': 'linear'})
    return out_kfs
