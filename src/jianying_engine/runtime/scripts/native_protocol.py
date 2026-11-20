"""Shared runtime admission gates, resource tree checks, and declarative native adapter hooks."""
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys

import file_hash
import runtime_admission


def _mod(name):
    return sys.modules.get(name) or importlib.import_module(name)


def iter_objects(value):
    """Yield every nested dict inside a draft JSON structure."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def hash_tree(
    root,
    *,
    dir_error='Native resource requires a regular directory',
    link_error='Native resource contains a link',
    entry_error='Unsupported native resource entry',
    include_path=False,
):
    """Hash every file in a regular non-linked directory tree."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError(dir_error)
    files = {}

    def visit(directory):
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError(f'{link_error}: {path}' if include_path else link_error)
            if path.is_dir():
                visit(path)
            elif path.is_file():
                files[path.relative_to(root).as_posix()] = file_hash.sha256(path)
            else:
                raise ValueError(f'{entry_error}: {path}' if include_path else entry_error)

    visit(root)
    return files


def verify_admitted_runtime(
    feature,
    label,
    *,
    install=None,
    contract=None,
    link_dirs=(),
    link_error=None,
    file_error_prefix='Runtime identity changed: ',
    file_error_full_path=False,
    reject_symlink_files=False,
    bun_runtime=None,
    bun_error_prefix=None,
):
    """Verify an admitted Jianying feature runtime build and its optional Bun executable."""
    import editor

    install_dir = Path(install) if install is not None else editor.jianying_install()
    expected = runtime_admission.expected(feature, install_dir, contract) if install_dir else None
    if expected is None:
        raise ValueError(runtime_admission.refusal(feature, install_dir, label))
    if link_dirs and any(
        p.is_symlink() or p.is_junction()
        for p in (install_dir, *(install_dir / rel for rel in link_dirs))
    ):
        raise ValueError(link_error)
    for relative, digest in expected.items():
        path = install_dir / relative
        if (reject_symlink_files and path.is_symlink()) or not file_hash.matches(path, digest):
            detail = str(path) if file_error_full_path else relative
            raise ValueError(file_error_prefix + detail)
    if bun_error_prefix:
        bun = json.loads(
            (runtime_admission.ASSETS / 'native-preview-11.6.0.14552.json').read_text(encoding='utf-8')
        )['runtime']['file']
        actual = Path(bun_runtime) if bun_runtime is not None else install_dir / bun
        if not file_hash.matches(actual, expected[bun]):
            raise ValueError(bun_error_prefix + str(actual))
    return install_dir, expected


def verify_plan_preview(
    content,
    root,
    *,
    observed,
    plan_declared,
    missing_plan_error,
    runtime_check=None,
    runtime_first=True,
    after_validate=None,
    pass_root=False,
):
    """Gate native preview execution on admitted runtime state and formal edit-plan validation."""
    import editor

    plan_path = Path(root) / 'edit-plan.json' if root is not None else None
    declared = plan_path is not None and plan_path.is_file() and bool(plan_declared(editor.read_json(plan_path)))
    if not observed and not declared:
        return
    if runtime_first and runtime_check is not None:
        runtime_check()
    if plan_path is None or not plan_path.is_file():
        raise ValueError(missing_plan_error)
    if not runtime_first and runtime_check is not None:
        runtime_check()
    plan, _, _ = editor.validate_plan(plan_path)
    if after_validate is not None:
        after_validate(plan)
    if pass_root:
        editor.verify_structure(content, plan, Path(root), Path(root))
    else:
        editor.verify_structure(content, plan)


@dataclass(frozen=True)
class NativeContext:
    """Execution context handed to native adapters during draft build and readback verification."""

    content: dict
    plan: dict
    physical_root: Path | None = None
    final_root: Path | None = None
    measure_loudness: object = None


@dataclass(frozen=True)
class NativeAdapter:
    """Declarative lifecycle descriptor for one native capability family."""

    name: str
    validate_raw_plan: object = None
    validate_pre_native_media: object = None
    validate_post_native_media: object = None
    apply_to_content: object = None
    verify_pre_expand: object = None
    verify_post_expand: object = None
    preview_check: object = None
    preview_order: int | None = None
    report_key: str | None = None
    integration_key: str | None = None


def _verify_chroma_post(ctx):
    return _mod('native_chroma').verify(ctx.content, ctx.plan)


def _verify_mix_post(ctx):
    return _mod('native_mix_mode').verify(ctx.content, ctx.plan)


def _verify_text_alpha_post(ctx):
    return _mod('native_text_alpha').verify(ctx.content, ctx.plan)


def _count_red_coverage(ctx):
    mod = _mod('native_mask_feather')
    return sum(mod.requested(item) for item in ctx.plan['clips'])


