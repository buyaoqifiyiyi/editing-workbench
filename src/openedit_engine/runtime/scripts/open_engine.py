"""Unified CLI for pure open-source editing: FilmCraft + EffectCraft + FFmpeg."""
import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys

import effectcraft_backend
import filmcraft_backend
import media_prep
import overlay_recipes
import runtime_core as core
import upstream_guard


def doctor():
    """Diagnose open-source FilmCraft, EffectCraft, FFmpeg, GPU, and optional AI tools."""
    from openedit_engine import software_detection

    backends = software_detection.detect_backends()
    gpu = software_detection.detect_gpu()
    optional = {}
    for pkg, mod in (
        ('faster_whisper', 'faster_whisper'),
        ('onnxruntime', 'onnxruntime'),
        ('librosa', 'librosa'),
        ('opencv_contrib', 'cv2'),
        ('opentimelineio', 'opentimelineio'),
        ('mcp', 'mcp'),
        ('av', 'av'),
    ):
        found = importlib.util.find_spec(mod) is not None
        ver = None
        if found:
            try:
                ver = importlib.metadata.version(pkg.replace('_', '-'))
            except importlib.metadata.PackageNotFoundError:
                pass
        optional[pkg] = {'available': found, 'version': ver}

    fc_ok = backends['filmcraft']['installed']
    ec_ok = backends['effectcraft']['installed']
    ff_ok = backends['ffmpeg']['installed']
    ready = bool(fc_ok and ec_ok and ff_ok)
    instructions = []
    if not fc_ok:
        instructions.append('缺少 filmcraft-cli；请安装 ArtCraft FilmCraft 或设置 OPENEDIT_FILMCRAFT_CLI。')
    if not ec_ok:
        instructions.append('缺少 effectcraft-cli；请安装 ArtCraft EffectCraft 或设置 OPENEDIT_EFFECTCRAFT_CLI。')
    if not ff_ok:
        instructions.append('缺少 ffmpeg/ffprobe；请将其加入 PATH 或设置 OPENEDIT_FFMPEG / OPENEDIT_FFPROBE。')

    warnings = [backends[name]['warning'] for name in ('filmcraft', 'effectcraft') if backends[name].get('warning')]

    return {
        'status': 'ready' if ready else 'incomplete',
        'ready': ready,
        'python': core.py(),
        'python_version': sys.version.split()[0],
        'runtime_root': str(core.SKILL_ROOT),
        'backends': backends,
        'gpu': gpu,
        'optional_ai_packages': optional,
        'instructions': instructions,
        'warnings': warnings,
        'license_profile': 'open_source_only (MIT/Apache-2.0/BSD/LGPL)',
    }


