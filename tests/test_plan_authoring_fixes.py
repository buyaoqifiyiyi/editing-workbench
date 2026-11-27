"""Plan validation fixes: caption stroke alias/auto width, mask hold keys, and auto track lanes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import runtime_core as core  # noqa: E402


class PlanAuthoringFixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='oe-plan-fix-')
        self.root = Path(self._tmp.name)
        Image.new('RGB', (320, 240), (40, 80, 120)).save(self.root / 'bg.png')
        Image.new('RGB', (64, 64), (255, 255, 255)).save(self.root / 'white.png')

    def tearDown(self):
        self._tmp.cleanup()

    def validate(self, **extra):
        plan = {'schema_version': 2, 'name': 'fix', 'canvas': {'width': 320, 'height': 240, 'fps': 30},
                'clips': [{'path': 'bg.png', 'start': 0, 'duration': 3}]}
        for key, value in extra.items():
            if key == 'clips':
                plan['clips'] += value
            else:
                plan[key] = value
        path = self.root / 'plan.json'
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return core.validate_plan(path)

    # ---- captions ----
    def test_stroke_false_alias_disables_border(self):
        plan, _, _ = self.validate(captions=[{'text': '片名', 'start': 0, 'duration': 1, 'stroke': False}])
        self.assertIs(plan['captions'][0]['border'], False)
        self.assertNotIn('stroke_width', plan['captions'][0])

    def test_conflicting_border_and_stroke_refused(self):
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.validate(captions=[{'text': 'x', 'start': 0, 'duration': 1, 'stroke': False, 'border': True}])

    def test_default_stroke_width_scales_with_size(self):
        plan, _, _ = self.validate(captions=[
            {'text': '小', 'start': 0, 'duration': 1, 'size': 24},
            {'text': '大', 'start': 1, 'duration': 1, 'size': 96},
        ])
        self.assertEqual(plan['captions'][0]['stroke_width'], 1.9)
        self.assertEqual(plan['captions'][1]['stroke_width'], 4.0)

    def test_heavy_stroke_and_translucent_outline_warn(self):
        _, _, warnings = self.validate(captions=[
            {'text': '片名', 'start': 0, 'duration': 1, 'size': 24, 'stroke_width': 4, 'stroke_color': '#00000066'},
        ])
        self.assertTrue(any('heavy for size 24' in w for w in warnings), warnings)
        self.assertTrue(any('alpha is not supported' in w for w in warnings), warnings)

    def test_explicit_reasonable_stroke_is_untouched_and_quiet(self):
        plan, _, warnings = self.validate(captions=[
            {'text': '对白', 'start': 0, 'duration': 1, 'size': 46, 'stroke_width': 2},
        ])
        self.assertEqual(plan['captions'][0]['stroke_width'], 2)
        self.assertFalse([w for w in warnings if 'captions' in w], warnings)

    # ---- mask hold keys ----
    def test_mask_hold_expands_to_one_frame_before_next_key(self):
        mask = {'shape': 'rectangle', 'center': [160, 120], 'size': [80, 60], 'keyframes': [
            {'time': 0.0, 'size': [80, 60], 'interpolation': 'hold'},
            {'time': 0.5, 'center': [160, 110], 'size': [160, 120], 'interpolation': 'hold'},
            {'time': 1.0, 'size': [300, 220]},
        ]}
        plan, _, _ = self.validate(clips=[{'path': 'bg.png', 'track': 'p', 'start': 0, 'duration': 2, 'mask': mask}])
        keys = plan['clips'][1]['mask']['keyframes']
        self.assertEqual([round(k['time'], 4) for k in keys], [0.0, 0.4667, 0.5, 0.9667, 1.0])
        self.assertEqual(keys[1]['size'], [80, 60])
        self.assertEqual(keys[1]['center'], [160, 120])
        self.assertEqual(keys[3]['size'], [160, 120])
        self.assertEqual(keys[3]['center'], [160, 110])

    def test_mask_hold_snaps_to_timeline_frame_grid_for_off_grid_keys(self):
        # Clip starts at 0.1s and the next key sits at local 0.38s (absolute 0.48s = frame 14.4 @30fps).
        # The hold must last through absolute frame 14 (local 0.3667s); a naive "next key - 1 frame"
        # (local 0.3467s) would leave frame 14 rendering a half-grown in-between shape.
        mask = {'shape': 'rectangle', 'center': [160, 120], 'size': [80, 60], 'keyframes': [
            {'time': 0.0, 'size': [80, 60], 'interpolation': 'hold'},
            {'time': 0.38, 'size': [160, 120]},
        ]}
        plan, _, _ = self.validate(clips=[{'path': 'bg.png', 'track': 'p', 'start': 0.1, 'duration': 1, 'mask': mask}])
        keys = plan['clips'][1]['mask']['keyframes']
        self.assertEqual([round(k['time'], 4) for k in keys], [0.0, 0.3667, 0.38])
        self.assertEqual(keys[1]['size'], [80, 60])
        for frame in range(3, 33):  # every rendered frame inside the clip shows only held or next shape
            local = frame / 30 - 0.1
            if keys[1]['time'] + 1e-6 < local < keys[2]['time'] - 1e-6:
                self.fail(f'frame {frame} would render an interpolated mask')

    def test_mask_rejects_easing_it_cannot_render(self):
        mask = {'shape': 'ellipse', 'center': [160, 120], 'size': [80, 80],
                'keyframes': [{'time': 0, 'size': [80, 80], 'interpolation': 'easeOut'}]}
        with self.assertRaisesRegex(ValueError, 'linear or hold'):
            self.validate(clips=[{'path': 'bg.png', 'track': 'p', 'start': 0, 'duration': 1, 'mask': mask}])

    def test_mask_without_hold_is_unchanged(self):
        mask = {'shape': 'rectangle', 'center': [160, 120], 'size': [80, 60],
                'keyframes': [{'time': 0, 'size': [80, 60]}, {'time': 1, 'size': [200, 150]}]}
        plan, _, _ = self.validate(clips=[{'path': 'bg.png', 'track': 'p', 'start': 0, 'duration': 2, 'mask': mask}])
        self.assertEqual(len(plan['clips'][1]['mask']['keyframes']), 2)

    # ---- auto tracks ----
    def test_auto_tracks_pack_into_fewest_lanes(self):
        flashes = [{'path': 'white.png', 'track': 'auto:flash', 'start': s, 'duration': 0.3}
                   for s in (0.0, 0.1, 0.2, 0.3, 0.6)]
        plan, _, _ = self.validate(clips=flashes)
        lanes = [c['track'] for c in plan['clips'][1:]]
        self.assertEqual(lanes, ['flash1', 'flash2', 'flash3', 'flash1', 'flash1'])

    def test_auto_tracks_expand_layer_order_and_skip_taken_names(self):
        clips = [{'path': 'bg.png', 'track': 'flash1', 'start': 0, 'duration': 1},
                 {'path': 'white.png', 'track': 'auto:flash', 'start': 0, 'duration': 1},
                 {'path': 'white.png', 'track': 'auto:flash', 'start': 0.5, 'duration': 1}]
        plan, _, _ = self.validate(clips=clips, layer_order=['video', 'flash1', 'auto:flash'])
        self.assertEqual(plan['layer_order'], ['video', 'flash1', 'flash2', 'flash3'])

    def test_auto_tracks_require_layer_order_entry_when_order_given(self):
        clips = [{'path': 'white.png', 'track': 'auto:flash', 'start': 0, 'duration': 1}]
        with self.assertRaisesRegex(ValueError, 'auto:flash'):
            self.validate(clips=clips, layer_order=['video'])

    def test_bad_auto_track_name_refused(self):
        with self.assertRaisesRegex(ValueError, 'auto:<prefix>'):
            self.validate(clips=[{'path': 'white.png', 'track': 'auto:', 'start': 0, 'duration': 1}])


if __name__ == '__main__':
    unittest.main()
