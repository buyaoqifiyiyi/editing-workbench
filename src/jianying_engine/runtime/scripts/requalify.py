"""Requalify version-gated native subsets on a newly installed Jianying build.

Runs every version-gated self-check twice, pinned once to the measured reference build and once to the
candidate, records each draft those checks build, renders the same drafts natively on both builds at the
same positions and compares the frames. A subset is admitted for the candidate only when its checks pass
on both builds and every compared frame matches within TOLERANCE; its runtime file hashes then go to the
external admission store (runtime_admission). Effect resource trees are not re-pinned, so a changed
resource keeps its subset refused. Never opens the Jianying UI and never publishes to the live draft root.
Admission covers still frames only. The sequence self-check uses a mock worker and cannot qualify a
native sequence. Reports are immutable eligibility evidence; only a successful admission transaction
and the subsequent identity/gate checks produce a successful return value.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
try:
    import file_hash
    import runtime_common
except ModuleNotFoundError:
    from jianying_engine.runtime.scripts import file_hash, runtime_common

SCRIPTS = Path(__file__).resolve().parent
TOLERANCE = 2  # max per-channel difference out of 255, the bound the admitted subsets already use
# All captured positive cases are required; no sampling may widen admission.
GATED = {'native_preview': (), 'preview_sequence': (), 'alpha_video': (),
         'chroma': ('chroma',), 'mix_mode': ('mix',), 'mask_feather': ('mask_feather',),
         'text_alpha': ('text_alpha',), 'text_highlight': ('text_highlight',), 'text_color_fade': ('text_highlight', 'text_alpha'),
         'corner_pin': ('corner',), 'green_lightness': ('green_lightness',), 'hsl_channels': ('green_lightness',)}
PREVIEW_CHECKS = ('native_preview', 'alpha_video')
# These checks do not build positive native drafts. Sequence remains unqualified.
STRUCTURAL_CHECKS = ('native_preview', 'preview_sequence')


sha = file_hash.sha256


def source_snapshot():
    """Bind the review to the exact checked-in runtime scripts and contracts used to run it."""
    return runtime_common.runtime_source_identity(SCRIPTS.parent)


def verify_source_snapshot(expected):
    if source_snapshot() != expected:
        raise ValueError('Review scripts or contracts changed during qualification')


def capture(check, root, captures, install, candidate_identities=None, identity_sha256=None):
    """Run one registered self-check in this process and record every draft it builds."""
    original_path = sys.path[:]
    sys.path.insert(0, str(SCRIPTS))
    import editor
    import run_checks
    script, kind = run_checks.CHECKS[check][:2]
    root.mkdir(parents=True, exist_ok=False)
    options = SimpleNamespace(install=install)
    argv = run_checks.arguments(kind, root / 'out', run_checks.Media(root / 'media') if kind not in ('out', 'install', 'out_install') else None, options)
    built = []
    original = editor.build_draft

    def recording(args):
        result = original(args)
        draft = Path(result['draft'])
        plan = draft / 'edit-plan.json'
        built.append({'draft': str(draft), 'content': sha(draft / 'draft_content.json'),
                      'plan': sha(plan) if plan.is_file() else None})
        captures.write_text(json.dumps(built, ensure_ascii=False, indent=1), encoding='utf-8')
        return result
    original_argv = sys.argv
    try:
        with pinned(install, load_candidate(candidate_identities, identity_sha256, install)):
            editor.build_draft = recording
            sys.argv = [str(SCRIPTS / script), *argv]
            try:
                runpy.run_path(str(SCRIPTS / script), run_name='__main__')
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        editor.build_draft = original
        sys.argv = original_argv
        sys.path[:] = original_path
        captures.write_text(json.dumps(built, ensure_ascii=False, indent=1), encoding='utf-8')
    return code


def load_candidate(path, digest, install):
    if path is None:
        if digest is not None:
            raise ValueError('Candidate identity digest needs an explicit manifest')
        return None
    raw = Path(path).read_bytes()
    if not digest or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Candidate identity manifest changed or lacks a digest')
    manifest = json.loads(raw)
    if Path(manifest['install']).resolve() != Path(install).resolve():
        raise ValueError('Candidate identity manifest names a different install')
    return manifest['identities']


@contextlib.contextmanager
def pinned(install, identities=None):
    import runtime_admission
    keys = ('JIANYING_INSTALL', 'JIANYING_REQUALIFY_CANDIDATE')
    saved = {k: os.environ.get(k) for k in keys}
    os.environ['JIANYING_INSTALL'] = str(install)
    os.environ.pop('JIANYING_REQUALIFY_CANDIDATE', None)
    try:
        with (runtime_admission.candidate_context(install, identities)
              if identities is not None else contextlib.nullcontext()):
            yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def positions(draft):
    import editor
    import native_preview
    _, duration, _ = native_preview.draft_clock(editor.read_json(draft / 'draft_content.json'))
    return [round(duration * f, 3) for f in (0.25, 0.5, 0.75) if 0 <= round(duration * f, 3) < duration]


def render(install, identities, draft, out, times):
    import native_preview
    with pinned(install, identities):
        result = native_preview.preview(Path(install), draft / 'draft_content.json', out, times)
    if result.get('status') != 'passed' or not times or len(result.get('frames', [])) != len(times):
        raise ValueError('native preview did not pass')
    return [Path(f['path']) for f in result['frames']]


def frame_diff(a, b, area=False):
    """Largest per-channel difference out of 255 ('size' if the frames differ in size); with area, also the
    share of pixels differing by more than TOLERANCE and the box around them."""
    from PIL import Image, ImageChops
    with Image.open(a) as x, Image.open(b) as y:
        x, y = x.convert('RGBA'), y.convert('RGBA')
        if x.size != y.size:
            return ('size', 1.0, None) if area else 'size'
        difference = ImageChops.difference(x, y)
        peak = max(high for _, high in difference.getextrema())
        if not area:
            return peak
        channels = difference.split()
        mask = channels[0]
        for channel in channels[1:]:
            mask = ImageChops.lighter(mask, channel)
        mask = mask.point(lambda v: 255 if v > TOLERANCE else 0)
        share = mask.histogram()[255] / (x.width * x.height)
        return peak, round(share, 4), mask.getbbox()


def unchanged(entry):
    draft = Path(entry['draft'])
    plan = draft / 'edit-plan.json'
    return ((draft / 'draft_content.json').is_file() and sha(draft / 'draft_content.json') == entry['content'] and
            (entry['plan'] is None or (plan.is_file() and sha(plan) == entry['plan'])))


def snapshot(install):
    import runtime_admission
    return {feature: runtime_admission.candidate_identity(feature, install)
            for feature in runtime_admission.FEATURES}


def verify_snapshot(install, frozen):
    if snapshot(install) != frozen:
        raise ValueError('Runtime identity inventory changed: ' + str(install))
    for files, _ in frozen.values():
        for rel, digest in (files or {}).items():
            if sha(install / rel) != digest:
                raise ValueError('Runtime identity changed: ' + str(install / rel))


def captured_cases(root):
    path = root / 'captures.json'
    entries = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(entries, list):
        raise ValueError('captures must be a list')
    cases = {}
    base = (root / 'out').resolve()
    for entry in entries:
        draft = Path(entry['draft']).resolve()
        key = draft.relative_to(base).as_posix()
        if key in cases:
            raise ValueError('duplicate captured draft: ' + key)
        cases[key] = entry
    return cases


def write_report(path, report):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def review_lock():
    """Serialize manual and workbench reviews that share an admission store."""
    import runtime_admission
    lock_path = runtime_admission.store_path().resolve().parent / 'requalification-execution.lock'
    with runtime_common.file_lock(
        lock_path,
        blocking=False,
        pad_empty=False,
        busy_message='Another Jianying version review is already running',
    ):
        yield


def requalify(candidate, reference, out, timeout=1500, accept=()):
    with review_lock():
        return _requalify(candidate, reference, out, timeout=timeout, accept=accept)


def _requalify(candidate, reference, out, timeout=1500, accept=()):
    import runtime_admission
    if accept:
        raise ValueError('--accept is disabled: acceptance must bind a reviewed, immutable report')
    candidate, reference, out = Path(candidate).resolve(), Path(reference).resolve(), Path(out).resolve()
    if not candidate.is_dir() or not reference.is_dir() or candidate == reference:
        raise ValueError('Need two different existing Jianying install directories')
    with pinned(reference):
        for feature in runtime_admission.FEATURES:
            if reference.name != runtime_admission.bundled(feature)[0] or not runtime_admission.matches(feature, reference):
                raise ValueError(f'Reference {reference.name} does not match the measured {feature} contract')
    frozen_reference, frozen_candidate = snapshot(reference), snapshot(candidate)
    frozen_source = source_snapshot()
    verify_snapshot(reference, frozen_reference)
    verify_snapshot(candidate, frozen_candidate)
    out.mkdir(parents=True, exist_ok=False)
    identities = {f: files for f, (files, _) in frozen_candidate.items() if files}
    manifest = out / 'candidate-identities.json'
    manifest.write_text(json.dumps({'install': str(candidate), 'identities': identities}, sort_keys=True), encoding='utf-8')
    manifest_digest = sha(manifest)
    scratch = out / 'scratch-admissions.json'
    checks = {}
    for phase, install in (('reference', reference), ('candidate', candidate)):
        env = dict(os.environ, JIANYING_INSTALL=str(install), JIANYING_RUNTIME_ADMISSIONS=str(scratch))
        env.pop('JIANYING_REQUALIFY_CANDIDATE', None)
        for check in GATED:
            root = out / phase / check
            command = [sys.executable, str(Path(__file__)), '--capture', check, '--into', str(root),
                       '--install', str(install)]
            if phase == 'candidate':
                command += ['--candidate-identities', str(manifest), '--identity-sha256', manifest_digest]
            try:
                proc = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8',
                                      errors='replace', timeout=timeout)
                code, tail = proc.returncode, (proc.stdout + proc.stderr)[-1500:]
            except subprocess.TimeoutExpired:
                code, tail = 'timeout', ''
            checks.setdefault(check, {})[phase] = {'rc': code, 'tail': tail}
            verify_snapshot(reference, frozen_reference)
            verify_snapshot(candidate, frozen_candidate)
            verify_source_snapshot(frozen_source)
    pairs = []
    for check in GATED:
        checks[check]['native_evidence'] = 'not_measured' if check in STRUCTURAL_CHECKS else 'required'
        loaded = {}
        try:
            for phase in ('reference', 'candidate'):
                loaded[phase] = captured_cases(out / phase / check)
            if set(loaded['reference']) != set(loaded['candidate']):
                raise ValueError('reference and candidate captured cases differ')
            if not loaded['reference'] and check not in STRUCTURAL_CHECKS:
                raise ValueError('no captured native cases')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            checks[check]['evidence_error'] = str(exc)
            continue
        common = sorted(loaded['reference'])
        checks[check]['required_cases'] = common
        for key in common:
            ref, cand = loaded['reference'][key], loaded['candidate'][key]
            pair = {'check': check, 'draft': key, 'features': ['preview'] if check in PREVIEW_CHECKS else list(GATED[check])}
            if not unchanged(ref) or not unchanged(cand):
                pair.update(error='captured draft changed after build', match=False)
                pairs.append(pair); continue
            slot = out / 'frames' / check / str(len([p for p in pairs if p['check'] == check]))
            try:
                times = positions(Path(ref['draft']))
                if not times:
                    raise ValueError('no frame positions available for comparison')
                if times != positions(Path(cand['draft'])):
                    raise ValueError('reference and candidate frame positions differ')
                ref_frames = render(reference, None, Path(ref['draft']), slot / 'reference', times)
                if len(ref_frames) != len(times):
                    raise ValueError('reference frame count differs from required positions')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                pair['error'] = 'reference render failed: ' + str(exc)[:200]
                pair['match'] = False
                pairs.append(pair); continue
            try:
                cand_frames = render(candidate, identities, Path(cand['draft']), slot / 'candidate', times)
                if len(cand_frames) != len(times):
                    raise ValueError('candidate frame count differs from required positions')
                measured = [frame_diff(a, b, area=True) for a, b in zip(ref_frames, cand_frames)]
                pair['diffs'] = [m[0] for m in measured]
                pair['differing_share'] = max(m[1] for m in measured) if measured else None
                pair['differing_boxes'] = [m[2] for m in measured]
                pair['match'] = bool(ref_frames) and len(cand_frames) == len(ref_frames) == len(times) and all(
                    d != 'size' and d <= TOLERANCE for d in pair['diffs'])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                pair['error'] = 'candidate render failed: ' + str(exc)[:200]
                pair['match'] = False
            pair['times'] = times
            pairs.append(pair)
            verify_snapshot(reference, frozen_reference)
            verify_snapshot(candidate, frozen_candidate)
    features = {}
    for feature in runtime_admission.FEATURES:
        evidence = PREVIEW_CHECKS if feature == 'preview' else [c for c, f in GATED.items() if feature in f]
        failed = [c for c in evidence if checks[c].get('evidence_error') or
                  any(checks[c][p]['rc'] != 0 for p in ('reference', 'candidate')) or
                  (c not in STRUCTURAL_CHECKS and not any(p['check'] == c for p in pairs))]
        compared = [p for p in pairs if p['check'] in evidence]
        mismatched = [p for p in compared if not p['match']]
        render_errors = [p for p in mismatched if 'error' in p]
        if failed:
            status, reason = 'refused', 'self-check failed: ' + ', '.join(failed)
        elif mismatched:
            status, reason = 'refused', f'{len(mismatched)} of {len(compared)} drafts differ'
        elif not compared:
            status, reason = 'refused', 'no comparable native frames'
        else:
            status, reason = 'admitted', f'{len(compared)} drafts, frames identical within {TOLERANCE}/255'
        features[feature] = {'status': status, 'reason': reason, 'checks': list(evidence), 'compared': len(compared),
                             'frames_differ_only': bool(mismatched) and not failed and not render_errors}
    absent = {}
    for feature, (_, missing) in frozen_candidate.items():
        if feature not in identities:
            features[feature].update(status='refused', reason='candidate identity unavailable')
        if missing:
            absent[feature] = missing
            features[feature]['absent_contract_files'] = missing
    if features['preview']['status'] != 'admitted':
        for feature in features:
            if feature != 'preview' and features[feature]['status'] == 'admitted':
                features[feature].update(status='refused', reason='required preview is not admitted')
    admitted = [f for f, v in features.items() if v['status'] == 'admitted']
    report_path = out / 'requalify-report.json'
    store = runtime_admission.store_path() if admitted else None
    verify_snapshot(reference, frozen_reference)
    verify_snapshot(candidate, frozen_candidate)
    verify_source_snapshot(frozen_source)
    frozen_feature_sources = (
        {f: runtime_admission.feature_source_identity(f, frozen_source) for f in runtime_admission.FEATURES}
        if hasattr(runtime_admission, 'feature_source_identity') else None
    )
    report = {'candidate': str(candidate), 'reference': str(reference),
              'measured': datetime.datetime.now().isoformat(timespec='seconds'), 'tolerance': TOLERANCE,
              'features': features, 'admitted': admitted, 'store': str(store) if store else None,
              'admission_state': 'eligible_not_committed', 'preview_scope': 'still_frames_only',
              'sequence': {'status': 'unverified', 'reason': 'no real native sequence acceptance run'},
              'identities': {f: identities[f] for f in admitted},
              'reference_identities': {f: files for f, (files, _) in frozen_reference.items()},
              'candidate_manifest_sha256': manifest_digest,
              'source_identity': frozen_source,
              **({'feature_source_identities': frozen_feature_sources} if frozen_feature_sources is not None else {}),
              'checks': checks, 'pairs': pairs}
    write_report(report_path, report)
    verify_snapshot(reference, frozen_reference)
    verify_snapshot(candidate, frozen_candidate)
    verify_source_snapshot(frozen_source)
    if admitted:
        # admit must lock, validate the persisted report and commit atomically. Never
        # rewrite that report afterwards: the store may bind its exact digest.
        store = runtime_admission.admit(candidate.name, reference.name,
                                        {f: identities[f] for f in admitted}, report_path,
                                        {f: absent[f] for f in admitted if f in absent})
        verify_snapshot(reference, frozen_reference)
        verify_snapshot(candidate, frozen_candidate)
        with pinned(candidate):
            for feature in admitted:
                if not runtime_admission.matches(feature, candidate):
                    raise AssertionError('admitted subset not accepted by its gate: ' + feature)
    return {'candidate': candidate.name, 'reference': reference.name, 'admitted': admitted,
            'refused': {f: v['reason'] for f, v in features.items() if v['status'] != 'admitted'},
            'report': str(report_path), 'store': str(store) if store else None}


def default_reference(candidate):
    import runtime_admission
    import editor
    measured = runtime_admission.bundled('preview')[0]
    data = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'JianyingEngine'
    candidates = [Path(candidate).resolve().parent / measured, data / 'references' / measured]
    for version, manifest in sorted(runtime_admission.preview_manifests().items(), reverse=True):
        try:
            recorded = editor.read_json(manifest).get('install')
            if recorded:
                candidates.append(Path(recorded))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    for path in candidates:
        try:
            if path.is_dir() and all(path.name == runtime_admission.bundled(f)[0] and
                                     runtime_admission.matches(f, path) for f in runtime_admission.FEATURES):
                return path
        except (OSError, ValueError):
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--candidate', type=Path, help='New Jianying install directory, for example ...\\11.7.0.14652')
    parser.add_argument('--reference', type=Path, help='Measured install directory; default: verified sibling, retained reference or recorded install')
    parser.add_argument('--out', type=Path, help='New evidence directory; keep it short (Jianying paths over ~120 characters fail)')
    parser.add_argument('--accept', default='', help='Disabled: acceptance must bind a reviewed immutable report')
    parser.add_argument('--capture', help=argparse.SUPPRESS)
    parser.add_argument('--into', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--install', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--candidate-identities', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--identity-sha256', help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(SCRIPTS))
    if args.capture:
        raise SystemExit(capture(args.capture, args.into, args.into / 'captures.json', args.install,
                                 args.candidate_identities, args.identity_sha256))
    import editor
    if not args.candidate or not args.out:
        parser.error('--candidate and --out are required')
    if args.accept:
        parser.error('--accept is disabled: acceptance must bind a reviewed immutable report')
    reference = args.reference
    if reference is None:
        reference = default_reference(args.candidate)
    if reference is None:
        parser.error('--reference is required: no verified measured install directory was found')
    try:
        editor.emit(requalify(args.candidate, reference, args.out))
    except (OSError, ValueError, AssertionError) as exc:
        editor.emit({'error': str(exc)})
        raise SystemExit(1)


if __name__ == '__main__':
    main()
