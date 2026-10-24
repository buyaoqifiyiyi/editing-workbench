"""Pure compiler checks: no Adobe process, installation or media decoder required."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


MODULE = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts/adobe_compile.py'
spec = importlib.util.spec_from_file_location('adobe_compile', MODULE)
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class AdobeCompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adobe-compile-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'shot & 空格 #%.mov').touch()
        (self.root / 'music.wav').touch()
        self.path = self.root / 'plan.json'
        self.plan = {
            'schema_version': 1, 'backend': 'pr-ae', 'name': 'Agent & 测试',
            'canvas': {'width': 320, 'height': 180, 'fps': 30},
            'clips': [{'path': 'shot & 空格 #%.mov', 'start': 0, 'source_in': .5, 'duration': 2}],
            'captions': [{'text': '原生文字', 'start': .5, 'duration': 1, 'font_size_px': 24}],
            'audio': [{'path': 'music.wav', 'start': 0, 'source_in': 1, 'duration': 2, 'volume': .5}],
        }

    def probe(self, path):
        if Path(path).suffix == '.wav':
            return {'kind': 'audio', 'duration': 8, 'has_audio': True, 'channels': 2, 'sample_rate': 44100}
        return {'kind': 'video', 'duration': 8, 'width': 640, 'height': 360, 'fps': 30,
                'has_audio': True, 'channels': 2, 'sample_rate': 48000}

    def validate(self, plan=None, probe=None):
        self.path.write_text(json.dumps(self.plan if plan is None else plan, ensure_ascii=False), encoding='utf-8')
        return compiler.validate_plan(self.path, probe or self.probe)

    def test_normalization_and_input_not_mutated(self):
        before = copy.deepcopy(self.plan)
        value = self.validate()
        self.assertEqual(self.plan, before)
        self.assertEqual(value['duration_frames'], 60)
        self.assertEqual(value['clips'][0]['source_in_frames'], 15)
        self.assertEqual(value['captions'][0]['start_frames'], 15)
        self.assertEqual(value['clips'][0]['path'], str((self.root/'shot & 空格 #%.mov').resolve()))
        self.assertEqual(value['clips'][0]['transform']['x'], 160)

    def test_reject_legacy_and_unknown_fields_at_every_boundary(self):
        for object_path, key in [([], 'effects'), (['canvas'], 'ratio'), (['clips', 0], 'speed'),
                                 (['clips', 0], 'mask'), (['clips', 0], 'chroma'),
                                 (['captions', 0], 'highlights'), (['audio', 0], 'ducking')]:
            with self.subTest(object_path=object_path, key=key):
                p = copy.deepcopy(self.plan); obj = p
                for component in object_path:
                    obj = obj[component]
                obj[key] = 1
                with self.assertRaisesRegex(ValueError, 'unsupported fields'):
                    self.validate(p)
        p = copy.deepcopy(self.plan); p['clips'][0]['transform'] = {'shear': 2}
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            self.validate(p)
        p['clips'][0]['transform'] = {}; p['clips'][0]['motion'] = [{'time': 0, 'easing': 'bezier'}]
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            self.validate(p)

    def test_reject_invalid_numbers_and_whole_frame_times(self):
        for bad in [True, False, '1', None, float('nan'), float('inf'), -1, .01]:
            with self.subTest(bad=bad):
                p = copy.deepcopy(self.plan); p['clips'][0]['source_in'] = bad
                with self.assertRaises(ValueError):
                    self.validate(p)
        for field, value in [('duration', 0), ('start', 1), ('duration', 9), ('volume', 2.01)]:
            p = copy.deepcopy(self.plan); p['clips'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(p)

    def test_canvas_and_schema_boundaries(self):
        for field, value in [('width', 321), ('width', 1922), ('height', 1922), ('fps', True), ('fps', 29.97), ('fps', 120)]:
            p = copy.deepcopy(self.plan); p['canvas'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(p)
        p = copy.deepcopy(self.plan); p['canvas'].update(width=1200, height=1200)
        with self.assertRaisesRegex(ValueError, '1080p'):
            self.validate(p)
        for field, value in [('schema_version', True), ('backend', 'jianying')]:
            p = copy.deepcopy(self.plan); p[field] = value
            with self.assertRaises(ValueError):
                self.validate(p)

    def test_sequential_clock_and_limits(self):
        p = copy.deepcopy(self.plan)
        p['clips'].append(dict(p['clips'][0], start=2, source_in=1, duration=1))
        self.assertEqual(self.validate(p)['duration_frames'], 90)
        for bad_start in [1, 2.5]:
            p['clips'][1]['start'] = bad_start
            with self.assertRaisesRegex(ValueError, 'without gaps or overlap'):
                self.validate(p)
        p['clips'] = [dict(self.plan['clips'][0], start=i*4, duration=4) for i in range(8)]
        with self.assertRaisesRegex(ValueError, '30 seconds'):
            self.validate(p)
        p['clips'] = [dict(self.plan['clips'][0], start=i) for i in range(9)]
        with self.assertRaisesRegex(ValueError, '1..8'):
            self.validate(p)

    def test_motion_strict_clock_property_and_range(self):
        p = copy.deepcopy(self.plan)
        p['clips'][0]['motion'] = [{'time': 0, 'x': 20}, {'time': 1, 'y': 40, 'opacity': .5}]
        normalized = self.validate(p)
        self.assertEqual(normalized['clips'][0]['motion'][1]['time_frames'], 30)
        for motion in [[{'time': 2, 'x': 1}], [{'time': 0}], [{'time': .01, 'x': 1}],
                       [{'time': 1, 'x': 1}, {'time': 0, 'x': 2}], [{'time': 0, 'opacity': 2}]]:
            p['clips'][0]['motion'] = motion
            with self.subTest(motion=motion), self.assertRaises(ValueError):
                self.validate(p)

    def test_position_axes_interpolate_independently(self):
        self.plan['clips'][0]['motion'] = [
            {'time': 0, 'x': 0, 'y': 0}, {'time': .5, 'y': 50}, {'time': 1, 'x': 100}]
        normalized = self.validate()
        keys = normalized['clips'][0]['position_keys']
        self.assertEqual(keys, [{'time': 0, 'x': 0, 'y': 0}, {'time': .5, 'x': 50, 'y': 50},
                                {'time': 1, 'x': 100, 'y': 50}])
        self.plan['clips'][0]['motion'] = [{'time': 1, 'x': 100}]
        keys = self.validate()['clips'][0]['position_keys']
        self.assertEqual(keys[0], {'time': 0, 'x': 160, 'y': 90})
        self.assertEqual(keys[-1], {'time': 1, 'x': 100, 'y': 90})
        jsx = compiler.compile_jsx(normalized, self.root/'out')
        self.assertIn('position.setValueAtTime(pk.time,[pk.x,pk.y])', jsx)
        self.assertNotIn('animate(tr.property("ADBE Position")', jsx)
        self.assertIn('if(prop.isSpatial)', jsx)
        self.assertIn('prop.setSpatialAutoBezierAtKey(k,false)', jsx)
        self.assertIn('prop.setSpatialContinuousAtKey(k,false)', jsx)
        self.assertIn('prop.keyValue(k).length', jsx)
        self.assertIn('prop.setSpatialTangentsAtKey(k,tangent,tangent)', jsx)

    def test_reject_bad_paths_missing_media_and_duplicate_json(self):
        for bad in ['https://example.org/source.mp4', '\\\\server\\share\\shot.mov', 'missing.mov', 'a\nb.mov']:
            p = copy.deepcopy(self.plan); p['clips'][0]['path'] = bad
            with self.subTest(path=bad), self.assertRaises(ValueError):
                self.validate(p)
        self.path.write_text('{"backend":"pr-ae","backend":"other"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
            compiler.validate_plan(self.path, self.probe)

    def test_probe_and_audio_rejections(self):
        for field, value in [('kind', 'image'), ('has_audio', 1), ('duration', float('nan')), ('width', 0)]:
            def probe(path, field=field, value=value):
                return dict(self.probe(path), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(probe=probe)
        for field, value in [('has_audio', False), ('channels', 6), ('sample_rate', 0)]:
            def probe(path, field=field, value=value):
                info = self.probe(path)
                return dict(info, **{field: value}) if Path(path).suffix == '.wav' else info
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(probe=probe)

    def test_caption_boundaries_and_no_silent_dropping(self):
        for field, value in [('text', '\x00'), ('text', '\ud800'), ('color', '#fff'),
                             ('font_size_px', True), ('x', -1), ('duration', 2), ('start', .01)]:
            p = copy.deepcopy(self.plan); p['captions'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(p)

    def test_jsx_json_escaping_and_host_safety_guards(self):
        self.plan['captions'][0]['text'] = '\");app.quit();//\n中文\u2028\\'
        p = self.validate(); jsx = compiler.compile_jsx(p, self.root/'out & 中文')
        raw = jsx.split('var data = ', 1)[1].split(';\nvar p =', 1)[0]
        restored = json.loads(raw)
        self.assertEqual(restored['plan']['captions'][0]['text'], self.plan['captions'][0]['text'])
        self.assertNotIn('\u2028', jsx)
        self.assertIn('\\u2028', jsx)
        for required in ['Refusing to replace an existing user project', 'original.exists || reopened.exists',
                         'app.open(original)', 'count!==p.clips.length', 'app.project.file.fsName!==reopened.fsName',
                         'templates[j]==="Lossless"', 'templates[j]==="无损"', 'no fallback codec allowed',
                         'KeyframeInterpolationType.LINEAR', 'app.quit()', '.avi']:
            self.assertIn(required, jsx)
        for forbidden in ['setPref', 'saveToDisk', 'system.callSystem', 'H.264']:
            self.assertNotIn(forbidden, jsx)

    def test_xmeml_frame_clock_escaped_uri_and_audio_structure(self):
        p = self.validate(); xml = compiler.compile_xmeml(p, self.root/'out & 中文 #')
        root = ET.fromstring(xml); seq = root.find('sequence')
        self.assertEqual(root.get('version'), '4')
        self.assertEqual(seq.findtext('name'), self.plan['name'])
        self.assertEqual(seq.findtext('duration'), '60')
        self.assertEqual(seq.findtext('rate/timebase'), '30')
        self.assertEqual(seq.findtext('rate/ntsc'), 'FALSE')
        v = seq.find('media/video/track/clipitem')
        self.assertEqual([v.findtext(k) for k in ('start', 'end', 'in', 'out')], ['0', '60', '0', '60'])
        self.assertIsNone(v.find('sourcetrack'))
        self.assertIn('%20%26%20', v.findtext('file/pathurl'))
        self.assertIn('%23', v.findtext('file/pathurl'))
        self.assertTrue(v.findtext('file/pathurl').endswith('/renders/shot-001.avi'))
        a = seq.findall('media/audio/track/clipitem')
        self.assertEqual(len(a), 4)
        self.assertEqual([x.findtext('sourcetrack/trackindex') for x in a], ['1', '2', '1', '2'])
        self.assertEqual([x.findtext('in') for x in a], ['0', '0', '30', '30'])
        self.assertEqual(a[-1].findtext('filter/effect/parameter/value'), '0.5')

    def test_xml_uses_pr_verified_paired_stereo_and_single_file_definitions(self):
        p = self.validate(); root = ET.fromstring(compiler.compile_xmeml(p, self.root/'out'))
        seq = root.find('sequence'); audio = seq.find('media/audio')
        self.assertEqual(seq.get('explodedTracks'), 'true')
        groups = audio.findall('outputs/group')
        self.assertEqual([(g.findtext('index'), g.findtext('numchannels'), g.findtext('downmix'),
                           g.findtext('channel/index')) for g in groups],
                         [('1', '1', '0', '1'), ('2', '1', '0', '2')])
        self.assertEqual(list(audio)[-1].tag, 'outputs')
        tracks = audio.findall('track')
        self.assertEqual(len(tracks), 4)
        for index, track in enumerate(tracks):
            self.assertEqual(track.attrib, {'currentExplodedTrackIndex': str(index % 2),
                                            'totalExplodedTrackCount': '2', 'premiereTrackType': 'Stereo'})
            self.assertEqual([child.tag for child in list(track)[-3:]], ['enabled', 'locked', 'outputchannelindex'])
            self.assertEqual(track.findtext('enabled'), 'TRUE')
            self.assertEqual(track.findtext('locked'), 'FALSE')
            self.assertEqual(track.findtext('outputchannelindex'), str(index % 2 + 1))
            for item in track.findall('clipitem'):
                self.assertEqual(item.get('premiereChannelType'), 'stereo')
                for link in item.findall('link'):
                    self.assertEqual(link.findtext('groupindex'), '1' if link.findtext('mediatype') == 'audio' else None)
        files = seq.findall('.//file')
        for identity in {f.get('id') for f in files}:
            matching = [f for f in files if f.get('id') == identity]
            self.assertGreater(len(matching), 1)
            self.assertGreater(len(matching[0]), 0)
            self.assertTrue(all(len(f) == 0 for f in matching[1:]))
        # The gain is linear; paired stereo must not introduce pan or sqrt(2) compensation.
        for track in tracks[-2:]:
            effect = track.find('clipitem/filter/effect')
            self.assertEqual(effect.findtext('effectid'), 'audiolevels')
            self.assertEqual(effect.findtext('parameter/value'), '0.5')
            self.assertEqual(len(effect.findall('parameter')), 1)

    def test_silent_video_omits_source_audio_and_bgm_optional(self):
        p = copy.deepcopy(self.plan); p.pop('audio'); p.pop('captions')
        normalized = self.validate(p, lambda path: dict(self.probe(path), has_audio=False))
        xml = ET.fromstring(compiler.compile_xmeml(normalized, self.root/'out'))
        self.assertEqual(xml.findall('sequence/media/audio/track'), [])
        self.assertEqual(xml.findall('.//sourcetrack'), [])
        self.assertEqual(xml.findall('.//link'), [])

    def test_caption_split_across_shots_uses_exact_global_frame_clock(self):
        self.plan['clips'] = [dict(self.plan['clips'][0], duration=1, start=0),
                              dict(self.plan['clips'][0], duration=1, start=1)]
        p = self.validate()
        jsx = compiler.compile_jsx(p, self.root/'out')
        self.assertIn('var captions=data.text_specs.shots[i].text_layers', jsx)
        specs = compiler.caption_layer_specs(p)['shots']
        self.assertEqual([(s['text_layers'][0]['in'], s['text_layers'][0]['out']) for s in specs],
                         [(.5, 1), (0, .5)])
        self.assertEqual(p['clips'][1]['start_frames'], 30)
        self.assertEqual(p['captions'][0]['start_frames'], 15)
        self.assertEqual(p['captions'][0]['duration_frames'], 30)

    def test_static_caption_specs_keep_chinese_and_native_units(self):
        self.plan['captions'][0].update(text='原生中文\n第二行', color='#12aB34', scale=.75, opacity=.4)
        p = self.validate()
        before = copy.deepcopy(p)
        specs = compiler.caption_layer_specs(p)
        layer = specs['shots'][0]['text_layers'][0]
        self.assertEqual(layer['text'], '原生中文\n第二行')
        self.assertEqual(layer['color'], [18/255, 171/255, 52/255])
        self.assertEqual(layer['caption_index'], 0)
        self.assertEqual(layer['transform'], {
            'position': {'value': [160, 153], 'keys': []},
            'scale': {'value': [75, 75], 'keys': []}, 'opacity': {'value': 40, 'keys': []}})
        compiler.compile_jsx(p, self.root/'out')
        self.assertEqual(p, before)
        self.plan['captions'][0].pop('scale'); self.plan['captions'][0].pop('opacity')
        cap = self.validate()['captions'][0]
        self.assertEqual((cap['scale'], cap['opacity'], cap['motion']), (1, 1, []))

    def test_caption_partial_motion_preserves_independent_axes_and_curves(self):
        self.plan['captions'][0].update(start=0, duration=2, motion=[
            {'time': 0, 'x': 0, 'y': 0, 'scale': .5, 'opacity': 0},
            {'time': .5, 'y': 50}, {'time': 1, 'x': 100, 'scale': 1.5, 'opacity': 1}])
        transform = compiler.caption_layer_specs(self.validate())['shots'][0]['text_layers'][0]['transform']
        self.assertEqual([(k['time'], k['value']) for k in transform['position']['keys']],
                         [(0, [0, 0]), (.5, [50, 50]), (1, [100, 50]), (2, [100, 50])])
        self.assertEqual([(k['time'], k['value']) for k in transform['scale']['keys']],
                         [(0, [50, 50]), (1, [150, 150]), (2, [150, 150])])
        self.assertEqual([(k['time'], k['value']) for k in transform['opacity']['keys']],
                         [(0, 0), (1, 100), (2, 100)])
        for prop in transform.values():
            self.assertEqual(prop['value'], prop['keys'][0]['value'])
            for key in prop['keys']:
                self.assertEqual((key['in_interpolation'], key['out_interpolation']), ('linear', 'linear'))

    def test_caption_motion_spanning_shots_uses_caption_clock_and_boundary_values(self):
        self.plan['clips'] = [dict(self.plan['clips'][0], duration=1, start=i) for i in range(2)]
        self.plan['captions'][0].update(x=0, y=80, opacity=0,
            motion=[{'time': .8, 'x': 100, 'scale': 2, 'opacity': 1}])
        p = self.validate()
        shots = compiler.caption_layer_specs(p)['shots']
        first, second = [shot['text_layers'][0] for shot in shots]
        self.assertEqual([(k['time'], k['value']) for k in first['transform']['position']['keys']],
                         [(.5, [0, 80]), (1, [62.5, 80])])
        self.assertEqual([(k['time'], k['value']) for k in second['transform']['position']['keys']],
                         [(0, [62.5, 80]), (.3, [100, 80]), (.5, [100, 80])])
        self.assertEqual(second['transform']['scale']['value'], [162.5, 162.5])
        self.assertEqual(second['transform']['opacity']['value'], 62.5)
        for name in ('position', 'scale', 'opacity'):
            self.assertEqual(first['transform'][name]['keys'][-1]['value'],
                             second['transform'][name]['keys'][0]['value'])

    def test_caption_first_last_overlap_and_nonkeyed_properties(self):
        self.plan['clips'] = [dict(self.plan['clips'][0], duration=1, start=i) for i in range(3)]
        self.plan['captions'][0].update(start=10/30, duration=70/30, x=0, motion=[{'time': 2, 'x': 240}])
        shots = compiler.caption_layer_specs(self.validate())['shots']
        layers = [shot['text_layers'][0] for shot in shots]
        self.assertEqual([(x['in'], x['out']) for x in layers], [(10/30, 1), (0, 1), (0, 20/30)])
        for layer, start_frame in zip(layers, (0, 20, 50)):
            position = layer['transform']['position']
            self.assertEqual(position['value'][0], start_frame * 4)
            self.assertEqual(layer['transform']['scale']['keys'], [])
            self.assertEqual(layer['transform']['opacity']['keys'], [])
        self.plan['captions'] = []
        self.assertEqual(compiler.caption_layer_specs(self.validate()),
                         {'shots': [{'name': f'shot-{i:03d}', 'text_layers': []} for i in (1, 2, 3)]})

    def test_caption_motion_rejects_unknown_invalid_clocks_and_ranges(self):
        bad_keys = [{'time': 0}, {'time': True, 'x': 1}, {'time': .01, 'x': 1},
                    {'time': -1/30, 'x': 1}, {'time': 1, 'x': 1}, {'time': 0, 'rotation': 1},
                    {'time': 0, 'expression': 'time'}, {'time': 0, 'x': -1}, {'time': 0, 'x': 321},
                    {'time': 0, 'y': 181}, {'time': 0, 'scale': .009}, {'time': 0, 'scale': 10.01},
                    {'time': 0, 'opacity': -1}, {'time': 0, 'opacity': 1.01}]
        bad_keys += [{'time': 0, field: value} for field in ('x', 'y', 'scale', 'opacity')
                    for value in (True, False, None, '1', float('nan'), float('inf'))]
        motions = [[key] for key in bad_keys] + [None, {}, [{'time': 0, 'x': 1}] * 65,
            [{'time': 0, 'x': 1}, {'time': 0, 'y': 2}], [{'time': .5, 'x': 1}, {'time': 0, 'y': 2}]]
        for motion in motions:
            with self.subTest(motion=motion), self.assertRaises(ValueError):
                p = copy.deepcopy(self.plan); p['captions'][0]['motion'] = motion
                self.validate(p)
        for field, value in [('scale', 0), ('scale', 11), ('opacity', -.01), ('opacity', 1.01),
                             ('scale', True), ('opacity', float('nan'))]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                p = copy.deepcopy(self.plan); p['captions'][0][field] = value
                self.validate(p)

    def test_caption_maximum_keys_and_final_frame_are_valid(self):
        self.plan['captions'][0].update(start=0, duration=2,
            motion=[{'time': frame/30, 'x': frame} for frame in range(60)])
        normalized = self.validate()
        self.assertEqual(normalized['captions'][0]['motion'][-1]['time_frames'], 59)
        self.plan['clips'][0]['duration'] = 3
        self.plan['captions'][0].update(duration=3,
            motion=[{'time': frame/30, 'x': frame} for frame in range(64)])
        self.assertEqual(len(self.validate()['captions'][0]['motion']), 64)

    def test_jsx_native_caption_readback_and_explicit_linear_parameters(self):
        jsx = compiler.compile_jsx(self.validate(), self.root/'out')
        for code in ['ae-text-readback.json', 'if(readback.exists)', 'snapshotTextLayers()',
                     'before_save:beforeSave,after_reopen:afterReopen',
                     'text.name="Editable caption "+(cap.caption_index+1)',
                     'textTransform(textTr.property("ADBE Position"),cap.transform.position)',
                     'textTransform(textTr.property("ADBE Scale"),cap.transform.scale)',
                     'textTransform(textTr.property("ADBE Opacity"),cap.transform.opacity)',
                     'prop.valueAtTime(prop.numKeys?prop.keyTime(1):0,false)',
                     'prop.keyInInterpolationType(k)', 'prop.keyOutInterpolationType(k)',
                     'function propertySnapshot(prop, expectedZ)',
                     'if(layer.threeDLayer!==false) throw Error("Unexpected 3D text layer")',
                     'value.length!==2 && value.length!==3',
                     'value.length===3 && value[2]!==expectedZ',
                     'return value.slice(0,2)', 'value:nativeValue(prop.keyValue(k))',
                     'value:nativeValue(prop.valueAtTime(',
                     'position:propertySnapshot(tr.property("ADBE Position"),0)',
                     'scale:propertySnapshot(tr.property("ADBE Scale"),100)',
                     'opacity:propertySnapshot(tr.property("ADBE Opacity"))',
                     'layers.sort(function(a,b){return a.caption_index-b.caption_index;})']:
            self.assertIn(code, jsx)
        self.assertNotIn('JSON.stringify', jsx)
        self.assertLess(jsx.index('var beforeSave=snapshotTextLayers()'), jsx.index('app.project.save(original)'))
        self.assertLess(jsx.index('app.open(original)'), jsx.index('var afterReopen=snapshotTextLayers()'))

    def test_foreground_convex_source_coordinates_and_defaults(self):
        for vertices in ([[0, 0], [640, 0], [0, 360]],
                         [[0, 0], [640, 0], [640, 360], [0, 360]],
                         [[0, 360], [640, 360], [640, 0], [0, 0]]):
            with self.subTest(vertices=vertices):
                self.plan['clips'][0]['foreground_mask'] = {'vertices': vertices}
                mask = self.validate()['clips'][0]['foreground_mask']
                self.assertEqual(mask, {'vertices': vertices, 'feather_px': 0, 'inverted': False})
        # The canvas is 320x180: source-pixel vertices must not be rescaled.
        self.assertEqual(mask['vertices'][0], [0, 360])
        self.plan['clips'][0]['foreground_mask'].update(feather_px=100, inverted=True)
        mask = self.validate()['clips'][0]['foreground_mask']
        self.assertEqual((mask['feather_px'], mask['inverted']), (100, True))
        vertices = [[320 + 100 * math.cos(i * math.tau / 32),
                     180 + 100 * math.sin(i * math.tau / 32)] for i in range(32)]
        self.plan['clips'][0]['foreground_mask']['vertices'] = vertices
        self.assertEqual(len(self.validate()['clips'][0]['foreground_mask']['vertices']), 32)

    def test_generated_readback_serializer_handles_native_boolean_fields(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node is required to execute generated readback serializer')
        jsx = compiler.compile_jsx(self.validate(), self.root/'out')
        serializer = 'function jsonText(value) {' + jsx.split('function jsonText(value) {', 1)[1].split('\n{\n    for', 1)[0]
        expected = {'shots': [{'foregrounds': [{'audio_enabled': False,
            'mask': {'closed': True, 'inverted': False, 'vertices': [[0, 0], [640, 0], [0, 360]]}}]}]}
        script = serializer + '\nprocess.stdout.write(jsonText(' + json.dumps(expected) + '));'
        result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_foreground_rejects_nonconvex_degenerate_and_self_intersections(self):
        # A pentagram has same-sign adjacent turns, but is not a convex boundary.
        pentagon = [[320 + 100 * math.cos(i * math.tau / 5),
                     180 + 100 * math.sin(i * math.tau / 5)] for i in range(5)]
        bad = [[], [[0, 0], [1, 1]], [[0, 0]] * 33,
               [[0, 0], [100, 0], [0, 100], [0, 0]],
               [[0, 0], [100, 0], [100, 100], [100, 0], [0, 100]],
               [[0, 0], [50, 0], [100, 0]],
               [[0, 0], [50, 0], [100, 0], [100, 100], [0, 100]],
               [[0, 0], [100, 0], [50, 20], [100, 100], [0, 100]],
               [[0, 0], [100, 100], [0, 100], [100, 0]],
               [pentagon[i] for i in (0, 2, 4, 1, 3)]]
        for vertices in bad:
            with self.subTest(vertices=vertices), self.assertRaises(ValueError):
                self.plan['clips'][0]['foreground_mask'] = {'vertices': vertices}
                self.validate()

    def test_foreground_strict_objects_coordinates_and_options(self):
        triangle = [[0, 0], [640, 0], [0, 360]]
        masks = [None, [], {}, {'vertices': None}, {'vertices': 'triangle'}]
        masks += [{'vertices': [point, [640, 0], [0, 360]]}
                  for point in (None, [], [0], [0, 0, 0], {'x': 0, 'y': 0})]
        masks += [{'vertices': [[value, 0], [640, 0], [0, 360]]}
                  for value in (True, False, None, '1', float('nan'), float('inf'), -1, 641)]
        masks += [{'vertices': [[0, value], [640, 0], [0, 360]]}
                  for value in (True, float('-inf'), -1, 361)]
        masks += [{'vertices': triangle, 'feather_px': value}
                  for value in (True, False, None, '1', float('nan'), float('inf'), -1, 100.01)]
        masks += [{'vertices': triangle, 'inverted': value} for value in (0, 1, None, 'false', [])]
        masks += [{'vertices': triangle, field: []}
                  for field in ('motion', 'in_tangents', 'closed', 'expansion', 'opacity', 'mode')]
        for mask in masks:
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                self.plan['clips'][0]['foreground_mask'] = mask
                self.validate()

    def test_foreground_specs_preserve_layer_order_clock_and_native_parameters(self):
        self.plan['clips'] = [dict(self.plan['clips'][0], duration=1, start=i) for i in range(2)]
        self.plan['clips'][0]['foreground_mask'] = {
            'vertices': [[0, 0], [640, 0], [0, 360]], 'feather_px': 2.5, 'inverted': True}
        self.plan['captions'].append(dict(self.plan['captions'][0], text='第二层', start=0, duration=1))
        plan = self.validate()
        before = copy.deepcopy(plan)
        shots = compiler.foreground_layer_specs(plan)['shots']
        self.assertEqual(shots[0]['layer_order'], ['Editable foreground 1', 'Editable caption 2',
                                                 'Editable caption 1', 'Editable source 1'])
        self.assertEqual(shots[1], {'name': 'shot-002', 'layer_order': ['Editable caption 1', 'Editable source 2'],
                                   'foregrounds': []})
        self.assertEqual(shots[0]['foregrounds'], [{
            'name': 'Editable foreground 1', 'source_path': plan['clips'][0]['path'],
            'source_in': .5, 'in': 0, 'out': 1, 'audio_enabled': False,
            'mask': {'name': 'Foreground polygon', 'mode': 'add', 'inverted': True,
                     'vertices': [[0, 0], [640, 0], [0, 360]],
                     'in_tangents': [[0, 0]] * 3, 'out_tangents': [[0, 0]] * 3, 'closed': True,
                     'feather': [2.5, 2.5], 'opacity': 100, 'expansion': 0}}])
        self.assertEqual(plan, before)
        shots[0]['foregrounds'][0]['mask']['vertices'][0][0] = 123
        self.assertEqual(plan, before)

    def test_foreground_omitted_legacy_plan_and_empty_captions(self):
        self.plan.pop('captions')
        plan = self.validate()
        self.assertNotIn('foreground_mask', plan['clips'][0])
        self.assertEqual(compiler.foreground_layer_specs(plan), {'shots': [
            {'name': 'shot-001', 'layer_order': ['Editable source 1'], 'foregrounds': []}]})
        jsx = compiler.compile_jsx(plan, self.root/'out')
        self.assertIn('if(c.foreground_mask)', jsx)

    def test_foreground_requires_opaque_source_but_keeps_old_opacity_behavior(self):
        for transform, motion in [({'opacity': .5}, []), ({}, [{'time': 1, 'opacity': 0}]),
                                  ({'opacity': .5}, [{'time': 0, 'opacity': 1}])]:
            with self.subTest(transform=transform, motion=motion):
                self.plan['clips'][0].update(transform=transform, motion=motion)
                self.validate()
                self.plan['clips'][0]['foreground_mask'] = {'vertices': [[0, 0], [640, 0], [0, 360]]}
                with self.assertRaisesRegex(ValueError, 'foreground mask requires opaque source transform; animate captions instead'):
                    self.validate()
                self.plan['clips'][0].pop('foreground_mask')
        self.plan['clips'][0].update(transform={'opacity': 1}, motion=[{'time': 1, 'opacity': 1}],
                                   foreground_mask={'vertices': [[0, 0], [640, 0], [0, 360]]})
        self.assertEqual(self.validate()['clips'][0]['motion'][0]['opacity'], 1)

    def test_jsx_foreground_duplicate_inherits_motion_and_reads_saved_native_masks(self):
        self.plan['clips'][0].update(
            foreground_mask={'vertices': [[0, 0], [640, 0], [0, 360]], 'feather_px': 4},
            transform={'scale': 1.2, 'rotation': 10},
            motion=[{'time': 0, 'x': 100, 'opacity': 1}, {'time': 1, 'x': 200, 'scale': 1.5}])
        plan = self.validate()
        jsx = compiler.compile_jsx(plan, self.root/'out')
        for code in ['ae-layer-readback.json', 'if(layerReadback.exists)',
                     'var foreground=layer.duplicate()', 'foreground.audioEnabled=false',
                     'foreground.moveToBeginning()', 'addProperty("ADBE Mask Atom")',
                     'mask.name="Foreground polygon"', 'mask.maskMode=MaskMode.ADD',
                     'mask.inverted=c.foreground_mask.inverted', 'shape.vertices=c.foreground_mask.vertices',
                     'shape.inTangents=tangents', 'shape.outTangents=tangents', 'shape.closed=true',
                     'mask.property("ADBE Mask Feather").setValue([feather,feather])',
                     'mask.property("ADBE Mask Opacity").setValue(100)',
                     'mask.property("ADBE Mask Offset").setValue(0)',
                     'function snapshotForegroundLayers()', 'order.push(layer.name)',
                     'source_path:layer.source.file.fsName', 'source_in:-layer.startTime',
                     'audio_enabled:layer.audioEnabled', 'masks.numProperties!==1',
                     'shape=mask.property("ADBE Mask Shape").value',
                     'vertices:shape.vertices', 'in_tangents:shape.inTangents', 'out_tangents:shape.outTangents',
                     'closed:shape.closed', 'mask.maskMode===MaskMode.ADD?"add":"other"',
                     'before_save:beforeLayers,after_reopen:afterLayers']:
            self.assertIn(code, jsx)
        foreground_code = jsx.split('if(c.foreground_mask)', 1)[1].split('var rq=', 1)[0]
        self.assertNotIn('Transform', foreground_code)
        self.assertNotIn('importFile', foreground_code)
        self.assertNotIn('JSON.stringify', jsx)
        self.assertLess(jsx.index('text.inPoint=cap["in"]'), jsx.index('var foreground=layer.duplicate()'))
        self.assertLess(jsx.index('var beforeLayers=snapshotForegroundLayers()'), jsx.index('app.project.save(original)'))
        self.assertLess(jsx.index('app.open(original)'), jsx.index('var afterLayers=snapshotForegroundLayers()'))


if __name__ == '__main__':
    unittest.main()
