"""Resolve plan trust-boundary checks; no host, decoder or installation needed."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts/resolve_compile.py'
spec = importlib.util.spec_from_file_location('resolve_compile', MODULE)
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class ResolveCompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='resolve-compile-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'shot 空格 & #%.mov').touch()
        (self.root / 'music.wav').touch()
        self.path = self.root / 'plan.json'
        self.plan = {
            'schema_version': 1, 'backend': 'resolve', 'name': '达芬奇 & 编辑',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 30},
            'clips': [{'path': 'shot 空格 & #%.mov', 'source_in': .5, 'duration': 2, 'start': 0}],
            'audio': [{'path': 'music.wav', 'source_in': 1, 'duration': 1, 'start': .5, 'volume': .75}],
        }

    def probe(self, path):
        if Path(path).suffix == '.wav':
            return {'path': path, 'kind': 'audio', 'duration': 700, 'has_audio': True,
                    'channels': 2, 'sample_rate': 48000}
        return {'path': path, 'kind': 'video', 'duration': 700, 'width': 640, 'height': 360,
                'fps': 30.0, 'has_audio': True, 'channels': 2, 'sample_rate': 48000}

    def validate(self, plan=None, probe=None):
        self.path.write_text(json.dumps(self.plan if plan is None else plan, ensure_ascii=False), encoding='utf-8')
        return compiler.validate_plan(self.path, probe or self.probe)

    def test_normalization_independence_and_probe_cache(self):
        before = copy.deepcopy(self.plan)
        self.plan['clips'].append(dict(self.plan['clips'][0], start=2, source_in=3))
        calls = []
        result = self.validate(probe=lambda path: calls.append(path) or self.probe(path))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['duration_frames'], 120)
        self.assertEqual(result['clips'][0]['source_in_frames'], 15)
        self.assertEqual(result['audio'][0]['start_frames'], 15)
        self.assertEqual(result['clips'][0]['info']['fps'], 30.0)
        self.assertNotIn('path', result['clips'][0]['info'])
        self.assertEqual(result['clips'][0]['transform'],
                         {'scale': 1, 'x': 960.0, 'y': 540.0, 'rotation': 0, 'opacity': 1})
        self.assertEqual(self.plan['clips'][0], before['clips'][0])
        self.assertEqual(result, compiler.validate_normalized(result))
        self.assertEqual(compiler.normalized_fingerprint(result), compiler.normalized_fingerprint(copy.deepcopy(result)))
        result['clips'][0]['transform']['scale'] = 2
        self.assertNotIn('transform', self.plan['clips'][0])

    def test_defaults_portrait_silent_and_static_transform(self):
        self.plan.pop('audio')
        self.plan['canvas'].update(width=1080, height=1920)
        self.plan['clips'][0]['transform'] = {'scale': 1.25, 'x': 400, 'rotation': -25, 'opacity': .5}
        def silent(path):
            value = self.probe(path)
            value.update(has_audio=False)
            value.pop('channels'); value.pop('sample_rate')
            return value
        result = self.validate(probe=silent)
        self.assertEqual(result['audio'], [])
        self.assertEqual(result['clips'][0]['transform']['y'], 960)
        self.assertNotIn('sample_rate', result['clips'][0]['info'])

    def test_data_level_defaults_explicit_full_and_input_independence(self):
        before = copy.deepcopy(self.plan)
        automatic = self.validate()
        self.assertEqual(automatic['clips'][0]['data_level'], 'auto')
        self.assertNotIn('data_level', automatic['audio'][0])
        self.assertEqual(self.plan, before)
        self.plan['clips'][0]['data_level'] = 'auto'
        self.assertEqual(self.validate(), automatic)
        self.plan['clips'][0]['data_level'] = 'full'
        full = self.validate()
        self.assertEqual(full['clips'][0]['data_level'], 'full')
        self.assertEqual(compiler.validate_normalized(full), full)
        self.assertNotEqual(compiler.normalized_fingerprint(full), compiler.normalized_fingerprint(automatic))

    def test_data_level_rejects_invalid_values_and_audio_field(self):
        for bad in ['Video', 'Full', 'limited', '', True, False, 0, None, [], {}]:
            plan = copy.deepcopy(self.plan); plan['clips'][0]['data_level'] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'data_level'):
                self.validate(plan)
        self.plan['audio'][0]['data_level'] = 'full'
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            self.validate()

    def test_shared_canonical_video_source_requires_identical_data_level(self):
        self.plan['clips'][0]['data_level'] = 'full'
        second = dict(self.plan['clips'][0], start=2,
                      path=str((self.root / 'shot 空格 & #%.mov').resolve()))
        self.plan['clips'].append(second)
        full = self.validate()
        self.assertEqual([c['data_level'] for c in full['clips']], ['full', 'full'])
        second.pop('data_level')
        with self.assertRaisesRegex(ValueError, 'inconsistent data level for shared source'):
            self.validate()
        second['data_level'] = 'auto'
        with self.assertRaisesRegex(ValueError, 'inconsistent data level for shared source'):
            self.validate()
        # A distinct source can retain automatic interpretation.
        (self.root / 'second.mov').touch()
        second['path'] = 'second.mov'
        self.assertEqual([c['data_level'] for c in self.validate()['clips']], ['full', 'auto'])

    def test_normalized_data_level_is_required_validated_and_consistent(self):
        self.plan['clips'].append(dict(self.plan['clips'][0], start=2))
        original = self.validate()
        for bad in ['Video', True, None, [], {}]:
            value = copy.deepcopy(original); value['clips'][0]['data_level'] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'data_level'):
                compiler.validate_normalized(value)
        value = copy.deepcopy(original); value['clips'][0].pop('data_level')
        with self.assertRaisesRegex(ValueError, 'missing fields'):
            compiler.validate_normalized(value)
        value = copy.deepcopy(original); value['clips'][1]['data_level'] = 'full'
        with self.assertRaisesRegex(ValueError, 'inconsistent data level for shared source'):
            compiler.validate_normalized(value)
        value = copy.deepcopy(original); value['audio'][0]['data_level'] = 'auto'
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            compiler.validate_normalized(value)

    def test_unknown_fields_rejected_at_every_boundary(self):
        cases = [([], 'styles'), ([], 'effects'), (['canvas'], 'color_space'),
                 (['clips', 0], 'motion'), (['clips', 0], 'speed'), (['clips', 0], 'effects'),
                 (['audio', 0], 'ducking')]
        for location, key in cases:
            value = copy.deepcopy(self.plan); target = value
            for part in location:
                target = target[part]
            target[key] = []
            with self.subTest(location=location, key=key), self.assertRaisesRegex(ValueError, 'unsupported fields'):
                self.validate(value)
        self.plan['clips'][0]['transform'] = {'skew': 0}
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            self.validate()

    def test_schema_canvas_and_names(self):
        for key, bad in [('schema_version', True), ('schema_version', 2), ('backend', 'pr-ae'),
                         ('name', ''), ('name', 'x\x00'), ('name', 'x' * 121)]:
            value = copy.deepcopy(self.plan); value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.validate(value)
        for key, bad in [('width', True), ('width', 321), ('width', 1922), ('height', 14),
                         ('fps', True), ('fps', 30.0), ('fps', 25), ('fps', 29.97)]:
            value = copy.deepcopy(self.plan); value['canvas'][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.validate(value)
        self.plan['canvas'].update(width=1200, height=1200)
        with self.assertRaisesRegex(ValueError, '1080p'):
            self.validate()

    def test_invalid_times_and_numbers(self):
        for bad in [True, False, None, '1', float('nan'), float('inf'), -1, .01, 10 ** 400]:
            value = copy.deepcopy(self.plan); value['clips'][0]['source_in'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.validate(value)
        for field, bad in [('duration', 0), ('duration', 701), ('start', 1), ('volume', -1),
                           ('volume', 2.01), ('volume', 1e-6)]:
            value = copy.deepcopy(self.plan); value['clips'][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                self.validate(value)
        for field, bad in [('scale', 0), ('x', float('nan')), ('opacity', True),
                           ('rotation', 361), ('rotation', -361), ('x', -6721), ('x', 8641),
                           ('y', -3781), ('y', 4861)]:
            value = copy.deepcopy(self.plan); value['clips'][0]['transform'] = {field: bad}
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                self.validate(value)

    def test_native_property_boundaries(self):
        for rotation, x, y in [(-360, -6720, -3780), (360, 8640, 4860)]:
            value = copy.deepcopy(self.plan)
            value['clips'][0]['transform'] = {'rotation': rotation, 'x': x, 'y': y}
            value['clips'][0]['volume'] = 1e-5
            value['audio'][0]['volume'] = 0
            result = self.validate(value)
            self.assertEqual(result['clips'][0]['transform']['rotation'], rotation)
            self.assertEqual(result['clips'][0]['volume'], 1e-5)

    def test_sequence_limits_and_exact_frame_boundary(self):
        self.plan.pop('audio')
        first = self.plan['clips'][0]
        first.update(source_in=0, duration=1/30)
        self.plan['clips'] = [dict(first, start=i/30) for i in range(256)]
        self.assertEqual(self.validate()['duration_frames'], 256)
        self.plan['clips'].append(dict(first, start=256/30))
        with self.assertRaisesRegex(ValueError, '1..256'):
            self.validate()
        self.plan['clips'] = [dict(first, duration=600)]
        self.assertEqual(self.validate()['duration_frames'], 18000)
        self.plan['clips'][0]['duration'] = 600 + 1/30
        with self.assertRaisesRegex(ValueError, '600 seconds'):
            self.validate()
        for bad_start in [1, 3]:
            self.plan['clips'] = [dict(first, duration=2), dict(first, duration=1, start=bad_start)]
            with self.assertRaisesRegex(ValueError, 'gaps or overlap'):
                self.validate()

    def test_audio_limits_and_placement(self):
        self.plan['audio'] *= 16
        self.assertEqual(len(self.validate()['audio']), 16)
        self.plan['audio'].append(self.plan['audio'][0])
        with self.assertRaisesRegex(ValueError, 'at most 16'):
            self.validate()
        self.plan['audio'] = [dict(self.plan['audio'][0], start=2)]
        with self.assertRaisesRegex(ValueError, 'beyond timeline'):
            self.validate()

    def test_bad_probe_and_media_rejected(self):
        for key, bad in [('duration', 0), ('duration', float('nan')), ('has_audio', 1),
                         ('kind', 'image'), ('fps', True), ('fps', 29.97), ('fps', 60),
                         ('width', False), ('height', 0), ('channels', 1), ('sample_rate', 44100)]:
            def probe(path, key=key, bad=bad):
                return dict(self.probe(path), **{key: bad})
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.validate(probe=probe)
        for bad in [None, [], True]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.validate(probe=lambda path: bad)
        for change in [{'has_audio': False}, {'kind': 'video'}, {'channels': 6}, {'sample_rate': True}]:
            def probe(path):
                original = self.probe(path)
                return dict(original, **change) if Path(path).suffix == '.wav' else original
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(probe=probe)

    def test_local_paths_and_missing_media(self):
        for bad in ['https://example.com/a.mp4', 'file:///C:/a.mp4', '\\\\server\\share\\a.mp4',
                    '//server/share/a.mp4', '\\\\?\\C:\\a.mp4', 'D:relative.mov', 'shot.mov:stream',
                    'shot\n.mov', 'missing.mov']:
            value = copy.deepcopy(self.plan); value['clips'][0]['path'] = bad
            calls = []
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.validate(value, probe=lambda path: calls.append(path))
            self.assertEqual(calls, [])
        self.plan['clips'][0]['path'] = str((self.root / 'shot 空格 & #%.mov').resolve())
        self.assertTrue(Path(self.validate()['clips'][0]['path']).is_absolute())

    def test_duplicate_json_and_nonfinite_constants(self):
        for text in ['{"schema_version":1,"schema_version":1}',
                     '{"canvas":{"fps":30,"fps":30}}',
                     '{"captions":[{"text":"a","text":"b"}]}', '{"x":NaN}', '{"x":Infinity}']:
            self.path.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaises(ValueError):
                compiler.validate_plan(self.path, self.probe)

    def test_normalized_tampering_and_immutable_identity(self):
        original = self.validate()
        fingerprint = compiler.normalized_fingerprint(original)
        cases = [(['duration_frames'], 61), (['clips', 0, 'start_frames'], False),
                 (['clips', 0, 'duration_frames'], 2), (['clips', 0, 'volume'], True),
                 (['clips', 0, 'path'], 'shot 空格 & #%.mov'),
                 (['clips', 0, 'info', 'fps'], 60)]
        for location, bad in cases:
            value = copy.deepcopy(original); target = value
            for part in location[:-1]:
                target = target[part]
            target[location[-1]] = bad
            with self.subTest(location=location), self.assertRaises(ValueError):
                compiler.validate_normalized(value)
        for location, key in [([], 'receipt'), (['clips', 0], 'motion'), (['clips', 0, 'info'], 'trust')]:
            value = copy.deepcopy(original); target = value
            for part in location:
                target = target[part]
            target[key] = True
            with self.subTest(location=location), self.assertRaises(ValueError):
                compiler.normalized_fingerprint(value)
        changed = copy.deepcopy(original); changed['clips'][0]['volume'] = .5
        self.assertNotEqual(fingerprint, compiler.normalized_fingerprint(changed))
        result = compiler.validate_normalized(original)
        result['clips'][0]['info']['duration'] = 1
        self.assertEqual(original['clips'][0]['info']['duration'], 700)

    def test_normalized_shared_source_info_must_match(self):
        self.plan['clips'].append(dict(self.plan['clips'][0], start=2))
        value = self.validate()
        value['clips'][1]['info']['duration'] = 699
        with self.assertRaisesRegex(ValueError, 'inconsistent source info'):
            compiler.validate_normalized(value)

    def test_no_captions_legacy_plan_and_explicit_empty_are_equivalent(self):
        legacy = self.validate()
        self.assertEqual(legacy['captions'], [])
        self.assertEqual(compiler.compile_srt(legacy), '')
        self.plan['captions'] = []
        self.assertEqual(legacy, self.validate())
        self.assertEqual(legacy, compiler.validate_normalized(legacy))

    def test_plain_chinese_two_lines_crlf_and_cross_clip_caption(self):
        self.plan['clips'].append(dict(self.plan['clips'][0], start=2))
        self.plan['captions'] = [
            {'text': '樱花落下\r\n刀刃仍在', 'start': 1.5, 'duration': 1},
            {'text': '再见', 'start': 2.5, 'duration': 1.5},
        ]
        before = copy.deepcopy(self.plan)
        result = self.validate()
        self.assertEqual(result['captions'], [
            {'text': '樱花落下\n刀刃仍在', 'start': 1.5, 'duration': 1,
             'start_frames': 45, 'duration_frames': 30},
            {'text': '再见', 'start': 2.5, 'duration': 1.5,
             'start_frames': 75, 'duration_frames': 45},
        ])
        self.assertEqual(result, compiler.validate_normalized(result))
        self.assertEqual(self.plan, before)
        self.assertEqual(compiler.compile_srt(result),
                         '1\n00:00:01,500 --> 00:00:02,500\n樱花落下\n刀刃仍在\n\n'
                         '2\n00:00:02,500 --> 00:00:04,000\n再见\n\n')

    def test_srt_rounds_absolute_frames_and_does_not_access_media(self):
        self.plan['captions'] = [
            {'text': '第一句', 'start': 1/30, 'duration': 22/30},
            {'text': '第二句', 'start': 33/30, 'duration': 24/30},
        ]
        result = self.validate()
        before = copy.deepcopy(result)
        (self.root / 'shot 空格 & #%.mov').unlink()
        (self.root / 'music.wav').unlink()
        self.assertEqual(compiler.compile_srt(result),
                         '1\n00:00:00,033 --> 00:00:00,767\n第一句\n\n'
                         '2\n00:00:01,100 --> 00:00:01,900\n第二句\n\n')
        self.assertEqual(result, before)

    def test_unicode_line_separator_normalizes_to_lf(self):
        self.plan['captions'] = [{'text': '首行\u2028次行', 'start': 0, 'duration': 1}]
        result = self.validate()
        self.assertEqual(result['captions'][0]['text'], '首行\n次行')
        self.assertEqual(result, compiler.validate_normalized(result))

    def test_caption_text_boundaries_and_styles_rejected(self):
        caption = {'text': '字', 'start': 0, 'duration': 1}
        for text in ['', ' ', '\n字', '字\n', '字\n \n字', '一\n二\n三', '字\r字',
                     '字\t字', '字\x00字', '字\x7f字', '字\x85字', '一\u2028二\u2028三', '字\u2029字',
                     '\ud800', '字' * 501, '<b>字</b>', '<font color="red">字</font>',
                     '{\\an8}字', '{\\i1}字', 1, None]:
            self.plan['captions'] = [dict(caption, text=text)]
            self.path.write_text(json.dumps(self.plan, ensure_ascii=True), encoding='utf-8')
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                compiler.validate_plan(self.path, self.probe)
        self.plan['captions'] = [dict(caption, text='字' * 500)]
        self.assertEqual(len(self.validate()['captions'][0]['text']), 500)
        for field in ['font', 'font_path', 'color', 'position', 'styles']:
            self.plan['captions'] = [dict(caption, **{field: 'unsupported'})]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'unsupported fields'):
                self.validate()
        for field in caption:
            value = dict(caption); value.pop(field)
            self.plan['captions'] = [value]
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, 'missing fields'):
                self.validate()

    def test_caption_placement_limit_and_invalid_frame_times(self):
        caption = {'text': '字', 'start': 0, 'duration': 1}
        for field, bad in [('start', -1), ('start', .01), ('start', True), ('start', float('nan')),
                           ('duration', 0), ('duration', -.1), ('duration', .01),
                           ('duration', True), ('duration', float('inf')), ('duration', 3)]:
            self.plan['captions'] = [dict(caption, **{field: bad})]
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                self.validate()
        for captions in [[dict(caption, start=1.5)],
                         [caption, dict(caption, start=.5)],
                         [dict(caption, start=1), caption]]:
            self.plan['captions'] = captions
            with self.subTest(captions=captions), self.assertRaises(ValueError):
                self.validate()
        for bad in [None, {}, 'text', [None]]:
            self.plan['captions'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.validate()
        self.plan['clips'][0]['duration'] = 600
        self.plan['captions'] = [dict(caption, start=i/30, duration=1/30) for i in range(512)]
        self.assertEqual(len(self.validate()['captions']), 512)
        self.plan['captions'].append(dict(caption, start=512/30, duration=1/30))
        with self.assertRaisesRegex(ValueError, 'at most 512'):
            self.validate()

    def test_normalized_caption_fields_and_fingerprint_are_revalidated(self):
        self.plan['captions'] = [{'text': '首行\n次行', 'start': 1/30, 'duration': 22/30}]
        original = self.validate()
        fingerprint = compiler.normalized_fingerprint(original)
        for field, bad in [('start_frames', True), ('start_frames', 2), ('duration_frames', 23),
                           ('text', '首行\r\n次行'), ('start', 0), ('duration', 3)]:
            value = copy.deepcopy(original); value['captions'][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                compiler.validate_normalized(value)
            with self.assertRaises(ValueError):
                compiler.compile_srt(value)
        for field in ['text', 'start_frames']:
            value = copy.deepcopy(original); value['captions'][0].pop(field)
            with self.subTest(missing=field), self.assertRaises(ValueError):
                compiler.validate_normalized(value)
        value = copy.deepcopy(original); value['captions'][0]['color'] = 'red'
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            compiler.normalized_fingerprint(value)
        value = copy.deepcopy(original); value['captions'][0]['text'] = '新文字'
        self.assertNotEqual(compiler.normalized_fingerprint(value), fingerprint)


if __name__ == '__main__':
    unittest.main()
