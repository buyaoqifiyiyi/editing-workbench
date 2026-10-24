"""Import transcript segments or plain-text SRT into a new plan; never transcribes or publishes."""
import argparse
import json
import re
from pathlib import Path
import sys
import tempfile

import editor
import migrate
import transcribe
import karaoke


def read_srt(path):
    """Strict UTF-8 SubRip subset: numbered cues, comma milliseconds, plain text."""
    text = Path(path).read_text(encoding='utf-8-sig').strip('\n')
    if not text:
        raise ValueError('SRT contains no cues')
    stamp = r'([0-9]{2,}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})'
    segments, previous_index = [], 0
    for block in re.split(r'\n[ \t]*\n', text):
        lines = block.split('\n')
        if len(lines) < 3 or not re.fullmatch(r'[0-9]+', lines[0]):
            raise ValueError('SRT requires numbered cues with timing and nonempty text')
        cue_index = int(lines[0])
        if cue_index <= previous_index:
            raise ValueError('SRT cue numbers must be positive and increasing')
        previous_index = cue_index
        timing = re.fullmatch(stamp + r' --> ' + stamp, lines[1])
        if not timing:
            raise ValueError(f'SRT cue {cue_index}: expected HH:MM:SS,mmm --> HH:MM:SS,mmm')
        values = [int(value) for value in timing.groups()]
        def seconds(parts):
            h, m, s, ms = parts
            return ((h * 60 + m) * 60 + s) + ms / 1000
        content = '\n'.join(lines[2:])
        if any(re.fullmatch(stamp + r' --> ' + stamp, line) for line in lines[2:]):
            raise ValueError('SRT cues require a blank line separator')
        if re.search(r'<[^>]+>|\{\\[^}]*\}', content):
            raise ValueError('SRT styling/position markup is unsupported; supply plain text')
        segments.append({'start': seconds(values[:4]), 'end': seconds(values[4:]), 'text': content})
    return segments


def map_segments(segments, clip, track, timebase, carry_group=False):
    """Map complete source cues into one selected clip, without writing output."""
    if 'speed_curve' in clip:
        raise ValueError('speed_curve is nonlinear and cannot use this caption mapping')
    start = editor.number(clip.get('start'), 'selected start')
    duration = editor.number(clip.get('duration'), 'selected duration', 0.000001)
    source_in = editor.number(clip.get('source_in', 0), 'selected source_in')
    speed = editor.number(clip.get('speed', 1), 'selected speed', 0.000001)
    selected_range = {'start': start, 'duration': duration}
    editor.snap_range(selected_range, 'selected clip')
    start_us = editor.us(selected_range['start'])
    source_in_us = editor.us(source_in)
    source_duration = editor.number(selected_range['duration'] * speed, 'selected source duration')
    source_end_us = source_in_us + editor.us(source_duration)
    if timebase == 'timeline':
        source_in_us = start_us
        source_end_us = start_us + editor.us(selected_range['duration'])
        speed = 1
    imported = []
    last_end = None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError('Every transcript segment must be an object')
        a = editor.us(editor.number(segment.get('start'), f'segment {index} start'))
        b = editor.us(editor.number(segment.get('end'), f'segment {index} end'))
        text = segment.get('text')
        if b <= a:
            raise ValueError('Transcript segment end must be after start')
        if last_end is not None and a < last_end:
            raise ValueError('Transcript segments overlap or are not ordered')
        last_end = b
        if not isinstance(text, str) or not text.strip() or '\x00' in text:
            raise ValueError('Transcript text must be nonempty original text without NUL')
        if b <= source_in_us or a >= source_end_us:
            continue
        if a < source_in_us or b > source_end_us:
            raise ValueError(f'Segment {index} is partially cut by the selected source range; choose whole segment boundaries')
        target_start = start_us + round((a - source_in_us) / speed)
        target_end = start_us + round((b - source_in_us) / speed)
        cue = {'text': text, 'start': target_start / 1_000_000,
               'duration': (target_end - target_start) / 1_000_000, 'track': track}
        if carry_group:
            cue['_karaoke_group'] = segment.get('segment_index', 0)
        editor.snap_range(cue, f'imported caption {index}')
        imported.append(cue)
    return imported, (start_us, start_us + editor.us(selected_range['duration']))


