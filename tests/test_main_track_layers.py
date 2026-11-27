"""Layer order is honored bottom-to-top and a renamed main track is still audited (plan main_track)."""
import contextlib
import io
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
import filmcraft_backend  # noqa: E402
import audit_project  # noqa: E402
import music_fit  # noqa: E402


class TrackMappingTests(unittest.TestCase):
    def test_layer_order_puts_background_under_video(self):
        plan = {'clips': [{'track': 'video'}, {'track': 'bg'}, {'track': 'glow'}],
                'layer_order': ['bg', 'video', 'glow']}
        v_map, _ = filmcraft_backend._plan_track_mapping(plan, [])
        self.assertEqual(v_map, {'bg': 'V1', 'video': 'V2', 'glow': 'V3'})

    def test_renamed_main_track_gets_no_empty_video_lane(self):
        plan = {'clips': [{'track': 'bg'}, {'track': 'main'}], 'layer_order': ['bg', 'main']}
        v_map, _ = filmcraft_backend._plan_track_mapping(plan, [])
        self.assertEqual(v_map, {'bg': 'V1', 'main': 'V2'})

    def test_default_mode_keeps_video_at_bottom(self):
        plan = {'clips': [{'track': 'b_roll'}, {}]}
        v_map, _ = filmcraft_backend._plan_track_mapping(plan, [{'track': 'overlay'}])
        self.assertEqual(v_map, {'video': 'V1', 'b_roll': 'V2', 'overlay': 'V3'})

    def test_overlay_only_track_still_mapped_under_layer_order(self):
        plan = {'clips': [{'track': 'main'}], 'layer_order': ['main', 'fx']}
        v_map, _ = filmcraft_backend._plan_track_mapping(plan, [{'track': 'fx'}])
        self.assertEqual(v_map, {'main': 'V1', 'fx': 'V2'})


class MainTrackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='oe-main-track-')
        self.root = Path(self._tmp.name)
        Image.new('RGB', (320, 180), (40, 80, 120)).save(self.root / 'a.png')
        Image.new('RGB', (320, 180), (200, 60, 60)).save(self.root / 'b.png')

    def tearDown(self):
        self._tmp.cleanup()

    def write_plan(self, name, **extra):
        clips = [{'path': 'a.png', 'track': 'bg', 'start': 0, 'duration': 2.0}]
        clips += [{'path': 'a.png' if i % 2 else 'b.png', 'track': 'main', 'start': i * 0.25, 'duration': 0.25}
                  for i in range(8)]
        plan = {'schema_version': 2, 'name': name, 'canvas': {'width': 320, 'height': 180, 'fps': 30},
                'clips': clips, 'layer_order': ['bg', 'main']}
        plan.update(extra)
        path = self.root / f'{name}.json'
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return path

    def audit(self, plan_path, out_name):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit_project.main(['--plan', str(plan_path), '--out', str(self.root / out_name)])
        return code, json.loads(buf.getvalue())

    def test_validate_accepts_main_track_and_rejects_unknown(self):
        plan, _, _ = core.validate_plan(self.write_plan('ok', main_track='main'))
        self.assertEqual(plan['main_track'], 'main')
        with self.assertRaisesRegex(ValueError, 'main_track'):
            core.validate_plan(self.write_plan('bad', main_track='nope'))

    def test_audit_reads_cuts_from_main_track(self):
        code, rep = self.audit(self.write_plan('named', main_track='main'), 'audit-named')
        self.assertEqual(code, 0, rep)
        self.assertEqual(rep['connection_qc']['cuts'], 7, rep['connection_qc'])
        self.assertFalse(any('has no clips' in r for r in rep.get('attention_reasons', [])), rep)

    def test_audit_flags_empty_default_main_track(self):
        code, rep = self.audit(self.write_plan('unnamed'), 'audit-unnamed')
        self.assertEqual(code, 0, rep)
        self.assertEqual(rep['status'], 'attention')
        self.assertTrue(any('main_track' in r for r in rep['attention_reasons']), rep['attention_reasons'])

    def test_music_fit_plan_track_defaults_to_main_track(self):
        info = music_fit._plan_cuts(self.write_plan('mf', main_track='main'), None, 2.0)
        self.assertEqual(len(info['cuts']), 7)  # main track ends with the video, so its end is not a cut


if __name__ == '__main__':
    unittest.main()
