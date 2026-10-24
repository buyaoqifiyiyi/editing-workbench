"""Strict, host-independent compiler for the first PR + AE workflow subset.

All times are seconds on input and exact canvas frames after validation. Scale is
a multiplier of fit-contain; position is canvas pixels; opacity/volume are linear.
This module neither imports editor nor accesses Adobe installations.
"""
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def _object(value, allowed, required, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected object')
    if set(value) - set(allowed):
        raise ValueError(f'{label}: unsupported fields {sorted(set(value) - set(allowed))}')
    if set(required) - set(value):
        raise ValueError(f'{label}: missing fields {sorted(set(required) - set(value))}')
    return value


def _number(value, label, low, high):
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{label}: expected finite number')
    if not low <= value <= high:
        raise ValueError(f'{label}: outside [{low}, {high}]')
    return value


def _frames(value, fps, label, positive=False):
    value = _number(value, label, 0, 86400)
    frames = round(value * fps)
    if abs(value * fps - frames) > 1e-6 or (positive and frames <= 0):
        raise ValueError(f'{label}: expected {"positive " if positive else ""}whole-frame time')
    return frames


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label}: expected nonempty text, max {maximum} characters')
    if any(ord(c) < 32 and c not in '\n\t' for c in value) or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f'{label}: unsupported control or surrogate character')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _position_keys(clip, fps):
    """Combine independent axis curves without adding a hold to the other axis."""
    curves = {}
    times = {0}
    for axis in ('x', 'y'):
        points = {0: clip['transform'][axis]}
        for key in clip['motion']:
            if axis in key:
                points[key['time_frames']] = key[axis]
                times.add(key['time_frames'])
        curves[axis] = sorted(points.items())
    if not any('x' in k or 'y' in k for k in clip['motion']):
        return []

    def value(points, frame):
        for (first, start), (last, end) in zip(points, points[1:]):
            if first <= frame <= last:
                return start + (end - start) * (frame - first) / (last - first)
        return points[-1][1]

    return [{'time': frame / fps, 'x': value(curves['x'], frame), 'y': value(curves['y'], frame)}
            for frame in sorted(times)]


def _foreground_mask(value, info, label):
    mask = _object(value, ('vertices', 'feather_px', 'inverted'), ('vertices',), label)
    vertices = mask['vertices']
    if not isinstance(vertices, list) or not 3 <= len(vertices) <= 32:
        raise ValueError(f'{label}.vertices: expected 3..32 source-pixel points')
    for index, point in enumerate(vertices):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f'{label}.vertices[{index}]: expected [x, y]')
        for axis, coordinate in enumerate(('width', 'height')):
            _number(point[axis], f'{label}.vertices[{index}][{axis}]', 0, info[coordinate])
    if len({tuple(point) for point in vertices}) != len(vertices):
        raise ValueError(f'{label}.vertices: duplicate points or repeated closing point')
    # Every other vertex must lie strictly on the same side of each edge.
    # Unlike adjacent turns alone, this also rejects self-intersecting stars.
    winding = None
    for index, a in enumerate(vertices):
        b = vertices[(index + 1) % len(vertices)]
        for point_index, point in enumerate(vertices):
            if point_index in (index, (index + 1) % len(vertices)):
                continue
            cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
            if not math.isfinite(cross) or cross == 0:
                raise ValueError(f'{label}.vertices: expected strictly convex polygon without collinear points')
            sign = cross > 0
            if winding is None:
                winding = sign
            elif sign != winding:
                raise ValueError(f'{label}.vertices: expected strictly convex polygon without intersections')
    feather = _number(mask.get('feather_px', 0), f'{label}.feather_px', 0, 100)
    inverted = mask.get('inverted', False)
    if type(inverted) is not bool:
        raise ValueError(f'{label}.inverted: expected boolean')
    return {'vertices': [point[:] for point in vertices], 'feather_px': feather, 'inverted': inverted}


