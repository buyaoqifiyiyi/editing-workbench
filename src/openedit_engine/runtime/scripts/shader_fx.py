"""Headless ModernGL OpenGL 3.3 GPU shader transitions (GL-Transitions MIT) & ISF effects (MIT)."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import cv2
import moderngl
import numpy as np
from PIL import Image

import file_hash
import runtime_core as core

_NO_WIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff'}

_VERT_GLSL = """#version 330 core
in vec2 a_pos;
out vec2 v_uv;
out vec2 isf_FragNormCoord;
void main() {
    vec2 uv = a_pos * 0.5 + 0.5;
    v_uv = uv;
    isf_FragNormCoord = uv;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

_GL_TRANSITION_HEADER = """#version 330 core
#define texture2D texture
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float progress;
uniform float ratio;
{extra_uniforms}

vec4 getFromColor(vec2 uv) {{
    return texture(u_from, clamp(uv, 0.0, 1.0));
}}

vec4 getToColor(vec2 uv) {{
    return texture(u_to, clamp(uv, 0.0, 1.0));
}}

{body}

void main() {{
    fragColor = transition(v_uv);
}}
"""

_ISF_HEADER = """#version 330 core
#define texture2D texture
in vec2 isf_FragNormCoord;
out vec4 o_fragColor;
#define gl_FragColor o_fragColor
uniform sampler2D inputImage;
uniform sampler2D prevImage;
uniform sampler2D nextImage;
uniform sampler2D bgImage;
uniform int hasBgImage;
uniform float TIME;
uniform float DURATION;
uniform vec2 RENDERSIZE;
uniform int FRAMEINDEX;
uniform int PASSINDEX;
{extra_uniforms}

vec4 IMG_NORM_PIXEL(sampler2D img, vec2 loc) {{
    return texture(img, clamp(loc, 0.0, 1.0));
}}

vec4 IMG_PIXEL(sampler2D img, vec2 loc) {{
    return texture(img, clamp(loc / max(RENDERSIZE, vec2(1.0)), 0.0, 1.0));
}}

vec4 IMG_THIS_PIXEL(sampler2D img) {{
    return texture(img, isf_FragNormCoord);
}}

vec4 IMG_THIS_NORM_PIXEL(sampler2D img) {{
    return texture(img, isf_FragNormCoord);
}}

vec2 IMG_SIZE(sampler2D img) {{
    return vec2(textureSize(img, 0));
}}

{body}
"""

TRANSITION_PRESETS = {
    'directionalwarp': {
        'defaults': {'direction': (-1.0, 0.5), 'smoothness': 0.5},
        'glsl': """
uniform vec2 direction;
uniform float smoothness;
vec4 transition(vec2 uv) {
    vec2 v = normalize(direction + vec2(1e-5, 0.0));
    vec2 center = vec2(0.5, 0.5);
    float m = smoothstep(-smoothness, 0.0, v.x * uv.x + v.y * uv.y - (progress * (1.0 + smoothness) - (sign(v.x) * 0.5 + sign(v.y) * 0.5) * 0.0));
    float d = dot(uv - center, v) + 0.5;
    float f = smoothstep(0.0, smoothness, d - progress * (1.0 + smoothness) + smoothness);
    return mix(
        getFromColor((uv - 0.5) * (1.0 - f * 0.25) + 0.5 + v * f * 0.15),
        getToColor((uv - 0.5) * (0.75 + 0.25 * f) + 0.5 - v * (1.0 - f) * 0.15),
        smoothstep(0.0, 1.0, progress * 1.15 - f * 0.15)
    );
}
""",
    },
    'crosszoom': {
        'defaults': {'strength': 0.42},
        'glsl': """
uniform float strength;
float linear_ease(float b, float c, float d, float t) { return c * t / d + b; }
float exp_ease_in_out(float b, float c, float d, float t) {
    if (t == 0.0) return b;
    if (t == d) return b + c;
    t /= d / 2.0;
    if (t < 1.0) return c / 2.0 * pow(2.0, 10.0 * (t - 1.0)) + b;
    return c / 2.0 * (-pow(2.0, -10.0 * (t - 1.0)) + 2.0) + b;
}
float sin_ease_in_out(float b, float c, float d, float t) {
    return -c / 2.0 * (cos(3.14159265 * t / d) - 1.0) + b;
}
vec4 crossFade(vec2 uv, float dissolve) {
    return mix(getFromColor(uv), getToColor(uv), dissolve);
}
vec4 transition(vec2 uv) {
    vec2 center = vec2(linear_ease(0.25, 0.5, 1.0, progress), 0.5);
    float dissolve = exp_ease_in_out(0.0, 1.0, 1.0, progress);
    float str = sin_ease_in_out(0.0, strength, 0.5, progress);
    vec3 color = vec3(0.0);
    float total = 0.0;
    vec2 toCenter = center - uv;
    float offset = fract(sin(dot(uv, vec2(12.9898, 78.233))) * 43758.5453);
    for (float t = 0.0; t <= 16.0; t += 1.0) {
        float pct = (t + offset) / 16.0;
        float weight = 4.0 * (pct - pct * pct);
        color += crossFade(uv + toCenter * pct * str, dissolve).rgb * weight;
        total += weight;
    }
    return vec4(color / max(total, 1e-4), 1.0);
}
""",
    },
    'glitch_memories': {
        'defaults': {'strength': 0.5},
        'glsl': """
uniform float strength;
vec4 transition(vec2 p) {
    vec2 block = floor(p.xy / vec2(0.06, 0.04));
    vec2 uv_noise = block / vec2(64.0);
    uv_noise += floor(vec2(progress) * vec2(1200.0, 3500.0)) / vec2(64.0);
    vec2 dist = progress > 0.0 ? (fract(uv_noise) - 0.5) * 0.3 * (1.0 - progress) : vec2(0.0);
    vec2 red = p + dist * 0.2 * strength;
    vec2 green = p + dist * 0.35 * strength;
    vec2 blue = p + dist * 0.5 * strength;
    vec4 ca = vec4(getFromColor(red).r, getFromColor(green).g, getFromColor(blue).b, 1.0);
    vec4 cb = vec4(getToColor(red).r, getToColor(green).g, getToColor(blue).b, 1.0);
    return mix(ca, cb, smoothstep(0.15, 0.85, progress));
}
""",
    },
    'swirl': {
        'defaults': {'radius': 1.0, 'rotations': 1.5},
        'glsl': """
uniform float radius;
uniform float rotations;
vec4 transition(vec2 uv) {
    vec2 p = uv - vec2(0.5);
    float dist = length(p);
    if (dist < radius) {
        float percent = (radius - dist) / radius;
        float amt = progress <= 0.5 ? mix(0.0, 1.0, progress * 2.0) : mix(1.0, 0.0, (progress - 0.5) * 2.0);
        float theta = percent * percent * amt * rotations * 8.0 * 3.14159;
        float s = sin(theta);
        float c = cos(theta);
        p = vec2(dot(p, vec2(c, -s)), dot(p, vec2(s, c)));
    }
    p += vec2(0.5);
    return mix(getFromColor(p), getToColor(p), smoothstep(0.25, 0.75, progress));
}
""",
    },
    'radial': {
        'defaults': {'smoothness': 1.0},
        'glsl': """
uniform float smoothness;
vec4 transition(vec2 p) {
    vec2 rp = p * 2.0 - 1.0;
    float angle = atan(rp.y, rp.x);
    float pa = (progress - 0.5) * 6.2831853 - 3.14159265;
    float t = smoothstep(0.0, smoothness, angle - (progress * 2.0 - 1.0) * 3.14159265);
    return mix(getToColor(p), getFromColor(p), t);
}
""",
    },
    'zoom_in_circles': {
        'defaults': {'rings': 6.0},
        'glsl': """
uniform float rings;
vec2 zoom(vec2 uv, float amount) {
    return 0.5 + ((uv - 0.5) * (1.0 - amount));
}
vec4 transition(vec2 uv) {
    vec2 r = 2.0 * ((uv - 0.5) * vec2(ratio, 1.0));
    float d = length(r);
    float t = progress;
    if (t < 0.45) {
        float z = t / 0.45;
        float band = fract(d * rings);
        return getFromColor(zoom(uv, z * (0.25 + 0.35 * band)));
    } else if (t < 0.55) {
        return mix(getFromColor(zoom(uv, 0.5)), getToColor(zoom(uv, 0.5)), (t - 0.45) / 0.10);
    }
    float z = 1.0 - ((t - 0.55) / 0.45);
    return getToColor(zoom(uv, z * 0.45));
}
""",
    },
    'wind': {
        'defaults': {'size': 0.2},
        'glsl': """
uniform float size;
float rand(vec2 co) {
    return fract(sin(dot(co, vec2(12.9898, 78.233))) * 43758.5453);
}
vec4 transition(vec2 uv) {
    float r = rand(vec2(0.0, floor(uv.y * 120.0) / 120.0));
    float m = smoothstep(0.0, -size, uv.x * (1.0 - size) + size * r - (progress * (1.0 + size)));
    return mix(getFromColor(uv), getToColor(uv), m);
}
""",
    },
    'dreamy_zoom': {
        'defaults': {'rotation': 6.0, 'scale': 1.2},
        'glsl': """
uniform float rotation;
uniform float scale;
vec4 transition(vec2 uv) {
    float phase = progress < 0.5 ? progress * 2.0 : (1.0 - progress) * 2.0;
    float angle = sin(progress * 3.14159265) * rotation * 0.05;
    vec2 centered = uv - 0.5;
    float s = sin(angle);
    float c = cos(angle);
    vec2 rot_uv = vec2(centered.x * c - centered.y * s, centered.x * s + centered.y * c);
    vec2 z_uv = rot_uv / (1.0 + phase * (scale - 1.0)) + 0.5;
    vec4 col = mix(getFromColor(z_uv), getToColor(z_uv), smoothstep(0.35, 0.65, progress));
    col.rgb += phase * 0.08 * vec3(1.0, 0.96, 0.9);
    return col;
}
""",
    },
    'burn': {
        'defaults': {'color': (0.9, 0.4, 0.2)},
        'glsl': """
uniform vec3 color;
vec4 transition(vec2 uv) {
    return mix(
        getFromColor(uv) + vec4(progress * color, 1.0),
        getToColor(uv) + vec4((1.0 - progress) * color, 1.0),
        progress
    );
}
""",
    },
    'chromatic_split': {
        'defaults': {'strength': 0.08},
        'glsl': """
uniform float strength;
vec4 transition(vec2 uv) {
    float bell = sin(clamp(progress, 0.0, 1.0) * 3.14159265);
    float shift = bell * strength;
    vec4 ca = vec4(
        getFromColor(uv + vec2(shift, 0.0)).r,
        getFromColor(uv).g,
        getFromColor(uv - vec2(shift, 0.0)).b,
        1.0
    );
    vec4 cb = vec4(
        getToColor(uv - vec2(shift, 0.0)).r,
        getToColor(uv).g,
        getToColor(uv + vec2(shift, 0.0)).b,
        1.0
    );
    return mix(ca, cb, smoothstep(0.2, 0.8, progress));
}
""",
    },
}

ISF_PRESETS = {
    'halation_bloom': """/*{
  "DESCRIPTION": "Warm 35mm film halation & soft specular bloom (ISF MIT)",
  "INPUTS": [
    {"NAME": "intensity", "TYPE": "float", "DEFAULT": 0.65, "MIN": 0.0, "MAX": 2.0},
    {"NAME": "threshold", "TYPE": "float", "DEFAULT": 0.65, "MIN": 0.1, "MAX": 1.0},
    {"NAME": "radius", "TYPE": "float", "DEFAULT": 4.0, "MIN": 0.5, "MAX": 16.0},
    {"NAME": "tint_red", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.0, "MAX": 1.5}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 base = IMG_NORM_PIXEL(inputImage, uv);
    vec2 texel = radius / max(RENDERSIZE, vec2(1.0));
    vec3 glow = vec3(0.0);
    float wsum = 0.0;
    for (int x = -2; x <= 2; x++) {
        for (int y = -2; y <= 2; y++) {
            float d = length(vec2(float(x), float(y)));
            float w = exp(-0.35 * d * d);
            vec3 s = IMG_NORM_PIXEL(inputImage, uv + vec2(float(x), float(y)) * texel).rgb;
            float luma = dot(s, vec3(0.2126, 0.7152, 0.0722));
            float mask = smoothstep(threshold, 1.0, luma);
            glow += s * mask * w;
            wsum += w;
        }
    }
    glow /= max(wsum, 1e-4);
    vec3 halation = glow * vec3(1.15 * tint_red + 0.15, 0.52, 0.24) * intensity;
    vec3 out_rgb = clamp(base.rgb + halation, 0.0, 1.0);
    float halo_alpha = clamp(max(base.a, dot(halation, vec3(0.3333)) * 1.4), 0.0, 1.0);
    gl_FragColor = vec4(out_rgb, halo_alpha);
}
""",
    'prism_chromatic': """/*{
  "DESCRIPTION": "Radial optical prism chromatic aberration & refraction",
  "INPUTS": [
    {"NAME": "aberration", "TYPE": "float", "DEFAULT": 0.025, "MIN": 0.0, "MAX": 0.12},
    {"NAME": "center", "TYPE": "point2D", "DEFAULT": [0.5, 0.5]},
    {"NAME": "falloff", "TYPE": "float", "DEFAULT": 1.5, "MIN": 0.5, "MAX": 4.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec2 delta = uv - center;
    float dist = length(delta);
    float mag = aberration * pow(dist * 1.414, falloff);
    vec2 dir = dist > 1e-5 ? delta / dist : vec2(0.0);
    float r = IMG_NORM_PIXEL(inputImage, uv + dir * mag).r;
    float g = IMG_NORM_PIXEL(inputImage, uv).g;
    float b = IMG_NORM_PIXEL(inputImage, uv - dir * mag).b;
    float a = IMG_NORM_PIXEL(inputImage, uv).a;
    gl_FragColor = vec4(r, g, b, a);
}
""",
    'film_grain_vignette': """/*{
  "DESCRIPTION": "Time-seeded organic film grain and lens vignette",
  "INPUTS": [
    {"NAME": "grain", "TYPE": "float", "DEFAULT": 0.055, "MIN": 0.0, "MAX": 0.25},
    {"NAME": "vignette", "TYPE": "float", "DEFAULT": 0.42, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "softness", "TYPE": "float", "DEFAULT": 0.48, "MIN": 0.1, "MAX": 1.0}
  ]
}*/
float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    float n = hash12(uv * RENDERSIZE + vec2(TIME * 61.3, float(FRAMEINDEX) * 17.1)) - 0.5;
    vec3 rgb = src.rgb + n * grain * 2.0;
    float d = length(uv - vec2(0.5)) * 1.4142135;
    float vig = 1.0 - vignette * smoothstep(1.0 - softness, 1.05, d);
    gl_FragColor = vec4(clamp(rgb * vig, 0.0, 1.0), src.a);
}
""",
    'glitch_scanline': """/*{
  "DESCRIPTION": "Cyber scanline jitter, block tear & RGB phase shift",
  "INPUTS": [
    {"NAME": "amount", "TYPE": "float", "DEFAULT": 0.45, "MIN": 0.0, "MAX": 1.5},
    {"NAME": "scanlines", "TYPE": "float", "DEFAULT": 160.0, "MIN": 20.0, "MAX": 600.0},
    {"NAME": "speed", "TYPE": "float", "DEFAULT": 1.5, "MIN": 0.0, "MAX": 8.0}
  ]
}*/
float h11(float x) {
    return fract(sin(x * 127.1 + TIME * speed * 19.7) * 43758.5453);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    float band = floor(uv.y * 24.0);
    float trig = step(0.76, h11(band));
    float offset = (h11(band + 13.7) - 0.5) * 0.08 * amount * trig;
    vec2 u = uv + vec2(offset, 0.0);
    float sep = 0.008 * amount * (0.35 + trig);
    float r = IMG_NORM_PIXEL(inputImage, u + vec2(sep, 0.0)).r;
    float g = IMG_NORM_PIXEL(inputImage, u).g;
    float b = IMG_NORM_PIXEL(inputImage, u - vec2(sep, 0.0)).b;
    float line_mod = 1.0 - 0.14 * amount * (0.5 + 0.5 * sin(uv.y * scanlines * 3.14159265));
    float a = IMG_NORM_PIXEL(inputImage, u).a;
    gl_FragColor = vec4(clamp(vec3(r, g, b) * line_mod, 0.0, 1.0), a);
}
""",
    'radial_zoom_blur': """/*{
  "DESCRIPTION": "Directional radial motion zoom blur",
  "INPUTS": [
    {"NAME": "strength", "TYPE": "float", "DEFAULT": 0.22, "MIN": 0.0, "MAX": 0.8},
    {"NAME": "center", "TYPE": "point2D", "DEFAULT": [0.5, 0.5]},
    {"NAME": "samples", "TYPE": "float", "DEFAULT": 14.0, "MIN": 4.0, "MAX": 24.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec2 dir = center - uv;
    int n = int(clamp(samples, 4.0, 24.0));
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 24; i++) {
        if (i >= n) break;
        float t = float(i) / max(float(n - 1), 1.0);
        acc += IMG_NORM_PIXEL(inputImage, uv + dir * t * strength);
    }
    gl_FragColor = acc / float(n);
}
""",
    'duotone_grade': """/*{
  "DESCRIPTION": "3-stop editorial duotone grade with highlight cap and subtle film grain",
  "INPUTS": [
    {"NAME": "shadowColor", "TYPE": "vec3", "DEFAULT": [0.05, 0.03, 0.10]},
    {"NAME": "midColor", "TYPE": "vec3", "DEFAULT": [0.48, 0.40, 0.72]},
    {"NAME": "highlightColor", "TYPE": "vec3", "DEFAULT": [0.95, 0.93, 0.99]},
    {"NAME": "gamma", "TYPE": "float", "DEFAULT": 1.15, "MIN": 0.5, "MAX": 2.5},
    {"NAME": "highlightCap", "TYPE": "float", "DEFAULT": 0.94, "MIN": 0.5, "MAX": 1.0},
    {"NAME": "mixAmount", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "grain", "TYPE": "float", "DEFAULT": 0.025, "MIN": 0.0, "MAX": 0.15}
  ]
}*/
float hash_dg(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    float raw_luma = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
    float luma = pow(clamp(raw_luma, 0.0, 1.0), gamma);
    luma = min(luma, highlightCap);
    vec3 duo = luma < 0.5
        ? mix(shadowColor, midColor, smoothstep(0.0, 0.5, luma))
        : mix(midColor, highlightColor, smoothstep(0.5, 1.0, luma));
    float n = (hash_dg(uv * RENDERSIZE + vec2(TIME * 53.7, float(FRAMEINDEX) * 19.3)) - 0.5) * 2.0 * grain;
    vec3 out_rgb = clamp(mix(src.rgb, duo, mixAmount) + n, 0.0, 1.0);
    gl_FragColor = vec4(out_rgb, src.a);
}
""",
    'color_pop': """/*{
  "DESCRIPTION": "Selective HSV hue color-pop over tinted monochrome backdrop",
  "INPUTS": [
    {"NAME": "targetHue", "TYPE": "float", "DEFAULT": 0.76, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "hueTolerance", "TYPE": "float", "DEFAULT": 0.10, "MIN": 0.01, "MAX": 0.45},
    {"NAME": "minSaturation", "TYPE": "float", "DEFAULT": 0.16, "MIN": 0.0, "MAX": 0.9},
    {"NAME": "popBoost", "TYPE": "float", "DEFAULT": 1.25, "MIN": 0.5, "MAX": 2.0},
    {"NAME": "desatTint", "TYPE": "vec3", "DEFAULT": [0.88, 0.85, 0.95]},
    {"NAME": "desatContrast", "TYPE": "float", "DEFAULT": 1.08, "MIN": 0.6, "MAX": 1.8}
  ]
}*/
vec3 rgb2hsv_cp(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    vec3 hsv = rgb2hsv_cp(src.rgb);
    float dh = abs(hsv.x - targetHue);
    float hue_dist = min(dh, 1.0 - dh);
    float hue_mask = 1.0 - smoothstep(hueTolerance * 0.6, hueTolerance * 1.4, hue_dist);
    float sat_mask = smoothstep(minSaturation * 0.7, minSaturation + 0.12, hsv.y);
    float keep = clamp(hue_mask * sat_mask, 0.0, 1.0);
    float luma = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
    float mono_l = clamp((luma - 0.5) * desatContrast + 0.5, 0.0, 1.0);
    vec3 mono = clamp(vec3(mono_l) * desatTint, 0.0, 1.0);
    vec3 popped = clamp(mix(vec3(luma), src.rgb, popBoost), 0.0, 1.0);
    gl_FragColor = vec4(mix(mono, popped, keep), src.a);
}
""",
    'neon_strobe': """/*{
  "DESCRIPTION": "Inverted Sobel wireframe and rhythmic neon color strobe",
  "INPUTS": [
    {"NAME": "neonColor", "TYPE": "vec3", "DEFAULT": [0.22, 1.0, 0.35]},
    {"NAME": "strobeHz", "TYPE": "float", "DEFAULT": 8.0, "MIN": 0.5, "MAX": 30.0},
    {"NAME": "duty", "TYPE": "float", "DEFAULT": 0.5, "MIN": 0.05, "MAX": 1.0},
    {"NAME": "invertMix", "TYPE": "float", "DEFAULT": 0.85, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "edgeStrength", "TYPE": "float", "DEFAULT": 1.6, "MIN": 0.0, "MAX": 4.0},
    {"NAME": "staticMode", "TYPE": "float", "DEFAULT": 0.0, "MIN": 0.0, "MAX": 1.0}
  ]
}*/
float luma_ns(vec2 p) {
    vec3 c = IMG_NORM_PIXEL(inputImage, p).rgb;
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    vec2 px = 1.0 / max(RENDERSIZE, vec2(1.0));
    float tl = luma_ns(uv + vec2(-px.x, px.y));
    float tc = luma_ns(uv + vec2(0.0, px.y));
    float tr = luma_ns(uv + vec2(px.x, px.y));
    float ml = luma_ns(uv + vec2(-px.x, 0.0));
    float mr = luma_ns(uv + vec2(px.x, 0.0));
    float bl = luma_ns(uv + vec2(-px.x, -px.y));
    float bc = luma_ns(uv + vec2(0.0, -px.y));
    float br = luma_ns(uv + vec2(px.x, -px.y));
    float gx = -tl - 2.0 * ml - bl + tr + 2.0 * mr + br;
    float gy = tl + 2.0 * tc + tr - bl - 2.0 * bc - br;
    float edge = clamp(length(vec2(gx, gy)) * edgeStrength, 0.0, 1.0);
    vec3 inv = mix(src.rgb, vec3(1.0) - src.rgb, invertMix);
    vec3 fx = clamp(mix(inv, inv * neonColor + neonColor * edge * 0.75, 0.82), 0.0, 1.0);
    float gate = (staticMode > 0.5 || fract(TIME * strobeHz) < duty) ? 1.0 : 0.0;
    gl_FragColor = vec4(mix(src.rgb, fx, gate), src.a);
}
""",
    'dot_grid_backdrop': """/*{
  "DESCRIPTION": "Drifting halftone dot-grid editorial MG canvas backdrop",
  "INPUTS": [
    {"NAME": "bgColor", "TYPE": "vec3", "DEFAULT": [0.05, 0.04, 0.09]},
    {"NAME": "dotColor", "TYPE": "vec3", "DEFAULT": [0.38, 0.32, 0.58]},
    {"NAME": "spacingPx", "TYPE": "float", "DEFAULT": 28.0, "MIN": 8.0, "MAX": 120.0},
    {"NAME": "dotRadius", "TYPE": "float", "DEFAULT": 1.7, "MIN": 0.5, "MAX": 12.0},
    {"NAME": "driftSpeed", "TYPE": "float", "DEFAULT": 18.0, "MIN": -120.0, "MAX": 120.0},
    {"NAME": "vignette", "TYPE": "float", "DEFAULT": 0.35, "MIN": 0.0, "MAX": 0.9},
    {"NAME": "blendInput", "TYPE": "float", "DEFAULT": 0.0, "MIN": 0.0, "MAX": 1.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    vec2 p = uv * RENDERSIZE + vec2(TIME * driftSpeed, TIME * driftSpeed * 0.55);
    float sp = max(spacingPx, 4.0);
    vec2 cell = mod(p, sp) - 0.5 * sp;
    float d = length(cell);
    float dot_mask = 1.0 - smoothstep(dotRadius - 0.65, dotRadius + 0.65, d);
    float r = length(uv - vec2(0.5)) * 1.4142135;
    float vig = 1.0 - vignette * smoothstep(0.35, 1.1, r);
    vec3 canvas = clamp(mix(bgColor, dotColor, dot_mask) * vig, 0.0, 1.0);
    vec3 out_rgb = mix(canvas, src.rgb, clamp(blendInput, 0.0, 1.0));
    gl_FragColor = vec4(out_rgb, blendInput > 0.0 ? src.a : 1.0);
}
""",
    'echo_chromatic_snap': """/*{
  "DESCRIPTION": "Motion-coupled ghost echo and RGB chromatic aberration (zero on static frames)",
  "INPUTS": [
    {"NAME": "echoMix", "TYPE": "float", "DEFAULT": 0.38, "MIN": 0.0, "MAX": 0.85},
    {"NAME": "chromaPx", "TYPE": "float", "DEFAULT": 6.0, "MIN": 0.0, "MAX": 28.0},
    {"NAME": "motionGain", "TYPE": "float", "DEFAULT": 4.5, "MIN": 0.5, "MAX": 15.0},
    {"NAME": "motionThreshold", "TYPE": "float", "DEFAULT": 0.03, "MIN": 0.0, "MAX": 0.4},
    {"NAME": "grain", "TYPE": "float", "DEFAULT": 0.02, "MIN": 0.0, "MAX": 0.12}
  ]
}*/
float hash_ec(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 cur = IMG_NORM_PIXEL(inputImage, uv);
    vec4 prv = IMG_NORM_PIXEL(prevImage, uv);
    vec2 tx = 2.0 / max(RENDERSIZE, vec2(1.0));
    float m = length(cur.rgb - prv.rgb);
    m += length(IMG_NORM_PIXEL(inputImage, uv + vec2(tx.x, 0.0)).rgb - IMG_NORM_PIXEL(prevImage, uv + vec2(tx.x, 0.0)).rgb);
    m += length(IMG_NORM_PIXEL(inputImage, uv - vec2(tx.x, 0.0)).rgb - IMG_NORM_PIXEL(prevImage, uv - vec2(tx.x, 0.0)).rgb);
    m += length(IMG_NORM_PIXEL(inputImage, uv + vec2(0.0, tx.y)).rgb - IMG_NORM_PIXEL(prevImage, uv + vec2(0.0, tx.y)).rgb);
    m += length(IMG_NORM_PIXEL(inputImage, uv - vec2(0.0, tx.y)).rgb - IMG_NORM_PIXEL(prevImage, uv - vec2(0.0, tx.y)).rgb);
    m /= 5.0;
    float dyn = smoothstep(motionThreshold, motionThreshold + 0.18, m * motionGain);
    float shift = (chromaPx / max(RENDERSIZE.x, 1.0)) * dyn;
    vec3 chroma = vec3(
        IMG_NORM_PIXEL(inputImage, uv + vec2(shift, 0.0)).r,
        cur.g,
        IMG_NORM_PIXEL(inputImage, uv - vec2(shift, 0.0)).b
    );
    vec3 echoed = mix(chroma, prv.rgb, clamp(echoMix * dyn, 0.0, 0.9));
    float n = (hash_ec(uv * RENDERSIZE + vec2(TIME * 47.1, float(FRAMEINDEX) * 13.7)) - 0.5) * 2.0 * grain;
    gl_FragColor = vec4(clamp(echoed + n, 0.0, 1.0), cur.a);
}
""",
    'shutter_motion_blur': """/*{
  "DESCRIPTION": "Temporal shutter-angle directional motion blur across prev/input/next frames (zero on static pixels)",
  "INPUTS": [
    {"NAME": "shutterAngle", "TYPE": "float", "DEFAULT": 180.0, "MIN": 0.0, "MAX": 360.0},
    {"NAME": "samples", "TYPE": "float", "DEFAULT": 8.0, "MIN": 2.0, "MAX": 16.0},
    {"NAME": "maxVelocityPx", "TYPE": "float", "DEFAULT": 18.0, "MIN": 1.0, "MAX": 64.0},
    {"NAME": "motionThreshold", "TYPE": "float", "DEFAULT": 0.03, "MIN": 0.0, "MAX": 0.30},
    {"NAME": "strength", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.0, "MAX": 2.0}
  ]
}*/
float luma_smb(sampler2D img, vec2 uv) {
    vec3 c = IMG_NORM_PIXEL(img, uv).rgb;
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}
float patch_err_smb(vec2 uv, vec2 shift, vec2 px) {
    float err = 0.0;
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            vec2 off = vec2(float(i), float(j)) * px * 2.0;
            float a = luma_smb(prevImage, uv - shift + off);
            float b = luma_smb(nextImage, uv + shift + off);
            err += abs(a - b);
        }
    }
    return err / 9.0;
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 cur = IMG_NORM_PIXEL(inputImage, uv);
    vec2 px = 1.0 / max(RENDERSIZE, vec2(1.0));
    float temporal_diff = 0.0;
    temporal_diff += length(IMG_NORM_PIXEL(prevImage, uv).rgb - IMG_NORM_PIXEL(nextImage, uv).rgb);
    temporal_diff += length(IMG_NORM_PIXEL(prevImage, uv + vec2(px.x * 2.0, 0.0)).rgb - IMG_NORM_PIXEL(nextImage, uv + vec2(px.x * 2.0, 0.0)).rgb);
    temporal_diff += length(IMG_NORM_PIXEL(prevImage, uv - vec2(px.x * 2.0, 0.0)).rgb - IMG_NORM_PIXEL(nextImage, uv - vec2(px.x * 2.0, 0.0)).rgb);
    temporal_diff += length(IMG_NORM_PIXEL(prevImage, uv + vec2(0.0, px.y * 2.0)).rgb - IMG_NORM_PIXEL(nextImage, uv + vec2(0.0, px.y * 2.0)).rgb);
    temporal_diff += length(IMG_NORM_PIXEL(prevImage, uv - vec2(0.0, px.y * 2.0)).rgb - IMG_NORM_PIXEL(nextImage, uv - vec2(0.0, px.y * 2.0)).rgb);
    temporal_diff /= 5.0;

    float moving = smoothstep(motionThreshold, motionThreshold + 0.14, temporal_diff);
    float shutter_frac = clamp(shutterAngle / 360.0, 0.0, 1.0) * strength * moving;
    if (shutter_frac <= 0.002) {
        gl_FragColor = cur;
        return;
    }

    vec2 best_shift = vec2(0.0);
    float best_err = patch_err_smb(uv, vec2(0.0), px);
    for (int ring = 1; ring <= 2; ring++) {
        float rad = maxVelocityPx * (float(ring) / 2.0) * 0.5;
        for (int k = 0; k < 8; k++) {
            float ang = float(k) * 0.78539816339;
            vec2 cand = vec2(cos(ang), sin(ang)) * rad * px;
            float e = patch_err_smb(uv, cand, px);
            if (e < best_err * 0.92) {
                best_err = e;
                best_shift = cand;
            }
        }
    }
    if (length(best_shift) < px.x * 0.4) {
        vec2 grad = vec2(
            luma_smb(inputImage, uv + vec2(px.x, 0.0)) - luma_smb(inputImage, uv - vec2(px.x, 0.0)),
            luma_smb(inputImage, uv + vec2(0.0, px.y)) - luma_smb(inputImage, uv - vec2(0.0, px.y))
        );
        float dt = luma_smb(nextImage, uv) - luma_smb(prevImage, uv);
        float g2 = dot(grad, grad) + 1e-4;
        best_shift = clamp(-dt * grad / g2 * px * maxVelocityPx * 0.35, -maxVelocityPx * px, maxVelocityPx * px);
    }

    vec2 blur_vec = best_shift * shutter_frac;
    int taps = int(clamp(samples, 2.0, 16.0));
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 16; i++) {
        if (i >= taps) break;
        float t = (float(i) / max(float(taps - 1), 1.0)) - 0.5;
        acc += IMG_NORM_PIXEL(inputImage, uv + blur_vec * t);
    }
    gl_FragColor = clamp(acc / float(taps), 0.0, 1.0);
}
""",
    'deep_glow': """/*{
  "DESCRIPTION": "Multi-pass 5-level Mipmap Bloom optical Deep Glow (pmndrs/postprocessing algorithm)",
  "INPUTS": [
    {"NAME": "threshold", "TYPE": "float", "DEFAULT": 0.58, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "softKnee", "TYPE": "float", "DEFAULT": 0.45, "MIN": 0.01, "MAX": 1.0},
    {"NAME": "intensity", "TYPE": "float", "DEFAULT": 0.85, "MIN": 0.0, "MAX": 3.0},
    {"NAME": "radius", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.2, "MAX": 2.5},
    {"NAME": "exposure", "TYPE": "float", "DEFAULT": 1.0, "MIN": 0.2, "MAX": 2.5},
    {"NAME": "tint", "TYPE": "vec3", "DEFAULT": [1.0, 0.96, 1.0]},
    {"NAME": "highlightCap", "TYPE": "float", "DEFAULT": 0.98, "MIN": 0.7, "MAX": 1.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 src = IMG_NORM_PIXEL(inputImage, uv);
    float lum = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
    float w = smoothstep(threshold - softKnee, threshold + softKnee, lum);
    vec3 glow = src.rgb * w * intensity * exposure * tint;
    vec3 combined = src.rgb + glow;
    combined = min(vec3(highlightCap), combined / (1.0 + max(vec3(0.0), combined - 0.85) * 1.4));
    gl_FragColor = vec4(clamp(combined, 0.0, 1.0), src.a);
}
""",
    'layer_motion_blur': """/*{
  "DESCRIPTION": "Premultiplied alpha-safe directional & zoom motion blur for animated foreground layers on entry/exit",
  "INPUTS": [
    {"NAME": "inDuration", "TYPE": "float", "DEFAULT": 0.18, "MIN": 0.0, "MAX": 2.0},
    {"NAME": "inAngleDeg", "TYPE": "float", "DEFAULT": 0.0, "MIN": -360.0, "MAX": 360.0},
    {"NAME": "inBlurPx", "TYPE": "float", "DEFAULT": 36.0, "MIN": 0.0, "MAX": 120.0},
    {"NAME": "inZoom", "TYPE": "float", "DEFAULT": 0.04, "MIN": -0.35, "MAX": 0.35},
    {"NAME": "outDuration", "TYPE": "float", "DEFAULT": 0.16, "MIN": 0.0, "MAX": 2.0},
    {"NAME": "outAngleDeg", "TYPE": "float", "DEFAULT": 180.0, "MIN": -360.0, "MAX": 360.0},
    {"NAME": "outBlurPx", "TYPE": "float", "DEFAULT": 30.0, "MIN": 0.0, "MAX": 120.0},
    {"NAME": "outZoom", "TYPE": "float", "DEFAULT": 0.0, "MIN": -0.35, "MAX": 0.35},
    {"NAME": "samples", "TYPE": "float", "DEFAULT": 16.0, "MIN": 2.0, "MAX": 24.0}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 base = IMG_NORM_PIXEL(inputImage, uv);
    float dur = max(DURATION, 0.05);
    float w_in = (inDuration > 0.001 && TIME < inDuration)
        ? pow(clamp(1.0 - TIME / inDuration, 0.0, 1.0), 2.1)
        : 0.0;
    float out_start = dur - outDuration;
    float w_out = (outDuration > 0.001 && TIME > out_start)
        ? pow(clamp((TIME - out_start) / outDuration, 0.0, 1.0), 2.1)
        : 0.0;

    float a_in = radians(inAngleDeg);
    float a_out = radians(outAngleDeg);
    vec2 px = 1.0 / max(RENDERSIZE, vec2(1.0));
    vec2 dir_vec = vec2(cos(a_in), sin(a_in)) * (inBlurPx * w_in) * px
                 + vec2(cos(a_out), sin(a_out)) * (outBlurPx * w_out) * px;
    float zoom_amt = inZoom * w_in + outZoom * w_out;
    vec2 rad_vec = (uv - vec2(0.5)) * zoom_amt;
    vec2 total_vec = dir_vec + rad_vec;

    if (length(total_vec) < max(px.x, px.y) * 0.35) {
        gl_FragColor = base;
        return;
    }

    int taps = int(clamp(samples, 2.0, 24.0));
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 24; i++) {
        if (i >= taps) break;
        float t = (float(i) / max(float(taps - 1), 1.0)) - 0.5;
        vec2 suv = uv + total_vec * t;
        if (suv.x < 0.0 || suv.x > 1.0 || suv.y < 0.0 || suv.y > 1.0) continue;
        vec4 c = IMG_NORM_PIXEL(inputImage, suv);
        acc += vec4(c.rgb * c.a, c.a);
    }
    acc /= float(taps);
    vec3 out_rgb = acc.a > 1e-4 ? clamp(acc.rgb / acc.a, 0.0, 1.0) : base.rgb;
    gl_FragColor = vec4(out_rgb, clamp(acc.a, 0.0, 1.0));
}
""",
    'light_wrap': """/*{
  "DESCRIPTION": "Optical background edge light-wrap for natural anime cutout integration",
  "INPUTS": [
    {"NAME": "wrapRadiusPx", "TYPE": "float", "DEFAULT": 14.0, "MIN": 1.0, "MAX": 48.0},
    {"NAME": "intensity", "TYPE": "float", "DEFAULT": 0.65, "MIN": 0.0, "MAX": 2.0},
    {"NAME": "lumaThreshold", "TYPE": "float", "DEFAULT": 0.42, "MIN": 0.0, "MAX": 0.95},
    {"NAME": "softKnee", "TYPE": "float", "DEFAULT": 0.25, "MIN": 0.02, "MAX": 0.8},
    {"NAME": "wrapTint", "TYPE": "vec3", "DEFAULT": [1.0, 0.96, 0.92]},
    {"NAME": "highlightCap", "TYPE": "float", "DEFAULT": 0.98, "MIN": 0.7, "MAX": 1.0},
    {"NAME": "outerHalo", "TYPE": "float", "DEFAULT": 0.12, "MIN": 0.0, "MAX": 0.6}
  ]
}*/
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 fg = IMG_NORM_PIXEL(inputImage, uv);
    vec2 px = wrapRadiusPx / max(RENDERSIZE, vec2(1.0));

    float inv_a_sum = 0.0;
    float fg_a_sum = 0.0;
    vec3 bg_sum = vec3(0.0);
    for (int ring = 1; ring <= 2; ring++) {
        float r = float(ring) * 0.5;
        for (int k = 0; k < 8; k++) {
            float ang = float(k) * 0.78539816339 + float(ring) * 0.392699;
            vec2 suv = clamp(uv + vec2(cos(ang), sin(ang)) * px * r, 0.0, 1.0);
            float sa = IMG_NORM_PIXEL(inputImage, suv).a;
            inv_a_sum += (1.0 - sa);
            fg_a_sum += sa;
            if (hasBgImage > 0) {
                bg_sum += texture(bgImage, suv).rgb;
            } else {
                bg_sum += wrapTint;
            }
        }
    }
    float inv_a_blur = inv_a_sum / 16.0;
    float outer_fg = fg_a_sum / 16.0;
    vec3 bg_blur = bg_sum / 16.0;

    float bg_luma = dot(bg_blur, vec3(0.2126, 0.7152, 0.0722));
    float hl_weight = hasBgImage > 0
        ? smoothstep(lumaThreshold - softKnee, lumaThreshold + softKnee, bg_luma)
        : 1.0;

    float inner_rim = clamp(inv_a_blur * 1.8, 0.0, 1.0) * fg.a;
    vec3 wrap_col = clamp(bg_blur * wrapTint * hl_weight * inner_rim * intensity, 0.0, 0.85);
    vec3 screened = 1.0 - (1.0 - fg.rgb) * (1.0 - wrap_col);
    screened = min(vec3(highlightCap), clamp(screened, 0.0, 1.0));

    float halo_mask = clamp(outer_fg * (1.0 - fg.a) * hl_weight * intensity * outerHalo, 0.0, 0.55);
    vec3 out_rgb = mix(screened, clamp(bg_blur * wrapTint, 0.0, highlightCap), halo_mask * (1.0 - fg.a));
    float out_a = clamp(max(fg.a, halo_mask), 0.0, 1.0);
    gl_FragColor = vec4(out_rgb, out_a);
}
""",
}

_DEEP_GLOW_PREFILTER_GLSL = """#version 330 core
in vec2 isf_FragNormCoord;
out vec4 o_fragColor;
uniform sampler2D u_src;
uniform float threshold;
uniform float softKnee;
uniform int useAlpha;
void main() {
    vec4 src = texture(u_src, clamp(isf_FragNormCoord, 0.0, 1.0));
    vec3 c = src.rgb * (useAlpha > 0 ? src.a : 1.0);
    float br = max(c.r, max(c.g, c.b));
    float knee = max(threshold * softKnee, 1e-4);
    float soft = clamp(br - threshold + knee, 0.0, 2.0 * knee);
    soft = (soft * soft) / (4.0 * knee + 1e-4);
    float contrib = max(soft, br - threshold) / max(br, 1e-4);
    o_fragColor = vec4(c * max(contrib, 0.0), 1.0);
}
"""

_DEEP_GLOW_DOWN_GLSL = """#version 330 core
in vec2 isf_FragNormCoord;
out vec4 o_fragColor;
uniform sampler2D u_src;
uniform vec2 texelSize;
float luma_karis(vec3 c) {
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}
vec4 karis_sample(vec2 uv) {
    vec3 c = texture(u_src, clamp(uv, 0.0, 1.0)).rgb;
    float w = 1.0 / (1.0 + luma_karis(c));
    return vec4(c * w, w);
}
void main() {
    vec2 uv = isf_FragNormCoord;
    vec2 d = texelSize * 1.0;
    vec4 s = karis_sample(uv) * 0.4;
    s += karis_sample(uv + vec2(-d.x, -d.y)) * 0.15;
    s += karis_sample(uv + vec2( d.x, -d.y)) * 0.15;
    s += karis_sample(uv + vec2(-d.x,  d.y)) * 0.15;
    s += karis_sample(uv + vec2( d.x,  d.y)) * 0.15;
    o_fragColor = vec4(s.rgb / max(s.a, 1e-5), 1.0);
}
"""

_DEEP_GLOW_UP_GLSL = """#version 330 core
in vec2 isf_FragNormCoord;
out vec4 o_fragColor;
uniform sampler2D u_low;
uniform sampler2D u_curr;
uniform vec2 texelSize;
uniform float radius;
uniform float decayWeight;
void main() {
    vec2 uv = isf_FragNormCoord;
    vec2 d = texelSize * radius * 1.15;
    vec3 tent = texture(u_low, clamp(uv, 0.0, 1.0)).rgb * 4.0;
    tent += (texture(u_low, clamp(uv + vec2(-d.x, 0.0), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2( d.x, 0.0), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2(0.0, -d.y), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2(0.0,  d.y), 0.0, 1.0)).rgb) * 2.0;
    tent += (texture(u_low, clamp(uv + vec2(-d.x, -d.y), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2( d.x, -d.y), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2(-d.x,  d.y), 0.0, 1.0)).rgb
           + texture(u_low, clamp(uv + vec2( d.x,  d.y), 0.0, 1.0)).rgb);
    tent /= 16.0;
    vec3 curr = texture(u_curr, clamp(uv, 0.0, 1.0)).rgb;
    o_fragColor = vec4(curr + tent * decayWeight, 1.0);
}
"""

_DEEP_GLOW_COMP_GLSL = """#version 330 core
in vec2 isf_FragNormCoord;
out vec4 o_fragColor;
uniform sampler2D u_orig;
uniform sampler2D u_bloom;
uniform float intensity;
uniform float exposure;
uniform vec3 tint;
uniform float highlightCap;
uniform int useAlpha;
void main() {
    vec2 uv = isf_FragNormCoord;
    vec4 orig = texture(u_orig, clamp(uv, 0.0, 1.0));
    vec3 bloom = texture(u_bloom, clamp(uv, 0.0, 1.0)).rgb;
    vec3 glow = bloom * intensity * exposure * tint;
    vec3 combined = orig.rgb + glow;
    float peak = max(combined.r, max(combined.g, combined.b));
    if (peak > 0.85) {
        float mapped = 0.85 + (peak - 0.85) / (1.0 + (peak - 0.85) * 1.6);
        mapped = min(highlightCap, mapped);
        combined *= (mapped / max(peak, 1e-5));
    }
    combined = min(vec3(highlightCap), clamp(combined, 0.0, 1.0));
    float out_a = orig.a;
    if (useAlpha > 0) {
        float g_peak = max(glow.r, max(glow.g, glow.b));
        out_a = clamp(max(orig.a, g_peak * 0.9), 0.0, 1.0);
    }
    o_fragColor = vec4(combined, out_a);
}
"""


def _run_deep_glow_multipass(ctx, frames, tw, th, comps, uniforms):
    """Render 5-level Mipmap Bloom (pmndrs/postprocessing style) cleanly on ModernGL."""
    quad = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype='f4')
    vbo = ctx.buffer(quad.tobytes())
    p_pre = p_down = p_up = p_comp = None
    vaos = []
    textures = []
    fbos = []
    tex_in = tex_out = fbo_out = None
    rendered = []

    def _make_tex(w, h, channels=4, dtype='f2'):
        t = ctx.texture((w, h), channels, dtype=dtype)
        t.repeat_x = False
        t.repeat_y = False
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        textures.append(t)
        return t

    try:
        p_pre = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=_DEEP_GLOW_PREFILTER_GLSL)
        p_down = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=_DEEP_GLOW_DOWN_GLSL)
        p_up = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=_DEEP_GLOW_UP_GLSL)
        p_comp = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=_DEEP_GLOW_COMP_GLSL)

        vao_pre = ctx.simple_vertex_array(p_pre, vbo, 'a_pos')
        vao_down = ctx.simple_vertex_array(p_down, vbo, 'a_pos')
        vao_up = ctx.simple_vertex_array(p_up, vbo, 'a_pos')
        vao_comp = ctx.simple_vertex_array(p_comp, vbo, 'a_pos')
        vaos.extend([vao_pre, vao_down, vao_up, vao_comp])

        tex_in = ctx.texture((tw, th), comps)
        tex_in.repeat_x = False
        tex_in.repeat_y = False
        tex_in.filter = (moderngl.LINEAR, moderngl.LINEAR)

        tex_out = ctx.texture((tw, th), comps)
        fbo_out = ctx.framebuffer(color_attachments=[tex_out])

        dims = [(max(4, tw >> lvl), max(4, th >> lvl)) for lvl in range(5)]
        # Verify f2 float attachment works on active GL driver, otherwise fall back to f4.
        hdr_dtype = 'f2'
        probe_t = ctx.texture(dims[0], 4, dtype='f2')
        try:
            probe_f = ctx.framebuffer(color_attachments=[probe_t])
            probe_f.release()
        except Exception:
            hdr_dtype = 'f4'
        finally:
            probe_t.release()

        down_tex = [_make_tex(w, h, 4, hdr_dtype) for (w, h) in dims]
        down_fbo = [ctx.framebuffer(color_attachments=[t]) for t in down_tex]
        fbos.extend(down_fbo)
        # Upsample chain targets (L3..L0) kept separate from down_tex so no feedback hazard.
        up_tex = [_make_tex(w, h, 4, hdr_dtype) for (w, h) in dims[:4]]
        up_fbo = [ctx.framebuffer(color_attachments=[t]) for t in up_tex]
        fbos.extend(up_fbo)

        threshold = float(uniforms.get('threshold', 0.58))
        soft_knee = max(0.01, float(uniforms.get('softKnee', 0.45)))
        intensity = float(uniforms.get('intensity', 0.85))
        radius = float(uniforms.get('radius', 1.0))
        exposure = float(uniforms.get('exposure', 1.0))
        tint_raw = uniforms.get('tint', [1.0, 0.96, 1.0])
        tint = (float(tint_raw[0]), float(tint_raw[1]), float(tint_raw[2]))
        highlight_cap = float(uniforms.get('highlightCap', 0.98))
        use_alpha = 1 if comps == 4 else 0

        p_pre['u_src'].value = 0
        p_pre['threshold'].value = threshold
        p_pre['softKnee'].value = soft_knee
        p_pre['useAlpha'].value = use_alpha

        p_down['u_src'].value = 0
        p_up['u_low'].value = 0
        p_up['u_curr'].value = 1
        p_up['radius'].value = radius

        p_comp['u_orig'].value = 0
        p_comp['u_bloom'].value = 1
        p_comp['intensity'].value = intensity
        p_comp['exposure'].value = exposure
        p_comp['tint'].value = tint
        p_comp['highlightCap'].value = highlight_cap
        p_comp['useAlpha'].value = use_alpha

        for fr in frames:
            tex_in.write(np.ascontiguousarray(fr[::-1]).tobytes())

            # 1. Prefilter bright pass -> down_fbo[0]
            down_fbo[0].use()
            ctx.viewport = (0, 0, dims[0][0], dims[0][1])
            tex_in.use(0)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            vao_pre.render(moderngl.TRIANGLE_STRIP)

            # 2. Downsample L0 -> L1 -> L2 -> L3 -> L4
            for lvl in range(1, 5):
                prev_w, prev_h = dims[lvl - 1]
                cur_w, cur_h = dims[lvl]
                down_fbo[lvl].use()
                ctx.viewport = (0, 0, cur_w, cur_h)
                p_down['texelSize'].value = (1.0 / prev_w, 1.0 / prev_h)
                down_tex[lvl - 1].use(0)
                ctx.clear(0.0, 0.0, 0.0, 0.0)
                vao_down.render(moderngl.TRIANGLE_STRIP)

            # 3. Upsample L4 -> up[3] -> up[2] -> up[1] -> up[0]
            low_t = down_tex[4]
            low_dim = dims[4]
            for idx, weight in ((3, 0.85), (2, 0.82), (1, 0.78), (0, 0.74)):
                cur_w, cur_h = dims[idx]
                up_fbo[idx].use()
                ctx.viewport = (0, 0, cur_w, cur_h)
                p_up['texelSize'].value = (1.0 / low_dim[0], 1.0 / low_dim[1])
                p_up['decayWeight'].value = weight
                low_t.use(0)
                down_tex[idx].use(1)
                ctx.clear(0.0, 0.0, 0.0, 0.0)
                vao_up.render(moderngl.TRIANGLE_STRIP)
                low_t = up_tex[idx]
                low_dim = (cur_w, cur_h)

            # 4. Composite orig + up[0] -> fbo_out
            fbo_out.use()
            ctx.viewport = (0, 0, tw, th)
            tex_in.use(0)
            up_tex[0].use(1)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            vao_comp.render(moderngl.TRIANGLE_STRIP)

            raw_out = fbo_out.read(components=comps)
            rendered.append(np.frombuffer(raw_out, dtype=np.uint8).reshape(th, tw, comps)[::-1].copy())
        return rendered
    finally:
        for obj in [*vaos, *fbos, *textures, fbo_out, tex_out, tex_in, vbo, p_pre, p_down, p_up, p_comp]:
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass


