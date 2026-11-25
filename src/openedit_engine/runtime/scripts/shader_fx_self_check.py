"""Regression check for shader_fx (headless ModernGL GL-Transitions & ISF effects)."""
import argparse
import contextlib
import io
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import runtime_core as core
import shader_fx


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = shader_fx.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _make_split_clip(mp4_path, top_rgb, bot_rgb, fps=30, frames=12, w=256, h=144):
    raw_dir = mp4_path.parent / f'_{mp4_path.stem}_frames'
    raw_dir.mkdir(exist_ok=True)
    for i in range(frames):
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[:h // 2, :] = top_rgb
        rgb[h // 2:, :] = bot_rgb
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        cv2.circle(bgr, (40 + i * 12, h // 4), 10, (255, 255, 255), -1)
        cv2.imwrite(str(raw_dir / f'{i:04d}.png'), bgr)
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-framerate', str(fps), '-i', str(raw_dir / '%04d.png'),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(mp4_path),
    ], timeout=60)
    return mp4_path


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []

    # 1. Asymmetric top=RED / bot=BLUE source A, top=GREEN / bot=YELLOW source B
    clip_a = _make_split_clip(root / 'a.mp4', (230, 30, 30), (30, 60, 230))
    clip_b = _make_split_clip(root / 'b.mp4', (40, 220, 60), (235, 220, 40))

    # Orientation-preserving custom GL-Transition test (at frame 0, t=0 => pure A; verify top stays RED!)
    custom_gl = root / 'pass.glsl'
    custom_gl.write_text("""
uniform float mix_bias; // = 0.0
vec4 transition(vec2 uv) {
    return mix(getFromColor(uv), getToColor(uv), clamp(progress + mix_bias, 0.0, 1.0));
}
""", encoding='utf-8')
    tr_custom_out = root / 'tr-custom'
    c1, r1 = _run_main([
        'transition', '--from', clip_a, '--to', clip_b, '--out', tr_custom_out,
        '--shader', custom_gl, '--duration', '0.4', '--fps', '30', '--easing', 'linear',
    ])
    assert c1 == 0 and r1['status'] == 'passed' and r1['frames'] == 12, r1
    assert r1['strict_decode'] == 'passed' and Path(r1['preview_png']).is_file()
    cap = cv2.VideoCapture(r1['output'])
    try:
        ok, first_bgr = cap.read()
        assert ok
    finally:
        cap.release()
    top_rgb = first_bgr[8, 128, ::-1]
    bot_rgb = first_bgr[-8, 128, ::-1]
    assert top_rgb[0] > 180 and bot_rgb[2] > 180, f'Orientation flipped: top={top_rgb}, bot={bot_rgb}'
    checks.append('custom GL-Transition: upright orientation (top=red, bot=blue) & strict_decode')

    # Built-in preset transition
    tr_pre_out = root / 'tr-preset'
    c2, r2 = _run_main([
        'transition', '--from', clip_a, '--to', clip_b, '--out', tr_pre_out,
        '--preset', 'crosszoom', '--duration', '0.4', '--fps', '30', '--params', '{"strength": 0.35}',
    ])
    assert c2 == 0 and r2['status'] == 'passed' and r2['preset'] == 'crosszoom', r2
    checks.append(f'transition crosszoom preset rendered {r2["frames"]} frames on {r2["renderer"]}')

    # 2. Built-in ISF effect + vertical orientation check on still image -> mp4
    still = root / 'split.png'
    arr = np.zeros((128, 192, 3), dtype=np.uint8)
    arr[:64, :] = (240, 40, 40)
    arr[64:, :] = (30, 80, 230)
    Image.fromarray(arr, mode='RGB').save(still)

    fx_pre_out = root / 'fx-prism'
    c3, r3 = _run_main([
        'effect', '--input', still, '--out', fx_pre_out,
        '--preset', 'prism_chromatic', '--duration', '0.3', '--fps', '30',
        '--params', '{"aberration": 0.03}',
    ])
    assert c3 == 0 and r3['status'] == 'passed' and r3['frames'] == 9, r3
    checks.append('ISF prism_chromatic effect passed on still image -> MP4')

    for p_name, p_src, extra in (
        ('duotone_grade', still, ['--duration', '0.2', '--fps', '30']),
        ('color_pop', still, ['--duration', '0.2', '--fps', '30']),
        ('neon_strobe', still, ['--duration', '0.2', '--fps', '30', '--params', '{"staticMode": 1.0}']),
        ('dot_grid_backdrop', still, ['--duration', '0.2', '--fps', '30']),
        ('echo_chromatic_snap', clip_a, []),
        ('deep_glow', still, ['--duration', '0.2', '--fps', '30']),
        ('deep_glow', still, ['--duration', '0.2', '--fps', '30', '--alpha']),
        ('shutter_motion_blur', clip_a, ['--params', '{"shutterAngle": 180.0, "samples": 8}']),
    ):
        tag = f'{p_name}_alpha' if '--alpha' in extra else p_name
        cp, rp = _run_main(['effect', '--input', p_src, '--out', root / f'fx-{tag}', '--preset', p_name, *extra])
        assert cp == 0 and rp['status'] == 'passed' and rp['strict_decode'] == 'passed', rp
        if '--alpha' in extra:
            assert rp['pix_fmt'] == 'yuva444p12le', rp
    checks.append('All 7 new technical & optical ISF presets (incl. 5-level Mipmap deep_glow + shutter_motion_blur) passed GPU & strict_decode')

    # 3. Custom ISF .fs with ProRes 4444 alpha (.mov yuva444p12le) + orientation check
    custom_isf = root / 'half_alpha.fs'
    custom_isf.write_text("""/*{
  "DESCRIPTION": "Pass RGB upright and put radial mask in alpha",
  "INPUTS": [
    {"NAME": "gain", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.0, "MAX": 2.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    float mask = uv.x > 0.25 && uv.x < 0.75 ? 1.0 : 0.25;
    gl_FragColor = vec4(clamp(src.rgb * gain, 0.0, 1.0), mask);
}
""", encoding='utf-8')
    fx_alpha_out = root / 'fx-alpha'
    c4, r4 = _run_main([
        'effect', '--input', clip_a, '--out', fx_alpha_out,
        '--shader', custom_isf, '--alpha',
    ])
    assert c4 == 0 and r4['status'] == 'passed' and r4['pix_fmt'] == 'yuva444p12le', r4
    mid_rgba = np.asarray(Image.open(r4['preview_png']), dtype=np.uint8)
    assert mid_rgba[8, 100, 0] > 180 and mid_rgba[-8, 100, 2] > 180, 'ISF orientation flipped!'
    assert mid_rgba[64, 96, 3] > 230 and mid_rgba[64, 4, 3] < 90, 'ISF alpha mask failed!'
    checks.append('custom ISF .fs + ProRes 4444 (yuva444p12le) alpha & orientation verified')

    # 3b. RGBA ProRes 4444 video input alpha preservation + layer_motion_blur + light_wrap
    fg_mov = root / 'fg_cutout.mov'
    fg_rgba = np.zeros((8, 128, 160, 4), dtype=np.uint8)
    for fi in range(8):
        # Upper circle head + lower rectangle torso + wing/hair strands in upper-right/left edge
        cv2.circle(fg_rgba[fi], (80, 38), 20, (50, 70, 120, 255), -1)
        cv2.rectangle(fg_rgba[fi], (62, 54), (98, 127), (40, 50, 90, 255), -1)
        # Hair strand extensions at left/right crown edges
        cv2.ellipse(fg_rgba[fi], (58, 44), (10, 20), 15, 0, 360, (35, 45, 95, 255), -1)
        cv2.ellipse(fg_rgba[fi], (102, 44), (10, 20), -15, 0, 360, (35, 45, 95, 255), -1)
    shader_fx._encode_frames(fg_rgba, 24.0, fg_mov, alpha=True)

    c_blur, r_blur = _run_main([
        'effect', '--input', fg_mov, '--out', root / 'fx-layer-blur',
        '--preset', 'layer_motion_blur', '--alpha',
        '--params', '{"inDuration": 0.25, "inBlurPx": 18.0, "outDuration": 0.0}',
    ])
    assert c_blur == 0 and r_blur['status'] == 'passed' and r_blur['pix_fmt'] == 'yuva444p12le', r_blur
    dec_blur, _, _, _, _, _ = shader_fx._read_media_frames(r_blur['output'], want_alpha=True)
    assert dec_blur[0, 64, 150, 3] < 20, f'Alpha was stripped on RGBA video round-trip: corner alpha={dec_blur[0,64,150,3]}'
    assert dec_blur[0, 44, 42, 3] > dec_blur[-1, 44, 42, 3], 'layer_motion_blur did not smear entry edge!'
    checks.append('RGBA ProRes 4444 decode + layer_motion_blur alpha & directional blur verified')

    bright_bg = root / 'bright_sky.png'
    Image.new('RGB', (160, 128), (255, 245, 220)).save(bright_bg)
    c_wrap, r_wrap = _run_main([
        'effect', '--input', fg_mov, '--out', root / 'fx-light-wrap',
        '--preset', 'light_wrap', '--bg', bright_bg, '--alpha',
        '--params', '{"wrapRadiusPx": 8.0, "intensity": 0.85, "lumaThreshold": 0.2}',
    ])
    assert c_wrap == 0 and r_wrap['status'] == 'passed' and r_wrap['has_bg'] is True and r_wrap['pix_fmt'] == 'yuva444p12le', r_wrap
    wrap_rgba = np.asarray(Image.open(r_wrap['preview_png']), dtype=np.uint8)
    edge_luma = float(np.mean(wrap_rgba[80, 63, :3]))
    core_luma = float(np.mean(wrap_rgba[80, 80, :3]))
    assert edge_luma > core_luma + 18.0, f'light_wrap did not wrap rim light: edge={edge_luma}, core={core_luma}'
    assert wrap_rgba[5, 5, 3] < 10, f'light_wrap polluted far corner alpha: {wrap_rgba[5,5,3]}'
    checks.append(f'light_wrap --bg optical rim wrap verified (edge={edge_luma:.1f} vs core={core_luma:.1f})')
    assert 'character_live25d' not in shader_fx.ISF_PRESETS

    # 4. Existing output refused
    cb, rb = _run_main(['effect', '--input', clip_a, '--out', fx_alpha_out])
    assert cb == 1 and rb['status'] == 'failed'
    checks.append('existing out refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'shader-fx-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