ADAPTERS = (
    NativeAdapter(
        name='local_font',
        preview_check=lambda content, parent, install, runtime: _mod('local_font').preview_check(content, parent),
        preview_order=10,
    ),
    NativeAdapter(
        name='text_glow',
        apply_to_content=lambda ctx: _mod('native_text_glow').apply(ctx.content, ctx.plan, None),
    ),
    NativeAdapter(
        name='matting_cache',
        apply_to_content=lambda ctx: _mod('native_matting_cache').apply(ctx.content, ctx.plan),
        verify_post_expand=lambda ctx: _mod('native_matting_cache').verify(ctx.content, ctx.plan),
        report_key='existing_matting_caches',
    ),
    NativeAdapter(
        name='chroma',
        validate_post_native_media=lambda plan, probes: [
            _mod('native_chroma').validate_media(item, probes[item['path']])
            for item in plan['clips']
            if _mod('native_chroma').requested(item)
        ],
        apply_to_content=lambda ctx: _mod('native_chroma').bind(ctx.content, ctx.plan),
        verify_post_expand=_verify_chroma_post,
        preview_check=lambda content, parent, install, runtime: _mod('native_chroma').preview_check(content),
        preview_order=30,
        report_key='installed_chroma2',
        integration_key='installed_chroma2_integration',
    ),
    NativeAdapter(
        name='mix_mode',
        validate_post_native_media=lambda plan, probes: [
            _mod('native_mix_mode').check_media(item, plan, probes)
            for item in plan['clips']
            if _mod('native_mix_mode').requested(item)
        ],
        apply_to_content=lambda ctx: _mod('native_mix_mode').bind(ctx.content, ctx.plan),
        verify_post_expand=_verify_mix_post,
        preview_check=lambda content, parent, install, runtime: _mod('native_mix_mode').preview_check(content, parent, install),
        preview_order=40,
        report_key='installed_mix',
        integration_key='installed_mix_integration',
    ),
    NativeAdapter(
        name='beat',
        validate_raw_plan=lambda plan: _mod('native_beat').validate(plan),
        validate_pre_native_media=lambda plan, probes: _mod('native_beat').validate_media(plan, probes),
        apply_to_content=lambda ctx: _mod('native_beat').apply(ctx.content, ctx.plan),
        verify_pre_expand=lambda content, plan: _mod('native_beat').verify(content, plan),
        preview_check=lambda content, parent, install, runtime: _mod('native_beat').preview_check(content, parent, install),
        preview_order=100,
        report_key='beat_marks',
        integration_key='beat_marks_integration',
    ),
    NativeAdapter(
        name='masks',
        apply_to_content=lambda ctx: _mod('native_advanced').apply_masks(ctx.content, ctx.plan, None),
        verify_post_expand=lambda ctx: _mod('native_advanced').verify_masks(
            ctx.content, ctx.plan, None, ctx.physical_root, ctx.final_root
        ),
        report_key='dynamic_rectangle_masks',
    ),
    NativeAdapter(
        name='mask_feather',
        apply_to_content=lambda ctx: _mod('native_mask_feather').prepare(
            ctx.content, ctx.plan, ctx.physical_root, ctx.final_root
        ),
        verify_post_expand=_count_red_coverage,
        preview_check=lambda content, parent, install, runtime: _mod('native_mask_feather').preview_check(content, parent),
        preview_order=20,
        report_key='rectangle_red_coverage',
        integration_key='rectangle_red_coverage_integration',
    ),
    NativeAdapter(
        name='text_highlight',
        preview_check=lambda content, parent, install, runtime: _mod('native_text_highlight').preview_check(
            content, parent, install
        ),
        preview_order=50,
    ),
    NativeAdapter(
        name='text_alpha',
        verify_post_expand=_verify_text_alpha_post,
        preview_check=lambda content, parent, install, runtime: _mod('native_text_alpha').preview_check(
            content, parent, install
        ),
        preview_order=60,
        report_key='text_global_alpha',
        integration_key='text_global_alpha_integration',
    ),
    NativeAdapter(
        name='corner_motion',
        verify_pre_expand=lambda content, plan: _mod('native_advanced').verify_perspectives(content, plan),
        preview_check=lambda content, parent, install, runtime: _mod('native_advanced').corner_preview_check(
            content, parent, install, runtime
        ),
        preview_order=70,
        report_key='corner_pin_perspectives',
    ),
    NativeAdapter(
        name='green_lightness',
        preview_check=lambda content, parent, install, runtime: _mod('native_advanced').green_lightness_preview_check(
            content, parent, install, runtime
        ),
        preview_order=80,
    ),
    NativeAdapter(
        name='hsl_channels',
        preview_check=lambda content, parent, install, runtime: _mod('native_hsl_channels').preview_check(
            content, parent, install, runtime
        ),
        preview_order=90,
    ),
    NativeAdapter(
        name='extensions',
        apply_to_content=lambda ctx: _mod('native_advanced').apply_extensions(
            ctx.content, ctx.plan, None, ctx.measure_loudness
        ),
        verify_post_expand=lambda ctx: _mod('native_advanced').verify_extensions(
            ctx.content, ctx.plan, None, ctx.measure_loudness
        ),
        report_key='extensions',
    ),
    NativeAdapter(
        name='primary_wheel',
        validate_raw_plan=lambda plan: _mod('native_primary_wheel').validate(plan),
        validate_pre_native_media=lambda plan, probes: _mod('native_primary_wheel').validate_media(plan, probes),
        apply_to_content=lambda ctx: _mod('native_primary_wheel').apply(ctx.content, ctx.plan),
        verify_pre_expand=lambda content, plan: _mod('native_primary_wheel').verify(content, plan),
        preview_check=lambda content, parent, install, runtime: _mod('native_primary_wheel').preview_check(
            content, parent, install
        ),
        preview_order=110,
        report_key='primary_wheel',
        integration_key='primary_wheel_integration',
    ),
    NativeAdapter(
        name='composites',
        apply_to_content=lambda ctx: _mod('native_advanced').apply_composites(ctx.content, ctx.plan),
    ),
)

