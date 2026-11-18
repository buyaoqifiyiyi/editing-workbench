"""Admission orchestration only: no installed host, real store, media worker or API."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts/requalify.py'
spec = importlib.util.spec_from_file_location('requalify_under_test', SCRIPT)
rq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rq)
NATIVE_RENDER = rq.render


class RequalifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reference, self.candidate = self.root / 'old', self.root / 'new'
        for install in (self.reference, self.candidate):
            install.mkdir()
            (install / 'runtime.bin').write_bytes(b'measured')
        self.ra = ModuleType('runtime_admission')
        self.ra.FEATURES = ('preview', 'chroma', 'mix', 'mask_feather', 'text_alpha',
                            'text_highlight', 'corner', 'green_lightness')
        self.ra.bundled = lambda feature: ('old', {'runtime.bin': rq.sha(self.reference / 'runtime.bin')})
        self.ra.candidate_identity = lambda feature, install: ({'runtime.bin': rq.sha(install / 'runtime.bin')}, [])
        self.ra.store_path = lambda: self.root / 'isolated-admissions.json'
        self.committed = False
        self.ra.matches = lambda feature, install: install == self.reference or self.committed
        self.contexts = []

        @contextlib.contextmanager
        def candidate_context(install, identities):
            self.contexts.append((Path(install), identities))
            for files in identities.values():
                for rel, digest in files.items():
                    if rq.sha(Path(install) / rel) != digest:
                        raise ValueError('frozen identity changed')
            yield
        self.ra.candidate_context = candidate_context
        self.admit = self.enterContext(patch.object(self.ra, 'admit', create=True, side_effect=self.commit))
        self.enterContext(patch.dict(sys.modules, {'runtime_admission': self.ra}))
        self.enterContext(patch.dict(os.environ, {'JIANYING_RUNTIME_ADMISSIONS': str(self.ra.store_path())}))
        self.receipts = {}
        self.case_counts = {}
        self.mutate_capture = lambda phase, check, entries: None
        self.render_hook = lambda install, check: None
        self.positions = [0.25, 0.5, 0.75]
        self.render_calls = []
        self.runner = self.enterContext(patch.object(rq.subprocess, 'run', side_effect=self.run_check))
        self.enterContext(patch.object(rq, 'positions', side_effect=lambda draft: list(self.positions)))
        self.enterContext(patch.object(rq, 'render', side_effect=self.render))

    def commit(self, version, reference, identities, report, absent=None):
        # This mock is the transaction boundary, never the real admission writer.
        document = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(document['identities'], identities)
        self.assertEqual(document['admission_state'], 'eligible_not_committed')
        for files in identities.values():
            self.assertEqual(files['runtime.bin'], rq.sha(self.candidate / 'runtime.bin'))
        self.report_digest_at_commit = rq.sha(report)
        self.committed = True
        return self.ra.store_path()

    def run_check(self, command, **kwargs):
        check = command[command.index('--capture') + 1]
        root = Path(command[command.index('--into') + 1])
        phase = root.parent.name
        self.assertNotIn('JIANYING_REQUALIFY_CANDIDATE', kwargs['env'])
        if phase == 'candidate':
            manifest = command[command.index('--candidate-identities') + 1]
            digest = command[command.index('--identity-sha256') + 1]
            frozen = rq.load_candidate(manifest, digest, self.candidate)
            self.assertTrue(frozen)
        else:
            self.assertNotIn('--candidate-identities', command)
        count = self.case_counts.get((phase, check), 0 if check in rq.STRUCTURAL_CHECKS else 1)
        root.mkdir(parents=True)
        entries = []
        for index in range(count):
            draft = root / 'out' / str(index)
            draft.mkdir(parents=True)
            (draft / 'draft_content.json').write_text('{}', encoding='utf-8')
            entries.append({'draft': str(draft), 'content': rq.sha(draft / 'draft_content.json'), 'plan': None})
        self.mutate_capture(phase, check, entries)
        (root / 'captures.json').write_text(json.dumps(entries), encoding='utf-8')
        return SimpleNamespace(returncode=self.receipts.get((phase, check), 0), stdout='', stderr='')

    def render(self, install, identities, draft, out, times):
        check = draft.parents[1].name
        self.render_calls.append((install, check, draft.name))
        self.render_hook(install, check)
        self.assertEqual(identities is not None, install == self.candidate)
        out.mkdir(parents=True)
        frames = []
        for index, _ in enumerate(times):
            frame = out / f'{index}.png'
            Image.new('RGBA', (2, 2), (10, 20, 30, 255)).save(frame)
            frames.append(frame)
        return frames

    def run_requalify(self):
        return rq.requalify(self.candidate, self.reference, self.root / 'evidence')

    def test_reference_discovery_uses_verified_archive_when_sibling_is_gone_or_invalid(self):
        archived = self.root / 'appdata' / 'JianyingEngine' / 'references' / 'old'
        archived.mkdir(parents=True)
        (archived / 'runtime.bin').write_bytes(b'measured')
        (self.reference / 'runtime.bin').write_bytes(b'changed')
        self.ra.preview_manifests = lambda: {}
        self.ra.matches = lambda feature, install: (install / 'runtime.bin').read_bytes() == b'measured'
        fake_editor = ModuleType('editor')
        fake_editor.read_json = lambda path: {}
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')}), \
                patch.dict(sys.modules, {'editor': fake_editor}):
            self.assertEqual(rq.default_reference(self.candidate), archived)
            (self.reference / 'runtime.bin').write_bytes(b'measured')
            self.assertEqual(rq.default_reference(self.candidate), self.reference)
            (self.reference / 'runtime.bin').write_bytes(b'changed')
            (archived / 'runtime.bin').write_bytes(b'changed')
            self.assertIsNone(rq.default_reference(self.candidate))

    def report(self):
        return json.loads((self.root / 'evidence/requalify-report.json').read_text(encoding='utf-8'))

    def test_complete_evidence_commits_immutable_report_and_no_sequence_claim(self):
        result = self.run_requalify()
        self.assertEqual(set(result['admitted']), set(self.ra.FEATURES))
        self.assertEqual(rq.sha(result['report']), self.report_digest_at_commit)
        self.assertEqual(self.report()['sequence']['status'], 'unverified')
        self.assertEqual(self.report()['preview_scope'], 'still_frames_only')
        self.assertFalse(self.ra.store_path().exists())

    def test_effect_failure_does_not_poison_basic_preview(self):
        self.render_hook = lambda install, check: (_ for _ in ()).throw(ValueError('effect failed')) if check == 'corner_pin' else None
        result = self.run_requalify()
        self.assertIn('preview', result['admitted'])
        self.assertNotIn('corner', result['admitted'])

    def test_preview_failure_blocks_all_effect_admission(self):
        self.receipts['candidate', 'alpha_video'] = 1
        self.assertEqual(self.run_requalify()['admitted'], [])
        self.admit.assert_not_called()

    def test_each_required_check_needs_native_cases(self):
        for phase in ('reference', 'candidate'):
            self.case_counts[phase, 'hsl_channels'] = 0
        result = self.run_requalify()
        self.assertNotIn('green_lightness', result['admitted'])
        self.assertIn('hsl_channels', result['refused']['green_lightness'])

    def test_asymmetric_case_sets_refused(self):
        self.case_counts['reference', 'hsl_channels'] = 2
        self.assertNotIn('green_lightness', self.run_requalify()['admitted'])

    def test_all_cases_above_old_sampling_limit_are_rendered(self):
        for phase in ('reference', 'candidate'):
            self.case_counts[phase, 'hsl_channels'] = 19
        self.run_requalify()
        self.assertEqual(sum(check == 'hsl_channels' for _, check, _ in self.render_calls), 38)

    def test_duplicate_case_refused(self):
        def duplicate(phase, check, entries):
            if check == 'chroma':
                entries.append(entries[0])
        self.mutate_capture = duplicate
        self.assertNotIn('chroma', self.run_requalify()['admitted'])

    def test_changed_draft_is_failure_not_skipped(self):
        def change(phase, check, entries):
            if check == 'chroma':
                Path(entries[0]['draft'], 'draft_content.json').write_text('{"changed":true}', encoding='utf-8')
        self.mutate_capture = change
        self.assertNotIn('chroma', self.run_requalify()['admitted'])
        pair = next(p for p in self.report()['pairs'] if p['check'] == 'chroma')
        self.assertFalse(pair['match'])
        self.assertNotIn('skipped', pair)

    def test_reference_failure_cannot_disappear_behind_other_check(self):
        def fail(install, check):
            if install == self.reference and check == 'hsl_channels':
                raise ValueError('reference renderer failed')
        self.render_hook = fail
        self.assertNotIn('green_lightness', self.run_requalify()['admitted'])

    def test_missing_frames_on_either_side_refused(self):
        original = self.render
        def incomplete(install, identities, draft, out, times):
            frames = original(install, identities, draft, out, times)
            return [] if draft.parents[1].name == 'chroma' else frames
        with patch.object(rq, 'render', side_effect=incomplete):
            self.assertNotIn('chroma', self.run_requalify()['admitted'])

    def test_partial_candidate_frames_refused(self):
        original = self.render
        def incomplete(install, identities, draft, out, times):
            frames = original(install, identities, draft, out, times)
            return frames[:-1] if install == self.candidate and draft.parents[1].name == 'chroma' else frames
        with patch.object(rq, 'render', side_effect=incomplete):
            self.assertNotIn('chroma', self.run_requalify()['admitted'])

    def test_empty_positions_refuse_everything(self):
        self.positions = []
        self.assertEqual(self.run_requalify()['admitted'], [])
        self.admit.assert_not_called()

    def test_identity_drift_during_checks_stops_before_commit(self):
        original = self.run_check
        def drift(command, **kwargs):
            result = original(command, **kwargs)
            (self.candidate / 'runtime.bin').write_bytes(b'updated')
            return result
        self.runner.side_effect = drift
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.run_requalify()
        self.admit.assert_not_called()

    def test_source_change_during_review_stops_before_commit(self):
        with patch.object(rq, 'source_snapshot', side_effect=[{'script.py': 'before'}, {'script.py': 'after'}]):
            with self.assertRaisesRegex(ValueError, 'scripts or contracts changed'):
                self.run_requalify()
        self.admit.assert_not_called()

    def test_review_lock_rejects_duplicate_and_releases(self):
        with rq.review_lock():
            with self.assertRaisesRegex(ValueError, 'already running'):
                with rq.review_lock():
                    pass
        with rq.review_lock():
            pass

    def test_identity_drift_after_report_prevents_commit(self):
        original = rq.write_report
        def drift(path, report):
            original(path, report)
            (self.reference / 'runtime.bin').write_bytes(b'updated')
        with patch.object(rq, 'write_report', side_effect=drift):
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.run_requalify()
        self.admit.assert_not_called()

    def test_post_commit_drift_never_returns_success(self):
        def drift(*args, **kwargs):
            result = self.commit(*args, **kwargs)
            (self.candidate / 'runtime.bin').write_bytes(b'updated')
            return result
        self.admit.side_effect = drift
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.run_requalify()

    def test_direct_identity_hash_catches_change_even_if_inventory_is_cached(self):
        frozen = rq.snapshot(self.candidate)
        self.ra.candidate_identity = lambda feature, install: frozen[feature]
        path = self.candidate / 'runtime.bin'
        before = path.stat()
        path.write_bytes(b'corrupt!')  # same size, with original mtime restored
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'Runtime identity changed'):
            rq.verify_snapshot(self.candidate, frozen)

    def test_absent_candidate_identity_cannot_be_admitted(self):
        original = self.ra.candidate_identity
        self.ra.candidate_identity = lambda feature, install: (None, ['missing.node']) if feature == 'chroma' and install == self.candidate else original(feature, install)
        self.assertNotIn('chroma', self.run_requalify()['admitted'])

    def test_report_failure_prevents_commit(self):
        with patch.object(rq, 'write_report', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_requalify()
        self.admit.assert_not_called()

    def test_unbound_accept_rejected_before_any_work(self):
        with self.assertRaisesRegex(ValueError, 'disabled'):
            rq.requalify(self.candidate, self.reference, self.root / 'evidence', accept=('preview',))
        self.runner.assert_not_called()
        self.admit.assert_not_called()

    def test_manifest_binds_exact_install_and_bytes(self):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'install': str(self.candidate), 'identities': {'preview': {'runtime.bin': rq.sha(self.candidate / 'runtime.bin')}}}))
        with self.assertRaisesRegex(ValueError, 'different install'):
            rq.load_candidate(path, rq.sha(path), self.reference)
        with self.assertRaisesRegex(ValueError, 'changed'):
            rq.load_candidate(path, '0' * 64, self.candidate)

    def test_pinned_context_uses_frozen_map_and_restores_environment(self):
        files = {'preview': {'runtime.bin': rq.sha(self.candidate / 'runtime.bin')}}
        with patch.dict(os.environ, {'JIANYING_INSTALL': 'original', 'JIANYING_REQUALIFY_CANDIDATE': 'ignored'}):
            with rq.pinned(self.candidate, files):
                self.assertEqual(os.environ['JIANYING_INSTALL'], str(self.candidate))
                self.assertNotIn('JIANYING_REQUALIFY_CANDIDATE', os.environ)
            self.assertEqual(os.environ['JIANYING_INSTALL'], 'original')
            self.assertEqual(os.environ['JIANYING_REQUALIFY_CANDIDATE'], 'ignored')
        self.assertEqual(self.contexts[-1], (self.candidate, files))

    def test_frame_area_counts_any_rgba_channel_above_tolerance(self):
        a, b = self.root / 'a.png', self.root / 'b.png'
        Image.new('RGBA', (1, 1), (0, 0, 0, 255)).save(a)
        Image.new('RGBA', (1, 1), (0, 0, 3, 255)).save(b)
        self.assertEqual(rq.frame_diff(a, b, area=True), (3, 1.0, (0, 0, 1, 1)))

    def test_missing_capture_manifest_refuses_its_feature(self):
        original = self.run_check
        def remove(command, **kwargs):
            result = original(command, **kwargs)
            check = command[command.index('--capture') + 1]
            if check == 'chroma':
                Path(command[command.index('--into') + 1], 'captures.json').unlink()
            return result
        self.runner.side_effect = remove
        self.assertNotIn('chroma', self.run_requalify()['admitted'])

    def test_self_check_failure_with_good_frames_still_refused(self):
        self.receipts['candidate', 'text_color_fade'] = 1
        result = self.run_requalify()
        self.assertIn('preview', result['admitted'])
        self.assertNotIn('text_alpha', result['admitted'])
        self.assertNotIn('text_highlight', result['admitted'])

    def test_changed_pixels_refuse_only_the_effect(self):
        original = self.render
        def different(install, identities, draft, out, times):
            frames = original(install, identities, draft, out, times)
            if install == self.candidate and draft.parents[1].name == 'chroma':
                Image.new('RGBA', (2, 2), (255, 20, 30, 255)).save(frames[0])
            return frames
        with patch.object(rq, 'render', side_effect=different):
            result = self.run_requalify()
        self.assertIn('preview', result['admitted'])
        self.assertNotIn('chroma', result['admitted'])

    def test_capture_enters_manifest_context_and_restores_on_exception(self):
        editor, checks = ModuleType('editor'), ModuleType('run_checks')
        editor.build_draft = lambda args: None
        original_build = editor.build_draft
        checks.CHECKS = {'fixture': ('fixture.py', 'out')}
        checks.arguments = lambda *args: []
        manifest = self.root / 'candidate.json'
        identities = {'preview': {'runtime.bin': rq.sha(self.candidate / 'runtime.bin')}}
        manifest.write_text(json.dumps({'install': str(self.candidate), 'identities': identities}))
        argv, paths = sys.argv[:], sys.path[:]
        def check_script(*args, **kwargs):
            self.assertEqual(self.contexts[-1], (self.candidate, identities))
            self.assertEqual(os.environ['JIANYING_INSTALL'], str(self.candidate))
            raise RuntimeError('self-check stopped')
        root = self.root / 'capture'
        with patch.dict(sys.modules, {'editor': editor, 'run_checks': checks}), \
                patch.object(rq.runpy, 'run_path', side_effect=check_script):
            with self.assertRaisesRegex(RuntimeError, 'self-check stopped'):
                rq.capture('fixture', root, root / 'captures.json', self.candidate, manifest, rq.sha(manifest))
        self.assertIs(editor.build_draft, original_build)
        self.assertEqual(sys.argv, argv)
        self.assertEqual(sys.path, paths)
        self.assertEqual(json.loads((root / 'captures.json').read_text()), [])

    def test_render_enters_candidate_context_and_rejects_empty_native_receipt(self):
        native = ModuleType('native_preview')
        identities = {'preview': {'runtime.bin': rq.sha(self.candidate / 'runtime.bin')}}
        def preview(*args):
            self.assertEqual(self.contexts[-1], (self.candidate, identities))
            return {'status': 'passed', 'frames': []}
        native.preview = preview
        with patch.dict(sys.modules, {'native_preview': native}):
            with self.assertRaisesRegex(ValueError, 'did not pass'):
                NATIVE_RENDER(self.candidate, identities, self.root / 'draft', self.root / 'frames', [0.5])


if __name__ == '__main__':
    unittest.main()
