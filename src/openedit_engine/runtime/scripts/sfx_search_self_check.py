"""Regression check for sfx_search (acoustic fallback + LAION CLAP ONNX zero-shot ranking)."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import wave

import numpy as np

import runtime_core as core
import runtime_paths
import sfx_search


def _run_main(arguments):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sfx_search.main([str(x) for x in arguments])
    return code, json.loads(buf.getvalue())


def _write_wav(path, y, sr=48000):
    pcm = (np.clip(y, -1.0, 1.0) * 32767.0).astype('<i2')
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _make_library(lib_dir):
    lib_dir.mkdir(parents=True, exist_ok=True)
    sr = 48000
    t = np.linspace(0.0, 1.2, int(sr * 1.2), endpoint=False, dtype=np.float64)
    ping = 0.7 * (np.sin(2.0 * np.pi * 2400.0 * t) + 0.6 * np.sin(2.0 * np.pi * 3720.0 * t)) * np.exp(-t * 5.0)
    sub = 0.85 * np.sin(2.0 * np.pi * (85.0 * np.exp(-t * 2.5) + 32.0) * t) * np.exp(-t * 2.0)
    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 1.0, size=t.shape)
    whoosh = 0.6 * np.diff(noise, prepend=0.0) * (np.sin(np.pi * t / 1.2) ** 2)
    _write_wav(lib_dir / 'cue_a.wav', ping)
    _write_wav(lib_dir / 'cue_b.wav', sub)
    _write_wav(lib_dir / 'cue_c.wav', whoosh)


def check(root):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    lib = root / 'sfx_lib'
    cache = root / 'cache'
    _make_library(lib)

    # 1. Deterministic acoustic descriptor fallback (cold + warm cache)
    ac_out = root / 'acoustic-1'
    c1, r1 = _run_main([
        '--library', lib, '--query', 'deep sub bass drop',
        '--out', ac_out, '--index-cache', cache, '--backend', 'acoustic', '--top-k', '3',
    ])
    assert c1 == 0 and r1['status'] == 'passed' and r1['cache_misses'] == 3, r1
    assert r1['matches'][0]['rel_path'] == 'cue_b.wav', r1['matches']
    c2, r2 = _run_main([
        '--library', lib, '--query', 'high metal ring',
        '--out', root / 'acoustic-2', '--index-cache', cache, '--backend', 'acoustic', '--top-k', '3',
    ])
    assert c2 == 0 and r2['cache_hits'] == 3 and r2['matches'][0]['rel_path'] == 'cue_a.wav', r2
    checks.append('acoustic fallback: cold/warm cache & ranking (sub->cue_b, high metal->cue_a)')

    # 2. Live LAION CLAP ONNX semantic retrieval when weights present
    if runtime_paths.discover_model('clap') is not None:
        cl1, rp1 = _run_main([
            '--library', lib, '--query', 'deep bass boom explosion sub drop',
            '--out', root / 'clap-sub', '--index-cache', cache, '--backend', 'clap', '--gpu',
        ])
        assert cl1 == 0 and rp1['backend'] == 'clap_htsat_onnx' and rp1['cache_misses'] == 3, rp1
        assert rp1['matches'][0]['rel_path'] == 'cue_b.wav', rp1['matches']

        cl2, rp2 = _run_main([
            '--library', lib, '--query', 'high pitched metallic bell chime ring',
            '--out', root / 'clap-ping', '--index-cache', cache, '--backend', 'clap', '--gpu',
        ])
        assert cl2 == 0 and rp2['cache_hits'] == 3 and rp2['matches'][0]['rel_path'] == 'cue_a.wav', rp2['matches']

        cl3, rp3 = _run_main([
            '--library', lib, '--query', 'wind whoosh swish air noise',
            '--out', root / 'clap-whoosh', '--index-cache', cache, '--backend', 'clap', '--gpu',
        ])
        assert cl3 == 0 and rp3['matches'][0]['rel_path'] == 'cue_c.wav', rp3['matches']
        checks.append(f'CLAP ONNX ({rp1["provider"]}): zero-shot ranked all 3 cues accurately with warm cache')
    else:
        checks.append('CLAP ONNX skipped: weights not installed')

    # 3. Refusal on empty/missing library
    empty = root / 'empty_lib'
    empty.mkdir()
    cf, rf = _run_main(['--library', empty, '--query', 'whoosh', '--out', root / 'fail-out'])
    assert cf == 1 and rf['status'] == 'failed'
    checks.append('empty library refused cleanly')

    res = {'status': 'passed', 'checks': checks, 'checks_count': len(checks)}
    core.write_json(root / 'sfx-search-check.json', res)
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    core.emit(check(args.out.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
