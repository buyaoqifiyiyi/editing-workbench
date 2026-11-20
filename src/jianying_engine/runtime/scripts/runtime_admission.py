"""Which Jianying runtime file identities a version-gated native subset accepts.

Each gated subset ships a contract measured on one Jianying build. A later build is accepted only
after `requalify` rendered the same synthetic drafts on both builds and found identical native
frames; that run stores the later build's file hashes per subset in an external, per-machine
admission file. A contract file that the later build no longer ships (web bundles are renamed or
merged between builds) is replaced by every file in the same folder of the later build, so a later
change there is still caught. Resource trees (effect bundles, mask sources, MixMode index) are never
re-pinned: their bundled hashes still apply, so a changed effect resource keeps the subset refused.
"""
import json
import os
import contextlib
import contextvars
import re
import tempfile
from pathlib import Path

import file_hash
import runtime_common

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
FEATURES = runtime_common.RUNTIME_FEATURES
_CANDIDATE = contextvars.ContextVar('requalify_candidate', default=None)


@contextlib.contextmanager
def candidate_context(install, identities):
    """Temporarily use frozen candidate identities inside the isolated review worker."""
    token = _CANDIDATE.set((Path(install).resolve(), identities))
    try:
        yield
    finally:
        _CANDIDATE.reset(token)


def store_path():
    return Path(os.environ.get('JIANYING_RUNTIME_ADMISSIONS') or
                Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'JianyingEngine/runtime-admissions.json')


def load():
    path = store_path()
    if not path.is_file():
        return {'schema_version': 1, 'versions': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Invalid runtime admission record: ' + str(path)) from exc
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('versions'), dict):
        raise ValueError('Invalid runtime admission record schema: ' + str(path))
    return data


def _asset(name):
    return json.loads((ASSETS / name).read_text(encoding='utf-8'))


def preview_manifests():
    return {p.stem[len('native-preview-'):]: p for p in ASSETS.glob('native-preview-*.json')}


CONTRACTS = {'chroma': 'chroma2-v1.json', 'mask_feather': 'mask-red-coverage-v1.json', 'mix': 'mix-modes-v1.json',
             'text_alpha': 'text-alpha-v1.json', 'text_highlight': 'text-highlights-v1.json'}


def _contract_files(spec):
    files = dict(spec['runtime_sha256'])
    if 'font_relative' in spec:
        files[spec['font_relative']] = spec['font_sha256']
    return spec['runtime_version'], files


def bundled(feature, contract=None):
    """(measured version, {relative path: sha256}) for the subset's runtime files; contract overrides the shipped one."""
    if feature in CONTRACTS:
        return _contract_files(contract if contract is not None else _asset(CONTRACTS[feature]))
    preview = _asset('native-preview-11.6.0.14552.json')
    files = dict(preview['files'])
    files[preview['runtime']['file']] = preview['runtime']['sha256']
    if feature == 'corner':
        font = _asset('text-alpha-v1.json')
        files[font['font_relative']] = font['font_sha256']
    return preview['version'], files


def gated_paths():
    return sorted({rel for feature in FEATURES for rel in bundled(feature)[1]})


def source_identity():
    """Hash the runtime scripts and contracts whose behavior the review recorded."""
    return runtime_common.runtime_source_identity(ASSETS.parent)


_SHARED_FEATURE_FILES = (
    'scripts/editor.py', 'scripts/editor_build.py', 'scripts/editor_env.py',
    'scripts/editor_validate.py', 'scripts/file_hash.py', 'scripts/local_font.py',
    'scripts/native_advanced.py', 'scripts/native_preview.py', 'scripts/native_preview.cjs',
    'scripts/native_preview_stability.cjs', 'scripts/native_protocol.py',
    'scripts/requalify.py', 'scripts/run_checks.py',
    'scripts/runtime_admission.py', 'scripts/runtime_common.py', 'scripts/runtime_paths.py',
    'assets/native-11.5.json', 'assets/native-extensions-11.5.json', 'assets/native-fonts-11.5.json',
)
_FEATURE_FILES = {
    'preview': ('scripts/native_preview_self_check.py', 'scripts/alpha_video_self_check.py', 'scripts/migrate.py'),
    'chroma': ('scripts/native_chroma.py', 'scripts/chroma_self_check.py', 'assets/chroma2-v1.json'),
    'mask_feather': ('scripts/native_mask_feather.py', 'scripts/mask_feather_self_check.py', 'scripts/migrate.py',
                     'assets/mask-red-coverage-v1.json'),
    'mix': ('scripts/native_mix_mode.py', 'scripts/native_chroma.py', 'scripts/mix_mode_self_check.py',
            'assets/mix-modes-v1.json'),
    'text_alpha': ('scripts/native_text_alpha.py', 'scripts/native_text_highlight.py',
                   'scripts/text_alpha_self_check.py', 'scripts/text_color_fade_self_check.py',
                   'assets/text-alpha-v1.json', 'assets/text-highlights-v1.json'),
    'text_highlight': ('scripts/native_text_highlight.py', 'scripts/native_text_alpha.py',
                       'scripts/text_highlight_self_check.py', 'scripts/text_color_fade_self_check.py',
                       'assets/text-highlights-v1.json', 'assets/text-alpha-v1.json'),
    'corner': ('scripts/corner_pin_self_check.py', 'assets/text-alpha-v1.json'),
    'green_lightness': ('scripts/native_hsl_channels.py', 'scripts/green_lightness_self_check.py',
                        'scripts/hsl_channels_self_check.py'),
}


