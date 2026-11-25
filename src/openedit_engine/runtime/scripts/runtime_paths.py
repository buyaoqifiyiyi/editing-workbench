"""Portable runtime resources and external state paths; never reads credentials."""
import json
import os
from pathlib import Path
import shutil
import site
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(__file__).absolute().parents[1]
_CUDA_DLL_HANDLES = []
_CUDA_PRELOADED = False


def external_directory(value, label):
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{label} must be an absolute directory')
    resolved = path.resolve()
    protected = [RUNTIME_ROOT.parent]
    protected.extend(Path.home() / name / 'skills' for name in ('.codex', '.dsh', '.agents', '.gemini'))
    protected.append(Path.home() / '.gemini' / 'config' / 'skills')
    if os.environ.get('CODEX_HOME'):
        protected.append(Path(os.environ['CODEX_HOME']) / 'skills')
    # Source checkouts can keep thin Skills beside the engine; wheel installs have no such sibling.
    protected.extend(parent / 'skills' for parent in RUNTIME_ROOT.parents if (parent / 'skills').is_dir())
    if any(resolved.is_relative_to(root.resolve()) or path.absolute().is_relative_to(root.absolute())
           for root in protected):
        raise ValueError(f'{label} must be outside the engine package and Skills directories')
    return resolved


_MODEL_ENV_VARS = {
    'demucs': ('OPENEDIT_DEMUCS_MODEL',),
    'whisper': ('OPENEDIT_WHISPER_MODEL',),
    'vad': ('OPENEDIT_VAD_MODEL',),
    'matting': ('OPENEDIT_MATTING_MODEL',),
    'matanyone2': ('OPENEDIT_MATANYONE2_MODEL',),
    'rvm': ('OPENEDIT_RVM_MODEL',),
    'face': ('OPENEDIT_FACE_MODEL',),
    'beat_this': ('OPENEDIT_BEAT_THIS_MODEL',),
    'clap': ('OPENEDIT_CLAP_MODEL',),
    'flow': ('OPENEDIT_FLOW_MODEL',),
    'depth': ('OPENEDIT_DEPTH_MODEL',),
    'depth_video': ('OPENEDIT_DEPTH_VIDEO_MODEL',),
    'siglip2': ('OPENEDIT_SIGLIP2_MODEL',),
}


def discover_matanyone2_python():
    """Locate isolated MatAnyone 2 CUDA Python interpreter (OPENEDIT_MATANYONE2_PYTHON or LocalAppData)."""
    explicit = os.environ.get('OPENEDIT_MATANYONE2_PYTHON', '').strip()
    if explicit:
        cand = Path(os.path.expandvars(explicit)).expanduser().resolve()
        return cand if cand.is_file() else None
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools' / 'venvs' / 'matanyone2'
    for rel in ('Scripts/python.exe', 'bin/python'):
        cand = base / rel
        if cand.is_file():
            return cand.resolve()
    return None


def discover_torch_worker_python():
    """Locate isolated CUDA PyTorch Python interpreter (music_ai / matanyone2)."""
    for env_key in ('OPENEDIT_TORCH_PYTHON', 'OPENEDIT_MATANYONE2_PYTHON'):
        explicit = os.environ.get(env_key, '').strip()
        if explicit:
            cand = Path(os.path.expandvars(explicit)).expanduser().resolve()
            return cand if cand.is_file() else None
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    venvs_root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools' / 'venvs'
    for name in ('music_ai', 'matanyone2'):
        for rel in ('Scripts/python.exe', 'bin/python'):
            cand = venvs_root / name / rel
            if cand.is_file():
                return cand.resolve()
    return None


def discover_vendor_dir(name):
    """Locate external vendor repository under %LOCALAPPDATA%\\OpenEditTools\\vendors."""
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    cand = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools' / 'vendors' / str(name)
    return cand.resolve() if cand.is_dir() else None


def discover_rife_cli():
    """Locate rife-ncnn-vulkan portable executable (OPENEDIT_RIFE_CLI, OpenEditTools, or PATH)."""
    explicit = os.environ.get('OPENEDIT_RIFE_CLI', '').strip()
    if explicit:
        cand = Path(os.path.expandvars(explicit)).expanduser().resolve()
        return cand if cand.is_file() else None
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    tools_root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools'
    if tools_root.is_dir():
        exe_names = ('rife-ncnn-vulkan.exe', 'rife-ncnn-vulkan') if os.name == 'nt' else ('rife-ncnn-vulkan',)
        for folder in sorted(tools_root.glob('rife-ncnn-vulkan*'), reverse=True):
            if folder.is_dir():
                for exe_name in exe_names:
                    cand = folder / exe_name
                    if cand.is_file():
                        return cand.resolve()
    found = shutil.which('rife-ncnn-vulkan')
    return Path(found).resolve() if found else None


