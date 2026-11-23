"""Offline local SFX library indexer & text-to-audio semantic search (LAION CLAP ONNX + acoustic fallback)."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

import numpy as np

import runtime_common
import runtime_core as core
import runtime_paths

_AUDIO_EXT = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac'}
_CLAP_SR = 48000
_CLAP_SAMPLES = 480000  # 10s @ 48kHz


def _cache_root(explicit=None):
    base = explicit or os.environ.get('OPENEDIT_SFX_CACHE_DIR') or (
        Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditEngine' / 'sfx-cache'
    )
    return runtime_paths.external_directory(base, 'SFX index cache')


def _find_clap_files(bundle_dir):
    bundle = Path(bundle_dir)
    audio_cands = sorted(bundle.glob('onnx/audio_model*.onnx')) or sorted(bundle.glob('audio_model*.onnx'))
    text_cands = sorted(bundle.glob('onnx/text_model*.onnx')) or sorted(bundle.glob('text_model*.onnx'))
    tok = bundle / 'tokenizer.json'
    if not audio_cands or not text_cands or not tok.is_file():
        raise ValueError(f'CLAP bundle incomplete in {bundle}')
    # Prefer fp16 when available
    a_fp16 = [p for p in audio_cands if 'fp16' in p.name.lower()]
    t_fp16 = [p for p in text_cands if 'fp16' in p.name.lower()]
    return (a_fp16 or audio_cands)[0], (t_fp16 or text_cands)[0], tok


def _decode_audio_48k(path, tmp_dir):
    wav_path = Path(tmp_dir) / 'decoded_48k.wav'
    core.run([
        core.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-protocol_whitelist', 'file,pipe', '-i', str(path),
        '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-ac', '1', '-ar', str(_CLAP_SR), '-c:a', 'pcm_s16le', str(wav_path),
    ], timeout=120)
    with wave.open(str(wav_path), 'rb') as handle:
        n = handle.getnframes()
        raw = np.frombuffer(handle.readframes(n), dtype='<i2')
    wav_path.unlink(missing_ok=True)
    y = raw.astype(np.float32) / 32768.0
    if y.size == 0:
        raise ValueError(f'Audio decoded to 0 samples: {path}')
    dur = float(y.size) / _CLAP_SR
    peak = float(np.max(np.abs(y)))
    peak_dbfs = round(20.0 * math.log10(max(peak, 1e-6)), 2)
    return y, dur, peak_dbfs


def _acoustic_stats(y):
    n = min(len(y), _CLAP_SR * 10)
    seg = y[:n]
    spec = np.abs(np.fft.rfft(seg)) + 1e-9
    freqs = np.fft.rfftfreq(len(seg), d=1.0 / _CLAP_SR)
    power = spec * spec
    total_p = float(np.sum(power)) + 1e-12
    centroid = float(np.sum(freqs * power) / total_p)
    low_ratio = float(np.sum(power[freqs < 180.0]) / total_p)
    high_ratio = float(np.sum(power[freqs >= 1500.0]) / total_p)
    geo_mean = float(np.exp(np.mean(np.log(spec))))
    flatness = min(1.0, geo_mean / (float(np.mean(spec)) + 1e-12))
    return {
        'centroid_hz': round(centroid, 1),
        'low_ratio': round(low_ratio, 4),
        'high_ratio': round(high_ratio, 4),
        'flatness': round(flatness, 4),
    }


def _clap_audio_embedding(y, audio_sess):
    import librosa

    if y.size > _CLAP_SAMPLES:
        clip = y[:_CLAP_SAMPLES]
    elif y.size < _CLAP_SAMPLES:
        n_rep = (_CLAP_SAMPLES + y.size - 1) // y.size
        clip = np.tile(y, n_rep)[:_CLAP_SAMPLES]
    else:
        clip = y
    mel = librosa.feature.melspectrogram(
        y=clip, sr=_CLAP_SR, n_fft=1024, hop_length=480, win_length=1024,
        n_mels=64, fmin=50.0, fmax=14000.0, htk=False, norm='slaney', power=2.0,
    )
    db = librosa.power_to_db(mel, ref=1.0, top_db=None)
    feats = db.T[None, None, :, :].astype(np.float32)  # (1, 1, 1001, 64)
    emb = audio_sess.run(None, {'input_features': feats})[0][0].astype(np.float32)
    return emb / (float(np.linalg.norm(emb)) + 1e-12)


def _acoustic_score(query, rel_path, stats):
    q = query.lower()
    tokens = {tok for tok in ''.join(ch if ch.isalnum() else ' ' for ch in q).split() if tok}
    score = 0.0
    stem_l = Path(rel_path).stem.lower()
    for tok in tokens:
        if len(tok) >= 2 and tok in stem_l:
            score += 0.25
    if any(w in q for w in ('sub', 'bass', 'boom', 'drop', 'deep', 'low', 'thunder', 'kick')):
        score += 0.65 * stats['low_ratio'] - 0.25 * stats['high_ratio']
    if any(w in q for w in ('metal', 'sword', 'blade', 'bell', 'ping', 'chime', 'ring', 'high', 'sharp', 'shimmer')):
        score += 0.65 * stats['high_ratio'] + 0.25 * (1.0 - stats['flatness']) - 0.45 * stats['flatness']
    if any(w in q for w in ('whoosh', 'wind', 'air', 'swish', 'noise', 'sweep')):
        score += 0.70 * stats['flatness']
    return round(float(score), 4)


def run_sfx_search(args):
    lib = Path(os.path.expanduser(os.path.expandvars(args.library))).resolve()
    if not lib.is_dir():
        raise ValueError(f'SFX library directory does not exist: {lib}')
    query = runtime_common.check_text(args.query, 'query', 500)
    if not query.strip():
        raise ValueError('query cannot be empty')
    top_k = int(args.top_k)
    if not 1 <= top_k <= 50:
        raise ValueError(f'--top-k must be in [1, 50], got {top_k}')
    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    audio_files = sorted(
        p for p in lib.rglob('*')
        if p.is_file() and p.suffix.lower() in _AUDIO_EXT
    )[:5000]
    if not audio_files:
        raise ValueError(f'No supported audio files found under SFX library: {lib}')

    backend = args.backend
    clap_dir = runtime_paths.discover_model('clap')
    if backend == 'auto':
        backend = 'clap' if clap_dir is not None else 'acoustic'
    elif backend == 'clap' and clap_dir is None:
        raise ValueError('CLAP model bundle not found (expect models/clap or pass --backend acoustic)')

    cache_dir = _cache_root(args.index_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lib_key = hashlib.sha256(str(lib).encode('utf-8')).hexdigest()[:16]
    meta_path = cache_dir / f'{lib_key}_{backend}.json'
    vecs_path = cache_dir / f'{lib_key}_{backend}.npz'

    cached_entries = {}
    cached_vecs = None
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding='utf-8'))
            if isinstance(cached, dict):
                cached_entries = cached.get('items', {}) or {}
        except Exception:
            cached_entries = {}
    if backend == 'clap' and vecs_path.is_file():
        try:
            with np.load(vecs_path) as data:
                cached_vecs = np.asarray(data['embeddings'], dtype=np.float32)
        except Exception:
            cached_vecs = None
            cached_entries = {}

    audio_sess = None
    text_sess = None
    tok = None
    active_provider = 'CPU'
    if backend == 'clap':
        import onnxruntime as ort
        from tokenizers import Tokenizer

        audio_onnx, text_onnx, tok_path = _find_clap_files(clap_dir)
        if args.gpu:
            runtime_paths.prepare_cuda_dlls()
        providers = runtime_paths.resolve_ort_providers(
            ort.get_available_providers(), device='auto' if args.gpu else 'cpu', gpu=bool(args.gpu),
        )
        audio_sess = ort.InferenceSession(str(audio_onnx), providers=providers)
        text_sess = ort.InferenceSession(str(text_onnx), providers=providers)
        active_provider = audio_sess.get_providers()[0]
        tok = Tokenizer.from_file(str(tok_path))

    updated_items = {}
    embeddings_list = []
    cache_hits = 0
    cache_misses = 0

    with tempfile.TemporaryDirectory(prefix='oe-sfx-search-') as td:
        for path in audio_files:
            rel = path.relative_to(lib).as_posix()
            st = path.stat()
            prev = cached_entries.get(rel)
            reused = False
            if (isinstance(prev, dict) and prev.get('size') == st.st_size
                    and prev.get('mtime_ns') == st.st_mtime_ns):
                if backend == 'acoustic':
                    updated_items[rel] = dict(prev)
                    reused = True
                elif cached_vecs is not None:
                    vidx = prev.get('vec_idx')
                    if isinstance(vidx, int) and 0 <= vidx < len(cached_vecs):
                        item_copy = dict(prev)
                        item_copy['vec_idx'] = len(embeddings_list)
                        embeddings_list.append(cached_vecs[vidx])
                        updated_items[rel] = item_copy
                        reused = True
            if reused:
                cache_hits += 1
                continue

            cache_misses += 1
            y, dur, peak_dbfs = _decode_audio_48k(path, td)
            stats = _acoustic_stats(y)
            entry = {
                'path': str(path),
                'rel_path': rel,
                'size': st.st_size,
                'mtime_ns': st.st_mtime_ns,
                'duration': round(dur, 4),
                'sample_rate': _CLAP_SR,
                'peak_dbfs': peak_dbfs,
                **stats,
            }
            if backend == 'clap':
                emb = _clap_audio_embedding(y, audio_sess)
                entry['vec_idx'] = len(embeddings_list)
                embeddings_list.append(emb)
            updated_items[rel] = entry

    meta_path.write_text(
        json.dumps({'library': str(lib), 'backend': backend, 'items': updated_items}, ensure_ascii=False, indent=1),
        encoding='utf-8',
    )
    if backend == 'clap' and embeddings_list:
        mat = np.vstack(embeddings_list).astype(np.float32)
        np.savez(vecs_path, embeddings=mat)
    else:
        mat = None

    ranked = []
    if backend == 'clap':
        ids = np.array([tok.encode(query).ids], dtype=np.int64)
        q_emb = text_sess.run(None, {'input_ids': ids})[0][0].astype(np.float32)
        q_emb = q_emb / (float(np.linalg.norm(q_emb)) + 1e-12)
        for item in updated_items.values():
            v = mat[item['vec_idx']]
            score = round(float(q_emb @ v), 4)
            ranked.append((score, item))
    else:
        for rel, item in updated_items.items():
            ranked.append((_acoustic_score(query, rel, item), item))

    ranked.sort(key=lambda pair: (-pair[0], pair[1]['rel_path']))
    matches = []
    for rank_idx, (score, item) in enumerate(ranked[:top_k], start=1):
        matches.append({
            'rank': rank_idx,
            'path': item['path'],
            'rel_path': item['rel_path'],
            'score': score,
            'duration': item['duration'],
            'sample_rate': item['sample_rate'],
            'peak_dbfs': item['peak_dbfs'],
            'centroid_hz': item['centroid_hz'],
        })

    out_dir = core.new_directory(out_cand)
    report = {
        'status': 'passed',
        'operation': 'sfx-search',
        'backend': 'clap_htsat_onnx' if backend == 'clap' else 'acoustic_descriptor',
        'provider': active_provider,
        'library': str(lib),
        'query': query,
        'indexed_files': len(updated_items),
        'cache_hits': cache_hits,
        'cache_misses': cache_misses,
        'matches': matches,
    }
    core.write_json(out_dir / 'sfx-search-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--library', required=True, help='Local directory of SFX audio files')
    parser.add_argument('--query', required=True, help='Text query describing desired sound effect')
    parser.add_argument('--out', required=True, help='New output directory for sfx-search-report.json')
    parser.add_argument('--top-k', type=int, default=5, help='Number of top matches in [1, 50] (default 5)')
    parser.add_argument('--index-cache', default=None, help='External directory to cache SFX audio embeddings')
    parser.add_argument('--backend', choices=('auto', 'clap', 'acoustic'), default='auto')
    parser.add_argument('--gpu', action='store_true', help='Enable CUDA GPU inference when available')
    args = parser.parse_args(argv)
    try:
        report = run_sfx_search(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