_BY_NAME = {adapter.name: adapter for adapter in ADAPTERS}


def adapter_registry():
    """Return ordered metadata describing all registered native adapters and active lifecycle hooks."""
    phase_fields = (
        'validate_raw_plan',
        'validate_pre_native_media',
        'validate_post_native_media',
        'apply_to_content',
        'verify_pre_expand',
        'verify_post_expand',
        'preview_check',
    )
    return [
        {
            'name': adapter.name,
            'phases': [field for field in phase_fields if getattr(adapter, field) is not None],
            'report_key': adapter.report_key,
            'integration_key': adapter.integration_key,
            'preview_order': adapter.preview_order,
        }
        for adapter in ADAPTERS
    ]


def validate_raw_plans(plan):
    for adapter in ADAPTERS:
        if adapter.validate_raw_plan is not None:
            adapter.validate_raw_plan(plan)


def validate_pre_native_media(plan, probes):
    for adapter in ADAPTERS:
        if adapter.validate_pre_native_media is not None:
            adapter.validate_pre_native_media(plan, probes)


def validate_post_native_media(plan, probes):
    for adapter in ADAPTERS:
        if adapter.validate_post_native_media is not None:
            adapter.validate_post_native_media(plan, probes)


def apply_post_save(content, plan, physical_root=None, final_root=None, measure_loudness=None):
    ctx = NativeContext(content, plan, physical_root, final_root, measure_loudness)
    for adapter in ADAPTERS:
        if adapter.apply_to_content is not None:
            adapter.apply_to_content(ctx)


def verify_pre_expand(content, plan):
    counts = {}
    for adapter in ADAPTERS:
        if adapter.verify_pre_expand is not None:
            counts[adapter.report_key] = adapter.verify_pre_expand(content, plan)
    return counts


def _integration(count):
    return 'limited' if count else 'not_requested'


def verify_post_expand(content, plan, native_report, pre_counts, physical_root=None, final_root=None, measure_loudness=None):
    ctx = NativeContext(content, plan, physical_root, final_root, measure_loudness)
    native_report['existing_matting_caches'] = _BY_NAME['matting_cache'].verify_post_expand(ctx)
    native_report['new_matting_computation'] = False

    chroma_count = _BY_NAME['chroma'].verify_post_expand(ctx)
    native_report['installed_chroma2'] = chroma_count
    native_report['installed_chroma2_integration'] = _integration(chroma_count)

    beat_count = pre_counts['beat_marks']
    native_report['beat_marks'] = beat_count
    native_report['beat_marks_integration'] = _integration(beat_count)

    wheel_count = pre_counts['primary_wheel']
    native_report['primary_wheel'] = wheel_count
    native_report['primary_wheel_integration'] = _integration(wheel_count)

    mix_count = _BY_NAME['mix_mode'].verify_post_expand(ctx)
    native_report['installed_mix'] = mix_count
    native_report['installed_mix_integration'] = _integration(mix_count)

    alpha_count = _BY_NAME['text_alpha'].verify_post_expand(ctx)
    native_report['text_global_alpha'] = alpha_count
    native_report['text_global_alpha_integration'] = _integration(alpha_count)

    native_report['dynamic_rectangle_masks'] = _BY_NAME['masks'].verify_post_expand(ctx)

    feather_count = _BY_NAME['mask_feather'].verify_post_expand(ctx)
    native_report['rectangle_red_coverage'] = feather_count
    native_report['rectangle_red_coverage_integration'] = _integration(feather_count)

    native_report['video_composites'] = len(plan.get('composites', []))
    native_report['corner_pin_perspectives'] = pre_counts['corner_pin_perspectives']
    native_report['extensions'] = _BY_NAME['extensions'].verify_post_expand(ctx)
    native_report['animation_paths'] = _mod('native_advanced').verify_animation_paths(content)
    return native_report


def run_preview_checks(content, draft_parent, install, runtime=None):
    ordered = sorted(
        (adapter for adapter in ADAPTERS if adapter.preview_check is not None),
        key=lambda adapter: adapter.preview_order,
    )
    for adapter in ordered:
        adapter.preview_check(content, draft_parent, install, runtime)