def discover_hyperframes_cli():
    """Locate HyperFrames CLI entry (OPENEDIT_HYPERFRAMES_CLI, OpenEditTools, or PATH)."""
    explicit = os.environ.get('OPENEDIT_HYPERFRAMES_CLI', '').strip()
    if explicit:
        cand = Path(os.path.expandvars(explicit)).expanduser().resolve()
        return cand if cand.is_file() else None
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled in ('1', 'true', 'yes'):
        return None
    tools_root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools'
    if tools_root.is_dir():
        rel_cands = (
            ('node_modules/hyperframes/bin/hyperframes.mjs', 'node_modules/.bin/hyperframes.cmd')
            if os.name == 'nt'
            else ('node_modules/hyperframes/bin/hyperframes.mjs', 'node_modules/.bin/hyperframes')
        )
        for folder in sorted(tools_root.glob('hyperframes*'), reverse=True):
            if folder.is_dir() and not folder.name.startswith('hyperframes-poc'):
                for rel in rel_cands:
                    cand = folder / rel
                    if cand.is_file():
                        return cand.resolve()
    found = shutil.which('hyperframes')
    return Path(found).resolve() if found else None


def discover_hyperframes_gsap(cli_path=None):
    """Locate local gsap.min.js bundled alongside HyperFrames for offline renders."""
    roots = []
    if cli_path:
        p = Path(cli_path).resolve()
        for parent in (p.parent, *p.parents):
            if parent.name == 'node_modules':
                roots.append(parent)
                break
            nm = parent / 'node_modules'
            if nm.is_dir():
                roots.append(nm)
                break
    disabled = os.environ.get('OPENEDIT_NO_AUTO_MODELS', '').strip().lower()
    if disabled not in ('1', 'true', 'yes'):
        tools_root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools'
        if tools_root.is_dir():
            for folder in sorted(tools_root.glob('hyperframes*'), reverse=True):
                if folder.is_dir() and not folder.name.startswith('hyperframes-poc'):
                    nm = folder / 'node_modules'
                    if nm.is_dir():
                        roots.append(nm)
    for nm in roots:
        cand = nm / 'gsap' / 'dist' / 'gsap.min.js'
        if cand.is_file():
            return cand.resolve()
    return None


def discover_cjk_font_file(explicit=None):
    """Locate a standalone static CJK TTF/OTF font file for deterministic offline @font-face."""
    if explicit:
        cand = Path(os.path.expandvars(str(explicit))).expanduser().resolve()
        if not cand.is_file():
            raise ValueError(f'CJK font file not found: {explicit}')
        return cand
    env_font = os.environ.get('OPENEDIT_CJK_FONT_PATH', '').strip()
    if env_font:
        cand = Path(os.path.expandvars(env_font)).expanduser().resolve()
        if cand.is_file():
            return cand
    cands = [
        Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'simhei.ttf',
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf'),
        Path('/System/Library/Fonts/STHeiti Medium.ttc'),
    ]
    for cand in cands:
        if cand.is_file():
            return cand.resolve()
    return None


def discover_chromium_browser():
    """Locate a local Chromium-based browser executable without downloading on first use."""
    for var in ('HYPERFRAMES_BROWSER_PATH', 'OPENEDIT_BROWSER_PATH'):
        raw = os.environ.get(var, '').strip()
        if raw:
            cand = Path(os.path.expandvars(raw)).expanduser().resolve()
            if cand.is_file():
                return cand
    fixed = (
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
    )
    for cand in fixed:
        if cand.is_file():
            return cand.resolve()
    for name in ('chrome', 'google-chrome', 'chromium', 'chromium-browser', 'msedge'):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None
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


def _discover_onnx_patterns(root, subdirs, patterns, *, fallback_subdir=None, exclude_substring=None):
    for sub in subdirs:
        base = (root / sub) if sub else root
        if not base.is_dir():
            continue
        for pat in patterns:
            matches = sorted(
                p for p in base.glob(pat)
                if p.is_file() and (not exclude_substring or exclude_substring not in p.name.lower())
            )
            if matches:
                return matches[0].resolve()
    if fallback_subdir:
        sub_dir = root / fallback_subdir
        if sub_dir.is_dir():
            any_onnx = sorted(
                p for p in sub_dir.glob('*.onnx')
                if p.is_file() and (not exclude_substring or exclude_substring not in p.name.lower())
            )
            if any_onnx:
                return any_onnx[0].resolve()
    return None


