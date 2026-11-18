"""Deterministic child process for workbench lifecycle tests; never touches a real installation."""
import hashlib
import json
import os
from pathlib import Path
import sys

from jianying_engine.workbench_requalification import FEATURES, source_identity


def main():
    candidate = Path(sys.argv[sys.argv.index('--candidate') + 1]).resolve()
    out = Path(sys.argv[sys.argv.index('--out') + 1])
    out.mkdir(parents=True, exist_ok=False)
    report_path = out / 'requalify-report.json'
    store_path = Path(os.environ['JIANYING_RUNTIME_ADMISSIONS'])
    source = source_identity()
    digest = hashlib.sha256((candidate / 'runtime.bin').read_bytes()).hexdigest()
    identities = {'preview': {'runtime.bin': digest}}
    features = {name: {'status': 'admitted' if name == 'preview' else 'refused',
                       'reason': 'synthetic fixture'} for name in FEATURES}
    report = {'candidate': str(candidate), 'reference': 'fixture-reference',
              'admission_state': 'eligible_not_committed', 'features': features,
              'admitted': ['preview'], 'identities': identities, 'store': str(store_path),
              'source_identity': source, 'preview_scope': 'still_frames_only',
              'sequence': {'status': 'unverified'}}
    report_path.write_text(json.dumps(report), encoding='utf-8')
    report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({'schema_version': 1, 'versions': {candidate.name: {
        'reference': 'fixture-reference', 'features': identities, 'report': str(report_path),
        'report_sha256': report_digest}}}), encoding='utf-8')
    (out.parent / 'fixture-started.txt').write_text('started', encoding='utf-8')
    print(json.dumps({'status': 'fixture', 'report': str(report_path)}))


if __name__ == '__main__':
    main()
