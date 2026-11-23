"""Offline video shot indexer & natural-language semantic shot retrieval (Google SigLIP 2 ONNX + histogram fallback)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

import runtime_common
import runtime_core as core
import runtime_paths

_VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mxf', '.ts'}


def _cache_root(explicit=None):
    base = explicit or os.environ.get('OPENEDIT_SHOT_CACHE_DIR') or (
        Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OpenEditEngine' / 'shot-cache'
    )
    return runtime_paths.external_directory(base, 'Shot search index cache')


def _find_siglip2_files(bundle_dir):
    bundle = Path(bundle_dir)
    vis = sorted(bundle.glob('onnx/vision_model*.onnx')) or sorted(bundle.glob('vision_model*.onnx'))
    txt = sorted(bundle.glob('onnx/text_model*.onnx')) or sorted(bundle.glob('text_model*.onnx'))
    tok = bundle / 'tokenizer.json'
    if not vis or not txt or not tok.is_file():
        raise ValueError(f'SigLIP 2 bundle incomplete in {bundle}')
    v_fp16 = [p for p in vis if 'fp16' in p.name.lower()]
    t_fp16 = [p for p in txt if 'fp16' in p.name.lower()]
    return (v_fp16 or vis)[0], (t_fp16 or txt)[0], tok


def _detect_shots(video_path, *, detector='auto', stride=1.0):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f'Failed to open video: {video_path}')
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if fps <= 0:
            fps = 30.0
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = max(0.1, n_frames / fps)
    finally:
        cap.release()

    shots = []
    if detector in ('auto', 'pyscenedetect'):
        try:
            from scenedetect import ContentDetector, SceneManager, open_video

            vid = open_video(str(video_path))
            try:
                mgr = SceneManager()
                mgr.add_detector(ContentDetector(threshold=26.0, min_scene_len=max(1, int(round(0.35 * fps)))))
                mgr.detect_scenes(vid, show_progress=False)
                scene_list = mgr.get_scene_list()
                for st_tc, en_tc in scene_list:
                    a = round(float(st_tc.get_seconds()), 4)
                    b = round(float(en_tc.get_seconds()), 4)
                    if b > a + 0.05:
                        shots.append((a, b))
            finally:
                cap_inner = getattr(vid, '_cap', None)
                if cap_inner is not None and hasattr(cap_inner, 'release'):
                    cap_inner.release()
                del vid
        except Exception:
            shots = []

    if not shots:
        shots = [(0.0, round(dur, 4))]

    # Split overly long unbroken intervals by --stride so single-cut clips are searchable too.
    max_chunk = max(stride * 2.5, 2.5) if detector == 'auto' else stride
    final_shots = []
    for a, b in shots:
        span = b - a
        if (detector == 'stride' and span > stride * 1.15) or (detector == 'auto' and span > max_chunk):
            cur = a
            while cur < b - 0.05:
                nxt = min(b, cur + stride)
                final_shots.append((round(cur, 4), round(nxt, 4)))
                cur = nxt
        else:
            final_shots.append((a, b))
    return final_shots, dur, fps


def _extract_midframe(cap, mid_s, fps, n_frames):
    idx = max(0, min(max(0, n_frames - 1), int(round(mid_s * fps))))
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, bgr = cap.read()
    if not ok:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, bgr = cap.read()
    if not ok:
        raise ValueError('Failed to decode midpoint keyframe')
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _siglip2_image_embedding(rgb, vision_sess):
    small = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA).astype(np.float32)
    norm = (small / 255.0 - 0.5) / 0.5
    inp = np.transpose(norm, (2, 0, 1))[None, ...].astype(np.float32)
    out_names = [o.name for o in vision_sess.get_outputs()]
    out_idx = out_names.index('pooler_output') if 'pooler_output' in out_names else -1
    emb = vision_sess.run(None, {'pixel_values': inp})[out_idx][0].astype(np.float32)
    return emb / (float(np.linalg.norm(emb)) + 1e-12)


def _histogram_stats(rgb):
    f = rgb.astype(np.float32) / 255.0
    r_m, g_m, b_m = [float(f[..., c].mean()) for c in range(3)]
    luma = 0.2126 * r_m + 0.7152 * g_m + 0.0722 * b_m
    return {
        'r_dom': round(r_m - max(g_m, b_m), 4),
        'g_dom': round(g_m - max(r_m, b_m), 4),
        'b_dom': round(b_m - max(r_m, g_m), 4),
        'luma': round(luma, 4),
    }


def _histogram_score(query, stats):
    q = query.lower()
    score = 0.0
    if any(w in q for w in ('red', 'crimson', 'fire', 'flame', 'blood', '红', '火', '炎')):
        score += 1.0 * stats['r_dom']
    if any(w in q for w in ('blue', 'ocean', 'sea', 'water', 'sky', 'cool', '蓝', '海', '水', '夜')):
        score += 1.0 * stats['b_dom']
    if any(w in q for w in ('green', 'forest', 'grass', 'leaf', 'emerald', '绿', '森林', '草')):
        score += 1.0 * stats['g_dom']
    if any(w in q for w in ('bright', 'white', 'flash', 'sun', '白', '亮', '闪')):
        score += 0.6 * stats['luma']
    if any(w in q for w in ('dark', 'black', 'shadow', 'night', '暗', '黑', '影')):
        score += 0.6 * (1.0 - stats['luma'])
    return round(float(score), 4)


def run_shot_search(args):
    src = Path(os.path.expanduser(os.path.expandvars(args.input))).resolve()
    if not src.exists():
        raise ValueError(f'Input video or directory does not exist: {src}')
    query = runtime_common.check_text(args.query, 'query', 500)
    if not query.strip():
        raise ValueError('query cannot be empty')
    top_k = int(args.top_k)
    if not 1 <= top_k <= 50:
        raise ValueError(f'--top-k must be in [1, 50], got {top_k}')
    stride = core.number(args.stride, 'stride', 0.2, 30.0)

    out_cand = Path(args.out).resolve()
    if out_cand.exists() or out_cand.is_symlink():
        raise ValueError(f'Output directory already exists; choose a new directory: {args.out}')

    if src.is_file():
        videos = [src]
    elif src.is_dir():
        videos = sorted(p for p in src.rglob('*') if p.is_file() and p.suffix.lower() in _VIDEO_EXT)[:500]
        if not videos:
            raise ValueError(f'No video files found in directory: {src}')
    else:
        raise ValueError(f'Unsupported input path: {src}')

    siglip_dir = runtime_paths.discover_model('siglip2')
    backend = args.backend
    if backend == 'auto':
        backend = 'siglip2' if siglip_dir is not None else 'histogram'
    elif backend == 'siglip2' and siglip_dir is None:
        raise ValueError('SigLIP 2 ONNX bundle not found (expect models/siglip2 or pass --backend histogram)')

    cache_dir = _cache_root(args.index_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lib_key = hashlib.sha256(f'{src}|{backend}|{args.detector}|{stride:.4f}'.encode('utf-8')).hexdigest()[:16]
    meta_path = cache_dir / f'{lib_key}.json'
    vecs_path = cache_dir / f'{lib_key}.npz'

    cached_videos = {}
    cached_vecs = None
    if meta_path.is_file():
        try:
            d = json.loads(meta_path.read_text(encoding='utf-8'))
            if isinstance(d, dict):
                cached_videos = d.get('videos', {}) or {}
        except Exception:
            cached_videos = {}
    if backend == 'siglip2' and vecs_path.is_file():
        try:
            with np.load(vecs_path) as data:
                cached_vecs = np.asarray(data['embeddings'], dtype=np.float32)
        except Exception:
            cached_vecs = None
            cached_videos = {}

    vis_sess = None
    txt_sess = None
    tok = None
    provider = 'CPU'
    if backend == 'siglip2':
        import onnxruntime as ort
        from tokenizers import Tokenizer

        vis_onnx, txt_onnx, tok_path = _find_siglip2_files(siglip_dir)
        if args.gpu:
            runtime_paths.prepare_cuda_dlls()
        providers = runtime_paths.resolve_ort_providers(
            ort.get_available_providers(), device='auto' if args.gpu else 'cpu', gpu=bool(args.gpu),
        )
        vis_sess = ort.InferenceSession(str(vis_onnx), providers=providers)
        txt_sess = ort.InferenceSession(str(txt_onnx), providers=providers)
        provider = vis_sess.get_providers()[0]
        tok = Tokenizer.from_file(str(tok_path))

    updated_videos = {}
    all_shots = []
    vecs_list = []
    cache_hits = 0
    cache_misses = 0

    for vid in videos:
        rel = vid.name if src.is_file() else vid.relative_to(src).as_posix()
        st = vid.stat()
        prev = cached_videos.get(rel)
        reused = False
        if (isinstance(prev, dict) and prev.get('size') == st.st_size
                and prev.get('mtime_ns') == st.st_mtime_ns):
            if backend == 'histogram':
                updated_videos[rel] = prev
                all_shots.extend(prev.get('shots', []))
                reused = True
            elif cached_vecs is not None:
                shot_copies = []
                ok_vecs = True
                for sh in prev.get('shots', []):
                    vidx = sh.get('vec_idx')
                    if not (isinstance(vidx, int) and 0 <= vidx < len(cached_vecs)):
                        ok_vecs = False
                        break
                    sh_c = dict(sh)
                    sh_c['vec_idx'] = len(vecs_list)
                    vecs_list.append(cached_vecs[vidx])
                    shot_copies.append(sh_c)
                if ok_vecs:
                    updated_videos[rel] = {**prev, 'shots': shot_copies}
                    all_shots.extend(shot_copies)
                    reused = True
        if reused:
            cache_hits += 1
            continue

        cache_misses += 1
        ranges, _, fps = _detect_shots(vid, detector=args.detector, stride=stride)
        cap = cv2.VideoCapture(str(vid))
        if not cap.isOpened():
            raise ValueError(f'Failed to open video for keyframes: {vid}')
        try:
            n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            vid_shots = []
            for s_idx, (a, b) in enumerate(ranges):
                mid_s = round(0.5 * (a + b), 4)
                rgb = _extract_midframe(cap, mid_s, fps, n_frames)
                sh_entry = {
                    'video': str(vid),
                    'rel_path': rel,
                    'shot_index': s_idx,
                    'start': a,
                    'end': b,
                    'mid_time': mid_s,
                    **_histogram_stats(rgb),
                }
                if backend == 'siglip2':
                    emb = _siglip2_image_embedding(rgb, vis_sess)
                    sh_entry['vec_idx'] = len(vecs_list)
                    vecs_list.append(emb)
                vid_shots.append(sh_entry)
        finally:
            cap.release()
        updated_videos[rel] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'shots': vid_shots}
        all_shots.extend(vid_shots)

    meta_path.write_text(
        json.dumps({'source': str(src), 'backend': backend, 'videos': updated_videos}, ensure_ascii=False, indent=1),
        encoding='utf-8',
    )
    mat = None
    if backend == 'siglip2' and vecs_list:
        mat = np.vstack(vecs_list).astype(np.float32)
        np.savez(vecs_path, embeddings=mat)

    ranked = []
    if backend == 'siglip2':
        enc = tok.encode(query)
        raw_ids = list(enc.ids)
        if 1 not in raw_ids:
            raw_ids = raw_ids[:63] + [1]
        if len(raw_ids) < 64:
            raw_ids = raw_ids + [0] * (64 - len(raw_ids))
        else:
            raw_ids = raw_ids[:64]
        ids_arr = np.array([raw_ids], dtype=np.int64)
        out_names = [o.name for o in txt_sess.get_outputs()]
        out_idx = out_names.index('pooler_output') if 'pooler_output' in out_names else -1
        q_emb = txt_sess.run(None, {'input_ids': ids_arr})[out_idx][0].astype(np.float32)
        q_emb = q_emb / (float(np.linalg.norm(q_emb)) + 1e-12)
        for sh in all_shots:
            score = round(float(q_emb @ mat[sh['vec_idx']]), 4)
            ranked.append((score, sh))
    else:
        for sh in all_shots:
            ranked.append((_histogram_score(query, sh), sh))

    ranked.sort(key=lambda pair: (-pair[0], pair[1]['rel_path'], pair[1]['start']))
    out_dir = core.new_directory(out_cand)
    thumbs_dir = out_dir / 'thumbs'
    thumbs_dir.mkdir(exist_ok=True)

    matches = []
    with_motion = bool(getattr(args, 'with_motion', False))
    motion_mod = None
    if with_motion:
        import motion_flow as motion_mod
    # Extract clean thumbnails for top-K matched shots only
    for rank_idx, (score, sh) in enumerate(ranked[:top_k], start=1):
        thumb_path = thumbs_dir / f'rank_{rank_idx:02d}_shot_{sh["shot_index"]:03d}.jpg'
        cap = cv2.VideoCapture(sh['video'])
        try:
            fps_v = float(cap.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
            n_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            rgb = _extract_midframe(cap, sh['mid_time'], fps_v, n_f)
            Image.fromarray(rgb, mode='RGB').save(thumb_path, quality=90)
        finally:
            cap.release()
        entry = {
            'rank': rank_idx,
            'video': sh['video'],
            'rel_path': sh['rel_path'],
            'shot_index': sh['shot_index'],
            'start': sh['start'],
            'end': sh['end'],
            'mid_time': sh['mid_time'],
            'score': score,
            'thumbnail': str(thumb_path),
        }
        if motion_mod is not None:
            flow = motion_mod.analyze_video(
                sh['video'], sample_fps=12.0, flow_backend='farneback',
                start_s=sh['start'], end_s=sh['end'],
            )
            entry['motion'] = {
                'entry_direction': flow['entry_vector']['direction'],
                'exit_direction': flow['exit_vector']['direction'],
                'zoom': flow['overall']['zoom'],
                'peak_speed': flow['peak_speed'],
                'action_peaks': flow['action_peaks'],
            }
        matches.append(entry)

    report = {
        'status': 'passed',
        'operation': 'shot-search',
        'backend': 'siglip2_onnx' if backend == 'siglip2' else 'color_histogram',
        'provider': provider,
        'query': query,
        'with_motion': with_motion,
        'indexed_videos': len(updated_videos),
        'indexed_shots': len(all_shots),
        'cache_hits': cache_hits,
        'cache_misses': cache_misses,
        'matches': matches,
    }
    core.write_json(out_dir / 'shot-search-report.json', report)
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='Local video file or folder of videos')
    parser.add_argument('--query', required=True, help='Natural-language shot search query (CN or EN)')
    parser.add_argument('--out', required=True, help='New output directory for shot-search-report.json and thumbnails')
    parser.add_argument('--top-k', type=int, default=8, help='Number of top matching shots in [1, 50] (default 8)')
    parser.add_argument('--stride', type=float, default=1.0, help='Uniform sampling stride seconds when no cuts exist (default 1.0)')
    parser.add_argument('--detector', choices=('auto', 'pyscenedetect', 'stride'), default='auto')
    parser.add_argument('--index-cache', default=None, help='External directory to cache shot embeddings')
    parser.add_argument('--backend', choices=('auto', 'siglip2', 'histogram'), default='auto')
    parser.add_argument('--gpu', action='store_true', help='Enable CUDA GPU inference when available')
    parser.add_argument('--with-motion', action='store_true', help='Attach optical-flow entry/exit motion & zoom trends to top-K matched shots')
    args = parser.parse_args(argv)
    try:
        report = run_shot_search(args)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