def _bundle_has_files(bundle_dir, required_groups):
    """Return True if bundle_dir contains at least one matching file for every glob group."""
    if not bundle_dir or not bundle_dir.is_dir():
        return False
    for group in required_groups:
        if not any(bundle_dir.glob(pat) for pat in group):
            return False
    return True


def _discover_bundle_dir(root, subdirs, required_groups):
    for sub in subdirs:
        base = (root / sub) if sub else root
        if _bundle_has_files(base, required_groups):
            return base.resolve()
    return None


def _bundle_onnx_bytes(bundle_dir):
    if not bundle_dir or not bundle_dir.is_dir():
        return None
    total = sum(p.stat().st_size for p in bundle_dir.rglob('*.onnx') if p.is_file() and '.cache' not in p.parts)
    return total or None


def discover_model(kind, *, include_env=True, prefer_fp16=False):
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
    if kind == 'face':
        return _discover_onnx_patterns(
            root, ('face', ''), ('*yunet*.onnx', '*face*.onnx'), fallback_subdir='face',
        )
    if kind == 'beat_this':
        return _discover_onnx_patterns(
            root, ('beats', 'beat_this', ''), ('beat_this_final*.onnx', 'beat_this_*.onnx'),
        )
    if kind == 'clap':
        return _discover_bundle_dir(
            root, ('clap', ''), (
                ('onnx/audio_model*.onnx', 'audio_model*.onnx'),
                ('onnx/text_model*.onnx', 'text_model*.onnx'),
                ('tokenizer.json',),
            ),
        )
    if kind == 'flow':
        return _discover_onnx_patterns(
            root, ('flow', 'optical_flow', ''), ('neuflow_mixed.onnx', 'neuflow*.onnx'), fallback_subdir='flow',
        )
    if kind == 'depth_video':
        return _discover_onnx_patterns(
            root, ('depth', 'depth_video', ''), ('video_depth_anything*.pth', '*video_depth*.pth'),
        )
    if kind == 'depth':
        if prefer_fp16:
            fp16 = _discover_onnx_patterns(
                root, ('depth/depth-anything-v2-small/onnx', 'depth/onnx', 'depth'), ('model_fp16.onnx', '*depth*fp16*.onnx'),
            )
            if fp16 is not None:
                return fp16
        return _discover_onnx_patterns(
            root,
            ('depth/depth-anything-v2-small/onnx', 'depth/onnx', 'depth'),
            ('model_fp16.onnx', 'model*.onnx', '*depth*v2*.onnx', '*depth*.onnx'),
            fallback_subdir='depth',
        )
    if kind == 'siglip2':
        return _discover_bundle_dir(
            root, ('siglip2', ''), (
                ('onnx/vision_model*.onnx', 'vision_model*.onnx'),
                ('onnx/text_model*.onnx', 'text_model*.onnx'),
                ('tokenizer.json',),
            ),
        )
    if kind == 'matanyone2':
        return _discover_onnx_patterns(
            root, ('matting', 'matanyone2', ''), ('matanyone2*.pth', '*matanyone2*.pth'),
        )
    if kind == 'rvm':
        if prefer_fp16:
            fp16 = _discover_onnx_patterns(
                root, ('matting/rvm', 'matting', 'rvm'), ('*rvm*fp16*.onnx',),
            )
            if fp16 is not None:
                return fp16
        return _discover_onnx_patterns(
            root, ('matting/rvm', 'matting', 'rvm'), ('rvm*.onnx', '*rvm*.onnx'),
            exclude_substring='fp16',
        )
    if prefer_fp16:
        fp16 = _discover_onnx_patterns(
            root, ('matting', ''), ('*birefnet*fp16*.onnx', '*fp16*.onnx'), fallback_subdir=None,
        )
        if fp16 is not None:
            return fp16
    return _discover_onnx_patterns(
        root,
        ('matting', ''),
        ('rvm*.onnx', '*rvm*.onnx', 'birefnet*.onnx', '*birefnet*.onnx', 'rmbg*.onnx', '*rmbg*.onnx'),
        fallback_subdir='matting',
        exclude_substring='fp16',
    )