def _ease(t, kind):
    t = max(0.0, min(1.0, float(t)))
    if kind == 'linear':
        return t
    if kind == 'ease_in_out':
        return t * t * (3.0 - 2.0 * t)
    if kind == 'expo_in_out':
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        if t < 0.5:
            return 0.5 * math.pow(2.0, 20.0 * t - 10.0)
        return 1.0 - 0.5 * math.pow(2.0, -20.0 * t + 10.0)
    raise ValueError(f'Unknown easing curve: {kind}')


def _read_media_frames(path, target_w=0, target_h=0, max_frames=900, default_fps=30.0, still_frames=1, want_alpha=False):
    src = Path(path).resolve()
    if not src.is_file():
        raise ValueError(f'Media file not found: {path}')
    channels = 4 if want_alpha else 3
    if src.suffix.lower() in _IMAGE_EXTS:
        with Image.open(src) as img:
            arr = np.asarray(img.convert('RGBA' if want_alpha else 'RGB'), dtype=np.uint8)
        h, w = arr.shape[:2]
        tw = target_w or max(2, w // 2 * 2)
        th = target_h or max(2, h // 2 * 2)
        if (w, h) != (tw, th):
            arr = cv2.resize(arr, (tw, th), interpolation=cv2.INTER_AREA)
        frames = [arr] * max(1, int(still_frames))
        return np.stack(frames, axis=0), float(default_fps), tw, th, False, False

    info = core.probe(src)
    has_audio = bool(info.get('has_audio'))
    if want_alpha and info.get('has_alpha'):
        sw = int(info.get('width') or 0)
        sh = int(info.get('height') or 0)
        if sw <= 0 or sh <= 0:
            raise ValueError(f'Failed to probe video dimensions: {path}')
        tw = target_w or max(2, sw // 2 * 2)
        th = target_h or max(2, sh // 2 * 2)
        fps = float(info.get('fps') or default_fps) or default_fps
        cmd = [
            core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-i', str(src), '-frames:v', str(int(max_frames)),
        ]
        if (sw, sh) != (tw, th):
            cmd.extend(['-vf', f'scale={tw}:{th}:flags=area'])
        cmd.extend(['-f', 'rawvideo', '-pix_fmt', 'rgba', '-'])
        proc = subprocess.run(cmd, capture_output=True, timeout=600, creationflags=_NO_WIN)
        if proc.returncode != 0:
            raise RuntimeError(f'FFmpeg RGBA decode failed: {proc.stderr.decode("utf-8", "replace")[-1200:]}')
        frame_bytes = tw * th * 4
        n_frames = len(proc.stdout) // frame_bytes
        if n_frames <= 0:
            raise ValueError(f'Decoded 0 RGBA video frames from {path}')
        arr = np.frombuffer(proc.stdout[:n_frames * frame_bytes], dtype=np.uint8).reshape(n_frames, th, tw, 4).copy()
        return arr, round(fps, 4), tw, th, True, has_audio

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise ValueError(f'Failed to open video: {path}')
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or default_fps) or default_fps
        raw = []
        while len(raw) < max_frames:
            ok, bgr = cap.read()
            if not ok:
                break
            raw.append(bgr)
    finally:
        cap.release()
    if not raw:
        raise ValueError(f'Decoded 0 video frames from {path}')

    sh, sw = raw[0].shape[:2]
    tw = target_w or max(2, sw // 2 * 2)
    th = target_h or max(2, sh // 2 * 2)
    out = []
    for bgr in raw:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if rgb.shape[1] != tw or rgb.shape[0] != th:
            rgb = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_AREA)
        if channels == 4:
            alpha = np.full((th, tw, 1), 255, dtype=np.uint8)
            rgb = np.concatenate([rgb, alpha], axis=-1)
        out.append(rgb)
    return np.stack(out, axis=0), round(fps, 4), tw, th, True, has_audio


def _dilate_field(val_arr, fg_mask, sigmas=(1.5, 5.0, 15.0, 42.0)):
    """Fill transparent/background pixels by progressive normalized Gaussian diffusion from foreground."""
    fg_f = fg_mask.astype(np.float32)
    cur = val_arr.astype(np.float32).copy()
    if float(fg_f.max()) <= 1e-6 or float(fg_f.min()) >= 1.0 - 1e-6:
        return cur
    unfilled = fg_f < 0.5
    is_color = cur.ndim == 3
    src_vals = cur * (fg_f[..., None] if is_color else fg_f)
    for sig in sigmas:
        if not unfilled.any():
            break
        w_blur = cv2.GaussianBlur(fg_f, (0, 0), sigmaX=sig, sigmaY=sig)
        v_blur = cv2.GaussianBlur(src_vals, (0, 0), sigmaX=sig, sigmaY=sig)
        valid = unfilled & (w_blur > 1e-4)
        if valid.any():
            denom = w_blur[valid][..., None] if is_color else w_blur[valid]
            cur[valid] = v_blur[valid] / denom
            unfilled[valid] = False
    if unfilled.any():
        cur[unfilled] = float(np.mean(cur[~unfilled])) if (~unfilled).any() else 0.0
    return cur


def _edge_extend_rgba(frames_rgba, static_single=False):
    """Extend foreground RGB colors smoothly into transparent border pixels to prevent dark fringe halos."""
    limit = 1 if static_single else len(frames_rgba)
    out = frames_rgba.copy()
    for i in range(limit):
        f = frames_rgba[i]
        fg = f[..., 3] > 20
        if fg.any() and not fg.all():
            ext = np.clip(_dilate_field(f[..., :3], fg, sigmas=(1.4, 4.0, 10.0)), 0.0, 255.0).round().astype(np.uint8)
            # Keep original sharp interior RGB intact; replace only transparent exterior RGB.
            inv = ~fg
            out[i, inv, :3] = ext[inv]
    if static_single and len(out) > 1:
        out[1:] = out[0]
    return out


def _parse_params_json(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid --params JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('--params must decode to a JSON object')
    return parsed


def _parse_gl_transition(glsl_text, defaults=None):
    lines = []
    for line in glsl_text.splitlines():
        st = line.strip()
        if st.startswith('#version') or st.startswith('precision '):
            continue
        lines.append(line)
    body = '\n'.join(lines)
    if 'transition(' not in body:
        raise ValueError('Custom GL-Transition .glsl must define vec4 transition(vec2 uv)')
    declared = set(re.findall(r'\buniform\s+\w+\s+(\w+)', body))
    merged = dict(defaults or {})
    extra_lines = []
    for name, val in merged.items():
        if name in declared or name in ('progress', 'ratio', 'u_from', 'u_to'):
            continue
        if isinstance(val, (tuple, list)):
            gtype = f'vec{len(val)}'
        elif isinstance(val, bool):
            gtype = 'bool'
        elif isinstance(val, int):
            gtype = 'int'
        else:
            gtype = 'float'
        extra_lines.append(f'uniform {gtype} {name};')
    src = _GL_TRANSITION_HEADER.format(extra_uniforms='\n'.join(extra_lines), body=body)
    return src, merged


def _parse_isf_shader(isf_text):
    m = re.match(r'\s*/\*\s*(\{.*?\})\s*\*/', isf_text, flags=re.S)
    meta = {}
    body = isf_text
    if m:
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError(f'Invalid ISF JSON header: {exc}') from exc
        body = isf_text[m.end():]
    lines = []
    for line in body.splitlines():
        st = line.strip()
        if st.startswith('#version') or st.startswith('precision '):
            continue
        lines.append(line)
    clean_body = '\n'.join(lines)
    declared = set(re.findall(r'\buniform\s+\w+\s+(\w+)', clean_body))

    defaults = {}
    extra_lines = []
    type_map = {
        'float': 'float',
        'bool': 'bool',
        'long': 'int',
        'int': 'int',
        'point2D': 'vec2',
        'vec2': 'vec2',
        'vec3': 'vec3',
        'color': 'vec4',
        'vec4': 'vec4',
    }
    for inp in meta.get('INPUTS') or []:
        name = inp.get('NAME')
        kind = inp.get('TYPE', 'float')
        if not name or kind in ('image', 'audio', 'audioFFT'):
            continue
        glsl_t = type_map.get(kind, 'float')
        if name not in declared:
            extra_lines.append(f'uniform {glsl_t} {name};')
        if 'DEFAULT' in inp:
            defaults[name] = inp['DEFAULT']
    src = _ISF_HEADER.format(extra_uniforms='\n'.join(extra_lines), body=clean_body)
    return src, defaults, meta


def _apply_uniforms(prog, values):
    for key, raw in values.items():
        if key not in prog:
            continue
        val = raw
        if isinstance(val, list):
            val = tuple(float(x) for x in val)
        elif isinstance(val, tuple):
            val = tuple(float(x) for x in val)
        prog[key].value = val


def _encode_frames(frames, fps, out_path, *, alpha=False, audio_src=None):
    _, h, w, _ = frames.shape
    ffmpeg = core.tool('ffmpeg')
    pix_in = 'rgba' if alpha else 'rgb24'
    cmd = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'rawvideo', '-pix_fmt', pix_in, '-s', f'{w}x{h}', '-r', f'{fps:.6f}', '-i', '-',
    ]
    if audio_src is not None:
        cmd.extend(['-protocol_whitelist', 'file,pipe', '-i', str(audio_src), '-map', '0:v:0', '-map', '1:a:0'])
    else:
        cmd.extend(['-map', '0:v:0'])

    if alpha:
        cmd.extend([
            '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p12le',
        ])
    else:
        cmd.extend([
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '14', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
        ])
    if audio_src is not None:
        cmd.extend(['-c:a', 'pcm_s16le' if alpha else 'aac', '-shortest'])
    cmd.extend(['-map_metadata', '-1', str(out_path)])
    proc = subprocess.run(
        cmd, input=np.ascontiguousarray(frames).tobytes(), capture_output=True, timeout=900, creationflags=_NO_WIN,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'FFmpeg shader encode failed: {proc.stderr.decode("utf-8", "replace")[-1500:]}')
    core.run([ffmpeg, '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-i', str(out_path), '-f', 'null', '-'], timeout=300)


def run_transition(args):
    src_a = Path(args.from_media).resolve()
    src_b = Path(args.to_media).resolve()
    if not src_a.is_file():
        raise ValueError(f'--from media not found: {args.from_media}')
    if not src_b.is_file():
        raise ValueError(f'--to media not found: {args.to_media}')

    duration = core.number(args.duration, 'duration', 0.1, 10.0)
    fps = core.number(args.fps, 'fps', 1.0, 120.0)
    req_w = int(args.width)
    req_h = int(args.height)
    if req_w < 0 or req_h < 0 or (req_w and not 64 <= req_w <= 4096) or (req_h and not 64 <= req_h <= 4096):
        raise ValueError('--width and --height must be 0 (auto) or in [64, 4096]')
    if req_w:
        req_w = req_w // 2 * 2
    if req_h:
        req_h = req_h // 2 * 2

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    total_frames = max(2, int(round(duration * fps)))
    frames_a, _, tw, th, _, _ = _read_media_frames(src_a, target_w=req_w, target_h=req_h, still_frames=total_frames)
    frames_b, _, _, _, _, _ = _read_media_frames(src_b, target_w=tw, target_h=th, still_frames=total_frames)

    if len(frames_a) >= total_frames:
        seq_a = frames_a[-total_frames:]
    else:
        pad = np.repeat(frames_a[-1:], total_frames - len(frames_a), axis=0)
        seq_a = np.concatenate([frames_a, pad], axis=0)
    if len(frames_b) >= total_frames:
        seq_b = frames_b[:total_frames]
    else:
        pad = np.repeat(frames_b[-1:], total_frames - len(frames_b), axis=0)
        seq_b = np.concatenate([frames_b, pad], axis=0)

    user_params = _parse_params_json(args.params)
    if args.shader:
        sp = Path(args.shader).resolve()
        if not sp.is_file():
            raise ValueError(f'Custom GL-Transition shader not found: {args.shader}')
        glsl_src, defaults = _parse_gl_transition(sp.read_text(encoding='utf-8-sig'))
        preset_name = f'custom:{sp.name}'
    else:
        spec = TRANSITION_PRESETS[args.preset]
        glsl_src, defaults = _parse_gl_transition(spec['glsl'], spec['defaults'])
        preset_name = args.preset
    uniforms = {**defaults, **user_params}

    rendered = []
    renderer_name = None
    gl_ver = None
    ctx = moderngl.create_standalone_context()
    prog = vbo = vao = fbo = tex_a = tex_b = tex_out = None
    try:
        renderer_name = ctx.info.get('GL_RENDERER')
        gl_ver = ctx.info.get('GL_VERSION')
        prog = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=glsl_src)
        quad = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype='f4')
        vbo = ctx.buffer(quad.tobytes())
        vao = ctx.simple_vertex_array(prog, vbo, 'a_pos')
        tex_a = ctx.texture((tw, th), 3)
        tex_b = ctx.texture((tw, th), 3)
        tex_out = ctx.texture((tw, th), 3)
        for tex in (tex_a, tex_b):
            tex.repeat_x = False
            tex.repeat_y = False
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        fbo = ctx.framebuffer(color_attachments=[tex_out])
        fbo.use()

        if 'u_from' in prog:
            prog['u_from'].value = 0
        if 'u_to' in prog:
            prog['u_to'].value = 1
        if 'ratio' in prog:
            prog['ratio'].value = float(tw) / float(th)
        _apply_uniforms(prog, uniforms)

        for idx in range(total_frames):
            # Flip Y vertically before upload and after readback so UV y=1 stays top row
            tex_a.write(np.ascontiguousarray(seq_a[idx][::-1]).tobytes())
            tex_b.write(np.ascontiguousarray(seq_b[idx][::-1]).tobytes())
            tex_a.use(0)
            tex_b.use(1)
            raw_t = idx / float(total_frames - 1) if total_frames > 1 else 0.5
            if 'progress' in prog:
                prog['progress'].value = _ease(raw_t, args.easing)
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            vao.render(moderngl.TRIANGLE_STRIP)
            out_rgb = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(th, tw, 3)[::-1].copy()
            rendered.append(out_rgb)
    finally:
        for obj in (vao, vbo, fbo, tex_a, tex_b, tex_out, prog, ctx):
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass

    out_frames = np.stack(rendered, axis=0)
    out_dir = core.new_directory(out_cand)
    out_mp4 = out_dir / 'transition.mp4'
    preview_png = out_dir / 'preview_mid.png'
    mid_idx = len(out_frames) // 2
    Image.fromarray(out_frames[mid_idx], mode='RGB').save(preview_png)
    _encode_frames(out_frames, fps, out_mp4, alpha=False, audio_src=None)

    report = {
        'status': 'passed',
        'operation': 'shader-fx',
        'mode': 'transition',
        'preset': preset_name,
        'easing': args.easing,
        'renderer': renderer_name,
        'opengl_version': gl_ver,
        'width': tw,
        'height': th,
        'fps': round(fps, 4),
        'frames': int(len(out_frames)),
        'duration': round(len(out_frames) / fps, 4),
        'from_sha256': file_hash.sha256(src_a),
        'to_sha256': file_hash.sha256(src_b),
        'output': str(out_mp4),
        'preview_png': str(preview_png),
        'strict_decode': 'passed',
    }
    core.write_json(out_dir / 'shader-fx-report.json', report)
    return report


def run_effect(args):
    src = Path(args.input).resolve()
    if not src.is_file():
        raise ValueError(f'--input media not found: {args.input}')

    duration = core.number(args.duration, 'duration', 0.1, 30.0)
    fps = core.number(args.fps, 'fps', 1.0, 120.0)
    max_frames = int(args.max_frames)
    if not 1 <= max_frames <= 3600:
        raise ValueError(f'--max-frames must be in [1, 3600], got {max_frames}')

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    want_alpha = bool(args.alpha)
    still_count = max(1, int(round(duration * fps)))
    frames, src_fps, tw, th, is_video, has_audio = _read_media_frames(
        src, max_frames=max_frames, default_fps=fps, still_frames=still_count, want_alpha=want_alpha,
    )
    use_fps = src_fps if is_video else fps
    comps = 4 if want_alpha else 3

    user_params = _parse_params_json(args.params)
    if args.shader:
        sp = Path(args.shader).resolve()
        if not sp.is_file():
            raise ValueError(f'Custom ISF .fs shader not found: {args.shader}')
        glsl_src, defaults, isf_meta = _parse_isf_shader(sp.read_text(encoding='utf-8-sig'))
        preset_name = f'custom:{sp.name}'
    else:
        glsl_src, defaults, isf_meta = _parse_isf_shader(ISF_PRESETS[args.preset])
        preset_name = args.preset

    uniforms = {**defaults, **user_params}

    bg_frames = None
    if getattr(args, 'bg', None):
        bg_path = Path(args.bg).resolve()
        if not bg_path.is_file():
            raise ValueError(f'--bg media not found: {args.bg}')
        bg_frames, _, _, _, _, _ = _read_media_frames(
            bg_path, target_w=tw, target_h=th, max_frames=max(len(frames), 1),
            default_fps=use_fps, still_frames=len(frames), want_alpha=False,
        )

    if want_alpha and preset_name == 'layer_motion_blur':
        frames = _edge_extend_rgba(frames, static_single=not is_video)

    rendered = []
    renderer_name = None
    gl_ver = None
    ctx = moderngl.create_standalone_context()
    prog = vbo = vao = fbo = tex_in = tex_prev = tex_next = tex_bg = tex_out = None
    try:
        renderer_name = ctx.info.get('GL_RENDERER')
        gl_ver = ctx.info.get('GL_VERSION')
        if preset_name == 'deep_glow' and not args.shader:
            rendered = _run_deep_glow_multipass(ctx, frames, tw, th, comps, uniforms)
        else:
            prog = ctx.program(vertex_shader=_VERT_GLSL, fragment_shader=glsl_src)
            quad = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype='f4')
            vbo = ctx.buffer(quad.tobytes())
            vao = ctx.simple_vertex_array(prog, vbo, 'a_pos')
            tex_in = ctx.texture((tw, th), comps)
            tex_out = ctx.texture((tw, th), comps)
            for t_obj in (tex_in,):
                t_obj.repeat_x = False
                t_obj.repeat_y = False
                t_obj.filter = (moderngl.LINEAR, moderngl.LINEAR)
            uses_prev = 'prevImage' in prog
            uses_next = 'nextImage' in prog
            uses_bg = bg_frames is not None and 'bgImage' in prog
            if uses_prev:
                tex_prev = ctx.texture((tw, th), comps)
                tex_prev.repeat_x = False
                tex_prev.repeat_y = False
                tex_prev.filter = (moderngl.LINEAR, moderngl.LINEAR)
            if uses_next:
                tex_next = ctx.texture((tw, th), comps)
                tex_next.repeat_x = False
                tex_next.repeat_y = False
                tex_next.filter = (moderngl.LINEAR, moderngl.LINEAR)
            if uses_bg:
                tex_bg = ctx.texture((tw, th), 3)
                tex_bg.repeat_x = False
                tex_bg.repeat_y = False
                tex_bg.filter = (moderngl.LINEAR, moderngl.LINEAR)
            fbo = ctx.framebuffer(color_attachments=[tex_out])
            fbo.use()

            if 'inputImage' in prog:
                prog['inputImage'].value = 0
            if uses_prev:
                prog['prevImage'].value = 1
            if uses_next:
                prog['nextImage'].value = 2
            if uses_bg:
                prog['bgImage'].value = 3
            if 'hasBgImage' in prog:
                prog['hasBgImage'].value = 1 if uses_bg else 0
            if 'DURATION' in prog:
                prog['DURATION'].value = float(len(frames)) / float(use_fps)
            if 'RENDERSIZE' in prog:
                prog['RENDERSIZE'].value = (float(tw), float(th))
            if 'PASSINDEX' in prog:
                prog['PASSINDEX'].value = 0
            _apply_uniforms(prog, uniforms)

            for idx in range(len(frames)):
                if is_video or idx == 0:
                    tex_in.write(np.ascontiguousarray(frames[idx][::-1]).tobytes())
                tex_in.use(0)
                if uses_prev:
                    prev_src = frames[idx - 1] if idx > 0 else frames[0]
                    tex_prev.write(np.ascontiguousarray(prev_src[::-1]).tobytes())
                    tex_prev.use(1)
                if uses_next:
                    next_src = frames[idx + 1] if idx + 1 < len(frames) else frames[-1]
                    tex_next.write(np.ascontiguousarray(next_src[::-1]).tobytes())
                    tex_next.use(2)
                if uses_bg:
                    bg_src = bg_frames[idx % len(bg_frames)]
                    tex_bg.write(np.ascontiguousarray(bg_src[::-1]).tobytes())
                    tex_bg.use(3)
                if 'TIME' in prog:
                    prog['TIME'].value = float(idx) / float(use_fps)
                if 'FRAMEINDEX' in prog:
                    prog['FRAMEINDEX'].value = int(idx)
                ctx.clear(0.0, 0.0, 0.0, 0.0)
                vao.render(moderngl.TRIANGLE_STRIP)
                raw_out = fbo.read(components=comps)
                out_img = np.frombuffer(raw_out, dtype=np.uint8).reshape(th, tw, comps)[::-1].copy()
                rendered.append(out_img)
    finally:
        for obj in (vao, vbo, fbo, tex_in, tex_prev, tex_next, tex_bg, tex_out, prog, ctx):
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass

    out_frames = np.stack(rendered, axis=0)
    out_dir = core.new_directory(out_cand)
    out_media = out_dir / ('effect.mov' if want_alpha else 'effect.mp4')
    preview_png = out_dir / 'preview_mid.png'
    mid_idx = len(out_frames) // 2
    Image.fromarray(out_frames[mid_idx], mode='RGBA' if want_alpha else 'RGB').save(preview_png)
    _encode_frames(
        out_frames, use_fps, out_media, alpha=want_alpha, audio_src=src if (is_video and has_audio) else None,
    )

    pix_fmt = None
    if want_alpha:
        pr = core.probe(out_media)
        pix_fmt = pr.get('pix_fmt')
    else:
        pix_fmt = 'yuv420p'

    report = {
        'status': 'passed',
        'operation': 'shader-fx',
        'mode': 'effect',
        'preset': preset_name,
        'isf_description': isf_meta.get('DESCRIPTION'),
        'renderer': renderer_name,
        'opengl_version': gl_ver,
        'width': tw,
        'height': th,
        'fps': round(use_fps, 4),
        'frames': int(len(out_frames)),
        'alpha': want_alpha,
        'pix_fmt': pix_fmt,
        'has_bg': bg_frames is not None,
        'input_sha256': file_hash.sha256(src),
        'output': str(out_media),
        'preview_png': str(preview_png),
        'strict_decode': 'passed',
    }
    core.write_json(out_dir / 'shader-fx-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='subcommand', required=True)

    p_tr = sub.add_parser('transition', help='Render GPU GL-Transitions transition between two clips/images')
    p_tr.add_argument('--from', dest='from_media', required=True, help='Source clip/image A')
    p_tr.add_argument('--to', dest='to_media', required=True, help='Destination clip/image B')
    p_tr.add_argument('--out', required=True, help='New output directory for transition.mp4')
    p_tr.add_argument('--preset', choices=tuple(TRANSITION_PRESETS), default='directionalwarp')
    p_tr.add_argument('--shader', default=None, help='Optional custom GL-Transitions .glsl file')
    p_tr.add_argument('--duration', type=float, default=0.6)
    p_tr.add_argument('--fps', type=float, default=30.0)
    p_tr.add_argument('--width', type=int, default=0)
    p_tr.add_argument('--height', type=int, default=0)
    p_tr.add_argument('--easing', choices=('linear', 'ease_in_out', 'expo_in_out'), default='ease_in_out')
    p_tr.add_argument('--params', default=None, help='Optional JSON uniform overrides')

    p_fx = sub.add_parser('effect', help='Render GPU ISF shader effect onto video/image (optional ProRes 4444 alpha)')
    p_fx.add_argument('--input', required=True, help='Input video or image file')
    p_fx.add_argument('--out', required=True, help='New output directory for effect.mp4 or effect.mov')
    p_fx.add_argument('--preset', choices=tuple(ISF_PRESETS), default='halation_bloom')
    p_fx.add_argument('--shader', default=None, help='Optional custom ISF .fs shader file')
    p_fx.add_argument('--duration', type=float, default=2.0)
    p_fx.add_argument('--fps', type=float, default=30.0)
    p_fx.add_argument('--max-frames', type=int, default=900)
    p_fx.add_argument('--params', default=None, help='Optional JSON input/uniform overrides')
    p_fx.add_argument('--bg', default=None, help='Optional background image/video for optical light_wrap')
    p_fx.add_argument('--alpha', action='store_true', help='Export RGBA ProRes 4444 (yuva444p12le) .mov')

    args = parser.parse_args(argv)
    try:
        report = run_transition(args) if args.subcommand == 'transition' else run_effect(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
