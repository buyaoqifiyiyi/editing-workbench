"""Regression check for hyperframes_backend: 4 data-motion recipes, ProRes 4444 alpha, and cache reuse."""
import argparse
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import hyperframes_backend
import runtime_core as core


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    cache_dir = root / 'cache'

    # 1. Validate & expand all 4 data-motion recipes before rendering.
    specs = {
        'kpi_bar_chart': {
            'name': 'hf_bar_test',
            'duration': 0.8,
            'width': 640,
            'height': 360,
            'fps': 30,
            'backend': 'hyperframes',
            'recipe': 'kpi_bar_chart',
            'params': {
                'title': '转化效率实测',
                'kpi_label': '线索转化提升',
                'kpi_target': 248,
                'bars': [
                    {'label': 'W1', 'value': 42, 'display': '+42%'},
                    {'label': 'W2', 'value': 120, 'display': '+120%'},
                    {'label': 'W3', 'value': 248, 'display': '+248%', 'highlight': True},
                ],
            },
        },
        'kpi_counter_banner': {
            'name': 'hf_banner_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'kpi_counter_banner',
            'params': {
                'title': '季度核心指标',
                'metrics': [
                    {'label': '营收增长', 'target': 185, 'prefix': '+', 'suffix': '%'},
                    {'label': '交付周期', 'target': 2.4, 'suffix': '天', 'decimals': 1},
                ],
            },
        },
        'comparison_table': {
            'name': 'hf_cmp_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'comparison_table',
            'params': {
                'title': '引擎能力对比',
                'rows': [
                    {'label': '渲染格式', 'left': 'WebM 无透明', 'right': 'ProRes 4444 RGBA'},
                    {'label': '字体方案', 'left': '外网拉取', 'right': '纯离线本地字体'},
                ],
            },
        },
        'milestone_progress': {
            'name': 'hf_mile_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'milestone_progress',
            'params': {
                'title': '自动化阶段达成',
                'steps': [
                    {'label': '内容识别', 'value': '99.2%', 'percent': 99},
                    {'label': '节拍对齐', 'value': '100%', 'percent': 100},
                ],
            },
        },
        'viewfinder_hud': {
            'name': 'hf_hud_test',
            'duration': 0.8,
            'width': 640,
            'height': 360,
            'fps': 30,
            'backend': 'hyperframes',
            'recipe': 'viewfinder_hud',
            'params': {'theme': 'editorial_scrapbook', 'top_right': '4:3 · 60FPS'},
        },
        'polaroid_grid': {
            'name': 'hf_pol_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'polaroid_grid',
            'params': {'theme': 'editorial_scrapbook', 'highlight_index': 0, 'cards': [{'label': 'FR.01'}, {'label': 'FR.02'}]},
        },
        'filmstrip_reel': {
            'name': 'hf_reel_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'filmstrip_reel',
            'params': {'frames': [{'label': '1A'}, {'label': '2A'}, {'label': '3A'}]},
        },
        'text_matte_dissolve': {
            'name': 'hf_txt_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'text_matte_dissolve',
            'params': {'text': 'EMILIA', 'subtitle': 'ROYAL SELECTION'},
        },
        'ui_flow_stage': {
            'name': 'hf_uiflow_test',
            'duration': 0.8,
            'width': 640,
            'height': 360,
            'fps': 30,
            'backend': 'hyperframes',
            'recipe': 'ui_flow_stage',
            'params': {
                'theme': 'editorial_scrapbook', 'focus_index': 0,
                'states': ['overview', 'focus', 'hero_zoom'],
                'cards': [{'title': 'ACT 01'}, {'title': 'ACT 02'}],
            },
        },
        'kinetic_ui_title': {
            'name': 'hf_kui_test',
            'duration': 0.8,
            'backend': 'hyperframes',
            'recipe': 'kinetic_ui_title',
            'params': {'theme': 'neon_cyber', 'badge': 'UI', 'title': 'Silky Motion'},
        },
    }
    assert len(hyperframes_backend.list_recipes()) == 10
    for name, sp in specs.items():
        validated = hyperframes_backend.validate_overlay_spec(dict(sp), label=name)
        files = hyperframes_backend._build_recipe_files(
            validated['recipe'], validated['params'], width=640, height=360, fps=30, duration=0.8
        )
        assert 'index.html' in files and 'compositions/card.html' in files

    # 2. Real headless render + alpha verification + cache hit check.
    with patch.dict(os.environ, {'OPENEDIT_OVERLAY_CACHE_DIR': str(cache_dir)}):
        first = hyperframes_backend.build_overlay(specs['kpi_bar_chart'], root / 'out_1', draft=True)
        assert first['status'] == 'passed', first
        assert first['backend'] == 'hyperframes'
        assert not first['cache']['hit']
        assert first['cache']['stored']
        assert first['overlay_video']['strict_decode'] == 'passed'
        assert first['overlay_video']['pix_fmt'] == 'yuva444p12le'

        with Image.open(first['preview_png']) as img:
            assert img.mode == 'RGBA' and img.size == (640, 360), (img.mode, img.size)
            arr = np.array(img)
            assert int(arr[:, :200, 3].max()) == 0, 'Left region must remain 100% transparent'
            assert int(arr[180, 480, 3]) > 200, 'Right card body must be opaque/semi-opaque'

        hud_res = hyperframes_backend.build_overlay(specs['viewfinder_hud'], root / 'out_hud', draft=True)
        assert hud_res['overlay_video']['pix_fmt'] == 'yuva444p12le' and hud_res['overlay_video']['strict_decode'] == 'passed'
        with Image.open(hud_res['preview_png']) as him:
            harr = np.array(him.convert('RGBA'))
            assert int(harr[180, 320, 3]) == 0, 'Viewfinder HUD center pixel must remain transparent'

        ui_res = hyperframes_backend.build_overlay(specs['ui_flow_stage'], root / 'out_uiflow', draft=True)
        assert ui_res['overlay_video']['pix_fmt'] == 'yuva444p12le' and ui_res['overlay_video']['strict_decode'] == 'passed'
        with Image.open(ui_res['preview_png']) as uim:
            uarr = np.array(uim.convert('RGBA'))
            assert int(uarr[:, :, 3].max()) > 200, 'ui_flow_stage must render opaque UI cards'

        kui_res = hyperframes_backend.build_overlay(specs['kinetic_ui_title'], root / 'out_kui', draft=True)
        assert kui_res['overlay_video']['pix_fmt'] == 'yuva444p12le' and kui_res['overlay_video']['strict_decode'] == 'passed'

        # 3. Live moving video inside ui_flow_stage cards must actually advance frames during steady overview dwell.
        ffmpeg = core.tool('ffmpeg')
        vid_a = root / 'live_a.mp4'
        vid_b = root / 'live_b.mp4'
        core.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=1.8',
            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(vid_a),
        ])
        core.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
            '-f', 'lavfi', '-i', 'smptebars=s=320x180:r=30:d=1.8',
            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(vid_b),
        ])
        live_ui = {
            'name': 'hf_uiflow_live',
            'duration': 1.6,
            'width': 640,
            'height': 360,
            'fps': 30,
            'backend': 'hyperframes',
            'recipe': 'ui_flow_stage',
            'params': {
                'theme': 'editorial_scrapbook', 'focus_index': 0, 'states': ['overview', 'focus'],
                'cards': [{'title': 'LIVE A', 'video': str(vid_a), 'source_in': 0.0}, {'title': 'LIVE B', 'video': str(vid_b), 'source_in': 0.2}],
            },
        }
        live_res = hyperframes_backend.build_overlay(live_ui, root / 'out_uiflow_live', draft=True)
        assert live_res['overlay_video']['pix_fmt'] == 'yuva444p12le' and live_res['overlay_video']['strict_decode'] == 'passed'
        mov_p = live_res['overlay_video']['path']
        f_early = root / 'live_t024.png'
        f_late = root / 'live_t038.png'
        for ts, dst in ((0.24, f_early), (0.38, f_late)):
            core.run([ffmpeg, '-hide_banner', '-v', 'error', '-y', '-ss', f'{ts:.2f}', '-i', str(mov_p), '-frames:v', '1', '-pix_fmt', 'rgba', str(dst)])
        with Image.open(f_early) as i1, Image.open(f_late) as i2:
            a1 = np.asarray(i1.convert('RGB'), dtype=np.float32)[110:250, 70:290, :]
            a2 = np.asarray(i2.convert('RGB'), dtype=np.float32)[110:250, 70:290, :]
            live_card_diff = float(np.mean(np.abs(a1 - a2)))
        assert live_card_diff > 1.5, f'Expected live video motion inside steady ui_flow_stage card, got diff={live_card_diff:.3f}'

        moved = {**specs['kpi_bar_chart'], 'track': 'V3', 'start': 3.5}
        second = hyperframes_backend.build_overlay(moved, root / 'out_2', draft=True)
        assert second['cache']['hit'], second
        assert second['overlay_video']['sha256'] == first['overlay_video']['sha256']

    return {
        'status': 'passed',
        'recipes': hyperframes_backend.list_recipes(),
        'pix_fmt': first['overlay_video']['pix_fmt'],
        'live_card_diff': round(live_card_diff, 3),
        'cache_hit_verified': True,
        'elapsed_seconds': first['elapsed_seconds'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    core.emit(check(args.out.resolve()))


if __name__ == '__main__':
    main()