def feature_source_identity(feature, inventory=None):
    """Hash only the scripts and contracts that affect one version-gated subset."""
    if feature not in _FEATURE_FILES:
        raise ValueError(f'Unknown feature: {feature}')
    inventory = source_identity() if inventory is None else inventory
    if not isinstance(inventory, dict):
        return None
    manifests = ('assets/' + p.name for p in sorted(ASSETS.glob('native-preview-*.json')))
    required = sorted(set(_SHARED_FEATURE_FILES) | set(manifests) | set(_FEATURE_FILES[feature]))
    if any(rel not in inventory for rel in required):
        return None
    return {rel: inventory[rel] for rel in required}


def _source_matches(feature, evidence):
    scoped = evidence.get('feature_source_identities')
    if scoped is not None:
        if not isinstance(scoped, dict):
            return False
        expected_identity = feature_source_identity(feature)
        return (expected_identity is not None
                and isinstance(scoped.get(feature), dict)
                and scoped[feature] == expected_identity)
    return evidence.get('source_identity') == source_identity()


def candidate_identity(feature, install):
    """({relative path: sha256}, [contract files this build no longer ships]) for a later build, or (None, absent)
    when a missing contract file has no folder to stand in for it."""
    install = Path(install)
    identity, absent = {}, []
    for rel in bundled(feature)[1]:
        path = install / rel
        if path.is_file():
            identity[rel] = file_hash.sha256(path)
            continue
        absent.append(rel)
        # Only the versioned web bundles are known to be renamed or merged.
        if not re.fullmatch(r'Resources/canvas_agent/app/static/js/ai-creation~\d+\.[0-9a-f]+\.js', rel):
            return None, absent
        folder = path.parent
        stand_ins = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        if not stand_ins:
            return None, absent
        for p in stand_ins:
            identity[p.relative_to(install).as_posix()] = file_hash.sha256(p)
    return identity, absent


def _provisional(install):
    candidate = _CANDIDATE.get()
    return bool(candidate) and candidate[0] == Path(install).resolve()


