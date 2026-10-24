"""Stdlib-only helper for Resolve's bundled Python. No engine imports or host startup."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import traceback

# ResolvePython's isolated runtime omits the executed script's directory.
# Admit only this hash-checked package directory, not the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_compile


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_new(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def check(value, message):
    if not value:
        raise ValueError(message)
    return value


def empty_folder(folder):
    return not folder.GetClipList() and all(empty_folder(child) for child in (folder.GetSubFolderList() or []))


def default_empty_project(project, manager):
    """Never replace a named/saved project, even if it has no timeline."""
    return (project is None or (project.GetName() == 'Untitled Project'
            and project.GetName() not in (manager.GetProjectListInCurrentFolder() or [])
            and project.GetTimelineCount() == 0 and empty_folder(project.GetMediaPool().GetRootFolder())))


def subtitle_text(item):
    # Resolve exposes an SRT line break as Unicode LINE SEPARATOR in GetName.
    return item.GetName().replace('\u2028', '\n')


def snapshot(project):
    timeline = check(project.GetCurrentTimeline(), 'Missing current timeline')
    settings = project.GetSettings()
    tracks = {}
    for kind in ('video', 'audio'):
        tracks[kind] = []
        for track in range(1, timeline.GetTrackCount(kind) + 1):
            clips = []
            for item in (timeline.GetItemListInTrack(kind, track) or []):
                properties = item.GetProperties()
                keys = ('TransformEnabled', 'ZoomGang', 'ZoomX', 'ZoomY', 'Pan', 'Tilt',
                        'RotationAngle', 'CompositeEnabled', 'Opacity') if kind == 'video' else ('AudioVolumeEnabled', 'AudioVolume')
                clips.append({'start': item.GetStart(), 'end': item.GetEnd(), 'duration': item.GetDuration(),
                              'source_in': item.GetLeftOffset(),
                              'path': item.GetMediaPoolItem().GetClipProperty('File Path'),
                              'properties': {key: properties.get(key) for key in keys}})
                if kind == 'video':
                    clips[-1]['data_level'] = item.GetMediaPoolItem().GetClipProperty('Data Level')
            tracks[kind].append(clips)
    captions = [{'enabled': timeline.GetIsTrackEnabled('subtitle', track),
                 'items': [{'text': subtitle_text(item), 'start': item.GetStart(), 'end': item.GetEnd(),
                            'duration': item.GetDuration()}
                           for item in (timeline.GetItemListInTrack('subtitle', track) or [])]}
                for track in range(1, timeline.GetTrackCount('subtitle') + 1)]
    return {'project_name': project.GetName(), 'project_id': project.GetUniqueId(),
            'timeline': timeline.GetName(), 'timeline_id': timeline.GetUniqueId(),
            'timeline_count': project.GetTimelineCount(), 'start': timeline.GetStartFrame(),
            'end': timeline.GetEndFrame(), 'captions': captions, 'settings': {key: settings[key] for key in
                ('timelineFrameRate', 'timelineResolutionWidth', 'timelineResolutionHeight',
                 'timelineInputResMismatchBehavior', 'timelineOutputResMatchTimelineRes')}, 'tracks': tracks}


def ensure_owned(manager, name, project_id, timeline_id=None):
    project = check(manager.GetCurrentProject(), 'Current project disappeared; host left untouched')
    check(project.GetName() == name and project.GetUniqueId() == project_id,
          'User switched current project identity; stop without changing it')
    if timeline_id is not None:
        timeline = project.GetCurrentTimeline()
        check(timeline and timeline.GetUniqueId() == timeline_id and timeline.GetName() == 'Main'
              and project.GetTimelineCount() == 1, 'User switched current timeline identity; stop without changing it')
    return project


def finish_owned_project(manager, name, project_id, timeline_id, expected):
    """Release only this unchanged project so another task can use the live host."""
    project = ensure_owned(manager, name, project_id, timeline_id)
    check(snapshot(project) == expected, 'Owned project changed during rendering; leave it open without saving')
    ensure_owned(manager, name, project_id, timeline_id)
    check(manager.SaveProject(), 'Final native save failed')
    project = ensure_owned(manager, name, project_id, timeline_id)
    check(snapshot(project) == expected, 'Owned project changed before close; leave it open')
    ensure_owned(manager, name, project_id, timeline_id)
    check(manager.CloseProject(project), 'Normal close of owned project failed; host left running')
    check(default_empty_project(manager.GetCurrentProject(), manager),
          'Host is not at the empty project after owned close; inspect before any new task')
    return True


def video_properties(item, plan):
    width, height = plan['canvas']['width'], plan['canvas']['height']
    transform = item['transform']
    return {'TransformEnabled': True, 'ZoomGang': False,
            'ZoomX': transform['scale'], 'ZoomY': transform['scale'],
            'Pan': transform['x'] - width / 2, 'Tilt': height / 2 - transform['y'],
            'RotationAngle': -transform['rotation'], 'CompositeEnabled': True,
            'Opacity': transform['opacity'] * 100}


def set_data_level(media, requested):
    value = {'auto': 'Auto', 'full': 'Full'}[requested]
    check(media.SetClipProperty('Data Level', value), 'Native source data level rejected')
    check(media.GetClipProperty('Data Level') == value, 'Native source data level readback differs')


def audio_properties(item):
    return {'AudioVolumeEnabled': True, 'AudioVolume': 20 * math.log10(item['volume'])}


def property_readback(actual, expected):
    for key, value in expected.items():
        if isinstance(value, bool):
            check(actual.get(key) is value, 'Native property differs: ' + key)
        else:
            check(type(actual.get(key)) in (int, float) and math.isfinite(actual[key])
                  and abs(actual[key] - value) <= 1e-5, 'Native property differs: ' + key)


def set_properties(clip, properties, guard):
    # Single-key writes fail closed and make a rejected native field unambiguous.
    for key, value in properties.items():
        guard()
        check(clip.SetProperties({key: value}), 'Native property rejected: ' + key)
    property_readback(clip.GetProperties(), properties)


def guarded_plan(plan):
    # Parent validates the complete schema. Guard SDK ranges here before creating
    # anything, so a future compiler cannot silently expand native admission.
    width, height = plan['canvas']['width'], plan['canvas']['height']
    for item in plan['clips']:
        properties = video_properties(item, plan)
        check(-360 <= properties['RotationAngle'] <= 360 and -4 * width <= properties['Pan'] <= 4 * width
              and -4 * height <= properties['Tilt'] <= 4 * height, 'Transform exceeds admitted native range')
    for item in plan['clips'] + plan['audio']:
        check(0 <= item['volume'] <= 2, 'Linear audio gain outside admitted range')
        if item['volume'] > 0:
            check(audio_properties(item)['AudioVolume'] >= -100,
                  'Positive gain below -100 dB native floor; use 0 for a muted clip')


def validate_readback(actual, plan):
    start = actual['start']
    check(actual.get('project_id') and actual.get('timeline_id') and actual['timeline_count'] == 1
          and actual['timeline'] == 'Main', 'Unexpected native project/timeline identity or count')
    check(actual['end'] - start == plan['duration_frames'], 'Native timeline duration mismatch')
    validate_caption_readback(actual.get('captions', []), plan.get('captions', []), start)
    expected_video = plan['clips']
    check(len(actual['tracks']['video']) == 1, 'Unexpected video tracks')
    check(len(actual['tracks']['video'][0]) == len(expected_video), 'Unexpected video clip count')
    expected_audio = [item for item in plan['clips'] + plan['audio'] if item['volume'] > 0 and item['info']['has_audio']]
    nonempty_audio = [track for track in actual['tracks']['audio'] if track]
    check(len(nonempty_audio) == len(expected_audio), 'Unexpected audio track count')
    pairs = list(zip(actual['tracks']['video'][0], expected_video))
    for track, item in zip(nonempty_audio, expected_audio):
        check(len(track) == 1, 'Expected one audio clip on each isolated track')
        pairs.append((track[0], item))
    for clip, expected in pairs:
        check(Path(clip['path']).resolve() == Path(expected['path']).resolve(), 'Native source path mismatch')
        check(clip['start'] == start + expected['start_frames']
              and clip['duration'] == expected['duration_frames']
              and clip['end'] == clip['start'] + clip['duration']
              and clip['source_in'] == expected['source_in_frames'], 'Native source/time range mismatch')
        property_readback(clip['properties'], video_properties(expected, plan) if 'transform' in expected
                          and 'ZoomX' in clip['properties'] else audio_properties(expected))
        if 'ZoomX' in clip['properties']:
            check(clip.get('data_level') == {'auto': 'Auto', 'full': 'Full'}[expected['data_level']],
                  'Native source data level differs')
    settings = actual['settings']
    check(float(settings['timelineFrameRate']) == 30
          and int(settings['timelineResolutionWidth']) == plan['canvas']['width']
          and int(settings['timelineResolutionHeight']) == plan['canvas']['height']
          and settings['timelineInputResMismatchBehavior'] == 'scaleToFit'
          and settings['timelineOutputResMatchTimelineRes'] == '1', 'Native canvas/fit differs from plan')


def validate_caption_readback(tracks, expected, start):
    check(len(tracks) == (1 if expected else 0), 'Unexpected native subtitle tracks')
    if not expected:
        return
    check(tracks[0]['enabled'] is True and len(tracks[0]['items']) == len(expected),
          'Native subtitle track disabled or caption count differs')
    for actual, caption in zip(tracks[0]['items'], expected):
        check(actual == {'text': caption['text'], 'start': start + caption['start_frames'],
                         'end': start + caption['start_frames'] + caption['duration_frames'],
                         'duration': caption['duration_frames']}, 'Native subtitle text or frame range differs')


def execute(resolve, root, plan, name, timeout):
    guarded_plan(plan)
    check(resolve.GetVersion()[:3] == [21, 1, 1], 'Resolve version is not admitted; revalidate native contracts before upgrading')
    manager = check(resolve.GetProjectManager(), 'Project manager unavailable')
    check(default_empty_project(manager.GetCurrentProject(), manager),
          'Current user project is open; close it manually before creating a new workflow bundle')
    check(name not in (manager.GetProjectListInCurrentFolder() or []), 'Project name already exists; prepare a new version')
    project = check(manager.CreateProject(name), 'Native CreateProject failed')
    project_id = check(project.GetUniqueId(), 'Native project unique identity unavailable')
    ensure_owned(manager, name, project_id)
    canvas = plan['canvas']
    check(project.SetSettings({'timelineResolutionWidth': str(canvas['width']),
                              'timelineResolutionHeight': str(canvas['height']), 'timelineFrameRate': '30'}),
          'Native project settings failed')
    for key, value in {'timelineInputResMismatchBehavior': 'scaleToFit', 'timelineOutputResMatchTimelineRes': '1'}.items():
        ensure_owned(manager, name, project_id)
        check(project.SetSettings({key: value}), 'Native fit setting rejected: ' + key)
        check(project.GetSettings().get(key) == value, 'Native fit setting readback differs: ' + key)
    pool = project.GetMediaPool()
    media = {}
    for path in dict.fromkeys(item['path'] for item in plan['clips'] + plan['audio']):
        ensure_owned(manager, name, project_id)
        imported = pool.ImportMedia([path])
        check(imported and len(imported) == 1, 'Native media import failed: ' + path)
        check(Path(imported[0].GetClipProperty('File Path')).resolve() == Path(path).resolve(), 'Import path mismatch')
        requested = next((item['data_level'] for item in plan['clips'] if item['path'] == path), None)
        if requested is not None:
            ensure_owned(manager, name, project_id)
            set_data_level(imported[0], requested)
        media[path] = imported[0]
    ensure_owned(manager, name, project_id)
    timeline = check(pool.CreateEmptyTimeline('Main'), 'Native timeline creation failed')
    timeline_id = check(timeline.GetUniqueId(), 'Native timeline unique identity unavailable')
    ensure_owned(manager, name, project_id)
    check(project.SetCurrentTimeline(timeline), 'Native current timeline selection failed')
    start = timeline.GetStartFrame()

    if plan.get('captions'):
        # Resolve appends an imported SRT relative to the current timeline end.
        # Import it while empty, then place video/audio at explicit record frames.
        ensure_owned(manager, name, project_id, timeline_id)
        srt = root / 'captions.srt'
        check(not srt.is_symlink() and srt.read_text(encoding='utf-8') == resolve_compile.compile_srt(plan),
              'Generated subtitle file changed')
        subtitles = pool.ImportMedia([str(srt)])
        check(subtitles and len(subtitles) == 1, 'Native SRT import failed')
        check(pool.AppendToTimeline([{'mediaPoolItem': subtitles[0]}]), 'Native SRT append failed')

    def append(item, kind, track):
        ensure_owned(manager, name, project_id, timeline_id)
        while timeline.GetTrackCount(kind) < track:
            ensure_owned(manager, name, project_id, timeline_id)
            check(timeline.AddTrack(kind, 'stereo') if kind == 'audio' else timeline.AddTrack(kind), 'Add native track failed')
        ensure_owned(manager, name, project_id, timeline_id)
        added = pool.AppendToTimeline([{'mediaPoolItem': media[item['path']],
                   'startFrame': item['source_in_frames'],
                   'endFrame': item['source_in_frames'] + item['duration_frames'],
                   'recordFrame': start + item['start_frames'], 'mediaType': 1 if kind == 'video' else 2,
                   'trackIndex': track}])
        check(added and len(added) == 1, 'Native append did not return one clip')
        return added[0]

    for item in plan['clips']:
        clip = append(item, 'video', 1)
        ensure_owned(manager, name, project_id, timeline_id)
        set_properties(clip, video_properties(item, plan), lambda: ensure_owned(manager, name, project_id, timeline_id))
    audio_items = [item for item in plan['clips'] + plan['audio'] if item['volume'] > 0 and item['info']['has_audio']]
    for track, item in enumerate(audio_items, 1):
        check(timeline.GetTrackSubType('audio', 1) == 'stereo', 'Default native audio track is not stereo')
        clip = append(item, 'audio', track)
        ensure_owned(manager, name, project_id, timeline_id)
        set_properties(clip, audio_properties(item), lambda: ensure_owned(manager, name, project_id, timeline_id))
        check(timeline.GetTrackSubType('audio', track) == 'stereo', 'Native audio track is not stereo')
    before = snapshot(project)
    validate_readback(before, plan)
    ensure_owned(manager, name, project_id, timeline_id)
    check(manager.SaveProject(), 'Native save failed')
    ensure_owned(manager, name, project_id, timeline_id)
    check(manager.CloseProject(project), 'Native close failed')
    check(default_empty_project(manager.GetCurrentProject(), manager), 'User opened a project during save/reopen')
    project = check(manager.LoadProject(name), 'Native reopen failed')
    ensure_owned(manager, name, project_id, timeline_id)
    after = snapshot(project)
    check(before == after, 'Native save/reopen readback differs')
    validate_readback(after, plan)
    write_new(root / 'readback.json', {'before_close': before, 'after_reopen': after})
    ensure_owned(manager, name, project_id, timeline_id)
    check(manager.ExportProject(name, str(root / 'project.drp'), False), 'Native DRP export failed')
    ensure_owned(manager, name, project_id, timeline_id)
    check(project.SetCurrentRenderMode(1), 'Single clip render mode unavailable')
    ensure_owned(manager, name, project_id, timeline_id)
    check(project.SetCurrentRenderFormatAndCodec('mp4', 'H264_NVIDIA'), 'NVIDIA H264 renderer unavailable; no silent fallback')
    settings = {'TargetDir': str(root), 'CustomName': 'output', 'SelectAllFrames': True,
                'ExportVideo': True, 'ExportAudio': bool(audio_items), 'FormatWidth': canvas['width'],
                'FormatHeight': canvas['height'], 'FrameRate': 30}
    if audio_items:
        settings.update(AudioCodec='aac', AudioSampleRate=48000)
    if plan.get('captions'):
        settings.update(ExportSubtitle=True, SubtitleFormat='BurnIn')
    for key, value in settings.items():
        ensure_owned(manager, name, project_id, timeline_id)
        check(project.SetRenderSettings({key: value}), 'Native render setting rejected: ' + key)
    check(not (root / 'output.mp4').exists(), 'Refusing existing render output')
    ensure_owned(manager, name, project_id, timeline_id)
    check(not project.IsRenderingInProgress(), 'Native render already in progress')
    check(resolve.OpenPage('deliver'), 'Native Deliver page unavailable')
    ensure_owned(manager, name, project_id, timeline_id)
    job = check(project.AddRenderJob(), 'Native render job creation failed')
    ensure_owned(manager, name, project_id, timeline_id)
    check(project.StartRendering([job], False), 'Native render start failed')
    deadline = time.monotonic() + timeout
    while project.IsRenderingInProgress() and time.monotonic() < deadline:
        time.sleep(.5)
    status = project.GetRenderJobStatus(job)
    check(not project.IsRenderingInProgress() and status.get('JobStatus') == 'Complete',
          'Native render incomplete or timed out; host is left running, no forced termination')
    check((root / 'output.mp4').is_file(), 'Native output missing')
    check((root / 'project.drp').is_file(), 'Native DRP missing')
    own_project_closed = finish_owned_project(manager, name, project_id, timeline_id, after)
    return {'status': 'native_completed', 'project_name': name, 'render_job': job, 'render_status': status,
            'own_project_closed': own_project_closed,
            'product': resolve.GetProductName(), 'version': resolve.GetVersionString(),
            'outputs': {filename: sha(root / filename) for filename in ('output.mp4', 'project.drp', 'readback.json')},
            'human_viewing_listening': 'not_performed', 'technical_decode': 'pending_parent_verification'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['run', 'doctor'])
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--normalized-sha256')
    parser.add_argument('--project-name')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    import DaVinciResolveScript as dvr
    resolve = check(dvr.scriptapp('Resolve'), 'No existing Resolve scripting connection; launch the app manually, settings unchanged')
    if args.operation == 'doctor':
        print(json.dumps({'connected': True, 'version': resolve.GetVersionString(), 'host_modified': False}))
        return
    root = args.bundle.resolve(strict=True)
    check(not (root / 'native-result.json').exists(), 'Native bundle already attempted')
    result = {'status': 'failed', 'host_terminated': False}
    try:
        normalized = root / 'normalized-plan.json'
        check(not normalized.is_symlink() and sha(normalized) == args.normalized_sha256, 'Normalized plan identity differs')
        plan = resolve_compile.validate_normalized(json.loads(normalized.read_text(encoding='utf-8')))
        result = execute(resolve, root, plan, args.project_name, args.timeout)
    except Exception as exc:
        result.update(error=str(exc), traceback=traceback.format_exc())
    write_new(root / 'native-result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] != 'native_completed':
        raise SystemExit(2)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    main()
