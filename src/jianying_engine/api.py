"""One whitelist and resource layout shared by the CLI and external Agent adapters."""
from pathlib import Path
import sys

RUNTIME = Path(__file__).resolve().parent / 'runtime'
_EDITOR = {
    'doctor': 'Diagnose the local runtime without opening Jianying.',
    'capabilities': 'List admitted native resources and parameters.',
    'inspect': 'Probe local media and extract frames.',
    'reference': 'Extract reference frames and audio candidates.',
    'validate': 'Validate an edit plan and media dependencies.',
    'build': 'Build a new editable draft; refuses existing output.',
    'publish': 'Copy a generated draft to the active draft root and check registration.',
    'refresh-registration': 'Recreate an owned generated draft to trigger registration.',
    'native-preview': 'Render selected low-resolution native frames without the desktop UI.',
    'admit-matting': 'Register an externally reviewed existing mask cache; never computes or rates a cutout.',
}
_SCRIPTS = {
    'music-edit': ('music_edit.py', 'Prepare bounded local media, call the configured native audio/video model through OpenAI-compatible or Gemini APIs, and compile musical/action anchors into validated Jianying or Resolve plans; analyze consumes API usage.'),
    'resolve-workflow': ('resolve_workflow.py', 'Validate/prepare bounded Resolve plans, create/save/reopen native projects and render verified NVIDIA H264/AAC outputs; no silent effect loss.'),
    'adobe-workflow': ('adobe_workflow.py', 'Validate/prepare bounded PR+AE plans, create native AE projects and lossless shots, then hand off standard XML to Premiere; no QE.'),
    'shot-exchange': ('shot_exchange.py', 'Prepare/build a bounded Blender shot, validate its return, or export plain silent OTIO cuts for Kdenlive.'),
    'beats': ('beats.py', 'Detect offline candidate beats from an explicitly selected local audio interval.'),
    'beat-apply': ('beat_apply.py', 'Create a new plain-clip plan with explicit every-N candidate-beat cuts; never overwrites input.'),
    'denoise-audio': ('offline_denoise.py', 'Create aligned dry/FFT-denoised PCM excerpts with explicit noise-floor/reduction; preserve originals.'),
    'reverse': ('reverse_media.py', 'Create bounded reversed video source media with explicit reverse/mute audio policy.'),
    'migrate': ('migrate.py', 'Diagnose and explicitly map external media paths.'),
    'transcribe': ('transcribe.py', 'Transcribe offline with caller-supplied Whisper weights; optional validated model word timestamps.'),
    'separate': ('separate.py', 'Separate audio using caller-supplied local Demucs weights.'),
    'ducking': ('ducking.py', 'Create a new plan with a measured music volume envelope.'),
    'captions': ('captions.py', 'Map transcript/SRT into editable captions; optional timed full-group karaoke highlights, with source/timeline mapping.'),
    'tracking': ('tracking.py', 'Track a selected ROI offline; reports lost tracking.'),
    'tracking-apply': ('tracking_apply.py', 'Apply an admitted trajectory as editable image or bounded text position keys.'),
    'stock': ('stock.py', 'Explicit online stock search/download with provenance.'),
    'templates': ('templates.py', 'Read the external template index.'),
    'verify-templates': ('verify_templates.py', 'Verify template subsets against the current runtime.'),
    'borrow-template': ('template_borrow.py', 'Borrow only externally verified template subsets.'),
    'checks': ('run_checks.py', 'Run registered regression checks in fresh output directories.'),
}
_GUIDES = {'editing': 'guides/editing.md', 'planning': 'guides/planning.md',
           'maintenance': 'guides/maintenance.md', 'capabilities': 'references/capabilities.md',
           'plan-format': 'references/plan-format.md', 'migration': 'references/migration.md'}
for _path in sorted((RUNTIME / 'references').rglob('*.md')):
    _relative = _path.relative_to(RUNTIME).as_posix()
    if _relative not in _GUIDES.values():
        _GUIDES['reference/' + _path.relative_to(RUNTIME / 'references').with_suffix('').as_posix()] = _relative


def operations():
    return {**_EDITOR, **{name: value[1] for name, value in _SCRIPTS.items()}}


def console_python():
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').is_file():
        python = python.with_name('python.exe')
    return str(python)


def command_for(operation, arguments):
    if not isinstance(operation, str) or operation not in operations():
        raise ValueError(f'Unknown operation: {operation}')
    if not isinstance(arguments, list) or any(not isinstance(arg, str) or '\0' in arg for arg in arguments):
        raise ValueError('arguments must be an array of strings without NUL')
    script, prefix = ('editor.py', [operation]) if operation in _EDITOR else (_SCRIPTS[operation][0], [])
    path = RUNTIME / 'scripts' / script
    if not path.is_file():
        raise ValueError(f'Installed engine is incomplete: missing {script}')
    return [console_python(), str(path), *prefix, *arguments]


def paths():
    return {'python': console_python(), 'runtime': str(RUNTIME),
            'guides': {name: str(RUNTIME / relative) for name, relative in _GUIDES.items()}}


def read_guide(name):
    if not isinstance(name, str) or name not in _GUIDES:
        raise ValueError(f'Unknown guide: {name}; choose from {", ".join(_GUIDES)}')
    return (RUNTIME / _GUIDES[name]).read_text(encoding='utf-8-sig')