def model_license_note(kind, path):
    """Describe known license boundaries for auto-discovered offline ONNX/CT2 weights."""
    if kind == 'demucs':
        return 'MIT (htdemucs ONNX)'
    if kind == 'whisper':
        return 'MIT (faster-whisper CTranslate2)'
    if kind == 'vad':
        return 'MIT (Silero VAD ONNX)'
    if kind == 'face':
        return 'MIT (OpenCV Zoo YuNet ONNX)'
    if kind == 'beat_this':
        return 'MIT (Beat This! CP-JKU ONNX)'
    if kind == 'clap':
        return 'Apache-2.0 / CC0-derived (LAION CLAP HTSAT ONNX)'
    if kind == 'flow':
        return 'Apache-2.0 (NeuFlow v2 ONNX)'
    if kind == 'depth':
        return 'Apache-2.0 (Depth Anything V2 Small ONNX)'
    if kind == 'depth_video':
        return 'Apache-2.0 (Video Depth Anything Small)'
    if kind == 'siglip2':
        return 'Apache-2.0 (Google SigLIP 2 ONNX)'
    if kind == 'matanyone2':
        return 'NTU S-Lab License 1.0 (MatAnyone 2, non-commercial default; commercial use requires author permission)'
    if kind == 'rvm':
        return 'GPL-3.0 (RobustVideoMatting ONNX)'
    if kind == 'matting':
        stem = Path(path).stem.lower() if path else ''
        if 'rmbg' in stem:
            return 'BRIA RMBG-1.4 non-commercial only (swap in MIT BiRefNet or GPL-3.0 RVM for commercial delivery)'
        if 'matanyone' in stem:
            return 'NTU S-Lab License 1.0 (MatAnyone 2, non-commercial default; commercial use requires author permission)'
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
    kinds = (
        'demucs', 'whisper', 'vad', 'matting', 'face', 'matanyone2', 'rvm',
        'beat_this', 'clap', 'flow', 'depth', 'depth_video', 'siglip2',
    )
    for kind in kinds:
        cand = discover_model(kind)
        if kind == 'whisper':
            ok = bool(cand is not None and _is_whisper_dir(cand))
            size_bytes = _whisper_model_bin_bytes(cand) if ok else None
        elif kind in ('clap', 'siglip2'):
            ok = bool(cand is not None and cand.is_dir())
            size_bytes = _bundle_onnx_bytes(cand) if ok else None
        else:
            ok = bool(cand is not None and cand.is_file())
            size_bytes = cand.stat().st_size if ok else None
        entry = {
            'available': ok,
            'path': str(cand) if ok else None,
            'size_bytes': size_bytes,
            'license_note': model_license_note(kind, cand) if ok else None,
        }
        if kind == 'matanyone2':
            py = discover_matanyone2_python()
            entry['worker_python'] = str(py) if py else None
            entry['weights_available'] = ok
            entry['available'] = bool(ok and py is not None)
        inventory[kind] = entry
    return inventory


def prepare_cuda_dlls():
    """Register pip-installed nvidia CUDA/cuDNN DLL folders and preload onnxruntime CUDA shared libraries."""
    global _CUDA_PRELOADED
    if _CUDA_PRELOADED:
        return
    dll_dirs = []
    candidates = [Path(sys.prefix) / 'Lib' / 'site-packages', Path(sys.prefix) / 'lib' / 'site-packages']
    try:
        candidates.extend(Path(p) for p in site.getsitepackages())
    except Exception:
        pass
    seen_roots = set()
    for sp in candidates:
        nv = sp / 'nvidia'
        if not nv.is_dir():
            continue
        r = str(nv.resolve())
        if r in seen_roots:
            continue
        seen_roots.add(r)
        for dll in nv.rglob('*.dll' if os.name == 'nt' else '*.so*'):
            p = str(dll.parent.resolve())
            if p not in dll_dirs:
                dll_dirs.append(p)
    if dll_dirs:
        if hasattr(os, 'add_dll_directory'):
            for d in dll_dirs:
                try:
                    _CUDA_DLL_HANDLES.append(os.add_dll_directory(d))
                except OSError:
                    pass
        cur_path = os.environ.get('PATH', '')
        existing = set(cur_path.split(os.pathsep)) if cur_path else set()
        missing = [d for d in dll_dirs if d not in existing]
        if missing:
            os.environ['PATH'] = os.pathsep.join(missing + ([cur_path] if cur_path else []))
    try:
        import onnxruntime as ort
        preload = getattr(ort, 'preload_dlls', None)
        if callable(preload):
            preload()
    except Exception:
        pass
    _CUDA_PRELOADED = True


def _provider_entry(name):
    if name == 'CUDAExecutionProvider':
        # Avoid ORT's EXHAUSTIVE cuDNN search & greedy power-of-two arena that can exhaust 6 GB VRAM.
        return (
            'CUDAExecutionProvider',
            {'cudnn_conv_algo_search': 'HEURISTIC', 'arena_extend_strategy': 'kSameAsRequested'},
        )
    return name


