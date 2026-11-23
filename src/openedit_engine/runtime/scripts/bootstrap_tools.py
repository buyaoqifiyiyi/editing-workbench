"""Tiered installer and readiness checker for OpenEdit external tools and offline AI models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

import runtime_common
import upstream_guard

TIERS = ('core', 'ai', 'full', 'gpu-heavy')
_TIER_RANK = {name: idx for idx, name in enumerate(TIERS)}
DEFAULT_HF_ENDPOINT = 'https://huggingface.co'
DEFAULT_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cu128'

_RIFE_URL = 'https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip'
_YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
_RVM_URL = 'https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_resnet50_fp32.onnx'
_MATANYONE2_URL = 'https://github.com/pq-yang/MatAnyone2/releases/download/v1.0.0/matanyone2.pth'

_ALLIN1_REPO = 'taejunkim/allinone'
_ALLIN1_REV = '379e5fd010b3fdd0ee8381ff8cbcfa51d70b5c19'
_ALLIN1_FOLDS = (
    'harmonix-fold0-0vra4ys2.pth',
    'harmonix-fold1-3ozjhtsj.pth',
    'harmonix-fold2-gmgo0nsy.pth',
    'harmonix-fold3-i92b7m8p.pth',
    'harmonix-fold4-1bql5qo0.pth',
    'harmonix-fold5-x4z5zeef.pth',
    'harmonix-fold6-x7t226rq.pth',
    'harmonix-fold7-qwwskhg6.pth',
)


def _hf_file(repo, remote_path, local_rel, *, sha256=None):
    return {'kind': 'hf', 'repo': repo, 'remote': remote_path, 'local': local_rel, 'sha256': sha256}


def _url_file(url, local_rel, *, sha256=None):
    return {'kind': 'url', 'url': url, 'local': local_rel, 'sha256': sha256}


CATALOG = (
    {
        'id': 'filmcraft',
        'tier': 'core',
        'kind': 'github_craft_release',
        'repo': 'storytold/filmcraft',
        'app_id': 'filmcraft',
        'license': 'MIT OR Apache-2.0',
        'size_mb': 214,
        'description': 'ArtCraft FilmCraft multitrack timeline, Lumetri grading, captions, and MP4/OTIO/FCPXML exporter.',
    },
    {
        'id': 'effectcraft',
        'tier': 'core',
        'kind': 'github_craft_release',
        'repo': 'storytold/effectcraft',
        'app_id': 'effectcraft',
        'license': 'MIT OR Apache-2.0',
        'size_mb': 216,
        'description': 'ArtCraft EffectCraft motion graphics, 308 VFX, and ProRes 4444 alpha overlay renderer.',
    },
    {
        'id': 'ffmpeg',
        'tier': 'core',
        'kind': 'system_ffmpeg',
        'license': 'LGPL / GPL',
        'size_mb': 120,
        'description': 'System FFmpeg and FFprobe binaries used for media inspection, CFR transcoding, audio master, and strict decode.',
    },
    {
        'id': 'rife',
        'tier': 'ai',
        'kind': 'zip_bundle',
        'url': _RIFE_URL,
        'target_dir': 'rife-ncnn-vulkan',
        'exe': 'rife-ncnn-vulkan.exe' if os.name == 'nt' else 'rife-ncnn-vulkan',
        'license': 'MIT',
        'size_mb': 448,
        'description': 'Vulkan RIFE optical-flow frame interpolation and slow-motion CLI.',
    },
    {
        'id': 'pyscenedetect',
        'tier': 'ai',
        'kind': 'pip_no_deps_target',
        'package': 'scenedetect==0.7.2',
        'target_subdir': 'optional/pyscenedetect-0.7.2',
        'license': 'BSD-3-Clause',
        'size_mb': 1,
        'description': 'Optional PySceneDetect cut detector installed without opencv-python to protect headless CSRT tracking.',
    },
    {
        'id': 'demucs',
        'tier': 'ai',
        'kind': 'files',
        'files': (_hf_file('StemSplitio/htdemucs-onnx', 'htdemucs_fp16weights.onnx', 'models/demucs/htdemucs_fp16weights.onnx'),),
        'license': 'MIT',
        'size_mb': 158,
        'description': 'Hybrid Transformer Demucs ONNX weights for 4-stem vocal/accompaniment separation.',
    },
    {
        'id': 'whisper',
        'tier': 'ai',
        'kind': 'files',
        'files': (
            _hf_file('Systran/faster-whisper-base', 'config.json', 'models/faster-whisper-base/config.json'),
            _hf_file('Systran/faster-whisper-base', 'model.bin', 'models/faster-whisper-base/model.bin'),
            _hf_file('Systran/faster-whisper-base', 'tokenizer.json', 'models/faster-whisper-base/tokenizer.json'),
            _hf_file('Systran/faster-whisper-base', 'vocabulary.txt', 'models/faster-whisper-base/vocabulary.txt'),
        ),
        'license': 'MIT',
        'size_mb': 141,
        'description': 'CTranslate2 faster-whisper-base weights for offline ASR transcription.',
    },
    {
        'id': 'vad',
        'tier': 'ai',
        'kind': 'files',
        'files': (_hf_file('onnx-community/silero-vad', 'onnx/model.onnx', 'models/vad/silero_vad.onnx'),),
        'license': 'MIT',
        'size_mb': 2,
        'description': 'Silero VAD ONNX model for auto-cut silence removal.',
    },
    {
        'id': 'face',
        'tier': 'ai',
        'kind': 'files',
        'files': (_url_file(_YUNET_URL, 'models/face/face_detection_yunet_2023mar.onnx'),),
        'license': 'MIT',
        'size_mb': 1,
        'description': 'OpenCV Zoo YuNet ONNX face detector for vertical Auto-Reframe tracking.',
    },
    {
        'id': 'birefnet',
        'tier': 'ai',
        'kind': 'files',
        'files': (_hf_file('onnx-community/BiRefNet_lite', 'onnx/model.onnx', 'models/matting/birefnet_lite.onnx'),),
        'license': 'MIT',
        'size_mb': 214,
        'description': 'BiRefNet-lite ONNX weights for commercial-safe non-green-screen matting.',
    },
    {
        'id': 'rvm',
        'tier': 'ai',
        'kind': 'files',
        'files': (_url_file(_RVM_URL, 'models/matting/rvm/rvm_resnet50_fp32.onnx'),),
        'license': 'GPL-3.0',
        'size_mb': 103,
        'description': 'RobustVideoMatting ONNX recurrent portrait matting weights.',
    },
    {
        'id': 'beat_this',
        'tier': 'ai',
        'kind': 'files',
        'files': (
            _hf_file(
                'nilsbc/beat-this-onnx', 'beat_this_final0.onnx', 'models/beats/beat_this_final0.onnx',
                sha256='a7e2c68b27f9db6a73416017f784ea8551a01373f4955a98db018827760c4a14',
            ),
            _hf_file(
                'nilsbc/beat-this-onnx', 'logmel.onnx', 'models/beats/logmel.onnx',
                sha256='f5afa499f5a42b05ea89a214561170cd4b76f92d67d3227f7c9833e8d79bbb43',
            ),
            _hf_file('nilsbc/beat-this-onnx', 'SHA256SUMS.txt', 'models/beats/SHA256SUMS.txt'),
        ),
        'license': 'MIT',
        'size_mb': 84,
        'description': 'CP-JKU Beat This! ONNX beat and downbeat tracker.',
    },
    {
        'id': 'clap',
        'tier': 'ai',
        'kind': 'files',
        'files': (
            _hf_file('Xenova/clap-htsat-unfused', 'config.json', 'models/clap/config.json'),
            _hf_file('Xenova/clap-htsat-unfused', 'preprocessor_config.json', 'models/clap/preprocessor_config.json'),
            _hf_file('Xenova/clap-htsat-unfused', 'tokenizer.json', 'models/clap/tokenizer.json'),
            _hf_file('Xenova/clap-htsat-unfused', 'onnx/audio_model_fp16.onnx', 'models/clap/onnx/audio_model_fp16.onnx'),
            _hf_file('Xenova/clap-htsat-unfused', 'onnx/text_model_fp16.onnx', 'models/clap/onnx/text_model_fp16.onnx'),
        ),
        'license': 'Apache-2.0',
        'size_mb': 299,
        'description': 'LAION CLAP HTSAT FP16 ONNX models for semantic SFX search.',
    },
    {
        'id': 'flow',
        'tier': 'ai',
        'kind': 'files',
        'files': (_hf_file('bukuroo/NeuFlowV2-ONNX', 'neuflow_mixed.onnx', 'models/flow/neuflow_mixed.onnx'),),
        'license': 'Apache-2.0',
        'size_mb': 40,
        'description': 'NeuFlow v2 ONNX optical-flow estimator for shot motion analysis.',
    },
    {
        'id': 'depth',
        'tier': 'ai',
        'kind': 'files',
        'files': (
            _hf_file('onnx-community/depth-anything-v2-small', 'config.json', 'models/depth/depth-anything-v2-small/config.json'),
            _hf_file('onnx-community/depth-anything-v2-small', 'preprocessor_config.json', 'models/depth/depth-anything-v2-small/preprocessor_config.json'),
            _hf_file('onnx-community/depth-anything-v2-small', 'onnx/model_fp16.onnx', 'models/depth/depth-anything-v2-small/onnx/model_fp16.onnx'),
        ),
        'license': 'Apache-2.0',
        'size_mb': 48,
        'description': 'Depth Anything V2 Small ONNX weights for DoF bokeh and 2.5D parallax.',
    },
    {
        'id': 'siglip2',
        'tier': 'ai',
        'kind': 'files',
        'files': (
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'config.json', 'models/siglip2/config.json'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'preprocessor_config.json', 'models/siglip2/preprocessor_config.json'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'special_tokens_map.json', 'models/siglip2/special_tokens_map.json'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'tokenizer_config.json', 'models/siglip2/tokenizer_config.json'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'tokenizer.json', 'models/siglip2/tokenizer.json'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'onnx/text_model_fp16.onnx', 'models/siglip2/onnx/text_model_fp16.onnx'),
            _hf_file('onnx-community/siglip2-base-patch16-224-ONNX', 'onnx/vision_model_fp16.onnx', 'models/siglip2/onnx/vision_model_fp16.onnx'),
        ),
        'license': 'Apache-2.0',
        'size_mb': 749,
        'description': 'Google SigLIP 2 ONNX vision/text encoders for bilingual natural-language shot search.',
    },
    {
        'id': 'hyperframes',
        'tier': 'full',
        'kind': 'npm_hyperframes',
        'packages': ('hyperframes', 'gsap'),
        'license': 'Apache-2.0',
        'size_mb': 112,
        'description': 'Headless HyperFrames + GSAP data-motion overlay backend (requires Node.js >=22 + Chrome/Edge).',
    },
    {
        'id': 'matanyone2_vendor',
        'tier': 'gpu-heavy',
        'kind': 'git_clone',
        'repo_url': 'https://github.com/pq-yang/MatAnyone2.git',
        'target_dir': 'vendors/MatAnyone2',
        'license': 'NTU S-Lab License 1.0 (non-commercial default)',
        'size_mb': 33,
        'description': 'MatAnyone 2 vendor source code for temporal-consistent video matting.',
    },
    {
        'id': 'matanyone2',
        'tier': 'gpu-heavy',
        'kind': 'files',
        'files': (_url_file(_MATANYONE2_URL, 'models/matting/matanyone2.pth'),),
        'license': 'NTU S-Lab License 1.0 (non-commercial default)',
        'size_mb': 135,
        'description': 'MatAnyone 2 temporal video matting checkpoint.',
    },
    {
        'id': 'video_depth_anything_vendor',
        'tier': 'gpu-heavy',
        'kind': 'git_clone',
        'repo_url': 'https://github.com/DepthAnything/Video-Depth-Anything.git',
        'target_dir': 'vendors/Video-Depth-Anything',
        'license': 'Apache-2.0',
        'size_mb': 16,
        'description': 'Video Depth Anything vendor source code for temporal video depth.',
    },
    {
        'id': 'depth_video',
        'tier': 'gpu-heavy',
        'kind': 'files',
        'files': (_hf_file('depth-anything/Video-Depth-Anything-Small', 'video_depth_anything_vits.pth', 'models/depth/video_depth_anything_vits.pth'),),
        'license': 'Apache-2.0',
        'size_mb': 111,
        'description': 'Video Depth Anything Small temporal depth weights.',
    },
    {
        'id': 'allin1',
        'tier': 'gpu-heavy',
        'kind': 'files',
        'files': tuple(
            _hf_file(
                _ALLIN1_REPO,
                fn,
                f'models/allin1/hub/models--taejunkim--allinone/snapshots/{_ALLIN1_REV}/{fn}',
            )
            for fn in _ALLIN1_FOLDS
        ),
        'license': 'MIT',
        'size_mb': 11,
        'description': 'allin1 music phrase structure analyzer checkpoints (harmonix-fold0..7).',
    },
    {
        'id': 'torch_worker',
        'tier': 'gpu-heavy',
        'kind': 'torch_venv',
        'venv_rel': 'venvs/matanyone2',
        'license': 'BSD-3-Clause / Apache-2.0 / MIT',
        'size_mb': 4950,
        'description': 'Isolated PyTorch CUDA worker venv for MatAnyone 2, Video Depth Anything, and allin1.',
    },
)

CATALOG_BY_ID = {entry['id']: entry for entry in CATALOG}


def default_tools_dir():
    return (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditTools').resolve()


def default_engine_dir():
    return (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditEngine').resolve()


def normalize_hf_endpoint(endpoint=None):
    raw = endpoint if endpoint is not None else os.environ.get('HF_ENDPOINT')
    cleaned = str(raw or DEFAULT_HF_ENDPOINT).strip().rstrip('/')
    if not cleaned.startswith(('https://', 'http://')):
        raise ValueError(f'hf_endpoint must start with https:// or http://: {cleaned}')
    return cleaned


def resolve_download_url(file_spec, hf_endpoint):
    if file_spec['kind'] == 'url':
        return file_spec['url']
    if file_spec['kind'] == 'hf':
        return f"{hf_endpoint}/{file_spec['repo']}/resolve/main/{file_spec['remote']}"
    raise ValueError(f"Unsupported file_spec kind: {file_spec['kind']}")


def download_file(url, dest_path, *, expected_sha256=None, timeout=120):
    """Stream download to a .part file, verify non-empty + optional SHA-256, and replace atomically."""
    dest = Path(dest_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    if part.exists():
        part.unlink()
    expected_lower = expected_sha256.lower() if expected_sha256 else None
    req = urllib.request.Request(url, headers={'User-Agent': 'openedit-engine-bootstrap/1.0'})
    hasher = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp, part.open('wb') as handle:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                hasher.update(chunk)
                handle.write(chunk)
                total += len(chunk)
        if total == 0:
            raise RuntimeError(f'Downloaded 0 bytes from {url}')
        actual = hasher.hexdigest().lower()
        if expected_lower and actual != expected_lower:
            raise RuntimeError(f'SHA-256 mismatch for {dest.name}: expected {expected_lower}, got {actual}')
        os.replace(part, dest)
        return {'path': str(dest), 'bytes': total, 'sha256': actual}
    except Exception:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise


def _has_whisper_dir(models_dir):
    for parent in (models_dir, models_dir / 'whisper'):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or 'whisper' not in child.name.lower():
                continue
            if (child / 'model.bin').is_file():
                return child.resolve()
            snapshots = child / 'snapshots'
            if snapshots.is_dir():
                for snap in sorted(snapshots.iterdir()):
                    if snap.is_dir() and (snap / 'model.bin').is_file():
                        return child.resolve()
    return None


def _system_ffmpeg_status():
    suffix = '.exe' if os.name == 'nt' else ''
    found = {}
    for tool_name in ('ffmpeg', 'ffprobe'):
        override = os.environ.get('OPENEDIT_' + tool_name.upper(), '').strip()
        cand = Path(os.path.expandvars(override)).expanduser().resolve() if override else None
        if cand and cand.is_file():
            found[tool_name] = str(cand)
        else:
            located = shutil.which(tool_name) or shutil.which(tool_name + suffix)
            if located and Path(located).is_file():
                found[tool_name] = str(Path(located).resolve())
    return found


def detect_component(entry, tools_dir, engine_dir):
    """Check strictly within tools_dir / engine_dir (except ffmpeg system check) if component is present."""
    cid = entry['id']
    kind = entry['kind']
    if kind == 'github_craft_release':
        app_id = entry['app_id']
        cli_name = f'{app_id}-cli.exe' if os.name == 'nt' else f'{app_id}-cli'
        for folder in sorted(tools_dir.glob(f'{app_id}*'), reverse=True):
            if folder.is_dir():
                for hit in sorted(folder.rglob(cli_name)):
                    if hit.is_file():
                        return True, str(hit.resolve())
        return False, None
    if kind == 'system_ffmpeg':
        found = _system_ffmpeg_status()
        if 'ffmpeg' in found and 'ffprobe' in found:
            return True, found['ffmpeg']
        return False, None
    if kind == 'zip_bundle':
        exe_name = entry['exe']
        for folder in sorted(tools_dir.glob(f"{entry['target_dir']}*"), reverse=True):
            if folder.is_dir():
                direct = folder / exe_name
                if direct.is_file():
                    return True, str(direct.resolve())
                for hit in sorted(folder.rglob(exe_name)):
                    if hit.is_file():
                        return True, str(hit.resolve())
        return False, None
    if kind == 'pip_no_deps_target':
        opt_dir = engine_dir / entry['target_subdir']
        init_py = opt_dir / 'scenedetect' / '__init__.py'
        return (init_py.is_file(), str(init_py.resolve()) if init_py.is_file() else None)
    if cid == 'whisper':
        hit = _has_whisper_dir(tools_dir / 'models')
        return (hit is not None, str(hit) if hit else None)
    if cid == 'allin1':
        allin1_dir = tools_dir / 'models' / 'allin1'
        if allin1_dir.is_dir():
            hits = sorted(allin1_dir.rglob('harmonix-fold0*.pth'))
            if hits:
                return True, str(hits[0].resolve())
        return False, None
    if kind == 'files':
        paths = [tools_dir / f['local'] for f in entry['files']]
        ok = all(p.is_file() and p.stat().st_size > 0 for p in paths)
        return ok, (str(paths[0].resolve()) if ok else None)
    if kind == 'npm_hyperframes':
        hf_root = tools_dir / 'hyperframes'
        mjs = hf_root / 'node_modules' / 'hyperframes' / 'bin' / 'hyperframes.mjs'
        gsap = hf_root / 'node_modules' / 'gsap' / 'dist' / 'gsap.min.js'
        ok = mjs.is_file() and gsap.is_file()
        return ok, (str(mjs.resolve()) if ok else None)
    if kind == 'git_clone':
        dst = tools_dir / entry['target_dir']
        has_py = dst.is_dir() and any(dst.iterdir())
        return has_py, (str(dst.resolve()) if has_py else None)
    if kind == 'torch_venv':
        for name in ('matanyone2', 'music_ai'):
            for rel in ('Scripts/python.exe', 'bin/python'):
                cand = tools_dir / 'venvs' / name / rel
                if cand.is_file():
                    return True, str(cand.resolve())
        return False, None
    raise ValueError(f'Unknown catalog kind: {kind}')


def select_components(*, tier='ai', only=None):
    if tier not in _TIER_RANK:
        raise ValueError(f'Unknown tier: {tier}; choose from {", ".join(TIERS)}')
    if only:
        unknown = [item for item in only if item not in CATALOG_BY_ID]
        if unknown:
            raise ValueError(f'Unknown component(s): {", ".join(unknown)}; valid: {", ".join(CATALOG_BY_ID)}')
        wanted = set(only)
        return [entry for entry in CATALOG if entry['id'] in wanted]
    max_rank = _TIER_RANK[tier]
    return [entry for entry in CATALOG if _TIER_RANK[entry['tier']] <= max_rank]


def _install_craft_release(entry, tools_dir, *, keep_archives=False, timeout=120):
    rel = upstream_guard._fetch_github_latest_release(entry['repo'], timeout=min(int(timeout), 30))
    tag = str(rel.get('tag_name') or '').strip()
    version = tag.lstrip('vV') or 'latest'
    asset, sums_asset = upstream_guard._select_portable_asset(entry['app_id'], version, rel.get('assets'))
    if not asset or not asset.get('download_url'):
        raise RuntimeError(f'No Windows portable asset found for {entry["repo"]} ({tag})')
    expected_sha = upstream_guard._expected_sha256(asset, sums_asset, entry['repo'], timeout=min(int(timeout), 30))
    dest_dir = (tools_dir / f"{entry['app_id']}-{version}").resolve()
    with tempfile.TemporaryDirectory(prefix=f"openedit-bootstrap-{entry['app_id']}-", dir=str(tools_dir)) as stage:
        stage_root = Path(stage)
        zip_path = stage_root / asset['name']
        if expected_sha:
            upstream_guard._download_verified_zip(asset['download_url'], zip_path, expected_sha, entry['repo'], timeout=timeout)
        else:
            download_file(asset['download_url'], zip_path, timeout=timeout)
        extract_dir = stage_root / 'extracted'
        upstream_guard._safe_extract_zip(zip_path, extract_dir)
        if keep_archives:
            arch_dir = tools_dir / '_archives'
            arch_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(zip_path, arch_dir / asset['name'])
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(extract_dir), str(dest_dir))
    cli = upstream_guard._find_staged_cli(dest_dir, entry['app_id'])
    return {'action': 'installed', 'path': str(cli), 'version': version}


def _install_zip_bundle(entry, tools_dir, *, keep_archives=False, timeout=120):
    dest_dir = (tools_dir / entry['target_dir']).resolve()
    with tempfile.TemporaryDirectory(prefix=f"openedit-bootstrap-{entry['id']}-", dir=str(tools_dir)) as stage:
        stage_root = Path(stage)
        zip_name = Path(entry['url'].split('?')[0]).name
        zip_path = stage_root / zip_name
        download_file(entry['url'], zip_path, expected_sha256=entry.get('sha256'), timeout=timeout)
        extract_dir = stage_root / 'extracted'
        upstream_guard._safe_extract_zip(zip_path, extract_dir)
        children = list(extract_dir.iterdir())
        source_dir = children[0] if len(children) == 1 and children[0].is_dir() else extract_dir
        if keep_archives:
            arch_dir = tools_dir / '_archives'
            arch_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(zip_path, arch_dir / zip_name)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(source_dir), str(dest_dir))
    exe = dest_dir / entry['exe']
    if not exe.is_file():
        matches = list(dest_dir.rglob(entry['exe']))
        if not matches:
            raise RuntimeError(f"Expected binary {entry['exe']} not found after extracting {entry['id']}")
        exe = matches[0]
    return {'action': 'installed', 'path': str(exe.resolve())}


def _install_files(entry, tools_dir, hf_endpoint, *, force=False, timeout=120):
    downloaded = []
    for spec in entry['files']:
        dest = (tools_dir / spec['local']).resolve()
        if not force and dest.is_file() and dest.stat().st_size > 0:
            downloaded.append({'path': str(dest), 'skipped': True})
            continue
        url = resolve_download_url(spec, hf_endpoint)
        downloaded.append(download_file(url, dest, expected_sha256=spec.get('sha256'), timeout=timeout))
    return {'action': 'installed', 'path': str((tools_dir / entry['files'][0]['local']).resolve()), 'files': len(downloaded)}


def _install_pyscenedetect(entry, engine_dir):
    target_dir = (engine_dir / entry['target_subdir']).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--no-deps', '--upgrade', '--target', str(target_dir), entry['package']],
        check=True,
    )
    return {'action': 'installed', 'path': str(target_dir)}


def _install_npm_hyperframes(entry, tools_dir):
    npm = shutil.which('npm.cmd') or shutil.which('npm')
    if not npm:
        raise RuntimeError('npm not found in PATH; install Node.js >=22 and re-run bootstrap --tier full')
    hf_dir = (tools_dir / 'hyperframes').resolve()
    hf_dir.mkdir(parents=True, exist_ok=True)
    pkg_json = hf_dir / 'package.json'
    if not pkg_json.is_file():
        pkg_json.write_text(json.dumps({'name': 'hyperframes', 'version': '1.0.0', 'private': True}, indent=2), encoding='utf-8')
    subprocess.run([npm, 'install', '--prefix', str(hf_dir), *entry['packages']], check=True)
    return {'action': 'installed', 'path': str(hf_dir)}


def _install_git_clone(entry, tools_dir):
    git = shutil.which('git')
    if not git:
        raise RuntimeError('git not found in PATH; install Git and re-run')
    dest = (tools_dir / entry['target_dir']).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run([git, 'clone', '--depth', '1', entry['repo_url'], str(dest)], check=True)
    return {'action': 'installed', 'path': str(dest)}


def _install_torch_venv(entry, tools_dir, torch_index_url):
    venv_dir = (tools_dir / entry['venv_rel']).resolve()
    py_exe = venv_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not py_exe.is_file():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)
    subprocess.run(
        [str(py_exe), '-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio', '--index-url', torch_index_url],
        check=True,
    )
    subprocess.run(
        [
            str(py_exe), '-m', 'pip', 'install',
            'hydra-core', 'omegaconf', 'einops', 'scipy', 'opencv-python-headless', 'huggingface-hub',
        ],
        check=True,
    )
    return {'action': 'installed', 'path': str(py_exe)}


def run_bootstrap(*, tier='ai', only=None, tools_dir=None, engine_dir=None, hf_endpoint=None,
                  torch_index_url=DEFAULT_TORCH_INDEX_URL, dry_run=False, force=False,
                  keep_archives=False, timeout=120):
    tools_root = (
        Path(os.path.expandvars(str(tools_dir))).expanduser().resolve()
        if tools_dir is not None else default_tools_dir()
    )
    engine_root = (
        Path(os.path.expandvars(str(engine_dir))).expanduser().resolve()
        if engine_dir is not None else default_engine_dir()
    )
    endpoint = normalize_hf_endpoint(hf_endpoint)
    selected = select_components(tier=tier, only=only)
    if not dry_run:
        tools_root.mkdir(parents=True, exist_ok=True)
        engine_root.mkdir(parents=True, exist_ok=True)

    summary = {'installed': 0, 'already_installed': 0, 'planned': 0, 'skipped': 0, 'failed': 0}
    items = []
    manual_hints = []

    def _run_install():
        for entry in selected:
            present, found_path = detect_component(entry, tools_root, engine_root)
            rec = {
                'id': entry['id'],
                'tier': entry['tier'],
                'license': entry['license'],
                'approx_size_mb': entry['size_mb'],
            }
            if present and not (force and entry['kind'] != 'system_ffmpeg'):
                rec.update(status='already_installed', path=found_path)
                summary['already_installed'] += 1
                items.append(rec)
                continue
            if entry['kind'] == 'system_ffmpeg':
                msg = 'Install FFmpeg & FFprobe (e.g. `winget install Gyan.FFmpeg`) or set OPENEDIT_FFMPEG/OPENEDIT_FFPROBE.'
                manual_hints.append(msg)
                rec.update(status='planned' if dry_run else 'skipped', hint=msg)
                summary['planned' if dry_run else 'skipped'] += 1
                items.append(rec)
                continue
            if dry_run:
                rec.update(status='planned', action='install')
                summary['planned'] += 1
                items.append(rec)
                continue
            try:
                kind = entry['kind']
                if kind == 'github_craft_release':
                    res = _install_craft_release(entry, tools_root, keep_archives=keep_archives, timeout=timeout)
                elif kind == 'zip_bundle':
                    res = _install_zip_bundle(entry, tools_root, keep_archives=keep_archives, timeout=timeout)
                elif kind == 'files':
                    res = _install_files(entry, tools_root, endpoint, force=force, timeout=timeout)
                elif kind == 'pip_no_deps_target':
                    res = _install_pyscenedetect(entry, engine_root)
                elif kind == 'npm_hyperframes':
                    res = _install_npm_hyperframes(entry, tools_root)
                elif kind == 'git_clone':
                    res = _install_git_clone(entry, tools_root)
                elif kind == 'torch_venv':
                    res = _install_torch_venv(entry, tools_root, torch_index_url)
                else:
                    raise ValueError(f'Unsupported installer kind: {kind}')
                rec.update(status='installed', path=res.get('path'))
                summary['installed'] += 1
            except Exception as exc:
                rec.update(status='failed', error=str(exc))
                summary['failed'] += 1
            items.append(rec)

    if dry_run:
        _run_install()
    else:
        with runtime_common.file_lock(
            tools_root / '.bootstrap.lock', blocking=False, pad_empty=False,
            busy_message='Another openedit bootstrap task is already running',
        ):
            _run_install()

    status = 'failed' if summary['failed'] > 0 else 'passed'
    return {
        'status': status,
        'tier': tier,
        'dry_run': bool(dry_run),
        'tools_dir': str(tools_root),
        'engine_dir': str(engine_root),
        'hf_endpoint': endpoint,
        'summary': summary,
        'components': items,
        'manual_hints': manual_hints,
    }


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tier', default='ai', choices=TIERS, help='Component tier to install or check')
    parser.add_argument('--only', nargs='+', help='Install/check only the listed component IDs')
    parser.add_argument('--tools-dir', help='Override %LOCALAPPDATA%\\OpenEditTools destination')
    parser.add_argument('--engine-dir', help='Override %LOCALAPPDATA%\\OpenEditEngine destination')
    parser.add_argument('--hf-endpoint', help='Hugging Face base URL (default HF_ENDPOINT or https://huggingface.co)')
    parser.add_argument('--torch-index-url', default=DEFAULT_TORCH_INDEX_URL, help='PyTorch wheel index URL for gpu-heavy tier')
    parser.add_argument('--dry-run', action='store_true', help='Detect existing tools/models and report install plan without downloading')
    parser.add_argument('--force', action='store_true', help='Re-download and overwrite already installed files')
    parser.add_argument('--keep-archives', action='store_true', help='Keep downloaded zip archives instead of removing them')
    parser.add_argument('--timeout', type=int, default=120, help='HTTP timeout in seconds (default 120)')
    args = parser.parse_args(argv)
    try:
        report = run_bootstrap(
            tier=args.tier,
            only=args.only,
            tools_dir=args.tools_dir,
            engine_dir=args.engine_dir,
            hf_endpoint=args.hf_endpoint,
            torch_index_url=args.torch_index_url,
            dry_run=args.dry_run,
            force=args.force,
            keep_archives=args.keep_archives,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