def capabilities(query=None, live=False):
    """Describe verified open-source capabilities and optionally probe live tool inventories."""
    caps = {
        'status': 'open_source_native',
        'constraints_removed': [
            'No 640x360/720p/320x180 resolution locks (supports 64..7680 width, 64..4320 height, 1..120fps)',
            'No 2s/4s/20s duration caps or single-track restrictions',
            'No discrete effect/grading/keyframe value whitelists',
            'No proprietary cloud cache, draft encryption, or version-requalification drift',
        ],
        'filmcraft': {
            'project_format': '.fcproj (plaintext JSON)',
            'editing': 'Multi-track V/A timeline, ripple/insert/overwrite, transitions (Cross Dissolve, wipes, etc.), speed/reverse',
            'color_grading': 'Lumetri exposure/contrast/saturation/curves (Luma + RGB)/wheels/HSL/3D LUT',
            'blend_modes_and_effects': '26 blend modes, motion position/scale/rotation/opacity/interpolation, 90+ effects',
            'text_and_captions': 'graphics.newText + graphics.setCharStyle (multi-color highlights, stroke, background, shadow, CJK Noto Sans SC/Microsoft YaHei) + SRT/Subtitle tracks',
            'audio_and_interchange': 'Multi-track volume/keyframes/EBU R128 loudness, OTIO / FCPXML / FCP7 XML / AAF / EDL',
            'export': 'H.264/HEVC/ProRes/AAC, optional GPU & NVENC hardware encoding',
        },
        'effectcraft': {
            'project_format': '.ecproj (plaintext JSON)',
            'features': '308 effects, 9 layer styles, shape/solid/text layers, CJK fonts, text animators, masks, expressions, 3D camera/lights, AE-style JS scripting',
            'overlay_recipes': sorted(overlay_recipes.RECIPES),
            'alpha_overlay_export': 'ProRes 4444 RGBA (.mov, yuva444p12le) -> direct composite onto FilmCraft V2+',
            'caveat': 'WebM VP9 default CLI export drops alpha; ProRes 4444 (.mov) is locked as the verified alpha interchange',
        },
        'offline_ai_and_media': [
            'beats / beat-apply (librosa)', 'transcribe (faster-whisper)', 'separate (Demucs ONNX)',
            'denoise-audio (FFT)', 'ducking', 'captions / karaoke', 'tracking / tracking-apply (OpenCV CSRT)',
            'reverse', 'practice-compare', 'normalize-media (FFmpeg VFR -> CFR)',
        ],
    }
    if live:
        try:
            ec_cli = effectcraft_backend.find_cli()
            fc_cli = filmcraft_backend.find_cli()
            caps['live_probe'] = {
                'filmcraft_cli': fc_cli,
                'effectcraft_cli': ec_cli,
            }
            if query:
                import craft_rpc
                with craft_rpc.CraftMcpClient([fc_cli, 'mcp']) as fc:
                    caps['live_probe']['filmcraft_commands'] = fc.call_tool('command_list', {'filter': query})
                with craft_rpc.CraftMcpClient([ec_cli, 'mcp']) as ec:
                    caps['live_probe']['effectcraft_effects'] = ec.call_tool('list_effects', {'filter': query, 'compact': True})
        except (ValueError, RuntimeError) as exc:
            caps['live_probe_error'] = str(exc)
    return caps


def build(plan_path, out_dir, *, export_path=None, preview_times=None, hardware_encoding='off', export_otio=None, export_fcpxml=None):
    plan, probes, warnings = core.validate_plan(plan_path)
    report = filmcraft_backend.build_project(
        plan,
        out_dir,
        probes=probes,
        preview_times=preview_times,
        export_path=export_path,
        hardware_encoding=hardware_encoding,
        export_otio=export_otio,
        export_fcpxml=export_fcpxml,
    )
    if warnings:
        report['warnings'] = warnings
    core.write_json(Path(out_dir).resolve() / 'build-report.json', report)
    return report


