"""Offline automatic ducking: derive a music gain envelope from a separated voice stem.

The native 自动闪避 entry point needs the host runtime token, so the Skill does it offline instead: read the
vocals stem produced by the offline separation route, find the speech intervals, and write volume keyframes
into the plan's music clip(s). The values are linear gain, exactly like a hand-written plan, so the draft
readback of every KFTypeVolume point is the evidence.

Time base: keyframe `time` is clip-local seconds, `value` is linear gain (10 ** (dB / 20)). A ramp starts
`attack` seconds before the speech onset so the ducked level is already reached when the voice starts, and
returns to base `release` seconds after it ends. Intervals closer than attack + release are merged, so ramps
never overlap and the emitted keys stay strictly increasing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np

import editor
import migrate


def read_mono(path):
    """PCM WAV → (float samples in −1..1, sample_rate). Compressed and float WAV are refused, not guessed."""
    path = Path(path)
    with wave.open(str(path), 'rb') as handle:
        channels, width, rate, frames = handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.readframes(handle.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype='<i2').astype(np.float64) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype='<i4').astype(np.float64) / 2147483648.0
    elif width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        data = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) |
                (raw[:, 2].astype(np.int8).astype(np.int32) << 16)).astype(np.float64) / 8388608.0
    else:
        raise ValueError(f'Ducking reads PCM 16/24/32-bit WAV only; {path.name} has {width * 8}-bit samples')
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def frame_db(samples, rate, hop_s=0.02, window_s=0.04):
    """Per-hop RMS in dBFS (floored at −120) with the hop times in seconds."""
    hop, window = max(1, round(hop_s * rate)), max(1, round(window_s * rate))
    starts = range(0, max(1, len(samples) - window + 1), hop)
    values = []
    for start in starts:
        chunk = samples[start:start + window]
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
        values.append(20.0 * np.log10(max(rms, 1e-6)))
    return np.array(values), np.array([start / rate for start in starts])


def speech_intervals(db, times, floor_margin_db=10.0, threshold_floor_db=-55.0, min_speech_s=0.15, merge_gap_s=0.25):
    """Intervals above (noise floor + margin), merged across short gaps and filtered by minimum length."""
    if not len(db):
        return []
    floor_db = float(np.percentile(db, 20))
    threshold_db = max(floor_db + floor_margin_db, threshold_floor_db)
    loud = db > threshold_db
    hop_s = float(times[1] - times[0]) if len(times) > 1 else 0.02
    intervals, start = [], None
    for index, active in enumerate(loud):
        if active and start is None:
            start = times[index]
        elif not active and start is not None:
            intervals.append([float(start), float(times[index] + hop_s)])
            start = None
    if start is not None:
        intervals.append([float(start), float(times[-1] + hop_s)])
    merged = []
    for begin, end in intervals:
        if merged and begin - merged[-1][1] < merge_gap_s:
            merged[-1][1] = end
        else:
            merged.append([begin, end])
    return [item for item in merged if item[1] - item[0] >= min_speech_s], threshold_db


def intervals_from_json(path):
    """Speech intervals from a segment list: [{start, end}] or {"segments": [...]} in seconds."""
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    segments = payload.get('segments', payload) if isinstance(payload, dict) else payload
    if not isinstance(segments, list):
        raise ValueError('--speech-json must hold a list of {"start","end"} segments or such a list under "segments"')
    intervals = []
    for item in segments:
        try:
            begin, end = float(item['start']), float(item['end'])
        except (TypeError, KeyError, ValueError):
            raise ValueError('--speech-json segments need numeric "start" and "end" seconds')
        if not math.isfinite(begin) or not math.isfinite(end):
            raise ValueError('--speech-json segment times must be finite')
        if end <= begin:
            raise ValueError('--speech-json segment ends before it starts')
        intervals.append([begin, end])
    return sorted(intervals)


def merge_ramps(intervals, gap_s):
    merged = []
    for begin, end in sorted(intervals):
        if merged and begin - merged[-1][1] < gap_s:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return merged


def ducking_keys(intervals, item, duck_db=-12.0, attack=0.15, release=0.35, min_spacing=0.02):
    """Clip-local volume keyframes for one audio item: base outside speech, ducked inside, ramps on both edges.

    `intervals` are media times of the voice stem; keyframe time is clip-local, so each edge maps through
    (media − source_in) / speed exactly like the plan format's own source-time rule.
    """
    duration, volume = item['duration'], item.get('volume', 1)
    source_in, speed = item.get('source_in', 0), item.get('speed', 1)
    switch_step = 0.000002 / min(1.0, speed)
    ducked = volume * (10.0 ** (duck_db / 20.0))
    points = [(0.0, volume)]

    def add(time, value):
        if time <= 0:
            points[0] = (0.0, value)
            return
        if time >= duration:
            return
        points.append((float(time), float(value)))

    local = [[(begin - source_in) / speed, (end - source_in) / speed] for begin, end in intervals]
    for begin, end in merge_ramps(local, attack + release):
        if end <= 0 or begin >= duration:
            continue
        # Native keys interpolate linearly; retain distinct keys in both local and source microseconds.
        add(max(begin - max(attack, switch_step), 0.0), volume)
        add(min(max(begin, 0.0), duration), ducked)
        add(min(max(end, 0.0), duration), ducked)
        release_at = min(end + max(release, switch_step), duration - (switch_step if release == 0 else min_spacing))
        if release_at > end:
            add(release_at, volume)

    keys, last_us = [], -1
    for time, value in sorted(points, key=lambda point: point[0]):
        moment = editor.us(time)
        if moment == last_us:
            keys[-1] = {'time': round(time, 6), 'value': round(value, 6)}
            continue
        keys.append({'time': round(time, 6), 'value': round(value, 6)})
        last_us = moment
    if len(keys) == 1 and keys[0]['value'] == round(volume, 6):
        return []
    editor.validate_parameter_keys(keys, duration, 'ducking', 0, 10, source_in, speed)
    return keys


def duck_plan(plan, intervals, track='music', duck_db=-12.0, attack=0.15, release=0.35):
    """Write the envelope into every audio item on `track`; other tracks and clips stay untouched."""
    report = []
    for item in plan.get('audio', []):
        if item.get('track', 'voice') != track:
            continue
        keys = ducking_keys(intervals, item, duck_db=duck_db, attack=attack, release=release)
        if keys:
            item['keyframes'] = keys
        report.append({'path': item['path'], 'start': item['start'], 'duration': item['duration'],
                       'source_in': item.get('source_in', 0), 'speed': item.get('speed', 1),
                       'volume': item.get('volume', 1), 'key_count': len(keys)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline automatic ducking from a separated voice stem.')
    parser.add_argument('plan', help='plan file to read; it is never modified')
    parser.add_argument('--vocals', help='separated vocals stem (PCM WAV) to detect speech in')
    parser.add_argument('--speech-json', help='speech intervals from an ASR/segment list instead of a stem')
    parser.add_argument('--out', required=True, help='plan file to write with the envelope')
    parser.add_argument('--track', default='music', help='audio track to duck (default music)')
    parser.add_argument('--duck-db', type=float, default=-12.0, help='gain offset while speech plays (default -12)')
    parser.add_argument('--attack', type=float, default=0.15, help='ramp-down seconds before a speech onset')
    parser.add_argument('--release', type=float, default=0.35, help='ramp-up seconds after a speech offset')
    parser.add_argument('--floor-margin-db', type=float, default=10.0)
    parser.add_argument('--threshold-floor-db', type=float, default=-55.0)
    parser.add_argument('--min-speech', type=float, default=0.15, help='drop speech bursts shorter than this')
    parser.add_argument('--merge-gap', type=float, default=0.25, help='merge speech separated by less than this')
    args = parser.parse_args(argv)

    if (args.vocals is None) == (args.speech_json is None):
        raise ValueError('Give exactly one of --vocals or --speech-json')
    for name in ('duck_db', 'attack', 'release', 'floor_margin_db', 'threshold_floor_db',
                 'min_speech', 'merge_gap'):
        if not math.isfinite(getattr(args, name)):
            raise ValueError('--' + name.replace('_', '-') + ' must be finite')
    if args.duck_db >= 0:
        raise ValueError('--duck-db must be negative; ducking lowers the music, it never boosts it')
    if args.attack < 0 or args.release < 0 or args.merge_gap < 0 or args.min_speech < 0:
        raise ValueError('Ramp, gap and minimum-speech seconds cannot be negative')

    plan_path, out_path = Path(args.plan).resolve(), Path(args.out).resolve()
    if out_path == plan_path:
        raise ValueError('--out must differ from the input plan; the input plan is never overwritten')
    if out_path.exists():
        raise ValueError(f'Output file already exists; choose a new plan version: {out_path}')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if out_path.parent != plan_path.parent:
        plan, _, missing = migrate.remap_plan(plan_path, [])
        if missing:
            raise ValueError('Cannot relocate plan with missing media: ' +
                             '; '.join(item['item'] + ': ' + item['expected'] for item in missing))
    threshold_db, measured = None, None
    if args.speech_json is not None:
        intervals = intervals_from_json(args.speech_json)
        source = {'kind': 'segments', 'path': str(Path(args.speech_json).resolve())}
    else:
        samples, rate = read_mono(args.vocals)
        db, times = frame_db(samples, rate)
        intervals, threshold_db = speech_intervals(db, times, args.floor_margin_db, args.threshold_floor_db,
                                                  args.min_speech, args.merge_gap)
        source = {'kind': 'stem', 'path': str(Path(args.vocals).resolve()), 'sample_rate': rate,
                  'seconds': round(len(samples) / rate, 6), 'noise_floor_db': round(float(np.percentile(db, 20)), 3),
                  'threshold_db': round(threshold_db, 3)}
    clips = duck_plan(plan, intervals, args.track, args.duck_db, args.attack, args.release)
    if not clips:
        raise ValueError(f'No audio item on track {args.track!r}; nothing to duck')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(plan, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps({'status': 'ducking_written', 'plan': str(out_path), 'track': args.track,
                      'duck_db': args.duck_db, 'attack': args.attack, 'release': args.release,
                      'intervals': [[round(a, 6), round(b, 6)] for a, b in intervals],
                      'speech_seconds': round(sum(b - a for a, b in intervals), 6),
                      'clips': clips, 'source': source}, ensure_ascii=False))


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):  # stdout is UTF-8 JSON for every caller, not the console code page
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