def resolve_ort_providers(available_providers, *, device='cpu', gpu=False):
    """Select deterministic onnxruntime execution providers from explicit device/gpu flags."""
    req = (device or 'cpu').strip().lower()
    if req not in ('cpu', 'cuda', 'dml', 'auto'):
        raise ValueError(f'Unsupported execution device: {device}; choose cpu, cuda, dml, or auto')
    if gpu and req == 'cpu':
        req = 'auto'
    avail = list(available_providers or [])
    if req == 'cpu':
        return ['CPUExecutionProvider']
    if req == 'cuda':
        if 'CUDAExecutionProvider' not in avail:
            raise ValueError('CUDAExecutionProvider is unavailable in onnxruntime; install onnxruntime-gpu[cuda,cudnn] or use --device cpu')
        names = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if 'CPUExecutionProvider' in avail else ['CUDAExecutionProvider']
        return [_provider_entry(n) for n in names]
    if req == 'dml':
        if 'DmlExecutionProvider' not in avail:
            raise ValueError('DmlExecutionProvider is unavailable in onnxruntime; use --device cpu or cuda')
        return ['DmlExecutionProvider', 'CPUExecutionProvider'] if 'CPUExecutionProvider' in avail else ['DmlExecutionProvider']
    preferred = [p for p in ('CUDAExecutionProvider', 'DmlExecutionProvider', 'CPUExecutionProvider') if p in avail]
    names = preferred or ['CPUExecutionProvider']
    return [_provider_entry(n) for n in names]


def stock_config():
    explicit = os.environ.get('OPENEDIT_STOCK_CONFIG')
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError('OPENEDIT_STOCK_CONFIG must name an existing absolute file')
        return path.resolve()
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home())
    return base / 'OpenEditEngine' / 'config.env'


def discover_mesh_avatar_studio(explicit=None):
    """Read-only probe of configured Mesh Avatar Studio installation and headless render dependencies."""
    cfg_file = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditEngine' / 'mesh-avatar.json'
    raw_studio = explicit
    source = 'explicit' if explicit else None
    node = shutil.which('node')
    browser = discover_chromium_browser()
    if raw_studio is None:
        env_studio = os.environ.get('OPENEDIT_MESH_AVATAR_STUDIO', '').strip()
        if env_studio:
            raw_studio = env_studio
            source = 'env'
        elif cfg_file.is_file():
            source = 'config'
            try:
                data = json.loads(cfg_file.read_text(encoding='utf-8-sig'))
                if isinstance(data, dict) and data.get('studio'):
                    raw_studio = str(data['studio'])
            except Exception as exc:
                return {
                    'configured': True,
                    'available': False,
                    'source': source,
                    'config_path': str(cfg_file),
                    'studio_path': None,
                    'runtime_built': False,
                    'playwright_installed': False,
                    'node': node,
                    'browser': str(browser) if browser else None,
                    'license': 'MIT (Mesh Avatar Studio runtime)',
                    'error': f'Invalid mesh-avatar config JSON: {exc}',
                }
    if not raw_studio:
        return {
            'configured': False,
            'available': False,
            'source': None,
            'config_path': str(cfg_file),
            'studio_path': None,
            'runtime_built': False,
            'playwright_installed': False,
            'node': node,
            'browser': str(browser) if browser else None,
            'license': 'MIT (Mesh Avatar Studio runtime)',
            'error': None,
        }
    studio_cand = Path(os.path.expandvars(str(raw_studio))).expanduser()
    exists = studio_cand.is_dir()
    studio_res = str(studio_cand.resolve()) if exists else str(studio_cand)
    runtime_built = bool(exists and (studio_cand / 'packages' / 'runtime' / 'dist' / 'index.js').is_file())
    playwright_installed = bool(exists and (studio_cand / 'node_modules' / 'playwright').is_dir())
    err = None
    if not exists:
        err = f'Mesh Avatar Studio directory not found: {studio_cand}'
    elif not runtime_built or not playwright_installed:
        err = 'studio requires built runtime and installed Playwright'
    elif not node:
        err = 'node must be available on PATH'
    elif not browser:
        err = 'no local Chrome/Edge Chromium browser found'
    available = bool(exists and runtime_built and playwright_installed and node and browser)
    return {
        'configured': True,
        'available': available,
        'source': source,
        'config_path': str(cfg_file),
        'studio_path': studio_res,
        'runtime_built': runtime_built,
        'playwright_installed': playwright_installed,
        'node': node,
        'browser': str(browser) if browser else None,
        'license': 'MIT (Mesh Avatar Studio runtime)',
        'error': err,
    }