def validate_plan(plan_path, probe):
    """Read a strict JSON plan; probe local sources through the supplied callable.

    Returns an independent normalized dictionary, including per-source ``info``
    and integer frame fields. Unknown legacy effects are errors, never ignored.
    """
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding='utf-8-sig'), object_pairs_hook=_pairs)
    _object(plan, ('schema_version', 'backend', 'name', 'canvas', 'clips', 'captions', 'audio'),
            ('schema_version', 'backend', 'name', 'canvas', 'clips'), 'plan')
    if type(plan['schema_version']) is not int or plan['schema_version'] != 1 or plan['backend'] != 'pr-ae':
        raise ValueError('expected schema_version=1 and backend="pr-ae"')
    _text(plan['name'], 'name', 120)
    canvas = _object(plan['canvas'], ('width', 'height', 'fps'), ('width', 'height', 'fps'), 'canvas')
    for axis in ('width', 'height'):
        if type(canvas[axis]) is not int or not 16 <= canvas[axis] <= 1920 or canvas[axis] % 2:
            raise ValueError(f'canvas.{axis}: expected even integer 16..1920')
    width, height = canvas['width'], canvas['height']
    if min(width, height) > 1080:
        raise ValueError('canvas: limited to 1080p landscape or portrait')
    fps = canvas['fps']
    if type(fps) is not int or fps not in (24, 25, 30, 50, 60):
        raise ValueError('canvas.fps: expected integer 24/25/30/50/60')
    if not isinstance(plan['clips'], list) or not 1 <= len(plan['clips']) <= 8:
        raise ValueError('clips: expected 1..8 sequential video clips')
    cache = {}

    def media(item, label, audio_only=False):
        raw = item['path']
        _text(raw, f'{label}.path', 32760)
        if '\n' in raw or '\t' in raw or '://' in raw or raw.startswith(('\\\\', '//')):
            raise ValueError(f'{label}.path: only local file paths are supported')
        path = Path(raw)
        path = (plan_path.parent / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_file():
            raise ValueError(f'{label}: missing local media {path}')
        if str(path) not in cache:
            cache[str(path)] = dict(probe(str(path)))
        info = dict(cache[str(path)])
        duration = _number(info.get('duration'), f'{label} source duration', 1e-9, 86400 * 365)
        if type(info.get('has_audio')) is not bool:
            raise ValueError(f'{label}: probe must declare has_audio')
        if audio_only:
            if not info['has_audio']:
                raise ValueError(f'{label}: source has no audio')
            if type(info.get('channels')) is not int or info['channels'] != 2:
                raise ValueError(f'{label}: independent BGM currently requires a stereo source')
            if type(info.get('sample_rate')) is not int or not 8000 <= info['sample_rate'] <= 192000:
                raise ValueError(f'{label}: invalid audio sample rate')
        else:
            if info.get('kind') != 'video':
                raise ValueError(f'{label}: source must be video')
            for axis in ('width', 'height'):
                if type(info.get(axis)) is not int or info[axis] <= 0:
                    raise ValueError(f'{label}: invalid source dimensions')
        item['path'], item['info'] = str(path), info
        for field in ('source_in', 'start', 'duration'):
            item[field + '_frames'] = _frames(item[field], fps, f'{label}.{field}', field == 'duration')
            item[field] = item[field + '_frames'] / fps
        if item['source_in'] + item['duration'] > duration + 1e-7:
            raise ValueError(f'{label}: source selection exceeds measured duration')
        item['volume'] = _number(item.get('volume', 1), f'{label}.volume', 0, 2)

    cursor = 0
    transform_limits = {'scale': (.01, 10), 'x': (-4 * width, 5 * width),
                        'y': (-4 * height, 5 * height), 'rotation': (-3600, 3600), 'opacity': (0, 1)}
    for index, clip in enumerate(plan['clips']):
        label = f'clips[{index}]'
        _object(clip, ('path', 'source_in', 'duration', 'start', 'volume', 'transform', 'motion', 'foreground_mask'),
                ('path', 'source_in', 'duration', 'start'), label)
        media(clip, label)
        if 'foreground_mask' in clip:
            clip['foreground_mask'] = _foreground_mask(clip['foreground_mask'], clip['info'], f'{label}.foreground_mask')
        if clip['start_frames'] != cursor:
            raise ValueError(f'{label}: clips must be sequential without gaps or overlap')
        cursor += clip['duration_frames']
        transform = _object(clip.get('transform', {}), transform_limits, (), f'{label}.transform')
        defaults = {'scale': 1, 'x': width / 2, 'y': height / 2, 'rotation': 0, 'opacity': 1}
        for key, value in transform.items():
            defaults[key] = _number(value, f'{label}.transform.{key}', *transform_limits[key])
        clip['transform'] = defaults
        motion = clip.get('motion', [])
        if not isinstance(motion, list) or len(motion) > 64:
            raise ValueError(f'{label}.motion: expected at most 64 keyframes')
        previous = -1
        for keyframe in motion:
            _object(keyframe, ('time', *transform_limits), ('time',), f'{label}.motion')
            frame = _frames(keyframe['time'], fps, f'{label}.motion.time')
            if frame <= previous or frame >= clip['duration_frames'] or len(keyframe) == 1:
                raise ValueError(f'{label}.motion: keys must increase within clip and contain a property')
            for key, value in keyframe.items():
                if key != 'time':
                    _number(value, f'{label}.motion.{key}', *transform_limits[key])
            keyframe['time_frames'] = frame
            keyframe['time'] = frame / fps
            previous = frame
        clip['motion'] = motion
        clip['position_keys'] = _position_keys(clip, fps)
        if clip.get('foreground_mask') is not None and (defaults['opacity'] != 1 or
                any(key.get('opacity', 1) != 1 for key in motion)):
            raise ValueError(f'{label}: foreground mask requires opaque source transform; animate captions instead')
    if cursor > fps * 30:
        raise ValueError('plan: first subset is limited to 30 seconds')
    plan['duration_frames'] = cursor
    plan['duration'] = cursor / fps
    captions = plan.get('captions', [])
    if not isinstance(captions, list) or len(captions) > 64:
        raise ValueError('captions: expected at most 64 captions')
    for index, caption in enumerate(captions):
        label = f'captions[{index}]'
        _object(caption, ('text', 'start', 'duration', 'font_size_px', 'color', 'x', 'y', 'scale', 'opacity', 'motion'),
                ('text', 'start', 'duration', 'font_size_px'), label)
        _text(caption['text'], f'{label}.text', 1000)
        caption['start_frames'] = _frames(caption['start'], fps, f'{label}.start')
        caption['duration_frames'] = _frames(caption['duration'], fps, f'{label}.duration', True)
        if caption['start_frames'] + caption['duration_frames'] > cursor:
            raise ValueError(f'{label}: extends beyond timeline')
        _number(caption['font_size_px'], f'{label}.font_size_px', 4, 500)
        caption['x'] = _number(caption.get('x', width / 2), f'{label}.x', 0, width)
        caption['y'] = _number(caption.get('y', height * .85), f'{label}.y', 0, height)
        limits = {'x': (0, width), 'y': (0, height), 'scale': (.01, 10), 'opacity': (0, 1)}
        for field in ('scale', 'opacity'):
            caption[field] = _number(caption.get(field, 1), f'{label}.{field}', *limits[field])
        motion = caption.get('motion', [])
        if not isinstance(motion, list) or len(motion) > 64:
            raise ValueError(f'{label}.motion: expected at most 64 keyframes')
        previous = -1
        for keyframe in motion:
            _object(keyframe, ('time', *limits), ('time',), f'{label}.motion')
            frame = _frames(keyframe['time'], fps, f'{label}.motion.time')
            if frame <= previous or frame >= caption['duration_frames'] or len(keyframe) == 1:
                raise ValueError(f'{label}.motion: keys must increase within caption and contain a property')
            for field, value in keyframe.items():
                if field != 'time':
                    _number(value, f'{label}.motion.{field}', *limits[field])
            keyframe['time_frames'] = frame
            keyframe['time'] = frame / fps
            previous = frame
        caption['motion'] = motion
        color = caption.get('color', '#FFFFFF')
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise ValueError(f'{label}.color: expected #RRGGBB')
        caption['color'] = color.upper()
    plan['captions'] = captions
    audio = plan.get('audio', [])
    if not isinstance(audio, list) or len(audio) > 8:
        raise ValueError('audio: expected at most 8 independent audio clips')
    for index, item in enumerate(audio):
        label = f'audio[{index}]'
        _object(item, ('path', 'start', 'source_in', 'duration', 'volume'),
                ('path', 'start', 'source_in', 'duration'), label)
        media(item, label, True)
        if item['start_frames'] + item['duration_frames'] > cursor:
            raise ValueError(f'{label}: extends beyond timeline')
    plan['audio'] = audio
    return plan


def caption_layer_specs(plan):
    """Expected native text layers, sliced on integer shot/caption frame clocks.

    Each declared property interpolates independently from its base at frame 0.
    Intersection endpoints preserve that curve across shot boundaries. Values
    use AE units: pixels, two-axis scale percent and opacity percent.
    """
    fps = plan['canvas']['fps']
    shots = []

    def sample(points, frame):
        for (first, start), (last, end) in zip(points, points[1:]):
            if first <= frame <= last:
                return start + (end - start) * (frame - first) / (last - first)
        return points[-1][1]

    for index, clip in enumerate(plan['clips'], 1):
        layers = []
        for caption_index, cap in enumerate(plan['captions']):
            first = max(cap['start_frames'], clip['start_frames'])
            last = min(cap['start_frames'] + cap['duration_frames'],
                       clip['start_frames'] + clip['duration_frames'])
            if last <= first:
                continue
            curves = {}
            for field in ('x', 'y', 'scale', 'opacity'):
                points = {0: cap[field]}
                for key in cap['motion']:
                    if field in key:
                        points[key['time_frames']] = key[field]
                curves[field] = sorted(points.items())
            transform = {}
            for name, fields in [('position', ('x', 'y')), ('scale', ('scale',)), ('opacity', ('opacity',))]:
                def native(frame):
                    values = [sample(curves[field], frame) for field in fields]
                    if name == 'scale':
                        return [values[0] * 100, values[0] * 100]
                    return values[0] * 100 if name == 'opacity' else values

                keyed = any(field in key for key in cap['motion'] for field in fields)
                frames = {first - cap['start_frames'], last - cap['start_frames']}
                if keyed:
                    frames.update(key['time_frames'] for key in cap['motion']
                                  if any(field in key for field in fields)
                                  and first < cap['start_frames'] + key['time_frames'] < last)
                keys = [{'time': (frame + cap['start_frames'] - clip['start_frames']) / fps,
                         'value': native(frame), 'in_interpolation': 'linear', 'out_interpolation': 'linear'}
                        for frame in sorted(frames)] if keyed else []
                transform[name] = {'value': keys[0]['value'] if keys else native(0), 'keys': keys}
            layers.append({'caption_index': caption_index, 'text': cap['text'],
                           'font_size_px': cap['font_size_px'],
                           'color': [int(cap['color'][i:i+2], 16) / 255 for i in (1, 3, 5)],
                           'in': (first - clip['start_frames']) / fps,
                           'out': (last - clip['start_frames']) / fps, 'transform': transform})
        shots.append({'name': f'shot-{index:03d}', 'text_layers': layers})
    return {'shots': shots}


def foreground_layer_specs(plan):
    """Expected foreground parameters and actual top-to-bottom shot layer order."""
    shots = []
    for index, (clip, text_shot) in enumerate(zip(plan['clips'], caption_layer_specs(plan)['shots']), 1):
        order = [f"Editable caption {cap['caption_index'] + 1}" for cap in reversed(text_shot['text_layers'])]
        order.append(f'Editable source {index}')
        foregrounds = []
        mask = clip.get('foreground_mask')
        if mask is not None:
            name = f'Editable foreground {index}'
            order.insert(0, name)
            vertices = [point[:] for point in mask['vertices']]
            foregrounds.append({'name': name, 'source_path': clip['path'], 'source_in': clip['source_in'],
                'in': 0, 'out': clip['duration'], 'audio_enabled': False,
                'mask': {'name': 'Foreground polygon', 'mode': 'add', 'inverted': mask['inverted'],
                    'vertices': vertices, 'in_tangents': [[0, 0] for _ in vertices],
                    'out_tangents': [[0, 0] for _ in vertices], 'closed': True,
                    'feather': [mask['feather_px'], mask['feather_px']], 'opacity': 100, 'expansion': 0}})
        shots.append({'name': text_shot['name'], 'layer_order': order, 'foregrounds': foregrounds})
    return {'shots': shots}


def compile_jsx(plan, output_dir):
    """Compile validated plan to native editable AE compositions (no preference changes)."""
    out = Path(output_dir).resolve()
    payload = json.dumps({'plan': plan, 'out': str(out), 'text_specs': caption_layer_specs(plan)},
                         ensure_ascii=True, allow_nan=False)
    return '/* Generated from validated PR-AE plan. */\n(function () {\nvar data = ' + payload + r''';
var p = data.plan, out = new Folder(data.out), w=p.canvas.width, h=p.canvas.height, fps=p.canvas.fps;
if (!out.exists) throw Error("Output directory must be prepared by workflow");
if (app.project && (app.project.numItems !== 0 || app.project.file !== null))
    throw Error("Refusing to replace an existing user project");
var original=new File(out.fsName+"/ae-project.aep"), reopened=new File(out.fsName+"/ae-reopened.aep");
if (original.exists || reopened.exists) throw Error("Refusing existing AE project output");
var readback=new File(out.fsName+"/ae-text-readback.json");
if(readback.exists) throw Error("Refusing existing AE text readback");
var layerReadback=new File(out.fsName+"/ae-layer-readback.json");
if(layerReadback.exists) throw Error("Refusing existing AE layer readback");
var renders=new Folder(out.fsName+"/renders");
if (!renders.exists) throw Error("Render directory must be prepared by workflow");
var outputs=[], i, j;
for(i=0;i<p.clips.length;i++) {
    var f=new File(renders.fsName+"/shot-"+("000"+(i+1)).slice(-3)+".avi");
    if(f.exists) throw Error("Refusing existing render: "+f.fsName);
    outputs.push(f);
}
function linear(prop) {
    for(var k=1;k<=prop.numKeys;k++) {
        prop.setInterpolationTypeAtKey(k,KeyframeInterpolationType.LINEAR,KeyframeInterpolationType.LINEAR);
        if(prop.isSpatial) {
            prop.setSpatialAutoBezierAtKey(k,false);
            prop.setSpatialContinuousAtKey(k,false);
            var tangent=[];
            for(var axis=0;axis<prop.keyValue(k).length;axis++) tangent.push(0);
            prop.setSpatialTangentsAtKey(k,tangent,tangent);
        }
    }
}
function animate(prop, clip, keys, convert, base) {
    prop.setValue(convert(base));
    var state=base.slice(0), keyed=false;
    for(var k=0;k<clip.motion.length;k++) {
        var key=clip.motion[k], use=false;
        for(var a=0;a<keys.length;a++) if(key[keys[a]]!==undefined) use=true;
        if(use) {
            if(!keyed && key.time>0) prop.setValueAtTime(0,convert(base));
            keyed=true;
            var vals=[];
            for(var b=0;b<keys.length;b++) {if(key[keys[b]]!==undefined) state[b]=key[keys[b]]; vals.push(state[b]);}
            prop.setValueAtTime(key.time,convert(vals));
        }
    }
    linear(prop);
}
function textTransform(prop, spec) {
    prop.setValue(spec.value);
    for(var k=0;k<spec.keys.length;k++) prop.setValueAtTime(spec.keys[k].time,spec.keys[k].value);
    linear(prop);
}
function propertySnapshot(prop, expectedZ) {
    function nativeValue(value) {
        if(expectedZ===undefined) return value;
        if(!(value instanceof Array) || (value.length!==2 && value.length!==3))
            throw Error("Unexpected 2D text transform dimensions");
        if(value.length===3 && value[2]!==expectedZ) throw Error("Unexpected 2D text transform Z");
        return value.slice(0,2);
    }
    var keys=[];
    for(var k=1;k<=prop.numKeys;k++) keys.push({time:prop.keyTime(k),value:nativeValue(prop.keyValue(k)),
        in_interpolation:prop.keyInInterpolationType(k)===KeyframeInterpolationType.LINEAR?"linear":"other",
        out_interpolation:prop.keyOutInterpolationType(k)===KeyframeInterpolationType.LINEAR?"linear":"other"});
    return {value:nativeValue(prop.valueAtTime(prop.numKeys?prop.keyTime(1):0,false)),keys:keys};
}
function snapshotTextLayers() {
    var shots=[];
    for(var s=0;s<p.clips.length;s++) {
        var name="shot-"+("000"+(s+1)).slice(-3), comp=null;
        for(var n=1;n<=app.project.numItems;n++) {
            var item=app.project.item(n);
            if(item instanceof CompItem && item.name===name) {
                if(comp!==null) throw Error("Duplicate shot composition");
                comp=item;
            }
        }
        if(comp===null) throw Error("Missing shot composition");
        var layers=[];
        for(var n=1;n<=comp.numLayers;n++) {
            var layer=comp.layer(n), group=layer.property("ADBE Text Properties");
            if(group===null) continue;
            if(layer.threeDLayer!==false) throw Error("Unexpected 3D text layer");
            var match=/^Editable caption ([1-9][0-9]*)$/.exec(layer.name);
            if(!match) throw Error("Unexpected text layer name");
            var doc=group.property("ADBE Text Document").value, tr=layer.property("ADBE Transform Group");
            layers.push({caption_index:Number(match[1])-1,text:doc.text.replace(/\r\n?/g,"\n"),font_size_px:doc.fontSize,
                color:[doc.fillColor[0],doc.fillColor[1],doc.fillColor[2]],"in":layer.inPoint,"out":layer.outPoint,
                transform:{position:propertySnapshot(tr.property("ADBE Position"),0),
                    scale:propertySnapshot(tr.property("ADBE Scale"),100),opacity:propertySnapshot(tr.property("ADBE Opacity"))}});
        }
        layers.sort(function(a,b){return a.caption_index-b.caption_index;});
        shots.push({name:name,text_layers:layers});
    }
    return {shots:shots};
}
function snapshotForegroundLayers() {
    var shots=[];
    for(var s=0;s<p.clips.length;s++) {
        var name="shot-"+("000"+(s+1)).slice(-3), comp=null;
        for(var n=1;n<=app.project.numItems;n++) {
            var item=app.project.item(n);
            if(item instanceof CompItem && item.name===name) {
                if(comp!==null) throw Error("Duplicate shot composition");
                comp=item;
            }
        }
        if(comp===null) throw Error("Missing shot composition");
        var order=[], foregrounds=[];
        for(var n=1;n<=comp.numLayers;n++) {
            var layer=comp.layer(n);
            order.push(layer.name);
            if(!/^Editable foreground [1-9][0-9]*$/.test(layer.name)) continue;
            var masks=layer.property("ADBE Mask Parade");
            if(masks.numProperties!==1) throw Error("Expected exactly one foreground mask");
            var mask=masks.property(1), shape=mask.property("ADBE Mask Shape").value;
            foregrounds.push({name:layer.name,source_path:layer.source.file.fsName,source_in:-layer.startTime,
                "in":layer.inPoint,"out":layer.outPoint,audio_enabled:layer.audioEnabled,
                mask:{name:mask.name,mode:mask.maskMode===MaskMode.ADD?"add":"other",inverted:mask.inverted,
                    vertices:shape.vertices,in_tangents:shape.inTangents,out_tangents:shape.outTangents,closed:shape.closed,
                    feather:mask.property("ADBE Mask Feather").value,opacity:mask.property("ADBE Mask Opacity").value,
                    expansion:mask.property("ADBE Mask Offset").value}});
        }
        shots.push({name:name,layer_order:order,foregrounds:foregrounds});
    }
    return {shots:shots};
}
function jsonText(value) {
    if(value===null) return "null";
    if(typeof value==="boolean") return value?"true":"false";
    if(typeof value==="number") {
        if(!isFinite(value)) throw Error("Nonfinite readback number");
        return String(value);
    }
    if(typeof value==="string") {
        var result='"';
        for(var n=0;n<value.length;n++) {
            var c=value.charAt(n), code=value.charCodeAt(n);
            if(c==='"' || c==='\\') result+='\\'+c;
            else if(code<32 || code===0x2028 || code===0x2029) result+='\\u'+("000"+code.toString(16)).slice(-4);
            else result+=c;
        }
        return result+'"';
    }
    var parts=[];
    if(value instanceof Array) {
        for(var n=0;n<value.length;n++) parts.push(jsonText(value[n]));
        return '['+parts.join(',')+']';
    }
    if(typeof value!=="object") throw Error("Unsupported readback value");
    for(var key in value) if(value.hasOwnProperty(key)) parts.push(jsonText(key)+':'+jsonText(value[key]));
    return '{'+parts.join(',')+'}';
}
{
    for(i=0;i<p.clips.length;i++) {
        var c=p.clips[i], name="shot-"+("000"+(i+1)).slice(-3);
        var comp=app.project.items.addComp(name,w,h,1,c.duration,fps);
        var imported=app.project.importFile(new ImportOptions(new File(c.path)));
        var layer=comp.layers.add(imported);
        layer.name="Editable source "+(i+1);
        layer.startTime=-c.source_in;
        layer.inPoint=0; layer.outPoint=c.duration;
        var fit=Math.min(w/c.info.width,h/c.info.height)*100;
        var tr=layer.property("ADBE Transform Group");
        var position=tr.property("ADBE Position");
        position.setValue([c.transform.x,c.transform.y]);
        for(j=0;j<c.position_keys.length;j++) {
            var pk=c.position_keys[j]; position.setValueAtTime(pk.time,[pk.x,pk.y]);
        }
        linear(position);
        animate(tr.property("ADBE Scale"),c,["scale"],function(v){return [v[0]*fit,v[0]*fit];},[c.transform.scale]);
        animate(tr.property("ADBE Rotate Z"),c,["rotation"],function(v){return v[0];},[c.transform.rotation]);
        animate(tr.property("ADBE Opacity"),c,["opacity"],function(v){return v[0]*100;},[c.transform.opacity]);
        if(layer.hasAudio) {
            layer.audioEnabled=c.volume>0;
            if(c.volume>0) {var db=20*Math.log(c.volume)/Math.LN10; layer.property("ADBE Audio Group").property("ADBE Audio Levels").setValue([db,db]);}
        }
        var captions=data.text_specs.shots[i].text_layers;
        for(j=0;j<captions.length;j++) {
            var cap=captions[j];
            var text=comp.layers.addText(cap.text), source=text.property("ADBE Text Properties").property("ADBE Text Document"), doc=source.value;
            text.name="Editable caption "+(cap.caption_index+1);
            doc.fontSize=cap.font_size_px; doc.applyFill=true; doc.applyStroke=false;
            doc.fillColor=cap.color;
            doc.justification=ParagraphJustification.CENTER_JUSTIFY; source.setValue(doc);
            var textTr=text.property("ADBE Transform Group");
            textTransform(textTr.property("ADBE Position"),cap.transform.position);
            textTransform(textTr.property("ADBE Scale"),cap.transform.scale);
            textTransform(textTr.property("ADBE Opacity"),cap.transform.opacity);
            text.inPoint=cap["in"]; text.outPoint=cap["out"];
        }
        if(c.foreground_mask) {
            var foreground=layer.duplicate();
            foreground.name="Editable foreground "+(i+1);
            foreground.audioEnabled=false;
            foreground.moveToBeginning();
            var mask=foreground.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
            mask.name="Foreground polygon"; mask.maskMode=MaskMode.ADD; mask.inverted=c.foreground_mask.inverted;
            var shape=new Shape(), tangents=[];
            for(j=0;j<c.foreground_mask.vertices.length;j++) tangents.push([0,0]);
            shape.vertices=c.foreground_mask.vertices;
            shape.inTangents=tangents; shape.outTangents=tangents; shape.closed=true;
            mask.property("ADBE Mask Shape").setValue(shape);
            var feather=c.foreground_mask.feather_px;
            mask.property("ADBE Mask Feather").setValue([feather,feather]);
            mask.property("ADBE Mask Opacity").setValue(100);
            mask.property("ADBE Mask Offset").setValue(0);
        }
        var rq=app.project.renderQueue.items.add(comp), om=rq.outputModule(1), templates=om.templates, chosen=null;
        for(j=0;j<templates.length;j++) if(templates[j]==="Lossless" || templates[j]==="无损") {chosen=templates[j];break;}
        if(chosen===null) throw Error("Lossless/无损 output module template unavailable; no fallback codec allowed");
        om.applyTemplate(chosen); om.file=outputs[i];
        rq.timeSpanStart=0; rq.timeSpanDuration=c.duration;
    }
    var beforeSave=snapshotTextLayers();
    var beforeLayers=snapshotForegroundLayers();
    app.project.save(original);
    app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
    app.open(original);
    var count=0;
    for(i=1;i<=app.project.numItems;i++) if(app.project.item(i) instanceof CompItem) {
        var current=app.project.item(i), expected=p.clips[count];
        if(!expected || current.name!=="shot-"+("000"+(count+1)).slice(-3) || Math.abs(current.duration-expected.duration)>0.000001 || current.width!==w || current.height!==h || current.frameRate!==fps)
            throw Error("AE reopen composition verification failed");
        count++;
    }
    if(count!==p.clips.length) throw Error("AE reopen composition count mismatch");
    var afterReopen=snapshotTextLayers();
    var afterLayers=snapshotForegroundLayers();
    if(readback.exists) throw Error("Refusing existing AE text readback");
    readback.encoding="UTF-8";
    if(!readback.open("w")) throw Error("Could not create AE text readback");
    var written=readback.write(jsonText({before_save:beforeSave,after_reopen:afterReopen}));
    readback.close();
    if(!written) throw Error("Could not write AE text readback");
    if(layerReadback.exists) throw Error("Refusing existing AE layer readback");
    layerReadback.encoding="UTF-8";
    if(!layerReadback.open("w")) throw Error("Could not create AE layer readback");
    written=layerReadback.write(jsonText({before_save:beforeLayers,after_reopen:afterLayers}));
    layerReadback.close();
    if(!written) throw Error("Could not write AE layer readback");
    app.project.save(reopened);
    if(!app.project.file || app.project.file.fsName!==reopened.fsName) throw Error("Refusing to close another project");
}
if(!app.project.file || app.project.file.fsName!==reopened.fsName) throw Error("Project changed before exit");
app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
app.quit();
})();
'''


def compile_xmeml(plan, output_dir):
    """Compile PR-verified FCP XML v4 with paired exploded stereo tracks.

    Shot audio is expected to be normalized/verified as 48 kHz stereo by the host
    workflow. Each BGM uses its own track to preserve overlapping placements.
    """
    out = Path(output_dir).resolve()
    fps, width, height = (plan['canvas'][k] for k in ('fps', 'width', 'height'))
    root = ET.Element('xmeml', version='4')

    def node(parent, tag, value=None, **attrs):
        child = ET.SubElement(parent, tag, **attrs)
        if value is not None:
            child.text = str(value)
        return child

    def rate(parent):
        r = node(parent, 'rate'); node(r, 'timebase', fps); node(r, 'ntsc', 'FALSE')

    seq = node(root, 'sequence', id='agent-sequence', explodedTracks='true')
    node(seq, 'name', plan['name']); node(seq, 'duration', plan['duration_frames']); rate(seq)
    tc = node(seq, 'timecode'); rate(tc); node(tc, 'string', '00:00:00:00'); node(tc, 'frame', 0); node(tc, 'displayformat', 'NDF')
    media = node(seq, 'media'); video = node(media, 'video')
    sample = node(node(video, 'format'), 'samplecharacteristics'); rate(sample)
    node(sample, 'width', width); node(sample, 'height', height); node(sample, 'anamorphic', 'FALSE'); node(sample, 'pixelaspectratio', 'square'); node(sample, 'fielddominance', 'none')
    vtrack = node(video, 'track')
    audio = node(media, 'audio'); node(audio, 'numOutputChannels', 2)
    aud_sample = node(node(audio, 'format'), 'samplecharacteristics'); node(aud_sample, 'depth', 16); node(aud_sample, 'samplerate', 48000)
    outputs = node(audio, 'outputs')
    for channel in (1, 2):
        group = node(outputs, 'group')
        node(group, 'index', channel); node(group, 'numchannels', 1); node(group, 'downmix', 0)
        node(node(group, 'channel'), 'index', channel)
    source_audio_tracks = [node(audio, 'track') for _ in range(2)] if any(c['info']['has_audio'] and c['volume'] > 0 for c in plan['clips']) else []
    for channel, track in enumerate(source_audio_tracks, 1):
        node(track, 'outputchannelindex', channel)

    defined_files = set()

    def file(parent, ident, path, duration, video_source, info):
        f = node(parent, 'file', id=ident)
        if ident in defined_files:
            return
        defined_files.add(ident)
        node(f, 'name', path.name); node(f, 'pathurl', path.as_uri()); rate(f); node(f, 'duration', duration)
        m = node(f, 'media')
        if video_source:
            sc = node(node(m, 'video'), 'samplecharacteristics'); rate(sc)
            node(sc, 'width', width); node(sc, 'height', height); node(sc, 'anamorphic', 'FALSE'); node(sc, 'pixelaspectratio', 'square'); node(sc, 'fielddominance', 'none')
        if info.get('has_audio'):
            a = node(m, 'audio'); sc = node(a, 'samplecharacteristics')
            node(sc, 'depth', 16); node(sc, 'samplerate', info.get('sample_rate', 48000)); node(a, 'channelcount', info.get('channels', 2))

    def clip(parent, ident, name, start, duration, source_in, path, file_id, file_duration, video_source, info, channel=None, volume=1):
        item = node(parent, 'clipitem', id=ident)
        node(item, 'name', name); node(item, 'enabled', 'TRUE'); node(item, 'duration', file_duration); rate(item)
        node(item, 'start', start); node(item, 'end', start+duration); node(item, 'in', source_in); node(item, 'out', source_in+duration)
        file(item, file_id, path, file_duration, video_source, info)
        if channel is not None:
            st = node(item, 'sourcetrack'); node(st, 'mediatype', 'audio'); node(st, 'trackindex', channel)
        if channel is not None and volume != 1:
            filt = node(item, 'filter'); effect = node(filt, 'effect'); node(effect, 'name', 'Audio Levels'); node(effect, 'effectid', 'audiolevels'); node(effect, 'effectcategory', 'audio'); node(effect, 'effecttype', 'audio'); node(effect, 'mediatype', 'audio')
            parameter = node(effect, 'parameter'); node(parameter, 'parameterid', 'level'); node(parameter, 'name', 'Level'); node(parameter, 'value', volume)
        return item

    audio_clipindex = 0
    for index, c in enumerate(plan['clips'], 1):
        name = f'shot-{index:03d}'; path = out / 'renders' / (name+'.avi')
        info = {'has_audio': c['info']['has_audio'] and c['volume'] > 0, 'sample_rate': 48000, 'channels': 2}
        item = clip(vtrack, name+'-v', name, c['start_frames'], c['duration_frames'], 0, path, name+'-file', c['duration_frames'], True, info)
        if info['has_audio']:
            audio_clipindex += 1
            linked = [(name+'-v', 'video', 1)]
            for channel, track in enumerate(source_audio_tracks, 1):
                aid = name+f'-a{channel}'
                linked.append((aid, 'audio', channel))
                aitem = clip(track, aid, name, c['start_frames'], c['duration_frames'], 0, path, name+'-file', c['duration_frames'], True, info, channel)
                # Bidirectional links keep A/V together during subsequent PR edits.
                for ref, kind, trackindex in [(name+'-v', 'video', 1), (name+'-a1', 'audio', 1), (name+'-a2', 'audio', 2)]:
                    link = node(aitem, 'link'); node(link, 'linkclipref', ref); node(link, 'mediatype', kind); node(link, 'trackindex', trackindex); node(link, 'clipindex', index if kind == 'video' else audio_clipindex)
            for ref, kind, trackindex in linked:
                link = node(item, 'link'); node(link, 'linkclipref', ref); node(link, 'mediatype', kind); node(link, 'trackindex', trackindex); node(link, 'clipindex', index if kind == 'video' else audio_clipindex)
    for index, a in enumerate(plan['audio'], 1):
        channels = a['info'].get('channels', 2)
        if channels != 2:
            raise ValueError('BGM supports stereo sources only')
        for channel in range(1, channels+1):
            track = node(audio, 'track'); node(track, 'outputchannelindex', channel)
            clip(track, f'bgm-{index}-a{channel}', f'BGM {index}', a['start_frames'], a['duration_frames'], a['source_in_frames'], Path(a['path']), f'bgm-{index}-file', math.floor(a['info']['duration']*fps), False, a['info'], channel, a['volume'])
    # Match the structure verified by native PR import/save/export. A pair of
    # ordinary mono tracks otherwise imports with both channels mixed to center.
    for index, track in enumerate(audio.findall('track')):
        track.set('currentExplodedTrackIndex', str(index % 2))
        track.set('totalExplodedTrackCount', '2')
        track.set('premiereTrackType', 'Stereo')
        for item in track.findall('clipitem'):
            item.set('premiereChannelType', 'stereo')
            for link in item.findall('link'):
                if link.findtext('mediatype') == 'audio':
                    node(link, 'groupindex', 1)
        node(track, 'enabled', 'TRUE'); node(track, 'locked', 'FALSE')
        output = track.find('outputchannelindex')
        track.remove(output); track.append(output)
    audio.remove(outputs); audio.append(outputs)
    ET.indent(root, space='  ')
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'+ET.tostring(root, encoding='unicode')+'\n'
