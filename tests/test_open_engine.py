"""Direct openedit_engine tests covering open_engine_self_check."""
import importlib.util
import json
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
import hyperframes_backend  # noqa: E402
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

            key_draft = effectcraft_backend._overlay_cache_key(
                fake_cli, spec_a, width=320, height=180, fps=30, duration=0.5,
                render_video=True, preview_time=0.25, gpu=False, resolution='half', quality='draft',
            )
            self.assertNotEqual(key_c, key_draft)

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

    def test_hero_recipes_path_resolution_provenance_and_real_render(self):
        import file_hash
        import overlay_recipes
        import runtime_core as core

        self.assertEqual(len(overlay_recipes.list_recipes()), 13)
        with tempfile.TemporaryDirectory(prefix='oe-hero-recipes-') as tmp:
            root = Path(tmp)
            a = root / 'a.png'
            b = root / 'b.png'
            fg = root / 'fg.png'
            Image.new('RGB', (320, 180), (210, 40, 40)).save(a)
            Image.new('RGB', (320, 180), (40, 120, 210)).save(b)
            im = Image.new('RGBA', (320, 180), (0, 0, 0, 0))
            for x in range(110, 210):
                for y in range(40, 140):
                    im.putpixel((x, y), (240, 200, 80, 255))
            im.save(fg)

            # Relative media path resolution and provenance tracking inside hero recipes
            plan_path = root / 'plan.json'
            plan_data = {
                'schema_version': 1,
                'name': 'hero-recipe-test',
                'canvas': {'width': 320, 'height': 180, 'fps': 30},
                'clips': [{'path': 'a.png', 'start': 0.0, 'duration': 1.0, 'volume': 0}],
                'overlays': [
                    {'name': 'hero_whip', 'track': 'overlay', 'start': 0.0, 'duration': 0.6,
                     'recipe': 'whip_transition', 'params': {'clip_a': 'a.png', 'clip_b': 'b.png'}},
                    {'name': 'hero_3d', 'track': 'V3', 'start': 0.0, 'duration': 0.6,
                     'recipe': 'parallax_3d_scene', 'params': {'background': 'a.png', 'foreground': 'fg.png', 'clones': True, 'orbit_rings': True}},
                ],
            }
            core.write_json(plan_path, plan_data)
            # Corrupt sidecar on b.png must be rejected by provenance(plan) via recipe media paths
            sidecar_b = root / 'b.png.provenance.json'
            core.write_json(sidecar_b, {'license_verification': 'agent_review_recorded', 'review_note': 'ok', 'sha256': '0' * 64})
            norm_plan, _, _ = core.validate_plan(plan_path)
            self.assertEqual(norm_plan['overlays'][0]['params']['clip_b'], str(b.resolve()))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                core.provenance(norm_plan)

            core.write_json(sidecar_b, {'license_verification': 'agent_review_recorded', 'review_note': 'ok', 'sha256': file_hash.sha256(b)})
            records = core.provenance(norm_plan)
            self.assertTrue(any(r.get('sha256') == file_hash.sha256(b) and r.get('review_note') == 'ok' for r in records))
            self.assertTrue(any(r.get('local_path') == str(fg.resolve()) for r in records))

            # Real EffectCraft ProRes 4444 render for whip_transition + impact_punch
            whip_out = effectcraft_backend.build_overlay(norm_plan['overlays'][0], root / 'whip_out', render_video=True, use_cache=False)
            self.assertEqual(whip_out['overlay_video']['strict_decode'], 'passed')
            self.assertEqual(whip_out['overlay_video']['pix_fmt'], 'yuva444p12le')

            punch_spec = {
                'name': 'hero_punch', 'width': 320, 'height': 180, 'fps': 30, 'duration': 0.6,
                'recipe': 'impact_punch', 'params': {'clip': str(a), 'peak_time': 0.25},
            }
            punch_out = effectcraft_backend.build_overlay(punch_spec, root / 'punch_out', render_video=True, use_cache=False)
            self.assertEqual(punch_out['overlay_video']['strict_decode'], 'passed')


