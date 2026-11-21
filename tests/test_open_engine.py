"""Direct openedit_engine & jianying_engine compatibility wrapper tests covering open_engine_self_check."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import effectcraft_backend  # noqa: E402
from open_engine_self_check import OpenEngineTests  # noqa: F401, E402


class OverlayCacheTests(unittest.TestCase):
    def test_cache_key_changes_when_footage_content_changes_and_ignores_placement(self):
        with tempfile.TemporaryDirectory(prefix='oe-cache-key-') as tmp:
            root = Path(tmp)
            fake_cli = root / 'effectcraft-cli.exe'
            fake_cli.write_bytes(b'cli-binary')
            img = root / 'badge.png'
            Image.new('RGBA', (64, 64), (255, 0, 0, 255)).save(img)

            spec_a = {'name': 'ov', 'track': 'overlay', 'start': 0.0, 'duration': 0.5,
                      'layers': [{'type': 'footage', 'path': str(img)}]}
            spec_b = {'name': 'ov', 'track': 'V3', 'start': 5.0, 'duration': 0.5,
                      'layers': [{'type': 'footage', 'path': str(img)}]}
            key_a = effectcraft_backend._overlay_cache_key(
                fake_cli, spec_a, width=320, height=180, fps=30, duration=0.5,
                render_video=True, preview_time=0.25, gpu=False,
            )
            key_b = effectcraft_backend._overlay_cache_key(
                fake_cli, spec_b, width=320, height=180, fps=30, duration=0.5,
                render_video=True, preview_time=0.25, gpu=False,
            )
            self.assertIsNotNone(key_a)
            self.assertEqual(key_a, key_b)

            Image.new('RGBA', (64, 64), (0, 255, 0, 255)).save(img)
            key_c = effectcraft_backend._overlay_cache_key(
                fake_cli, spec_a, width=320, height=180, fps=30, duration=0.5,
                render_video=True, preview_time=0.25, gpu=False,
            )
            self.assertNotEqual(key_a, key_c)

    def test_real_effectcraft_overlay_cache_miss_hit_invalidation_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix='oe-real-ov-cache-') as tmp:
            root = Path(tmp)
            cache_dir = root / 'cache'
            spec = {
                'name': 'flash_test',
                'track': 'overlay',
                'start': 0.0,
                'duration': 0.5,
                'width': 320,
                'height': 180,
                'fps': 30,
                'recipe': 'beat_flash',
                'params': {'color': '#FFFFFF', 'peak_opacity': 80, 'glow': False},
            }
            with patch.dict(os.environ, {'OPENEDIT_OVERLAY_CACHE_DIR': str(cache_dir)}):
                first = effectcraft_backend.build_overlay(spec, root / 'out_1')
                self.assertEqual(first['status'], 'passed')
                self.assertFalse(first['cache']['hit'])
                self.assertTrue(first['cache']['stored'])
                self.assertEqual(first['overlay_video']['pix_fmt'], 'yuva444p12le')
                key = first['cache']['key']

                # Second build with moved start/track must hit cache and match ProRes SHA-256.
                moved_spec = {**spec, 'track': 'V3', 'start': 4.5}
                second = effectcraft_backend.build_overlay(moved_spec, root / 'out_2')
                self.assertTrue(second['cache']['hit'])
                self.assertEqual(second['cache']['key'], key)
                self.assertEqual(second['overlay_video']['sha256'], first['overlay_video']['sha256'])
                self.assertTrue(Path(second['overlay_video']['path']).is_file())

                # Bypass with use_cache=False.
                bypassed = effectcraft_backend.build_overlay(spec, root / 'out_nocache', use_cache=False)
                self.assertEqual(bypassed['cache'], {'hit': False, 'stored': False, 'reason': 'disabled'})

                # Corrupted cache entry triggers safe rebuild without raising.
                entry_mov = cache_dir / key[:2] / key / 'overlay.mov'
                entry_mov.write_bytes(b'corrupted')
                rebuilt = effectcraft_backend.build_overlay(spec, root / 'out_rebuilt')
                self.assertFalse(rebuilt['cache']['hit'])
                self.assertEqual(rebuilt['overlay_video']['strict_decode'], 'passed')

                # Parameter change invalidates cache key cleanly.
                changed_spec = {**spec, 'params': {'color': '#38BDF8', 'peak_opacity': 80, 'glow': False}}
                missed = effectcraft_backend.build_overlay(changed_spec, root / 'out_changed')
                self.assertFalse(missed['cache']['hit'])
                self.assertNotEqual(missed['cache']['key'], key)

    def test_new_overlay_recipes_real_effectcraft_alpha_renders(self):
        with tempfile.TemporaryDirectory(prefix='oe-new-recipes-') as tmp:
            root = Path(tmp)
            # 1. pip_card: interior must stay 100% transparent (alpha == 0) while corner header + border stay opaque
            pip_spec = {
                'name': 'pip_real', 'width': 640, 'height': 360, 'fps': 30, 'duration': 1.0,
                'recipe': 'pip_card',
                'params': {'title': '演示窗口', 'corner': 'bottom_right', 'width_ratio': 0.4, 'border_width': 8},
            }
            pip_res = effectcraft_backend.build_overlay(pip_spec, root / 'pip', render_video=False, preview_time=0.5, use_cache=False)
            with Image.open(pip_res['preview_png']).convert('RGBA') as im:
                # Center of whole 640x360 frame (left of bottom_right PiP) and interior of PiP box must be transparent
                self.assertEqual(im.getpixel((100, 180))[3], 0)
                margin_x, margin_y = round(640 * 0.055), round(360 * 0.065)
                box_w = round(640 * 0.4)
                box_h = round(box_w / 1.7778)
                pip_cx = round(640 - margin_x - box_w * 0.5)
                pip_cy = round(360 - margin_y - box_h * 0.5)
                self.assertEqual(im.getpixel((pip_cx, pip_cy))[3], 0)
                self.assertGreater(im.getpixel((pip_cx, round(pip_cy + box_h * 0.5)))[3], 200)

            # 2. split_screen_frame: divider bar opaque while left & right panel interiors remain alpha == 0
            split_spec = {
                'name': 'split_real', 'width': 640, 'height': 360, 'fps': 30, 'duration': 1.0,
                'recipe': 'split_screen_frame',
                'params': {'layout': 'vertical_2', 'divider_width': 8, 'labels': ['A面', 'B面']},
            }
            split_res = effectcraft_backend.build_overlay(split_spec, root / 'split', render_video=False, preview_time=0.6, use_cache=False)
            with Image.open(split_res['preview_png']).convert('RGBA') as im:
                self.assertEqual(im.getpixel((160, 220))[3], 0)
                self.assertEqual(im.getpixel((480, 220))[3], 0)
                self.assertGreater(im.getpixel((320, 220))[3], 200)

            # 3. chapter_opener: renders RGBA preview cleanly
            chap_spec = {
                'name': 'chap_real', 'width': 640, 'height': 360, 'fps': 30, 'duration': 1.2,
                'recipe': 'chapter_opener',
                'params': {'badge': 'PART 01', 'title': '开源剪辑配方', 'subtitle': '透明叠层实测'},
            }
            chap_res = effectcraft_backend.build_overlay(chap_spec, root / 'chap', render_video=False, preview_time=0.6, use_cache=False)
            with Image.open(chap_res['preview_png']).convert('RGBA') as im:
                self.assertEqual(im.getpixel((10, 10))[3], 0)
                self.assertGreater(im.getpixel((320, 180))[3], 150)

            # 4. wipe_transition: ProRes 4444 yuva444p12le + transparent edges + opaque midpoint
            wipe_spec = {
                'name': 'wipe_real', 'width': 320, 'height': 180, 'fps': 30, 'duration': 0.8,
                'recipe': 'wipe_transition',
                'params': {'direction': 'left', 'angle_offset': 0.0, 'peak_hold': 0.2},
            }
            w_start = effectcraft_backend.build_overlay(wipe_spec, root / 'wipe_s', render_video=False, preview_time=0.0, use_cache=False)
            w_mid = effectcraft_backend.build_overlay(wipe_spec, root / 'wipe_m', render_video=True, preview_time=0.4, use_cache=False)
            w_end = effectcraft_backend.build_overlay(wipe_spec, root / 'wipe_e', render_video=False, preview_time=0.79, use_cache=False)
            self.assertEqual(w_mid['overlay_video']['pix_fmt'], 'yuva444p12le')
            with Image.open(w_start['preview_png']) as im_s, Image.open(w_mid['preview_png']) as im_m, Image.open(w_end['preview_png']) as im_e:
                self.assertEqual(im_s.getpixel((160, 90))[3], 0)
                self.assertEqual(im_m.getpixel((160, 90))[3], 255)
                self.assertEqual(im_e.getpixel((160, 90))[3], 0)


class CompatibilityAliasTests(unittest.TestCase):
    def test_jianying_engine_cli_and_package_aliases_match_openedit_engine(self):
        import openedit_engine
        import jianying_engine
        from openedit_engine import api as oe_api, cli as oe_cli, mcp_server as oe_mcp
        from jianying_engine import api as jy_api, cli as jy_cli, mcp_server as jy_mcp

        self.assertEqual(jianying_engine.__version__, openedit_engine.__version__)
        self.assertIs(jy_api, oe_api)
        self.assertIs(jy_cli, oe_cli)
        self.assertIs(jy_mcp, oe_mcp)

        oe_ver = subprocess.run([sys.executable, '-m', 'openedit_engine', '--version'],
                                capture_output=True, text=True, check=True)
        jy_ver = subprocess.run([sys.executable, '-m', 'jianying_engine', '--version'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(oe_ver.stdout.strip(), openedit_engine.__version__)
        self.assertEqual(jy_ver.stdout.strip(), openedit_engine.__version__)


if __name__ == '__main__':
    unittest.main()
