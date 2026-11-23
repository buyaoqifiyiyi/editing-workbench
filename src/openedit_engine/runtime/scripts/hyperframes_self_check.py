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
    }
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

        moved = {**specs['kpi_bar_chart'], 'track': 'V3', 'start': 3.5}
        second = hyperframes_backend.build_overlay(moved, root / 'out_2', draft=True)
        assert second['cache']['hit'], second
        assert second['overlay_video']['sha256'] == first['overlay_video']['sha256']

    return {
        'status': 'passed',
        'recipes': hyperframes_backend.list_recipes(),
        'pix_fmt': first['overlay_video']['pix_fmt'],
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