def execute(args):
    source_plan = Path(args.plan).resolve()
    output = Path(args.out).resolve()
    if output.exists() or output == source_plan:
        raise FileExistsError(f'Output already exists; choose a new plan: {output}')
    indices = [args.index] if type(args.index) is int else args.index
    if (args.group not in ('clips', 'audio') or not isinstance(indices, list) or not indices or
            any(type(index) is not int or index < 0 for index in indices)):
        raise ValueError('Choose --group clips|audio and one or more nonnegative --index values')
    if len(set(indices)) != len(indices):
        raise ValueError('Duplicate selected clip indices are not allowed')
    if not isinstance(args.track, str) or not args.track.strip():
        raise ValueError('--track must be nonempty')
    plan, _, missing = migrate.remap_plan(source_plan, [])
    if missing:
        raise ValueError('Plan has missing media or nested local files; migrate them first')
    if plan.get('composites'):
        raise ValueError('Caption import into plans with composites is not supported; use an independent plan')
    items = plan.get(args.group, [])
    if any(index >= len(items) for index in indices):
        raise ValueError('Selected clip index is out of range')
    clips = [items[index] for index in indices]
    clip = clips[0]
    srt_path = getattr(args, 'srt', None)
    timebase = getattr(args, 'timebase', None)
    level = getattr(args, 'level', 'segment')
    karaoke_mode = getattr(args, 'karaoke_mode', None)
    if karaoke_mode not in (None, 'active', 'cumulative'):
        raise ValueError('Choose --karaoke-mode active|cumulative')
    if not karaoke_mode and any(getattr(args, 'karaoke_' + key, None) is not None for key in
                               ('color', 'max_chars', 'max_words', 'joiner', 'max_gap', 'size')):
        raise ValueError('Karaoke options require --karaoke-mode')
    if level not in ('segment', 'word'):
        raise ValueError('--level must be segment|word')
    if bool(srt_path) == bool(getattr(args, 'transcript', None)):
        raise ValueError('Choose exactly one of --transcript or --srt')
    if srt_path and timebase not in ('source', 'timeline'):
        raise ValueError('--srt requires explicit --timebase source|timeline')
    if not srt_path and timebase is not None:
        raise ValueError('--timebase is only valid with --srt')
    if srt_path and level != 'segment':
        raise ValueError('Word level requires an original word-timestamp transcript and report, not SRT')
    if karaoke_mode and not srt_path and level != 'word':
        raise ValueError('Transcript karaoke requires --level word and original word-timestamp output')
    if karaoke_mode and not srt_path and getattr(args, 'karaoke_joiner', None) is not None:
        raise ValueError('Model word text already contains its separators; --karaoke-joiner is for timed SRT units only')
    if timebase != 'timeline' and any(Path(item['path']).resolve() != Path(clip['path']).resolve() for item in clips):
        raise ValueError('Source-time batch requires the same media for every selected clip')
    if srt_path:
        segments = read_srt(srt_path)
        identity = 'caller-supplied SRT; association with selected media is not automatically verified'
    else:
        transcript = Path(args.transcript).resolve()
        report_path = transcript.with_name('transcript-report.json')
        if not report_path.is_file():
            raise ValueError('Missing transcript-report.json; regenerate with transcribe.py using an absolute --input path')
        data, report = editor.read_json(transcript), editor.read_json(report_path)
        if not isinstance(data, dict) or not isinstance(data.get('segments'), list) or not isinstance(report, dict):
            raise ValueError('Expected a transcript segments array and its source report')
        recorded = report.get('input')
        if not isinstance(recorded, str) or not Path(recorded).is_absolute():
            raise ValueError('Source report input must be absolute; regenerate with transcribe.py using an absolute --input path')
        media = Path(clip['path'])
        if Path(recorded).resolve() != media.resolve():
            raise ValueError('Transcript source does not match the selected media')
        if report.get('status') != 'passed' or report.get('segments') != data['segments']:
            raise ValueError('Transcript segments do not match the passed source report')
        if not isinstance(report.get('audio'), dict) or report['audio'].get('bytes') != media.stat().st_size:
            raise ValueError('Transcript source size changed or is missing; regenerate transcription')
        segments = data['segments']
        identity = 'report path and byte size only; source content hash not checked in segment mode'
        if level == 'word':
            params = report.get('params', {})
            if not isinstance(params, dict) or params.get('word_timestamps') is not True or params.get('level') != 'word':
                raise ValueError('Word level requires transcribe --word-timestamps output and report')
            digest = report['audio'].get('sha256')
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest) or transcribe.sha256(media) != digest.upper():
                raise ValueError('Word transcript source SHA256 is missing or changed')
            segments = transcribe.word_cues(segments, report['audio'].get('seconds'))
            identity = 'report path, byte size and source SHA256; model word estimates, not forced alignment'
    imported, mappings, ranges = [], [], []
    for selected_index, selected_clip in zip(indices, clips):
        cues, target_range = map_segments(segments, selected_clip, args.track, timebase or 'source', carry_group=bool(karaoke_mode))
        if karaoke_mode and cues:
            # Map complete words first, then group only the selected timeline words.
            units = [{'text': c['text'], 'start': c['start'],
                      'end': (editor.us(c['start']) + editor.us(c['duration'])) / 1e6,
                      'group': c['_karaoke_group']} for c in cues]
            def option(key, default):
                value = getattr(args, 'karaoke_' + key, None)
                return default if value is None else value
            states = karaoke.compile_cues(units, mode=karaoke_mode, color=option('color', '#FFD54F'),
                        max_chars=option('max_chars', 12), max_words=option('max_words', 6),
                        joiner=option('joiner', ' ' if srt_path else ''), max_gap=option('max_gap', 1.0))
            cues = []
            for state in states:
                cue = {key: value for key, value in state.items() if key != 'end'}
                cue.update(duration=state['end'] - state['start'], track=args.track)
                if getattr(args, 'karaoke_size', None) is not None:
                    cue['size'] = editor.number(args.karaoke_size, 'karaoke size', 1, 100)
                editor.snap_range(cue, 'karaoke state')
                cues.append(cue)
        imported.extend(cues)
        ranges.append(target_range)
        mappings.append({'index': selected_index, 'imported_captions': len(cues)})
    ranges.sort()
    if any(current[0] < previous[1] for previous, current in zip(ranges, ranges[1:])):
        raise ValueError('Selected clips overlap on the timeline; select disjoint ranges')
    imported.sort(key=lambda cue: editor.us(cue['start']))
    if not imported:
        raise ValueError('No complete transcript segment intersects the selected source range')
    existing = plan.setdefault('captions', [])
    if not isinstance(existing, list):
        raise ValueError('captions must be an array')
    existing.extend(imported)
    if 'layer_order' in plan:
        if not isinstance(plan['layer_order'], list):
            raise ValueError('layer_order must be an array')
        if args.track not in plan['layer_order']:
            plan['layer_order'].append(args.track)
    # Reuse the complete editor contract before creating the user-visible output.
    # All media paths were made absolute by remap_plan, so the temporary parent is irrelevant.
    with tempfile.TemporaryDirectory(prefix='jianying-captions-') as temporary:
        candidate = Path(temporary) / 'edit-plan.json'
        editor.write_json(candidate, plan)
        editor.validate_plan(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    return {'status': 'passed', 'output_plan': str(output), 'imported_captions': len(imported),
            'selected_group': args.group, 'selected_index': indices[0] if len(indices) == 1 else None,
            'selected_indices': indices, 'mappings': mappings, 'track': args.track,
            'source_modified': False, 'text_modified': False, 'plan_validation': 'passed',
            'input_format': 'srt' if srt_path else 'transcript',
            'level': level,
            'timebase': timebase if srt_path else 'source',
            'source_identity': identity,
            'karaoke': ({'mode': karaoke_mode, 'rendering': 'editable full-group text repeated per timed state',
                         'timing': 'caller-supplied SRT units' if srt_path else 'model word estimates; review required',
                         'edit_group_text': 'regenerate from timed source to change every state consistently',
                         'text_policy': 'original timing-unit text is preserved; SRT groups insert the explicit/default joiner',
                         'continuous_color_sweep': False} if karaoke_mode else None),
            'timing': ('model-word-level linear mapping; partial words refused; not forced alignment'
                       if level == 'word' else 'segment-level linear mapping; partial segments refused'),
            'ui_compatibility': 'unverified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--transcript')
    source.add_argument('--srt', help='UTF-8 plain-text SRT, optionally with BOM; no style markup')
    parser.add_argument('--timebase', choices=('source', 'timeline'),
                        help='Required for SRT: original media timestamps or final plan timestamps; only complete cues within selected clips are imported')
    parser.add_argument('--group', choices=('clips', 'audio'), default='clips')
    parser.add_argument('--index', type=int, nargs='+', required=True,
                        help='One or more explicit clip indices in the selected group; no duplicate or overlapping selections')
    parser.add_argument('--out', required=True)
    parser.add_argument('--track', default='captions')
    parser.add_argument('--karaoke-mode', choices=('active', 'cumulative'),
                        help='Keep each display group visible while highlighting timed words; SRT cues must be word/syllable units')
    parser.add_argument('--karaoke-color', help='Active #RRGGBB color; default #FFD54F')
    parser.add_argument('--karaoke-size', type=float, help='Editable text size using the normal caption size contract (default 8)')
    parser.add_argument('--karaoke-max-chars', type=int, help='Maximum display group characters (default 12); never guesses character timing')
    parser.add_argument('--karaoke-max-words', type=int, help='Maximum timing units per display group (default 6)')
    parser.add_argument('--karaoke-max-gap', type=float, help='Maximum pause inside a display group in timeline seconds (default 1)')
    parser.add_argument('--karaoke-joiner', help='Separator for SRT units (default one space; use empty string for Chinese); model text preserves original separators')
    parser.add_argument('--level', choices=('segment', 'word'), default='segment',
                        help='word requires original word-timestamp transcript; without karaoke mode it makes one editable cue per word')
    args = parser.parse_args(argv)
    try:
        editor.emit(execute(args))
        return 0
    except (ValueError, OSError, TypeError, KeyError, ImportError) as exc:
        editor.emit({'status': 'failed', 'error': str(exc), 'source_modified': False})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
