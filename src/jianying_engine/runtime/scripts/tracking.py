"""Offline single-ROI CSRT tracking with decoded presentation timestamps; no Jianying/UI control."""
import argparse
import math
from pathlib import Path
import sys

import editor
import file_hash

MAX_FRAMES = 1800


def frame_intervals(container, stream):
    """Yield frames with actual following PTS; only the last frame uses an explicit duration."""
    if stream.sample_aspect_ratio is None or stream.sample_aspect_ratio != 1:
        raise ValueError('Tracking requires explicitly square pixels (sample_aspect_ratio=1)')
    if float(stream.metadata.get('rotate', 0)) != 0:
        raise ValueError('Rotation metadata is not supported for tracking')
    first_pts = previous_time = previous = None
    dimensions = None
    for frame in container.decode(stream):
        if frame.pts is None or frame.time_base is None:
            raise ValueError('Every decoded frame must have a presentation timestamp')
        if frame.rotation != 0:
            raise ValueError('Rotation metadata is not supported for tracking')
        if dimensions is None:
            dimensions = (frame.width, frame.height)
        if dimensions != (frame.width, frame.height):
            raise ValueError('Changing frame dimensions are not supported')
        stamp = frame.pts * frame.time_base
        if first_pts is None:
            first_pts = stamp
        time = stamp - first_pts
        if previous_time is not None and time <= previous_time:
            raise ValueError('Decoded presentation timestamps must strictly increase')
        if previous is not None:
            yield previous, float(previous_time), float(time)
        previous, previous_time = frame, time
    if previous is None:
        raise ValueError('Input has no decoded video frames')
    if previous.duration and previous.duration > 0:
        end = previous_time + previous.duration * previous.time_base
    elif stream.duration is not None and stream.start_time is not None:
        end = (stream.start_time + stream.duration) * stream.time_base - first_pts
    else:
        raise ValueError('Final frame has no duration or usable stream end; cannot infer frame coverage')
    if end <= previous_time:
        raise ValueError('Final frame end is not after its presentation timestamp')
    yield previous, float(previous_time), float(end)


def valid_bbox(box, width, height):
    return (len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                and math.isfinite(v) for v in box)
            and box[0] >= 0 and box[1] >= 0 and box[2] > 0 and box[3] > 0
            and box[0] + box[2] <= width and box[1] + box[3] <= height)


def execute(args):
    source, output = Path(args.input).expanduser().resolve(), Path(args.out).resolve()
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')
    if output.exists():
        raise FileExistsError(f'Output already exists; choose a new directory: {output}')
    start = editor.number(args.start, 'start')
    duration = editor.number(args.duration, 'duration', 0.000001)
    editor.number(start + duration, 'requested end')
    start_us, duration_us = editor.us(start), editor.us(duration)
    end_us = start_us + duration_us
    if len(args.bbox) != 4 or any(type(v) is not int for v in args.bbox):
        raise ValueError('Initial --bbox must contain four integer pixel coordinates x y width height')
    try:
        import av
        import cv2
    except ImportError as exc:
        raise ValueError('Tracking requires av and opencv-contrib-python-headless in this interpreter') from exc
    if not hasattr(cv2, 'TrackerCSRT_create'):
        raise ValueError('This OpenCV build has no CSRT; use opencv-contrib-python-headless')
    initial_stat = source.stat()
    digest = file_hash.sha256(source)
    samples, pictures = [], {}
    coverage_end, lost_at, reached_end = None, None, False
    tracker = cv2.TrackerCSRT_create()
    with av.open(str(source)) as container:
        if len(container.streams.video) != 1:
            raise ValueError('Select a media file containing exactly one video stream')
        stream = container.streams.video[0]
        width, height = stream.codec_context.width, stream.codec_context.height
        if not valid_bbox(args.bbox, width, height):
            raise ValueError('Initial ROI is empty or outside the decoded frame')
        for frame, time, following_time in frame_intervals(container, stream):
            time_us, following_us = editor.us(time), editor.us(following_time)
            if following_us <= time_us:
                raise ValueError('Decoded frame interval is below the supported microsecond resolution')
            time = time_us / 1_000_000
            if time_us >= end_us:
                reached_end = True
                break
            if time_us < start_us:
                continue
            if len(samples) >= MAX_FRAMES:
                raise ValueError(f'Tracking is limited to {MAX_FRAMES} selected frames; choose a shorter range')
            image = frame.to_ndarray(format='bgr24')
            if not samples:
                result = tracker.init(image, tuple(args.bbox))
                if result is False:
                    raise ValueError('CSRT could not initialize the selected ROI')
                box = list(args.bbox)
            else:
                okay, box = tracker.update(image)
                box = [float(v) for v in box]
                if not okay or not valid_bbox(box, width, height):
                    lost_at = time
                    break
            samples.append({'time': time, 'bbox': box})
            coverage_end = min(end_us, following_us) / 1_000_000
            # Retain at most three diagnostic images, never a rendered video.
            if 'first' not in pictures:
                pictures['first'] = (image.copy(), list(box), time)
            if 'middle' not in pictures and 2 * time_us >= 2 * start_us + duration_us:
                pictures['middle'] = (image.copy(), list(box), time)
            pictures['last'] = (image.copy(), list(box), time)
            if following_us >= end_us:
                reached_end = True
                break
    if not samples:
        raise ValueError('Requested range contains no video frame timestamps')
    if lost_at is None and not reached_end:
        raise ValueError('Requested range extends beyond the effective video end')
    current_stat = source.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (initial_stat.st_size, initial_stat.st_mtime_ns):
        raise ValueError('Input changed during tracking; discard this trajectory and retry with a stable file')
    report = {'schema_version': 1, 'status': 'lost' if lost_at is not None else 'tracked',
              'method': 'opencv_csrt',
              'source': {'path': str(source), 'sha256': digest, 'bytes': initial_stat.st_size,
                         'width': width, 'height': height},
              'samples': samples, 'coverage_start': samples[0]['time'], 'coverage_end': coverage_end,
              'lost_at': lost_at,
              'boundaries': ['Single ROI, decoded PTS normalized to first video frame and quantized to editor microseconds; no FPS-based timing.',
                             'Coverage begins at the first frame PTS at or after requested start.',
                             'Coverage end is capped at requested end and bounded by the next real PTS or explicit final frame end.',
                             'CSRT success does not prove target identity; visual review is required.',
                             'This trajectory is not guaranteed suitable for privacy redaction.',
                             'No native Jianying tracking, UI actions, publication, rendered video or model download.']}
    output.mkdir(parents=True, exist_ok=False)
    seen = set()
    for label, (image, box, time) in pictures.items():
        if time in seen:
            continue
        seen.add(time)
        x, y, w, h = [round(v) for v in box]
        cv2.rectangle(image, (x, y), (x + w - 1, y + h - 1), (0, 255, 0), 1)
        cv2.putText(image, f'{time:.6f}s', (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
        okay, encoded = cv2.imencode('.png', image)
        if not okay:
            raise ValueError('Could not encode a diagnostic image')
        (output / (label + '.png')).write_bytes(encoded.tobytes())
    editor.write_json(output / 'tracking.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--bbox', type=int, nargs=4, required=True, metavar=('X', 'Y', 'W', 'H'))
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        report = execute(args)
        editor.emit(report)
        return 2 if report['status'] == 'lost' else 0
    except Exception as exc:
        editor.emit({'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