def _valid_identity(feature, files):
    if not isinstance(files, dict) or not files:
        return False
    for rel, digest in files.items():
        if not isinstance(rel, str):
            return False
        path = Path(rel)
        if (path.is_absolute() or '..' in path.parts or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            return False
    for required in bundled(feature)[1]:
        if required in files:
            continue
        if not re.fullmatch(r'Resources/canvas_agent/app/static/js/ai-creation~\d+\.[0-9a-f]+\.js', required):
            return False
        folder = str(Path(required).parent).replace('\\', '/') + '/'
        if not any(rel.replace('\\', '/').startswith(folder) and rel.endswith('.js') for rel in files):
            return False
    return True


def expected(feature, install, contract=None):
    """Runtime file hashes this install must match for the subset, or None when the build is not admitted."""
    install = Path(install)
    version, files = bundled(feature, contract)
    if install.name == version:
        return files
    if _provisional(install):
        if contract is not None and feature in CONTRACTS and files != bundled(feature)[1]:
            return files  # a caller's stubbed contract (self-check refusal cases) applies as written
        identity = _CANDIDATE.get()[1].get(feature)
        return dict(identity) if _valid_identity(feature, identity) else None
    record = load().get('versions', {}).get(install.name)
    if not isinstance(record, dict):
        return None
    report, report_digest = record.get('report'), record.get('report_sha256')
    if not isinstance(report, str) or not isinstance(report_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', report_digest):
        return None
    if not file_hash.matches(Path(report), report_digest):
        return None
    admitted = record.get('features') if record else None
    identity = admitted.get(feature) if isinstance(admitted, dict) else None
    if not _valid_identity(feature, identity):
        return None
    try:
        evidence = json.loads(Path(report).read_text(encoding='utf-8'))
        if (evidence.get('admission_state') != 'eligible_not_committed'
                or Path(evidence.get('candidate', '')).name != install.name
                or evidence.get('identities', {}).get(feature) != identity
                or evidence.get('features', {}).get(feature, {}).get('status') != 'admitted'
                or not _source_matches(feature, evidence)):
            return None
        reported_absent = evidence['features'][feature].get('absent_contract_files', [])
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    # Derive whether a directory substitution exists from the authenticated
    # identity, not solely from the mutable store's optional absent list.
    substituted = [rel for rel in bundled(feature)[1] if rel not in identity]
    absent_records = record.get('absent_contract_files', {})
    if not isinstance(absent_records, dict):
        return None
    recorded_absent = absent_records.get(feature, [])
    if (not isinstance(recorded_absent, list) or not isinstance(reported_absent, list)
            or any(not isinstance(item, str) for item in [*recorded_absent, *reported_absent])
            or len(set(recorded_absent)) != len(recorded_absent)
            or len(set(reported_absent)) != len(reported_absent)
            or sorted(recorded_absent) != sorted(reported_absent)):
        return None
    if substituted or recorded_absent:
        actual, actual_absent = candidate_identity(feature, install)
        if actual != identity or sorted(actual_absent) != sorted(recorded_absent):
            return None
    if contract is not None and feature in CONTRACTS and files != bundled(feature)[1]:
        return files
    return dict(identity)


def matches(feature, install):
    files = expected(feature, install)
    return files is not None and all(file_hash.matches(Path(install) / rel, digest) for rel, digest in files.items())


def refusal(feature, install, measured):
    return (f'{measured} requires measured Jianying {bundled(feature)[0]}; {Path(install).name if install else "no install"} '
            'is not admitted (after a Jianying update run: jianying-engine requalify --candidate <new install>)')


def preview_manifest(install):
    """A native preview manifest for an admitted later build, derived from the reference manifest."""
    install = Path(install)
    record = load().get('versions', {}).get(install.name)
    provisional = _provisional(install)
    admitted = (record or {}).get('features')
    identity = expected('preview', install)
    if identity is None or (not provisional and not (isinstance(admitted, dict) and 'preview' in admitted)):
        return None
    manifests = preview_manifests()
    reference = (record or {}).get('reference') if not provisional else None
    base = json.loads(manifests[reference if reference in manifests else sorted(manifests)[-1]].read_text(encoding='utf-8'))
    try:
        files = {rel: identity[rel] for rel in base['files']}
        runtime = dict(base['runtime'], sha256=identity[base['runtime']['file']])
    except KeyError:
        return None
    return {'version': install.name, 'files': files, 'runtime': runtime, 'admitted_from': base['version']}


def admit(version, reference, identities, report, absent=None, accepted=None):
    """Store {feature: {relative path: sha256}} for a later build that requalify compared frame by frame."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not Path(report).is_file():
        raise ValueError('A completed review report is required before admission')
    if not identities or any(feature not in FEATURES or not _valid_identity(feature, files)
                             for feature, files in identities.items()):
        raise ValueError('Invalid or incomplete runtime identity')
    report_data = json.loads(Path(report).read_text(encoding='utf-8'))
    if (not isinstance(report_data, dict) or report_data.get('admission_state') != 'eligible_not_committed'
            or Path(report_data.get('candidate', '')).name != version
            or Path(report_data.get('reference', '')).name != reference
            or set(report_data.get('admitted', [])) != set(identities)
            or report_data.get('identities') != identities
            or any(not _source_matches(feature, report_data) for feature in identities)
            or any(report_data.get('features', {}).get(feature, {}).get('status') != 'admitted'
                   for feature in identities)):
        raise ValueError('Review report does not authorize these exact runtime identities')
    report_hash = file_hash.sha256(report)
    candidate = Path(report_data['candidate'])
    supplied_absent = absent or {}
    if not isinstance(supplied_absent, dict) or set(supplied_absent) - set(identities):
        raise ValueError('Invalid substituted runtime identity evidence')

    def check_substitutions():
        for feature, files in identities.items():
            actual, missing = candidate_identity(feature, candidate)
            supplied = supplied_absent.get(feature, [])
            reported = report_data['features'][feature].get('absent_contract_files', [])
            if (not isinstance(supplied, list) or not isinstance(reported, list)
                    or any(not isinstance(item, str) for item in [*supplied, *reported])
                    or len(set(supplied)) != len(supplied) or len(set(reported)) != len(reported)
                    or actual != files or sorted(missing) != sorted(supplied)
                    or sorted(missing) != sorted(reported)):
                raise ValueError('Substituted runtime identity does not match reviewed evidence')

    check_substitutions()
    if any(not file_hash.matches(candidate / rel, digest)
           for files in identities.values() for rel, digest in files.items()):
        raise ValueError('Candidate identity changed before admission')
    lock_path = path.with_suffix(path.suffix + '.lock')
    # An OS-backed lock is released automatically on process exit; the lock file is only a rendezvous.
    with runtime_common.file_lock(lock_path, blocking=True, pad_empty=False):
        if (not file_hash.matches(report, report_hash) or
                any(not file_hash.matches(candidate / rel, digest)
                    for files in identities.values() for rel, digest in files.items())):
            raise ValueError('Review report or candidate identity changed during admission')
        check_substitutions()
        data = load()
        data.setdefault('versions', {})[version] = {'reference': reference, 'features': identities,
                                                   'absent_contract_files': absent or {},
                                                   'accepted_frame_differences': accepted or {}, 'report': str(report),
                                                   'report_sha256': report_hash}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    return path
