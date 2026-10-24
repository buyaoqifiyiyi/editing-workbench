"""Animation resource binding regression: every animation entry must point at the effect bundle
the app itself caches (Cache/effect/<id>/<hash>), and an id the cache cannot resolve must be dropped
from the draft body instead of shipping with an invented or empty path (剪映 draws no animation
without its resource: it marks the segment lost and keeps hunting for it while opening).
Uses a synthetic cache tree, so it needs no Jianying install. Also pins the user data lookup order
(configured draft location -> JIANYING_USER_DATA -> %LOCALAPPDATA%), which decides whether the cache
is found at all on a machine whose settings mirror did not come along: both the animation layers and
every other native resource family (grading, blur, glow, masks, fonts, extensions) must resolve
through that tolerant lookup, and must still fail loudly when no root carries the cache.
"""
import argparse
import copy
import os
import shutil
import tempfile
from pathlib import Path

import editor
import native_advanced


BUNDLE = '3d880239a1fa70fbaedcc7fd20794e22'


def content_with_animations():
    def material(identifier, name):
        return {'id': identifier + '-material', 'type': 'sticker_animation',
                'animations': [{'id': identifier, 'name': name, 'type': 'in', 'duration': 400000,
                                'material_type': 'sticker'}]}

    nested = {'id': 'nested-draft', 'name': '背景组', 'duration': 4000000,
              'tracks': [{'id': 'nested-track', 'segments': [
                  {'id': 'nested-segment',
                   'extra_material_refs': ['922958-material', '431662-material']}]}],
              'materials': {'material_animations': [material('922958', '三分割'), material('431662', '动感放大')]}}
    return {'duration': 4000000,
            'tracks': [{'id': 'video-track', 'segments': [
                {'id': 'segment', 'extra_material_refs': ['431662-material', '1621978-material']}]}],
            'materials': {'material_animations': [material('431662', '动感放大'), material('1621978', '右上弹入')],
                          'drafts': [{'id': 'combination', 'draft': nested}]}}


def check_user_data_order(root):
    """Where the effect cache is looked up, in order; a machine without the settings mirror still binds."""
    plain = root / 'no-cache-here'
    plain.mkdir()
    install = root / 'install' / 'JianyingPro' / 'User Data'
    (install / 'Cache' / 'effect').mkdir(parents=True)
    real_native = editor.native_user_data
    saved = os.environ.get('JIANYING_USER_DATA'), os.environ.get('LOCALAPPDATA')

    def picks(primary, override, local_app_data):
        editor.native_user_data = lambda: primary
        os.environ['JIANYING_USER_DATA'] = '' if override is None else str(override)
        os.environ['LOCALAPPDATA'] = '' if local_app_data is None else str(local_app_data)
        try:
            return editor.configured_user_data()
        finally:
            editor.native_user_data = real_native

    try:
        cases = [('configured', picks(root, plain, plain), root),
                 ('environment', picks(plain, root, plain), root),
                 ('install', picks(plain, None, install.parent.parent), install),
                 ('fallback', picks(plain, None, plain), plain)]
    finally:
        editor.native_user_data = real_native
        for name, value in zip(('JIANYING_USER_DATA', 'LOCALAPPDATA'), saved):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    for label, picked, expected in cases:
        if picked is None or Path(picked) != expected:
            raise AssertionError('User data lookup order mismatch at ' + label + ': ' +
                                 repr((str(picked), str(expected))))
    return [label for label, _, _ in cases]


RESOLVERS = ('def native_user_data(', 'def configured_user_data(', 'def resource_user_data(')


def resolver_ranges(source):
    """The line ranges of the three user data resolvers: the only places allowed to read the setting."""
    starts = [number for number, line in enumerate(source, start=1)
              if any(line.startswith(name) for name in RESOLVERS)]
    if len(starts) != len(RESOLVERS):
        raise AssertionError('User data resolvers not found where expected: ' + repr(starts))
    boundaries = [number for number, line in enumerate(source, start=1)
                  if line.startswith(('def ', 'class '))]
    ranges = []
    for start in starts:
        later = [number for number in boundaries if number > start]
        ranges.append((start, (later[0] - 1) if later else len(source)))
    return ranges


