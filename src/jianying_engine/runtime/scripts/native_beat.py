"""Limited native source-clock beats, validated against 11.7 synthetic scenes.

Evidence: 11.7.0.14652 UI research, 320x180/30, four-second silent
video plus one music WAV. Audio ranges (in, start, duration) were (0,0,4)
and (.5,1,2.5), with 1..3 marks in this candidate. Additional parent-agent
UI evidence: 640x360/30 silent ten-second video, twelve-second WAV,
audio (2,1,8), tested with 1, 2 and 16 marks; candidate permits 1..16.
No effects, gain, fades or keys. Marks strictly increase in source seconds
within [source_in, source_out). Scene dimensions/durations cannot be mixed.
No frame snapping: 1.25s is valid despite the half-frame UI tooltip.
Endpoint policy and untested intermediate counts have synthetic checks only;
they do not constitute additional UI evidence. Other scenes remain refused.
Runtime hashes freeze the local research installation, not a portable claim.
"""
from decimal import Decimal
import json
import math
from pathlib import Path
import uuid

import file_hash
import native_protocol

CONTRACT = Path(__file__).resolve().parents[1] / 'assets/native-beat-11.7-candidate.json'
MATERIAL = dict(type='beats', enable_ai_beats=False, gear=404, gear_count=0,
                mode=404, user_delete_ai_beats=None, ai_beats=None)
SCENES = {
    (320, 180, 30): dict(video_seconds=4, audio_seconds=4, max_marks=3,
                        audio_ranges=((0, 0, 4000000), (500000, 1000000, 2500000))),
    (640, 360, 30): dict(video_seconds=10, audio_seconds=12, max_marks=16,
                        audio_ranges=((2000000, 1000000, 8000000),)),
}


def scene(plan):
    canvas = plan.get('canvas', {})
    result = SCENES.get(tuple(canvas.get(k) for k in ('width', 'height', 'fps')))
    if result is None:
        raise ValueError('Native beat candidate canvas was not measured')
    return result


def micros(value):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError('beat time must be a finite JSON number, not bool/string')
    scaled = Decimal(str(value)) * 1000000
    if scaled != scaled.to_integral_value() or not 0 <= scaled <= 12000000:
        raise ValueError('beat time must be exact microseconds within the twelve-second candidate ceiling')
    return int(scaled)


def requested(plan):
    return any('beat_marks' in item for item in plan.get('audio', []))


def runtime(install=None):
    import editor
    spec = json.loads(CONTRACT.read_text(encoding='utf-8'))
    install = Path(install) if install is not None else editor.jianying_install()
    if install is None or install.name != spec['runtime_version']:
        raise ValueError('Native beat candidate requires exactly ' + spec['runtime_version'])
    for relative, digest in spec['runtime_sha256'].items():
        if not file_hash.matches(install / relative, digest):
            raise ValueError('Native beat candidate runtime identity changed: ' + relative)


def validate(plan):
    """Run on raw input BEFORE editor rounds times, and on normalized readback."""
    if not requested(plan):
        return
    runtime()
    spec = scene(plan)
    if (len(plan.get('audio', [])) != 1 or len(plan.get('clips', [])) != 1 or
            any(plan.get(k) for k in ('captions', 'effects', 'filters', 'composites', 'jianying_tasks'))):
        raise ValueError('Native beat candidate requires a measured single-video/music scene')
    audio, video = plan['audio'][0], plan['clips'][0]
    for item in (audio, video):
        if (type(item.get('speed', 1)) not in (int, float) or item.get('speed', 1) != 1 or
                type(item.get('volume', 1 if item is audio else 0)) not in (int, float) or
                item.get('volume', 1 if item is audio else 0) != (1 if item is audio else 0) or
                any(item.get(k) for k in ('fade_in', 'fade_out', 'keyframes', 'denoise', 'loudness',
                    'speed_curve', 'motion_easing', 'transition', 'effects', 'filters', 'animations',
                    'transform', 'mask', 'chroma', 'mix_mode', 'background', 'grading', 'matting_cache'))):
            raise ValueError('Native beat candidate refuses gain/fades/effects/keys/speed changes')
        for key in ('start', 'duration', 'source_in'):
            micros(item.get(key, 0))
    if (video.get('track', 'video') != 'video' or video.get('fit', 'contain') != 'contain' or
            tuple(micros(video.get(k, 0)) for k in ('source_in', 'start', 'duration')) !=
            (0, 0, spec['video_seconds'] * 1000000)):
        raise ValueError('Native beat candidate requires the measured base video range')
    if (audio.get('track') != 'music' or
            tuple(micros(audio.get(k, 0)) for k in ('source_in', 'start', 'duration')) not in
            spec['audio_ranges']):
        raise ValueError('Native beat candidate audio range/track was not measured')
    marks = audio['beat_marks']
    if not isinstance(marks, list) or not 1 <= len(marks) <= spec['max_marks']:
        raise ValueError(f"beat_marks requires 1..{spec['max_marks']} source-second numbers for this scene")
    values = [micros(v) for v in marks]
    low = micros(audio.get('source_in', 0))
    high = low + micros(audio['duration'])
    if any(not low <= v < high for v in values) or any(a >= b for a, b in zip(values, values[1:])):
        raise ValueError('beat_marks must strictly increase within [source_in, source_out)')


