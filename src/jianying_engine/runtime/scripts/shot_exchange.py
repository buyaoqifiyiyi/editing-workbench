"""Bounded Blender shot handoff, verified return, and plain OTIO timeline exchange."""
import argparse
import copy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import editor
import file_hash
from reverse_media import media_details

FPS = (24, 25, 30, 50, 60)
PLAIN = {'path', 'start', 'duration', 'source_in', 'speed', 'volume', 'track', 'reason', 'fit'}


def frame_times(path, start, end):
    # Decode beyond the requested end before filtering: B-frame presentation can lag packet DTS.
    data = media_details(path, '-select_streams', 'v:0', '-read_intervals',
                         f'{max(0, start - 1):.9f}%{end + 1:.9f}', '-show_frames',
                         '-show_entries', 'frame=best_effort_timestamp_time')
    times = [editor.number(float(f['best_effort_timestamp_time']), 'frame timestamp')
             for f in data.get('frames', [])]
    return [t for t in times if start - .00001 <= t < end - .00001]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def new_output(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ValueError('Output must be new, with an existing parent; never overwrite an exchange or plan')
    return path.resolve()


def frame_number(seconds, fps):
    value = Fraction(editor.us(seconds) * fps, 1_000_000)
    frame = round(value)
    if abs(value - frame) > Fraction(fps, 1_000_000):
        raise ValueError('Source and timeline boundaries must align to whole frames')
    return frame


def video_contract(path, fps, canvas, returned=False):
    data = media_details(path, '-show_streams', '-show_format')
    videos = [x for x in data.get('streams', []) if x.get('codec_type') == 'video']
    if len(videos) != 1 or videos[0].get('disposition', {}).get('attached_pic'):
        raise ValueError('Exactly one real video stream is required')
    v = videos[0]
    formats = ('yuv420p', 'yuvj420p', 'yuv422p10le') if returned else ('yuv420p', 'yuvj420p')
    if (Path(path).suffix.lower() not in ('.mp4', '.mov') or v.get('pix_fmt') not in formats or
            v.get('width') != canvas['width'] or v.get('height') != canvas['height'] or
            Fraction(v.get('avg_frame_rate', '0/1')) != fps or
            Fraction(v.get('r_frame_rate', '0/1')) != fps):
        raise ValueError('Exchange requires MP4/MOV, matching dimensions and constant integer FPS; unsupported pixel format')
    if (v.get('sample_aspect_ratio') not in (None, '1:1', 'N/A') or
            v.get('color_transfer') not in (None, 'unknown', 'bt709') or
            v.get('field_order') not in (None, 'unknown', 'progressive') or
            abs(float(v.get('start_time', 0))) > .000001 or
            float(v.get('tags', {}).get('rotate', 0)) != 0 or
            any(float(x.get('rotation', 0)) != 0 for x in v.get('side_data_list', []))):
        raise ValueError('Only zero-based, square-pixel, progressive SDR Rec709/unspecified video is admitted')
    if returned and any(x.get('codec_type') == 'audio' for x in data.get('streams', [])):
        raise ValueError('Return must be silent: independent plan audio is preserved, never doubled')
    return v


def selection(plan_path, index, head, tail):
    plan, _, warnings = editor.validate_plan(plan_path)
    if type(index) is not int or not 0 <= index < len(plan['clips']):
        raise ValueError('Clip index is outside the plan')
    if any(type(x) is not int or not 0 <= x <= 120 for x in (head, tail)):
        raise ValueError('Head/tail handles must be integers from 0 to 120 frames')
    c = plan['clips'][index]
    if (any(c.get(k) for k in set(c) - PLAIN) or c['track'] != 'video' or c['speed'] != 1 or c['volume'] != 0 or
            any(plan.get(k) for k in ('composites', 'jianying_tasks', 'missing_assets'))):
        raise ValueError('Select a plain muted primary video at speed=1, without effects/keys/composites/tasks/missing assets')
    fps = plan['canvas']['fps']
    if fps not in FPS or plan['canvas']['width'] * plan['canvas']['height'] > 1920 * 1080:
        raise ValueError('First exchange subset: 24/25/30/50/60 fps, at most 1920x1080 pixels')
    v = video_contract(c['path'], fps, plan['canvas'])
    start, count = frame_number(c['source_in'], fps), frame_number(c['duration'], fps)
    timeline = frame_number(c['start'], fps)
    first, total = start - head, head + count + tail
    if first < 0 or count < 2 or total > 20 * fps:
        raise ValueError('Handles must exist; select at least two frames and at most 20 seconds including handles')
    times = frame_times(c['path'], first / fps, (first + total) / fps)
    if len(times) != total or any(abs(t - (first + i) / fps) > .00002 for i, t in enumerate(times)):
        raise ValueError('Source has missing/irregular timestamps or insufficient handle coverage')
    shot = {'index': index, 'source': c['path'], 'source_sha256': file_hash.sha256(c['path']),
            'canvas': plan['canvas'], 'fps': fps, 'source_first_frame': first,
            'timeline_start_frame': timeline, 'content_frames': count,
            'head_frames': head, 'tail_frames': tail, 'total_frames': total,
            'color_tags': {k: v.get(k) for k in ('color_space', 'color_transfer', 'color_primaries', 'color_range')}}
    return plan, shot, warnings


def blender_script(shot, destination, scale):
    config = dict(shot, directory=str(destination), end_scale=scale,
                  shot_id=digest({'shot': shot, 'end_scale': scale}))
    # The generated script runs only in a new background process, never in the user's open project.
    return '''import bpy, json, pathlib, sys
if not bpy.app.background:
    raise RuntimeError("Run in a separate Blender --background --factory-startup process")
C = json.loads(CONFIG)
root = pathlib.Path(C['directory'])
project, receipt, movie = root/'shot.blend', root/'blender-created.json', root/'processed.mp4'
if any(p.exists() for p in (project, receipt, movie)):
    raise RuntimeError("Refusing to overwrite existing project, receipt or render")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.name = 'Shot_' + C['shot_id'][:12]
scene.render.resolution_x = C['canvas']['width']
scene.render.resolution_y = C['canvas']['height']
scene.render.resolution_percentage = 100
scene.render.fps, scene.render.fps_base = C['fps'], 1.0
scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
scene.frame_start, scene.frame_end = 1, C['total_frames']
scene.render.use_sequencer = True
scene.view_settings.view_transform = 'Standard'
scene.view_settings.exposure, scene.view_settings.gamma = 0, 1
sequencer = scene.sequence_editor_create()
strip = sequencer.strips.new_movie('Editable source', C['source'], channel=1,
    frame_start=1-C['source_first_frame'])
strip.frame_final_start = 1
strip.frame_final_end = C['total_frames']+1
if strip.frame_final_duration != C['total_frames']:
    raise RuntimeError('Blender source trim differs from handoff frame count')
if C['end_scale'] != 1:
    for prop in ('scale_x', 'scale_y'):
        setattr(strip.transform, prop, 1.0)
        strip.transform.keyframe_insert(data_path=prop, frame=1+C['head_frames'])
        setattr(strip.transform, prop, C['end_scale'])
        strip.transform.keyframe_insert(data_path=prop, frame=C['head_frames']+C['content_frames'])
scene.render.image_settings.media_type = 'VIDEO'
scene.render.ffmpeg.format, scene.render.ffmpeg.codec = 'MPEG4', 'H264'
scene.render.ffmpeg.audio_codec = 'NONE'
scene.render.ffmpeg.constant_rate_factor = 'HIGH'
scene.render.filepath = str(movie)
scene['shot_exchange_id'] = C['shot_id']
scene['source_sha256'] = C['source_sha256']
scene['head_frames'], scene['content_frames'] = C['head_frames'], C['content_frames']
bpy.ops.wm.save_as_mainfile(filepath=str(project), check_existing=False, compress=False)
if '--render' in sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else False:
    bpy.ops.render.render(animation=True)
receipt.write_text(json.dumps({'shot_id':C['shot_id'], 'blender_version':bpy.app.version_string,
    'project':str(project), 'frame_start':scene.frame_start, 'frame_end':scene.frame_end,
    'source_first_frame':C['source_first_frame'], 'strip_start':strip.frame_final_start,
    'strip_end_exclusive':strip.frame_final_end, 'rendered':movie.is_file()}), encoding='utf-8')
'''.replace('CONFIG', repr(json.dumps(config, ensure_ascii=True)))


def prepare(args):
    out = new_output(args.out)
    source = Path(args.plan).resolve()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    plan, shot, warnings = selection(source, args.index, args.head_frames, args.tail_frames)
    scale = editor.number(args.end_scale, 'end-scale', 1, 2)
    script = blender_script(shot, out, scale)
    manifest = {'schema_version': 1, 'operation': 'blender_shot_exchange',
                'shot_id': digest({'shot': shot, 'end_scale': scale}), 'end_scale': scale,
                'source_plan': str(source), 'source_plan_sha256': before, 'shot': shot,
                'snapshot_digest': digest(plan), 'script_sha256': hashlib.sha256(script.encode()).hexdigest()}
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise ValueError('Input plan changed during preparation')
    with tempfile.TemporaryDirectory(prefix='.shot-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir()
        editor.write_json(stage / 'handoff.json', manifest)
        editor.write_json(stage / 'source-plan.json', plan)
        (stage / 'create_blender.py').write_text(script, encoding='utf-8', newline='\n')
        stage.rename(out)
    return {'status': 'handoff_prepared', 'handoff': str(out / 'handoff.json'), 'shot': shot,
            'native_project': 'not_created', 'warnings': warnings}


def load_handoff(path):
    path = Path(path).resolve()
    if path.is_dir():
        path /= 'handoff.json'
    h = editor.read_json(path)
    if h.get('schema_version') != 1 or h.get('operation') != 'blender_shot_exchange':
        raise ValueError('Unsupported handoff schema or operation')
    source = Path(h['source_plan'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != h['source_plan_sha256']:
        raise ValueError('Original plan changed; prepare a new shot version')
    s = h['shot']
    plan, shot, warnings = selection(source, s['index'], s['head_frames'], s['tail_frames'])
    scale = editor.number(h['end_scale'], 'end-scale', 1, 2)
    if (shot != s or digest({'shot': shot, 'end_scale': scale}) != h['shot_id'] or digest(plan) != h['snapshot_digest'] or
            digest(editor.read_json(path.parent / 'source-plan.json')) != h['snapshot_digest']):
        raise ValueError('Source media, selection or snapshot changed; prepare a new shot version')
    return path.parent, h, plan, warnings


def verify_return(path, shot):
    path = Path(path).resolve()
    if path == Path(shot['source']).resolve():
        raise ValueError('A returned render must be a separate file, never the original media')
    v = video_contract(path, shot['fps'], shot['canvas'], returned=True)
    duration = shot['total_frames'] / shot['fps']
    if abs(float(v.get('duration', 0)) - duration) > .00002:
        raise ValueError('Returned duration differs from content plus handles')
    times = frame_times(path, 0, duration + 1 / shot['fps'])
    if len(times) != shot['total_frames'] or any(abs(t - i / shot['fps']) > .00002 for i, t in enumerate(times)):
        raise ValueError('Returned frame count/timestamps differ; no silent stretching or truncation')
    for key, value in shot['color_tags'].items():
        if value not in (None, 'unknown', 'unspecified') and v.get(key) != value:
            raise ValueError(f'Returned {key} differs from source; explicit color conversion needs separate validation')
    editor.run([editor.tool('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', '-protocol_whitelist', 'file,pipe',
                '-i', path, '-map', '0:v:0', '-f', 'null', '-'])
    return {'path': str(path), 'sha256': file_hash.sha256(path), 'frames': len(times)}


def build(args):
    root, h, _, _ = load_handoff(args.handoff)
    executable = Path(args.blender).resolve()
    if executable.name.lower() not in ('blender', 'blender.exe') or not executable.is_file():
        raise ValueError('Pass the installed Blender executable explicitly')
    script = root / 'create_blender.py'
    trusted_script = blender_script(h['shot'], root, h['end_scale']).encode('utf-8')
    if (script.read_bytes() != trusted_script or
            hashlib.sha256(trusted_script).hexdigest() != h['script_sha256']):
        raise ValueError('Generated script changed; do not execute an unverified handoff script')
    if any((root / name).exists() or (root / name).is_symlink()
           for name in ('shot.blend', 'blender-created.json', 'processed.mp4')):
        raise ValueError('Blender output exists; prepare a new shot version')
    done = editor.run([executable, '--background', '--factory-startup', '--python-exit-code', '1',
                       '--python', script, '--', *(['--render'] if args.render else [])], timeout=600)
    receipt = editor.read_json(root / 'blender-created.json')
    if (receipt.get('shot_id') != h['shot_id'] or receipt.get('frame_start') != 1 or
            receipt.get('frame_end') != h['shot']['total_frames'] or
            not (root / 'shot.blend').is_file()):
        raise ValueError('Blender did not produce this shot project and completion receipt')
    load_handoff(root)
    returned = verify_return(root / 'processed.mp4', h['shot']) if args.render else None
    return {'status': 'render_verified' if args.render else 'native_project_created',
            'project': str(root / 'shot.blend'), 'receipt': receipt, 'render': returned,
            'blender_log_tail': done.stdout[-1200:], 'ui_save_reopen': 'unverified',
            'color_appearance': 'unverified', 'final_export': 'user_owned'}


def apply(args):
    out = new_output(args.out)
    root, h, plan, warnings = load_handoff(args.handoff)
    project = Path(args.project).resolve() if args.project else root / 'shot.blend'
    with project.open('rb') as handle:
        if handle.read(7) != b'BLENDER':
            raise ValueError('Keep an uncompressed native .blend project alongside the returned render')
    returned = verify_return(args.render_file, h['shot'])
    output = copy.deepcopy(plan)
    clip = output['clips'][h['shot']['index']]
    clip['path'] = returned['path']
    clip['source_in'] = h['shot']['head_frames'] / h['shot']['fps']
    output['notes'].append('外部Blender镜头回填；原生图层留在.blend，剪映使用渲染素材；最终视听与色彩外观待验。')
    with tempfile.TemporaryDirectory(prefix='.shot-return-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'result'
        stage.mkdir()
        target = stage / 'edit-plan.json'
        editor.write_json(target, output)
        normalized, _, extra = editor.validate_plan(target)
        editor.write_json(target, normalized)
        load_handoff(root)
        if file_hash.sha256(returned['path']) != returned['sha256']:
            raise ValueError('Returned media changed during application')
        report = {'status': 'return_plan_created', 'shot_id': h['shot_id'], 'render': returned,
                  'project': str(project), 'project_sha256': file_hash.sha256(project),
                  'source_plan_unchanged': True, 'source_media_unchanged': True,
                  'project_validation': 'attachment_header_only', 'producer': 'caller_supplied',
                  'ui_save_reopen': 'unverified', 'color_appearance': 'unverified',
                  'warnings': warnings + extra}
        editor.write_json(stage / 'return.json', report)
        stage.rename(out)
    return dict(report, plan=str(out / 'edit-plan.json'))


def timeline(args):
    try:
        import opentimelineio as otio
    except ImportError as exc:
        raise ValueError('Install the optional engine [exchange] extra for OpenTimelineIO export') from exc
    out = new_output(args.out)
    plan, probes, warnings = editor.validate_plan(args.plan)
    if (any(plan.get(k) for k in ('audio', 'captions', 'effects', 'filters', 'composites', 'jianying_tasks', 'missing_assets')) or
            any(any(c.get(k) for k in set(c) - PLAIN) or c['track'] != 'video' or c['speed'] != 1 or c['volume'] != 0 for c in plan['clips'])):
        raise ValueError('Initial OTIO export supports plain muted primary cuts only; audio/text/effects are never silently dropped')
    fps = plan['canvas']['fps']
    result = otio.schema.Timeline(name=plan['name'], global_start_time=otio.opentime.RationalTime(0, fps))
    track = otio.schema.Track(name='video', kind=otio.schema.TrackKind.Video)
    cursor = 0
    for c in sorted(plan['clips'], key=lambda x: x['start']):
        info = probes[c['path']]
        if info['kind'] != 'video' or info['has_audio'] or info.get('fps') != fps or any(info[k] != plan['canvas'][k] for k in ('width', 'height')):
            raise ValueError('OTIO subset requires silent video with matching FPS/dimensions; source audio cannot be silently restored')
        first, count = frame_number(c['source_in'], fps), frame_number(c['duration'], fps)
        if frame_number(c['start'], fps) != cursor:
            raise ValueError('OTIO primary clips must be contiguous on the frame grid')
        item = otio.schema.Clip(name=Path(c['path']).stem,
            media_reference=otio.schema.ExternalReference(target_url=Path(c['path']).as_uri()),
            source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(first, fps), otio.opentime.RationalTime(count, fps)))
        track.append(item)
        cursor += count
    result.tracks.append(track)
    result.metadata['shot_exchange'] = {'canvas': plan['canvas'], 'limitations': 'plain silent cuts; Kdenlive import requires native verification'}
    payload = otio.adapters.write_to_string(result, adapter_name='otio_json')
    restored = otio.adapters.read_from_string(payload, adapter_name='otio_json')
    if restored.duration().value != cursor:
        raise ValueError('OTIO library roundtrip changed timeline duration')
    with out.open('x', encoding='utf-8') as handle:
        handle.write(payload)
    return {'status': 'otio_written', 'path': str(out), 'frames': cursor,
            'fps': fps, 'kdenlive_import': 'unverified', 'warnings': warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare', help='Create a new immutable shot handoff and Blender build script')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--head-frames', type=int, default=0)
    p.add_argument('--tail-frames', type=int, default=0)
    p.add_argument('--end-scale', type=float, default=1)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('build', help='Create the native project in a separate background Blender; optional intermediate render')
    p.add_argument('--handoff', type=Path, required=True)
    p.add_argument('--blender', type=Path, required=True)
    p.add_argument('--render', action='store_true')
    p = sub.add_parser('apply', help='Validate a returned silent render and write a new plan')
    p.add_argument('--handoff', type=Path, required=True)
    p.add_argument('--render-file', type=Path, required=True)
    p.add_argument('--project', type=Path)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('timeline', help='Export plain silent cuts to OpenTimelineIO for Kdenlive import')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        editor.emit({'prepare': prepare, 'build': build, 'apply': apply, 'timeline': timeline}[args.action](args))
        return 0
    except Exception as exc:
        editor.emit({'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'})
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