def check_user_data_call_sites():
    """Only the resolvers may reach for the strict setting; family caches resolve per file.

    The strict native_user_data() reads only the configured draft location, so a settings mirror
    without its cache makes a caller report a missing resource the machine actually has: animations
    drop silently, other resource families fail a build that could have succeeded. Keying a resource
    off the cache directory alone keeps that failure for a mirror that has the directory but not the
    package, so every family path resolves through the per-file resolver instead.
    """
    source = Path(editor.__file__).read_text(encoding='utf-8').splitlines()
    ranges = resolver_ranges(source)
    offenders = [str(number) + ': ' + line.strip() for number, line in enumerate(source, start=1)
                 if 'native_user_data()' in line
                 and not any(start <= number <= end for start, end in ranges)]
    if offenders:
        raise AssertionError('Outside the resolvers use resource_user_data() or '
                             'configured_user_data(): ' + repr(offenders))
    strict_joins = [str(number) + ': ' + line.strip() for number, line in enumerate(source, start=1)
                    if 'native_user_data()' in line and 'cache_relative' in line]
    if strict_joins:
        raise AssertionError('A family cache path must be joined to resource_user_data(): '
                             + repr(strict_joins))
    if not any('resource_user_data(spec[' in line for line in source):
        raise AssertionError('The admitted font path must resolve per file: resource_user_data('
                             'spec[cache_relative]) is missing')
    # An explicit root handed to a family helper is a promise about where its cache is (self-checks and
    # overrides rely on it), so editor must pass None -- never a root it resolved -- and let the helper
    # resolve per file. Handing over a resolved root is what silently shadowed the negative cases.
    strays = [str(number) + ': ' + line.strip() for number, line in enumerate(source, start=1)
              if 'resource_user_data(' in line and 'def resource_user_data' not in line
              and 'spec[' not in line]
    if strays:
        raise AssertionError('Family helpers resolve their own cache path: pass None or the caller\'s '
                             'explicit override, not a resolved root: ' + repr(strays))
    joined = [{'file': 'editor.py', 'strict_joins': strict_joins,
               'resolved_root_handoffs': strays,
               'per_file_resolver_calls': sum(1 for line in source if 'resource_user_data(' in line)}]
    for name in ('native_advanced.py', 'native_text_glow.py'):
        lines = (Path(editor.__file__).parent / name).read_text(encoding='utf-8').splitlines()
        loose = [str(number) + ': ' + line.strip() for number, line in enumerate(lines, start=1)
                 if 'cache_relative' in line and 'cache_root(' not in line and '.pop(' not in line]
        calls = sum(1 for line in lines if 'cache_root(' in line)
        if loose or not calls:
            raise AssertionError(name + ' must route every family cache path through cache_root(): '
                                 + repr({'loose': loose, 'cache_root_calls': calls}))
        joined.append({'file': name, 'cache_root_calls': calls})
    return {'editor_source_lines': len(source), 'resolver_ranges': ranges,
            'strict_reads': sum(1 for line in source if 'native_user_data()' in line),
            'per_file_join_sites': joined}


def check_resource_lookup(root):
    """Resource families resolve to a root that carries the family cache, and stay loud when none does."""
    plain = root / 'no-cache-here'
    plain.mkdir(exist_ok=True)
    install = root / 'install' / 'JianyingPro' / 'User Data'
    (install / 'Cache' / 'effect').mkdir(parents=True, exist_ok=True)
    family = 'Cache/effect/7416258989467374091/9e1cfdca2b07a183049bfc9b5ed32895'
    (install / family).mkdir(parents=True, exist_ok=True)
    mirror = root / 'mirror' / 'JianyingPro' / 'User Data'
    (mirror / 'Cache' / 'effect').mkdir(parents=True, exist_ok=True)
    real_native = editor.native_user_data
    saved = os.environ.get('JIANYING_USER_DATA'), os.environ.get('LOCALAPPDATA')

    def picks(primary, override, local_app_data, relative=None):
        if primary is None:
            def missing():
                raise ValueError('Native resources require configured Jianying user data')
            editor.native_user_data = missing
        else:
            editor.native_user_data = lambda: primary
        os.environ['JIANYING_USER_DATA'] = '' if override is None else str(override)
        os.environ['LOCALAPPDATA'] = '' if local_app_data is None else str(local_app_data)
        try:
            return Path(editor.resource_user_data(relative))
        finally:
            editor.native_user_data = real_native

    try:
        cases = [('environment', picks(plain, mirror, install.parent.parent), mirror),
                 ('install', picks(plain, None, install.parent.parent), install),
                 ('no-cache-anywhere', picks(plain, None, plain), plain),
                 ('family-install', picks(mirror, None, install.parent.parent, family), install),
                 ('family-nowhere', picks(plain, None, plain, family), plain)]
        for label, picked, expected in cases:
            if picked != expected:
                raise AssertionError('Resource lookup mismatch at ' + label + ': ' +
                                     repr((str(picked), str(expected))))
        try:
            picks(None, None, plain)
        except ValueError as error:
            assert 'configured Jianying user data' in str(error), error
        else:
            raise AssertionError('Without any cache the resolver must leave the caller to fail loudly')
        explicit = root / 'explicit-root'
        explicit.mkdir(exist_ok=True)
        handed = native_advanced.cache_root(family, explicit)
        if Path(handed) != explicit:
            raise AssertionError('An explicit user data root must stay exact, even when another root '
                                 'carries the cache: ' + repr(str(handed)))
    finally:
        editor.native_user_data = real_native
        for name, value in zip(('JIANYING_USER_DATA', 'LOCALAPPDATA'), saved):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return [label for label, _, _ in cases] + ['loud-without-cache', 'explicit-root-exact']