class OpenEditCliVersionTests(unittest.TestCase):
    def test_openedit_engine_version_and_no_legacy_shim_package(self):
        import openedit_engine

        self.assertIsNone(importlib.util.find_spec('jianying_engine'))
        oe_ver = subprocess.run([sys.executable, '-m', 'openedit_engine', '--version'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(oe_ver.stdout.strip(), openedit_engine.__version__)


class DraftPreviewFlagsTests(unittest.TestCase):
    def test_filmcraft_export_and_build_draft_flags_forward_cleanly(self):
        import filmcraft_backend
        recorded = []

        def fake_run(cmd, timeout=120):
            recorded.append(list(cmd))
            if 'export' in cmd:
                dst = Path(cmd[cmd.index('export') + 1])
                dst.write_bytes(b'fake-mp4')
            return type('Res', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()

        with tempfile.TemporaryDirectory(prefix='oe-draft-exp-') as tmp:
            root = Path(tmp)
            proj = root / 'sample.fcproj'
            proj.write_text('{}', encoding='utf-8')
            out_mp4 = root / 'draft.mp4'
            with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                 patch('filmcraft_backend.core.run', side_effect=fake_run), \
                 patch('filmcraft_backend.core.probe', return_value={'has_video': True, 'has_audio': True}):
                rep = filmcraft_backend.export_project(proj, out_mp4, draft=True, gpu_rendering='off')
            self.assertTrue(rep['draft'])
            self.assertEqual(rep['scale'], 0.5)
            self.assertEqual(rep['gpu_rendering'], 'off')
            export_cmd = recorded[0]
            self.assertIn('--scale', export_cmd)
            self.assertEqual(export_cmd[export_cmd.index('--scale') + 1], '0.5')
            self.assertEqual(export_cmd[export_cmd.index('--gpu-rendering') + 1], 'off')
            settings = json.loads(export_cmd[export_cmd.index('--settings') + 1])
            self.assertEqual(settings['crf'], 28)


class HyperFramesOverlayTests(unittest.TestCase):
    def test_hyperframes_recipe_validation_isolation_and_cache_key(self):
        import runtime_core as core

        self.assertEqual(len(hyperframes_backend.list_recipes()), 4)
        with tempfile.TemporaryDirectory(prefix='oe-hf-val-') as tmp:
            root = Path(tmp)
            bg = root / 'bg.png'
            Image.new('RGB', (640, 360), (20, 60, 120)).save(bg)

            valid_specs = [
                {
                    'name': 'hf_bar', 'backend': 'hyperframes', 'track': 'overlay', 'start': 0.0, 'duration': 2.0,
                    'recipe': 'kpi_bar_chart',
                    'params': {
                        'title': '季度转化增长', 'kpi_target': 248,
                        'bars': [{'label': 'W1', 'value': 40}, {'label': 'W2', 'value': 248, 'highlight': True}],
                    },
                },
                {
                    'name': 'hf_cnt', 'backend': 'hyperframes', 'track': 'V3', 'start': 2.0, 'duration': 1.5,
                    'recipe': 'kpi_counter_banner',
                    'params': {'metrics': [{'label': '曝光', 'target': 520, 'suffix': '万'}]},
                },
                {
                    'name': 'hf_cmp', 'backend': 'hyperframes', 'track': 'V4', 'start': 3.5, 'duration': 1.5,
                    'recipe': 'comparison_table',
                    'params': {'title': '版本对比', 'rows': [{'label': '画质', 'left': '720p', 'right': '4K'}, {'label': 'Alpha', 'left': '无', 'right': '4444'}]},
                },
                {
                    'name': 'hf_mile', 'backend': 'hyperframes', 'track': 'V5', 'start': 5.0, 'duration': 1.5,
                    'recipe': 'milestone_progress',
                    'params': {'title': '达标进度', 'steps': [{'label': '初筛', 'value': '80%', 'percent': 80}, {'label': '复核', 'value': '98%', 'percent': 98}]},
                },
            ]
            plan_path = root / 'plan.json'
            core.write_json(plan_path, {
                'schema_version': 2,
                'name': 'hf-val',
                'canvas': {'width': 640, 'height': 360, 'fps': 30},
                'clips': [{'path': str(bg), 'start': 0.0, 'duration': 6.5, 'volume': 0.0}],
                'overlays': valid_specs,
            })
            norm_plan, _, _ = core.validate_plan(plan_path)
            self.assertEqual(len(norm_plan['overlays']), 4)

            # Reject cross-backend fields and unpermitted external network URLs.
            bad_cases = [
                ({'name': 'x', 'start': 0, 'duration': 1, 'backend': 'hyperframes', 'recipe': 'kpi_bar_chart', 'layers': []}, 'only supported with backend="effectcraft"'),
                ({'name': 'x', 'start': 0, 'duration': 1, 'backend': 'effectcraft', 'recipe': 'beat_flash', 'html': '<div></div>'}, 'only supported with backend="hyperframes"'),
                ({'name': 'x', 'start': 0, 'duration': 1, 'backend': 'hyperframes', 'html': '<script src="https://cdn.example.com/gsap.js"></script>'}, 'external HTTP'),
                ({'name': 'x', 'start': 0, 'duration': 1, 'backend': 'hyperframes', 'recipe': 'kpi_bar_chart', 'params': {'title': 'A', 'kpi_target': 10, 'bars': [{'label': 'only1', 'value': 1}]}}, '2\\.\\.6'),
            ]
            for bad_ov, pattern in bad_cases:
                bad_plan = root / 'bad.json'
                core.write_json(bad_plan, {
                    'schema_version': 2, 'name': 'bad', 'canvas': {'width': 640, 'height': 360, 'fps': 30},
                    'clips': [{'path': str(bg), 'start': 0.0, 'duration': 1.0, 'volume': 0.0}],
                    'overlays': [bad_ov],
                })
                with self.assertRaisesRegex(ValueError, pattern):
                    core.validate_plan(bad_plan)

            # allow_network=true permits explicit external URLs during validation.
            net_ok = hyperframes_backend.validate_overlay_spec({
                'name': 'net', 'duration': 1.0, 'backend': 'hyperframes',
                'html': '<script src="https://cdn.example.com/gsap.js"></script>', 'allow_network': True,
            })
            self.assertTrue(net_ok['allow_network'])

            # Cache key ignores start/track and changes when params or draft quality change.
            fake_cli = root / 'hyperframes.mjs'
            fake_cli.write_text('console.log(1)', encoding='utf-8')
            fake_font = root / 'cjk.ttf'
            fake_font.write_bytes(b'fontdata')
            fake_gsap = root / 'gsap.min.js'
            fake_gsap.write_text('window.gsap={};', encoding='utf-8')
            sp_a = norm_plan['overlays'][0]
            sp_b = {**sp_a, 'track': 'V9', 'start': 3.0}
            files_a = hyperframes_backend._build_recipe_files(sp_a['recipe'], sp_a['params'], width=640, height=360, fps=30, duration=2.0)
            ka = hyperframes_backend._overlay_cache_key(
                fake_cli, sp_a, width=640, height=360, fps=30, duration=2.0, render_video=True,
                preview_time=1.0, gpu=False, quality='looks', generated_files=files_a, font_file=fake_font, gsap_file=fake_gsap,
            )
            kb = hyperframes_backend._overlay_cache_key(
                fake_cli, sp_b, width=640, height=360, fps=30, duration=2.0, render_video=True,
                preview_time=1.0, gpu=False, quality='looks', generated_files=files_a, font_file=fake_font, gsap_file=fake_gsap,
            )
            kd = hyperframes_backend._overlay_cache_key(
                fake_cli, sp_a, width=640, height=360, fps=30, duration=2.0, render_video=True,
                preview_time=1.0, gpu=False, quality='draft', generated_files=files_a, font_file=fake_font, gsap_file=fake_gsap,
            )
            self.assertEqual(ka, kb)
            self.assertNotEqual(ka, kd)

    def test_real_hyperframes_overlay_cache_and_filmcraft_build(self):
        import numpy as np
        import open_engine
        import runtime_paths

        if runtime_paths.discover_hyperframes_cli() is None or runtime_paths.discover_cjk_font_file() is None:
            self.skipTest('HyperFrames CLI or static CJK font unavailable on host')

        with tempfile.TemporaryDirectory(prefix='oe-hf-e2e-') as tmp:
            root = Path(tmp)
            cache_dir = root / 'cache'
            bg = root / 'bg.png'
            Image.new('RGB', (640, 360), (20, 140, 210)).save(bg)

            ov_spec = {
                'name': 'ad_kpi',
                'backend': 'hyperframes',
                'track': 'overlay',
                'start': 0.0,
                'duration': 0.6,
                'recipe': 'kpi_bar_chart',
                'params': {
                    'title': '转化跃升实测',
                    'kpi_label': '线索提升',
                    'kpi_target': 248,
                    'bars': [
                        {'label': 'W1', 'value': 50, 'display': '+50%'},
                        {'label': 'W2', 'value': 130, 'display': '+130%'},
                        {'label': 'W3', 'value': 248, 'display': '+248%', 'highlight': True},
                    ],
                },
            }
            plan_path = root / 'plan.json'
            plan_path.write_text(json.dumps({
                'schema_version': 2,
                'name': 'hf-filmcraft-e2e',
                'canvas': {'width': 640, 'height': 360, 'fps': 30},
                'clips': [{'path': str(bg), 'start': 0.0, 'duration': 0.6, 'volume': 0.0}],
                'overlays': [ov_spec],
                'export': {'format': 'h264', 'crf': 24, 'hardware_encoding': 'off', 'audio': False, 'burn_captions': False},
            }, ensure_ascii=False), encoding='utf-8')

            with patch.dict(os.environ, {'OPENEDIT_OVERLAY_CACHE_DIR': str(cache_dir)}):
                build_dir = root / 'build_1'
                export_mp4 = root / 'final.mp4'
                report = open_engine.build(
                    str(plan_path), str(build_dir),
                    export_path=str(export_mp4),
                    preview_times=[0.3],
                    draft=True,
                )
                self.assertEqual(report['status'], 'passed')
                self.assertEqual(report['export']['strict_decode'], 'passed')
                self.assertEqual(len(report['overlays']), 1)
                ov_rep = report['overlays'][0]
                self.assertEqual(ov_rep['backend'], 'hyperframes')
                self.assertEqual(ov_rep['overlay_video']['pix_fmt'], 'yuva444p12le')
                self.assertFalse(ov_rep['cache']['hit'])

                with Image.open(ov_rep['preview_png']) as ov_im:
                    arr = np.array(ov_im.convert('RGBA'))
                    self.assertEqual(int(arr[:, :200, 3].max()), 0)
                    self.assertGreater(int(arr[180, 480, 3]), 200)

                # Verify FilmCraft preview left transparent region preserves blue background rather than black occlusion.
                with Image.open(report['previews'][0]['path']) as comp_im:
                    px = comp_im.convert('RGB').getpixel((80, 180))
                    self.assertGreater(px[2], 170, f'Expected blue background passthrough, got {px}')

                # Second build hits HyperFrames overlay cache.
                report2 = open_engine.build(str(plan_path), str(root / 'build_2'), draft=True)
                self.assertTrue(report2['overlays'][0]['cache']['hit'])
                self.assertEqual(report2['overlays'][0]['overlay_video']['sha256'], ov_rep['overlay_video']['sha256'])

if __name__ == '__main__':
    unittest.main()