def _load_json_spec(arg_value):
    candidate = Path(arg_value)
    if candidate.is_file():
        return core.read_json(candidate)
    return json.loads(arg_value, parse_constant=core.reject_constant)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)

    sub.add_parser('doctor', help='Diagnose local open-source FilmCraft, EffectCraft, FFmpeg, GPU, and AI deps')

    c_cap = sub.add_parser('capabilities', help='Describe open-source capabilities and optionally filter live commands')
    c_cap.add_argument('--query', help='Filter live FilmCraft commands and EffectCraft effects')
    c_cap.add_argument('--live', action='store_true', help='Query live CLI/MCP backends')

    c_insp = sub.add_parser('inspect', help='Probe local media file/folder and extract sample frames')
    c_insp.add_argument('--input', required=True)
    c_insp.add_argument('--out', required=True)
    c_insp.add_argument('--frames', type=int, default=3)
    c_insp.add_argument('--max-files', type=int, default=200)

    c_ref = sub.add_parser('reference', help='Extract scene-change reference frames via FFmpeg')
    c_ref.add_argument('--input', required=True)
    c_ref.add_argument('--out', required=True)
    c_ref.add_argument('--start', type=float, default=0.0)
    c_ref.add_argument('--duration', type=float)
    c_ref.add_argument('--threshold', type=float, default=0.35)
    c_ref.add_argument('--max-frames', type=int, default=24)

    c_norm = sub.add_parser('normalize-media', help='Normalize VFR or exotic-codec media to CFR H.264/AAC or ProRes')
    c_norm.add_argument('--input', required=True)
    c_norm.add_argument('--out', required=True)
    c_norm.add_argument('--fps', type=float, default=30.0)
    c_norm.add_argument('--width', type=int)
    c_norm.add_argument('--height', type=int)
    c_norm.add_argument('--codec', choices=('h264', 'prores'), default='h264')
    c_norm.add_argument('--crf', type=int, default=16)

    c_val = sub.add_parser('validate', help='Validate an open edit-plan.json and probe media')
    c_val.add_argument('--plan', required=True)

    c_build = sub.add_parser('build', help='Compile edit-plan.json to EffectCraft overlays + FilmCraft .fcproj')
    c_build.add_argument('--plan', required=True)
    c_build.add_argument('--out', required=True)
    c_build.add_argument('--export', dest='export_path', help='Optional immediate final media export path')
    c_build.add_argument('--preview-times', type=float, nargs='+', help='Optional preview frame times in seconds')
    c_build.add_argument('--hardware-encoding', choices=('off', 'auto'), default='off')
    c_build.add_argument('--export-otio', help='Optional OTIO interchange output path')
    c_build.add_argument('--export-fcpxml', help='Optional FCPXML interchange output path')

    c_prev = sub.add_parser('preview', help='Render PNG review frames from an existing .fcproj')
    c_prev.add_argument('--project', required=True)
    c_prev.add_argument('--out', required=True)
    c_prev.add_argument('--times', type=float, nargs='+', required=True)
    c_prev.add_argument('--max-side', type=int, default=0)

    c_exp = sub.add_parser('export', help='Export an existing .fcproj and run strict FFmpeg decode verification')
    c_exp.add_argument('--project', required=True)
    c_exp.add_argument('--out', required=True)
    c_exp.add_argument('--format', dest='fmt', choices=('h264', 'hevc', 'prores', 'wav', 'aac'), default='h264')
    c_exp.add_argument('--crf', type=int, default=20)
    c_exp.add_argument('--hardware-encoding', choices=('off', 'auto'), default='off')
    c_exp.add_argument('--no-burn-captions', action='store_true')
    c_exp.add_argument('--no-audio', action='store_true')

    c_ov = sub.add_parser('render-overlay', help='Build and verify a standalone EffectCraft ProRes 4444 RGBA overlay')
    c_ov.add_argument('--spec', required=True, help='JSON file path or inline JSON string')
    c_ov.add_argument('--out', required=True)
    c_ov.add_argument('--no-video', action='store_true')
    c_ov.add_argument('--gpu', action='store_true')

    c_fce = sub.add_parser('filmcraft-exec', help='Direct JSON command escape hatch to FilmCraft engine')
    c_fce.add_argument('--commands', required=True, help='JSON array [{id, params}] or JSON file path')
    c_fce.add_argument('--project', help='Optional existing .fcproj to open')
    c_fce.add_argument('--save-as', help='Optional destination .fcproj path')

    c_ece = sub.add_parser('effectcraft-exec', help='Direct JSON/JSX escape hatch to EffectCraft engine')
    c_ece.add_argument('--commands', default='[]', help='JSON array [{command, params}] or JSON file path')
    c_ece.add_argument('--script', help='Optional AE-style JS script string or .jsx path')
    c_ece.add_argument('--project', help='Optional existing .ecproj to open')
    c_ece.add_argument('--save-as', help='Optional destination .ecproj path')

    c_cc = sub.add_parser('contract-check', help='Verify live Craft MCP contracts and micro E2E strict-decode build')
    c_cc.add_argument('--out', help='Optional new directory to retain regression build artifacts')
    c_cc.add_argument('--skip-e2e', action='store_true', help='Skip micro E2E render and check MCP contracts only')

    c_uc = sub.add_parser('upstream-check', help='Read-only check of upstream FilmCraft/EffectCraft releases')
    c_uc.add_argument('--state', help='Optional JSON state file for previously seen upstream tags')
    c_uc.add_argument('--save-state', action='store_true', help='Persist fetched upstream tags into --state')
    c_uc.add_argument('--timeout', type=int, default=15, help='HTTP timeout seconds (default 15)')

    c_uu = sub.add_parser('upstream-upgrade', help='Safely stage, verify, and promote passing upstream Craft releases')
    c_uu.add_argument('--state', help='Optional JSON state file tracking verified/blocked releases')
    c_uu.add_argument('--tools-dir', help='Override OpenEditTools destination directory')
    c_uu.add_argument('--min-age-hours', type=int, default=0, help='Minimum release age in hours before upgrading')
    c_uu.add_argument('--keep-versions', type=int, default=2, help='Versions per tool to retain for rollback')
    c_uu.add_argument('--dry-run', action='store_true', help='Check eligibility without downloading or writing files')
    c_uu.add_argument('--retry-failed', action='store_true', help='Retry releases previously blocked by contract-check')
    c_uu.add_argument('--timeout', type=int, default=30, help='HTTP timeout seconds (default 30)')

    args = parser.parse_args(argv)
    exit_code = 0
    try:
        if args.action == 'doctor':
            result = doctor()
        elif args.action == 'capabilities':
            result = capabilities(query=args.query, live=args.live or bool(args.query))
        elif args.action == 'inspect':
            result = media_prep.inspect_media(args.input, args.out, frames=args.frames, max_files=args.max_files)
        elif args.action == 'reference':
            result = media_prep.extract_reference(args.input, args.out, start=args.start, duration=args.duration, threshold=args.threshold, max_frames=args.max_frames)
        elif args.action == 'normalize-media':
            result = media_prep.normalize_media(args.input, args.out, fps=args.fps, width=args.width, height=args.height, codec=args.codec, crf=args.crf)
        elif args.action == 'validate':
            plan, probes, warnings = core.validate_plan(args.plan)
            result = {'status': 'valid', 'name': plan['name'], 'canvas': plan['canvas'],
                      'clips': len(plan['clips']), 'audio': len(plan['audio']),
                      'captions': len(plan['captions']), 'overlays': len(plan['overlays']),
                      'probes': probes, 'warnings': warnings}
        elif args.action == 'build':
            result = build(args.plan, args.out, export_path=args.export_path, preview_times=args.preview_times,
                           hardware_encoding=args.hardware_encoding, export_otio=args.export_otio, export_fcpxml=args.export_fcpxml)
        elif args.action == 'preview':
            result = filmcraft_backend.preview_project(args.project, args.out, args.times, max_side=args.max_side)
        elif args.action == 'export':
            result = filmcraft_backend.export_project(args.project, args.out, fmt=args.fmt, crf=args.crf,
                                                      hardware_encoding=args.hardware_encoding,
                                                      burn_captions=not args.no_burn_captions, audio=not args.no_audio)
        elif args.action == 'render-overlay':
            spec = _load_json_spec(args.spec)
            result = effectcraft_backend.build_overlay(spec, args.out, render_video=not args.no_video, gpu=args.gpu)
        elif args.action == 'filmcraft-exec':
            cmds = _load_json_spec(args.commands)
            result = filmcraft_backend.exec_commands(cmds, project=args.project, save_as=args.save_as)
        elif args.action == 'effectcraft-exec':
            cmds = _load_json_spec(args.commands)
            script_code = Path(args.script).read_text(encoding='utf-8') if args.script and Path(args.script).is_file() else args.script
            result = effectcraft_backend.exec_commands(cmds, project=args.project, save_as=args.save_as, script=script_code)
        elif args.action == 'contract-check':
            result = upstream_guard.contract_check(out_dir=args.out, skip_e2e=args.skip_e2e)
            if result.get('status') != 'passed':
                exit_code = 1
        elif args.action == 'upstream-check':
            result = upstream_guard.upstream_check(state_path=args.state, save_state=args.save_state, timeout=args.timeout)
            if result.get('status') == 'failed':
                exit_code = 1
        else:
            result = upstream_guard.upstream_upgrade(
                state_path=args.state,
                tools_dir=args.tools_dir,
                min_age_hours=args.min_age_hours,
                keep_versions=args.keep_versions,
                dry_run=args.dry_run,
                retry_failed=args.retry_failed,
                timeout=args.timeout,
            )
            if result.get('status') in ('failed', 'blocked'):
                exit_code = 1
        core.emit(result)
        return exit_code
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        core.emit({'status': 'failed', 'error': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