def validate_media(plan, probes):
    if requested(plan):
        import editor
        from resolve_workflow import validate_cfr_timestamps
        spec = scene(plan)
        audio, video = (probes[plan[k][0]['path']] for k in ('audio', 'clips'))
        if (audio['kind'] != 'audio' or audio.get('duration') != spec['audio_seconds'] or
                Path(plan['audio'][0]['path']).suffix.lower() != '.wav' or
                (audio.get('sample_rate'), audio.get('channels'), audio.get('audio_codec')) != (44100, 1, 'pcm_s16le') or
                video['kind'] != 'video' or video.get('duration') != spec['video_seconds'] or
                video.get('fps') != 30 or video.get('codec') != 'h264' or
                Path(plan['clips'][0]['path']).suffix.lower() != '.mp4' or
                (video.get('width'), video.get('height')) !=
                (plan['canvas']['width'], plan['canvas']['height']) or video.get('has_audio')):
            raise ValueError('Native beat candidate requires scene-matched 44100Hz mono WAV and silent 30fps video dimensions/durations')
        result = editor.run([editor.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                             '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
                             '-of', 'json', plan['clips'][0]['path']], timeout=30)
        try:
            timestamps = [float(frame['best_effort_timestamp_time'])
                          for frame in json.loads(result.stdout).get('frames', [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Native beat video requires valid per-frame timestamps') from exc
        validate_cfr_timestamps(timestamps, spec['video_seconds'] * 30)


objects = native_protocol.iter_objects


def preview_check(content, root, install):
    """Admit beat materials only with their verified plan and the actual preview installation."""
    observed = any(obj.get('type') == 'beats' or bool(obj.get('beats')) for obj in objects(content))
    native_protocol.verify_plan_preview(
        content,
        root,
        observed=observed,
        plan_declared=requested,
        missing_plan_error='Native beat preview requires its formal edit-plan.json',
        runtime_check=lambda: runtime(install),
        runtime_first=False,
        pass_root=True,
    )


def apply(content, plan):
    if not requested(plan):
        return
    ids = {m['id'] for m in objects(content) if 'id' in m}
    identity = str(uuid.uuid4()).upper()
    while identity in ids:
        identity = str(uuid.uuid4()).upper()
    item = plan['audio'][0]
    segment, = [s for t in content['tracks'] if t['name'] == 'music' and t['type'] == 'audio'
                for s in t['segments']]
    if content['materials'].get('beats'):
        raise ValueError('Native beat candidate will not overwrite existing beats')
    content['materials']['beats'] = [dict(MATERIAL, id=identity, user_beats=[micros(v) for v in item['beat_marks']])]
    segment['extra_material_refs'].append(identity)


def verify(content, plan):
    """Reject undeclared, moved, orphaned, duplicate or cross-bound beats."""
    rows = [(slot, m) for slot, values in content['materials'].items() if isinstance(values, list)
            for m in values if isinstance(m, dict)]
    all_objects = list(objects(content))
    beats = [(slot, m) for slot, m in rows if slot == 'beats' or m.get('type') == 'beats']
    nested_beats = [m for m in all_objects if m.get('type') == 'beats']
    if not requested(plan):
        if beats or nested_beats:
            raise ValueError('Unplanned native beats')
        return 0
    validate(plan)
    spec = scene(plan)
    if len(beats) != 1 or beats[0][0] != 'beats' or len(nested_beats) != 1:
        raise ValueError('Native beats must occupy exactly one beats material')
    material = beats[0][1]
    identity = material.get('id')
    if not isinstance(identity, str) or not identity or sum(m.get('id') == identity for m in all_objects) != 1:
        raise ValueError('Native beat material id must be globally unique')
    expected = dict(MATERIAL, id=identity, user_beats=[micros(v) for v in plan['audio'][0]['beat_marks']])
    # JSON spelling/type equality matters (False != 0, 404 != 404.0).
    if json.dumps(material, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError('Native beat material payload changed')
    if (content.get('fps') != 30 or content.get('duration') != spec['video_seconds'] * 1000000 or
            len(content['tracks']) != 2):
        raise ValueError('Native beat scene changed')
    used = 0
    if any(values for slot, values in content['materials'].items()
           if slot not in ('videos', 'audios', 'speeds', 'beats')):
        raise ValueError('Native beat candidate contains unadmitted extra materials')
    if any(len(content['materials'].get(slot, [])) != count for slot, count in
           (('videos', 1), ('audios', 1), ('speeds', 2))):
        raise ValueError('Native beat candidate material counts changed')
    if sum(m.get('extra_material_refs', []).count(identity) for m in all_objects) != 1:
        raise ValueError('Native beat candidate reference must occur exactly once')
    by_id = {m.get('id'): m for _, m in rows}
    for track in content['tracks']:
        if len(track['segments']) != 1:
            raise ValueError('Native beat scene requires one segment per track')
        segment = track['segments'][0]
        is_audio = track['name'] == 'music' and track['type'] == 'audio'
        item = plan['audio' if is_audio else 'clips'][0]
        if not is_audio and (track['name'], track['type']) != ('video', 'video'):
            raise ValueError('Native beat track changed')
        refs = segment.get('extra_material_refs', [])
        allowed = {'speed', 'beats'} if is_audio else {'speed'}
        if (len(refs) != (2 if is_audio else 1) or len(refs) != len(set(refs)) or
                any(by_id.get(r, {}).get('type') not in allowed for r in refs) or
                refs.count(identity) != int(is_audio) or segment.get('material_id') == identity):
            raise ValueError('Native beat references changed')
        used += int(is_audio)
        if any(type(segment.get(key, {}).get(part)) is not int
               for key in ('source_timerange', 'target_timerange') for part in ('start', 'duration')):
            raise ValueError('Native beat ranges require integer microseconds')
        if (segment.get('source_timerange') != dict(start=micros(item['source_in']), duration=micros(item['duration'])) or
                segment.get('target_timerange') != dict(start=micros(item['start']), duration=micros(item['duration'])) or
                type(segment.get('speed')) not in (int, float) or segment.get('speed') != 1 or
                type(segment.get('volume')) not in (int, float) or segment.get('volume') != item['volume'] or
                segment.get('common_keyframes') or segment.get('keyframe_refs') or
                segment.get('reverse') is not False or segment.get('is_tone_modify') is not False):
            raise ValueError('Native beat alignment/audio configuration changed')
        source = by_id.get(segment['material_id'], {})
        if (source.get('duration') != spec['audio_seconds' if is_audio else 'video_seconds'] * 1000000 or
                source.get('path') != item['path'] or
                (is_audio and source.get('type') != 'extract_music') or
                (is_audio and any(source.get(k) for k in ('effect_id', 'formula_id'))) or
                (not is_audio and (source.get('width'), source.get('height')) !=
                 (plan['canvas']['width'], plan['canvas']['height']))):
            raise ValueError('Native beat source material changed')
        expected_clip = None if is_audio else dict(alpha=1., flip=dict(horizontal=False, vertical=False),
            rotation=0., scale=dict(x=1, y=1), transform=dict(x=0., y=0.))
        if segment.get('clip') != expected_clip:
            raise ValueError('Native beat scene transform changed')
        for ref in refs:
            m = by_id[ref]
            if m.get('type') == 'speed' and (m.get('speed') != 1 or m.get('curve_speed')):
                raise ValueError('Native beat speed material changed')
    if used != 1:
        raise ValueError('Native beat music binding missing')
    return 1
