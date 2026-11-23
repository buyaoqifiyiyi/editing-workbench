"""Isolated CUDA PyTorch worker for all-in-one music structure analysis (allin1_infer)."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--out-json', required=True)
    parser.add_argument('--model', default='harmonix-all')
    parser.add_argument('--device', default='cuda')
    parser.add_argument('--cache-dir', default='')
    args = parser.parse_args(argv)

    if args.cache_dir:
        cache_p = Path(args.cache_dir).resolve()
        cache_p.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('HF_HOME', str(cache_p))
        os.environ.setdefault('TORCH_HOME', str(cache_p))

    import torch
    import allin1_infer

    dev = 'cuda' if (args.device == 'cuda' and torch.cuda.is_available()) else 'cpu'
    inp = Path(args.input).resolve()
    out_json = Path(args.out_json).resolve()

    with tempfile.TemporaryDirectory(prefix='oe-allin1-work-') as td:
        work = Path(td)
        res = allin1_infer.analyze(
            paths=str(inp),
            out_dir=None,
            model=args.model,
            device=dev,
            demix_dir=str(work / 'demix'),
            spec_dir=str(work / 'spec'),
            keep_byproducts=False,
            overwrite=True,
            multiprocess=False,
        )
    if isinstance(res, list):
        res = res[0]
    segs = [
        {'label': str(s.label), 'start': round(float(s.start), 4), 'end': round(float(s.end), 4)}
        for s in (getattr(res, 'segments', None) or [])
    ]
    payload = {
        'status': 'passed',
        'model': args.model,
        'device': dev,
        'bpm': round(float(getattr(res, 'bpm', 0.0) or 0.0), 3),
        'beats': [round(float(x), 4) for x in (getattr(res, 'beats', None) or [])],
        'downbeats': [round(float(x), 4) for x in (getattr(res, 'downbeats', None) or [])],
        'beat_positions': [int(x) for x in (getattr(res, 'beat_positions', None) or [])],
        'segments': segs,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
