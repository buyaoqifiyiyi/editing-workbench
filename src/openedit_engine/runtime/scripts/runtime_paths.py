"""Portable runtime resources and external state paths; never reads credentials."""
import os
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(__file__).absolute().parents[1]


def resource_skill_root():
    """Locate the owning Skill for both historical and integrated resource layouts."""
    if RAW_ROOT.name == 'jianying-editor':
        return RAW_ROOT
    if (RAW_ROOT.name == 'jianying' and RAW_ROOT.parent.name == 'backends'
            and RAW_ROOT.parent.parent.name == 'editing-workbench'):
        return RAW_ROOT.parent.parent
    return None


def external_directory(value, label):
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{label} must be an absolute directory')
    resolved = path.resolve()
    protected = [RUNTIME_ROOT.parent]
    skill = resource_skill_root()
    if skill is not None:
        protected.append(skill.parent)
    protected.extend(Path.home() / name / 'skills' for name in ('.codex', '.dsh', '.agents'))
    if os.environ.get('CODEX_HOME'):
        protected.append(Path(os.environ['CODEX_HOME']) / 'skills')
    # Source checkouts can keep thin Skills beside the engine; wheel installs have no such sibling.
    protected.extend(parent / 'skills' for parent in RUNTIME_ROOT.parents if (parent / 'skills').is_dir())
    if any(resolved.is_relative_to(root.resolve()) or path.absolute().is_relative_to(root.absolute())
           for root in protected):
        raise ValueError(f'{label} must be outside the engine package and Skills directories')
    return resolved


_MODEL_ENV_VARS = {
    'demucs': ('OPENEDIT_DEMUCS_MODEL', 'JIANYING_DEMUCS_MODEL'),
    'whisper': ('OPENEDIT_WHISPER_MODEL', 'JIANYING_WHISPER_MODEL'),
    'vad': ('OPENEDIT_VAD_MODEL',),
    'matting': ('OPENEDIT_MATTING_MODEL',),
}
_WHISPER_PREFERRED = (
    'faster-whisper-large-v3-turbo',
    'faster-whisper-large-v3',
    'faster-whisper-medium',
    'faster-whisper-small',
    'faster-whisper-base',
    'faster-whisper-tiny',
)