def check(out):
    with tempfile.TemporaryDirectory(prefix='animation-paths-') as temporary:
        root = Path(temporary)
        cache = root / 'Cache' / 'effect'
        (cache / '431662' / BUNDLE).mkdir(parents=True)
        (cache / '431662' / BUNDLE / 'anim.prefab').write_text('{}', encoding='utf-8')
        (cache / '922958' / 'a').mkdir(parents=True)
        (cache / '922958' / 'b').mkdir(parents=True)
        content = content_with_animations()
        report = native_advanced.bind_animation_paths(content, root)
        expected = (cache / '431662' / BUNDLE).as_posix()
        if report['bound'] != ['431662', '431662']:
            raise AssertionError('Bound animation set mismatch: ' + repr(report))
        if report['missing'] != ['1621978'] or report['ambiguous'] != ['922958']:
            raise AssertionError('Unresolvable animation reporting mismatch: ' + repr(report))
        if [entry['id'] for entry in report['dropped']] != ['1621978', '922958']:
            raise AssertionError('Dropped animation set mismatch: ' + repr(report))
        outer = content['materials']['material_animations']
        nested = content['materials']['drafts'][0]['draft']['materials']['material_animations']
        if [material['id'] for material in outer] != ['431662-material'] or len(nested) != 1:
            raise AssertionError('An unresolvable animation must remove its material entry')
        if outer[0]['animations'][0]['path'] != expected or nested[0]['animations'][0]['path'] != expected:
            raise AssertionError('Animation path not bound in both draft bodies')
        refs = content['tracks'][0]['segments'][0]['extra_material_refs']
        nested_refs = content['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['extra_material_refs']
        if refs != ['431662-material'] or nested_refs != ['431662-material']:
            raise AssertionError('A dropped material must not leave a dangling segment reference: ' +
                                 repr((refs, nested_refs)))
        if native_advanced.verify_animation_paths(content) != 2:
            raise AssertionError('Bound animation path readback count mismatch')
        before = copy.deepcopy(content)
        if native_advanced.bind_animation_paths(content, root)['bound'] != report['bound']:
            raise AssertionError('Rebinding must be idempotent')
        if content != before:
            raise AssertionError('Rebinding must not rewrite the draft body')
        shutil.rmtree(cache / '431662' / BUNDLE)
        try:
            native_advanced.verify_animation_paths(content)
        except ValueError as error:
            assert 'Animation bundle is unavailable' in str(error), error
        else:
            raise AssertionError('A vanished animation bundle must fail readback')
        empty = content_with_animations()
        unverified = native_advanced.bind_animation_paths(empty, None)
        if unverified['bound'] or sorted(set(unverified['unverified'])) != ['1621978', '431662', '922958']:
            raise AssertionError('Without a user data root nothing may be bound: ' + repr(unverified))
        if native_advanced.verify_animation_paths(empty) != 0 or empty['materials']['material_animations']:
            raise AssertionError('An unresolvable animation must not survive into the draft body')
        order = check_user_data_order(root)
        resource_order = check_resource_lookup(root)
        call_sites = check_user_data_call_sites()
    report = {'status': 'passed', 'bundle': BUNDLE, 'bound_ids': ['431662'], 'bound_entries': 2,
              'dropped_ids': ['1621978', '922958'],
              'dropped_materials': ['1621978-material', '922958-material'],
              'user_data_order': order,
              'resource_user_data_order': resource_order,
              'user_data_call_sites': call_sites,
              'app_rendering': 'separate: 剪映 UI draw is user-side evidence'}
    Path(out).mkdir(parents=True, exist_ok=True)
    editor.write_json(Path(out) / '回归结果.json', report)
    # This check proves binding, dropping, reference cleanup, readback and which lookup each layer
    # uses; whether the app draws the animation stays with 剪映 UI and the user.
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    editor.emit(check(args.out.resolve()))