def models_root():
    """Return local offline models directory, or None when auto-discovery is disabled."""
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    if 'OPENEDIT_MODELS_DIR' in os.environ:
        raw = os.environ['OPENEDIT_MODELS_DIR'].strip()
        if not raw:
            return None
        return Path(os.path.expandvars(raw)).expanduser().resolve()
    return (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools' / 'models').resolve()


def _is_whisper_dir(path):
    if not path.is_dir():
        return False
    if (path / 'model.bin').is_file():
        return True
    snapshots = path / 'snapshots'
    return snapshots.is_dir() and any((snap / 'model.bin').is_file() for snap in snapshots.iterdir() if snap.is_dir())


def _whisper_rank(name):
    lower = name.lower()
    for idx, tag in enumerate(('large-v3-turbo', 'large-v3', 'medium', 'small', 'base', 'tiny')):
        if tag in lower:
            return (0, idx, lower)
    return (1, 99, lower)


def _discover_whisper(root):
    candidates = []
    for parent in (root, root / 'whisper'):
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and 'whisper' in child.name.lower() and _is_whisper_dir(child):
                pref = _WHISPER_PREFERRED.index(child.name) if child.name in _WHISPER_PREFERRED else len(_WHISPER_PREFERRED)
                layout = 0 if not child.name.startswith('models--') else 1
                candidates.append((_whisper_rank(child.name), layout, pref, child.name, child))
    if candidates:
        candidates.sort(key=lambda item: item[:4])
        return candidates[0][4].resolve()
    return root.resolve() if _is_whisper_dir(root) else None


def _discover_onnx_patterns(root, subdirs, patterns, *, fallback_subdir=None):
    for sub in subdirs:
        base = (root / sub) if sub else root
        if not base.is_dir():
            continue
        for pat in patterns:
            matches = sorted(p for p in base.glob(pat) if p.is_file())
            if matches:
                return matches[0].resolve()
    if fallback_subdir:
        sub_dir = root / fallback_subdir
        if sub_dir.is_dir():
            any_onnx = sorted(p for p in sub_dir.glob('*.onnx') if p.is_file())
            if any_onnx:
                return any_onnx[0].resolve()
    return None


def discover_model(kind, *, include_env=True):
    """Resolve an explicit env override or auto-discover local offline AI weights; never downloads."""
    if kind not in _MODEL_ENV_VARS:
        raise ValueError(f'Unknown model kind: {kind}')
    if include_env:
        for env_key in _MODEL_ENV_VARS[kind]:
            raw = os.environ.get(env_key)
            if raw and raw.strip():
                return Path(os.path.expandvars(raw.strip())).expanduser().resolve()
    root = models_root()
    if root is None or not root.is_dir():
        return None
    if kind == 'whisper':
        return _discover_whisper(root)
    if kind == 'demucs':
        return _discover_onnx_patterns(
            root, ('demucs', ''), ('htdemucs_fp16weights.onnx', '*demucs*.onnx'), fallback_subdir='demucs',
        )
    if kind == 'vad':
        return _discover_onnx_patterns(
            root, ('vad', ''), ('silero_vad.onnx', '*silero*vad*.onnx'), fallback_subdir='vad',
        )
    return _discover_onnx_patterns(
        root,
        ('matting', ''),
        ('rvm*.onnx', '*rvm*.onnx', 'birefnet*.onnx', '*birefnet*.onnx', 'rmbg*.onnx', '*rmbg*.onnx'),
        fallback_subdir='matting',
    )


def model_license_note(kind, path):
    """Describe known license boundaries for auto-discovered offline ONNX/CT2 weights."""
    if kind == 'demucs':
        return 'MIT (htdemucs ONNX)'
    if kind == 'whisper':
        return 'MIT (faster-whisper CTranslate2)'
    if kind == 'vad':
        return 'MIT (Silero VAD ONNX)'
    if kind == 'matting':
        stem = Path(path).stem.lower() if path else ''
        if 'rmbg' in stem:
            return 'BRIA RMBG-1.4 non-commercial only (swap in MIT BiRefNet or GPL-3.0 RVM for commercial delivery)'
        if 'birefnet' in stem:
            return 'MIT (BiRefNet ONNX)'
        if 'rvm' in stem:
            return 'GPL-3.0 (RobustVideoMatting ONNX)'
        return 'caller-supplied ONNX matting weights'
    return 'caller-supplied weights'


def _whisper_model_bin_bytes(path):
    if (path / 'model.bin').is_file():
        return (path / 'model.bin').stat().st_size
    snapshots = path / 'snapshots'
    if snapshots.is_dir():
        bins = sorted((s / 'model.bin' for s in snapshots.iterdir() if (s / 'model.bin').is_file()))
        if bins:
            return bins[-1].stat().st_size
    return None


def discover_local_models():
    """Return read-only inventory of offline AI weights detected on disk."""
    inventory = {}
    for kind in ('demucs', 'whisper', 'vad', 'matting'):
        cand = discover_model(kind)
        if kind == 'whisper':
            ok = bool(cand is not None and _is_whisper_dir(cand))
            size_bytes = _whisper_model_bin_bytes(cand) if ok else None
        else:
            ok = bool(cand is not None and cand.is_file())
            size_bytes = cand.stat().st_size if ok else None
        inventory[kind] = {
            'available': ok,
            'path': str(cand) if ok else None,
            'size_bytes': size_bytes,
            'license_note': model_license_note(kind, cand) if ok else None,
        }
    return inventory


def stock_config():
    explicit = os.environ.get('OPENEDIT_STOCK_CONFIG') or os.environ.get('JIANYING_STOCK_CONFIG')
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError('OPENEDIT_STOCK_CONFIG / JIANYING_STOCK_CONFIG must name an existing absolute file')
        return path.resolve()
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home())
    default = base / 'OpenEditEngine' / 'config.env'
    legacy_jy = base / 'JianyingEngine' / 'config.env'
    # A resource-path caller retains its configuration after consolidation as well.
    # __file__ preserves the lexical internal directory when launched through its junction.
    legacy = RAW_ROOT / 'config.env'
    if resource_skill_root() is not None and legacy.is_file():
        return legacy
    if not default.is_file() and legacy_jy.is_file():
        return legacy_jy
    return default
