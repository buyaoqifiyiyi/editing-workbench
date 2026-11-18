import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jianying_engine import workbench_requalification as rq
from jianying_engine.workbench import Workbench


class Process:
    def __init__(self, code=0, blocked=False):
        self.code = code
        self.stdout, self.stderr = io.BytesIO(b'output'), io.BytesIO(b'error')
        self.ready = threading.Event()
        self.waiting = threading.Event()
        if not blocked:
            self.ready.set()

    def wait(self, timeout=None):
        self.waiting.set()
        if not self.ready.wait(timeout=timeout or 2):
            raise AssertionError('mock process was not released')
        return self.code


class Tree:
    def __init__(self, process):
        self.process, self.closed = process, False

    def close(self):
        self.closed = True
        if not self.process.ready.is_set():
            self.process.code = -9
            self.process.ready.set()


class RequalificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / '11.7.0.14652'
        self.candidate.mkdir()
        self.binary = self.candidate / 'runtime.bin'
        self.binary.write_bytes(b'fixture')
        self.availability = dict(installed=True, compatible=True, selectable=True, state='available',
                                 version=self.candidate.name, path=str(self.candidate / 'JianyingPro.exe'))
        self.env = patch.dict(rq.os.environ, {'JIANYING_RUNTIME_ADMISSIONS': str(self.root / 'admissions.json')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = self.root / 'runtime-fixture'
        (self.runtime / 'scripts').mkdir(parents=True)
        (self.runtime / 'assets').mkdir()
        (self.runtime / 'scripts/check.py').write_text('# source one\n')
        (self.runtime / 'scripts/worker.cjs').write_text('// worker\n')
        (self.runtime / 'assets/text-alpha-v1.json').write_text(json.dumps({
            'runtime_version': '11.6.0.14552', 'runtime_sha256': {'nested/runtime.bin': 'fixture'}}))
        patch.object(rq, 'RUNTIME', self.runtime).start()
        self.manager = rq.Requalification()
        self.addCleanup(self.manager.close)
        self.spawn = patch.object(rq, '_spawn', side_effect=AssertionError('real spawn forbidden')).start()
        self.addCleanup(patch.stopall)

    def report(self, out, passed=None):
        passed = rq.FEATURES if passed is None else set(passed)
        out.mkdir(parents=True, exist_ok=True)
        path = out / 'requalify-report.json'
        identities = {f: {'runtime.bin': rq.digest(self.binary)} for f in passed}
        report = dict(candidate=str(self.candidate), admission_state='eligible_not_committed',
                      features={f: {'status': 'admitted' if f in passed else 'refused', 'reason': 'fixture'} for f in rq.FEATURES},
                      admitted=sorted(passed), identities=identities, store=str(self.root / 'admissions.json'),
                      source_identity=rq.source_identity(),
                      preview_scope='still_frames_only', sequence={'status': 'unverified'})
        path.write_text(json.dumps(report), encoding='utf-8')
        (self.root / 'admissions.json').write_text(json.dumps({'schema_version': 1, 'versions': {self.candidate.name: {
            'report': str(path), 'report_sha256': rq.digest(path), 'features': identities}}}), encoding='utf-8')
        return path

    def launch(self, passed=None, code=0, blocked=False):
        process = Process(code, blocked)
        tree = Tree(process)
        def spawn(command):
            self.assertIn('--candidate', command)
            self.assertNotIn('--accept', command)
            self.report(Path(command[command.index('--out') + 1]), passed)
            return process, tree
        self.spawn.side_effect = spawn
        return process, tree

    def test_complete_once_across_polls_and_restart(self):
        self.launch()
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'complete')
        self.manager._observe(self.availability, False)
        other = rq.Requalification()
        other._observe(self.availability, False)
        self.assertEqual(other.result['state'], 'complete')
        self.assertEqual(self.spawn.call_count, 1)

    def test_exit_zero_partial_preserves_selectable_and_subset_results(self):
        self.launch({'preview'})
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'partial')
        result = rq.reviewed_availability(self.availability, self.manager.result)
        self.assertTrue(result['selectable'])
        self.assertEqual(result['requalification']['subsets']['preview']['status'], 'admitted')
        self.assertEqual(result['requalification']['subsets']['mix']['status'], 'refused')
        self.assertEqual(len(result['requalification']['refused']), 7)

    def test_exit_zero_all_refused_is_failed(self):
        self.launch(set())
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')

    def test_review_never_changes_base_installation_or_compatibility(self):
        for state in ('needed', 'pending', 'checking', 'claiming', 'running', 'waiting', 'deferred',
                      'blocked', 'partial', 'failed', 'interrupted', 'complete'):
            for base_state in ('available', 'missing', 'incompatible'):
                with self.subTest(review=state, installation=base_state):
                    base = dict(self.availability, installed=base_state != 'missing',
                                compatible=base_state == 'available', selectable=base_state == 'available',
                                state=base_state, label='基础安装检测', reason='保留基础检测原因')
                    review = {'state': state, 'reason': '复核告警'}
                    result = rq.reviewed_availability(base, review)
                    self.assertEqual({k: v for k, v in result.items() if k != 'requalification'}, base)
                    self.assertEqual(result['requalification']['reason'], '复核告警')
                    self.assertEqual(result['requalification']['subsets']['mix']['status'], 'unverified')
                    self.assertNotIn('subsets', review)

    def test_report_content_and_fingerprints_still_required(self):
        for damage in ('candidate', 'feature_list', 'report_digest', 'identity_map'):
            with self.subTest(damage=damage):
                path = self.report(self.root / 'tampered')
                data = rq.read_json(path)
                if damage == 'candidate':
                    data['candidate'] = str(self.root / 'wrong-install')
                elif damage == 'feature_list':
                    data['admitted'] = []
                elif damage == 'report_digest':
                    data['measured'] = 'changed after commit'
                else:
                    data['identities']['preview'] = {'runtime.bin': '0' * 64}
                path.write_text(json.dumps(data), encoding='utf-8')
                if damage == 'identity_map':
                    store = rq.read_json(self.root / 'admissions.json')
                    store['versions'][self.candidate.name]['report_sha256'] = rq.digest(path)
                    (self.root / 'admissions.json').write_text(json.dumps(store))
                with self.assertRaises(ValueError):
                    rq.report_result(path, self.candidate)

    def test_added_file_in_replacement_directory_invalidates_review(self):
        path = self.report(self.root / 'replacement')
        data = rq.read_json(path)
        folder = self.candidate / 'Resources/canvas_agent/app/static/js'
        folder.mkdir(parents=True)
        replacement = folder / 'ai-creation.merged.js'
        replacement.write_text('stable', encoding='utf-8')
        rel = replacement.relative_to(self.candidate).as_posix()
        missing = 'Resources/canvas_agent/app/static/js/ai-creation~12.12345678.js'
        data['identities']['preview'][rel] = rq.digest(replacement)
        data['features']['preview']['absent_contract_files'] = [missing]
        path.write_text(json.dumps(data), encoding='utf-8')
        store = rq.read_json(self.root / 'admissions.json')
        record = store['versions'][self.candidate.name]
        record['features']['preview'] = data['identities']['preview']
        record['absent_contract_files'] = {'preview': [missing]}
        record['report_sha256'] = rq.digest(path)
        (self.root / 'admissions.json').write_text(json.dumps(store), encoding='utf-8')
        self.assertEqual(rq.report_result(path, self.candidate)['state'], 'complete')
        (folder / 'new.js').write_text('added', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '目录发生变化'):
            rq.report_result(path, self.candidate)

    def test_nonzero_even_with_report_is_failed_no_retry(self):
        self.launch(code=1)
        self.manager._observe(self.availability, False)
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.assertEqual(self.spawn.call_count, 1)

    def test_nonzero_report_is_not_reused_after_reference_change_or_restart(self):
        self.launch(code=1)
        with patch.dict(rq.os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')}):
            self.manager._observe(self.availability, False)
            first_report = self.manager.result['report']
            self.assertEqual(self.manager.result['state'], 'failed')
            archive = self.root / 'appdata/JianyingEngine/references/11.6.0.14552'
            archive.mkdir(parents=True)
            self.launch(code=1)
            self.manager._observe(self.availability, False)
            self.assertEqual(self.manager.result['state'], 'failed')
            self.assertNotEqual(self.manager.result['report'], first_report)
            other = rq.Requalification()
            self.addCleanup(other.close)
            other._observe(self.availability, False)
            self.assertEqual(other.result['state'], 'failed')

    def test_restored_reference_allows_one_new_attempt_without_repeating_an_unchanged_failure(self):
        failed = Process(code=2)
        failed.stderr = io.BytesIO(b'error: --reference is required: no verified measured install directory was found')
        self.spawn.side_effect = lambda command: (failed, Tree(failed))
        with patch.dict(rq.os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')}):
            self.manager._observe(self.availability, False)
            self.assertEqual(self.manager.result['state'], 'blocked')
            self.assertIn('旧版本', self.manager.result['reason'])
            self.manager._observe(self.availability, False)
            self.assertEqual(self.spawn.call_count, 1)
            archive = self.root / 'appdata/JianyingEngine/references/11.6.0.14552'
            archive.mkdir(parents=True)
            self.launch()
            self.manager._observe(self.availability, False)
            self.manager._observe(self.availability, False)
            self.assertEqual(self.spawn.call_count, 2)
            self.assertEqual(self.manager.result['state'], 'complete')

    def test_repair_inside_existing_reference_triggers_one_new_attempt(self):
        failed = Process(code=2)
        failed.stderr = io.BytesIO(b'error: --reference is required: no verified measured install directory was found')
        self.spawn.side_effect = lambda command: (failed, Tree(failed))
        with patch.dict(rq.os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')}):
            archive = self.root / 'appdata/JianyingEngine/references/11.6.0.14552'
            (archive / 'nested').mkdir(parents=True)
            file = archive / 'nested/runtime.bin'
            file.write_bytes(b'bad')
            self.manager._observe(self.availability, False)
            self.assertEqual(self.manager.result['state'], 'blocked')
            directory_stamp = archive.stat().st_mtime_ns
            file.write_bytes(b'good')
            os.utime(archive, ns=(archive.stat().st_atime_ns, directory_stamp))
            self.launch()
            self.manager._observe(self.availability, False)
            self.manager._observe(self.availability, False)
            self.assertEqual(self.spawn.call_count, 2)
            self.assertEqual(self.manager.result['state'], 'complete')

    def test_external_report_replaces_waiting_lock_result_without_duplicate_worker(self):
        locked = Process(code=1)
        locked.stdout = io.BytesIO(b'{"error":"Another Jianying version review is already running"}')
        self.spawn.side_effect = lambda command: (locked, Tree(locked))
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'waiting')
        self.report(self.root / 'external-evidence', {'preview'})
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'partial')
        self.assertEqual(self.spawn.call_count, 1)

    def test_lock_release_without_report_allows_only_one_retry(self):
        def locked(command):
            process = Process(code=1)
            process.stdout = io.BytesIO(b'{"error":"Another Jianying version review is already running"}')
            return process, Tree(process)
        self.spawn.side_effect = locked
        with patch.object(rq, 'execution_busy', return_value=False):
            self.manager._observe(self.availability, False)
            self.manager._observe(self.availability, False)
            self.manager._observe(self.availability, False)
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.manager.result['state'], 'blocked')

    def test_execution_lock_probe_detects_real_os_lock(self):
        lock_path = self.root / 'requalification-execution.lock'
        with lock_path.open('a+b') as lock:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertTrue(rq.execution_busy(self.root / 'admissions.json'))
            finally:
                if os.name == 'nt':
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.assertFalse(rq.execution_busy(self.root / 'admissions.json'))

    def test_spawn_failure_is_durable(self):
        self.spawn.side_effect = OSError('spawn denied')
        self.manager._observe(self.availability, False)
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.assertEqual(self.spawn.call_count, 1)

    def test_long_running_poll_and_second_owner_never_duplicate_or_kill(self):
        process, tree = self.launch(blocked=True)
        immediate = self.manager.observe(self.availability)
        self.assertEqual(immediate['state'], 'needed')
        self.assertTrue(process.waiting.wait(2))
        self.manager.checked_at = 0
        self.assertEqual(self.manager.observe(self.availability)['state'], 'running')
        other = rq.Requalification()
        other._observe(self.availability, False)
        self.assertEqual(other.result['state'], 'waiting')
        other.close()
        self.assertFalse(tree.closed)
        self.manager.close()
        self.assertTrue(tree.closed)
        self.assertEqual(self.manager.result['state'], 'interrupted')
        self.assertEqual(self.spawn.call_count, 1)

    def test_restart_claim_is_interrupted_without_pid_kill(self):
        self.launch()
        self.manager._observe(self.availability, False)
        journal = next(self.manager.directory.glob('*.json'))
        saved = rq.read_json(journal)
        saved.update(state='running', pid=12345)
        journal.write_text(json.dumps(saved))
        other = rq.Requalification()
        other._observe(self.availability, False)
        self.assertEqual(other.result['state'], 'interrupted')
        self.assertEqual(self.spawn.call_count, 1)

    def test_changed_reference_does_not_adopt_interrupted_old_claim(self):
        self.launch()
        with patch.dict(rq.os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')}):
            self.manager._observe(self.availability, False)
            old_journal = next(self.manager.directory.glob('*.json'))
            saved = rq.read_json(old_journal)
            saved.update(state='running', pid=12345)
            old_journal.write_text(json.dumps(saved))
            archive = self.root / 'appdata/JianyingEngine/references/11.6.0.14552'
            archive.mkdir(parents=True)
            self.launch(code=1)
            other = rq.Requalification()
            self.addCleanup(other.close)
            other._observe(self.availability, False)
            self.assertEqual(other.result['state'], 'failed')
            self.assertEqual(rq.read_json(old_journal)['state'], 'interrupted')
            self.assertTrue(rq.read_json(old_journal)['rejected_report_sha256'])

    def test_busy_deferred_then_idle_once(self):
        self.launch()
        self.manager._observe(self.availability, True)
        self.assertEqual(self.manager.result['state'], 'deferred')
        self.spawn.assert_not_called()
        self.manager._observe(self.availability, False)
        self.assertEqual(self.spawn.call_count, 1)

    def test_changed_version_gets_one_independent_attempt(self):
        self.launch()
        self.manager._observe(self.availability, False)
        self.candidate = self.root / '11.8.0.15000'
        self.candidate.mkdir()
        self.binary = self.candidate / 'runtime.bin'
        self.binary.write_bytes(b'new fixture')
        self.availability.update(version=self.candidate.name, path=str(self.candidate / 'JianyingPro.exe'))
        self.launch()
        self.manager._observe(self.availability, False)
        self.manager._observe(self.availability, False)
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.manager.result['version'], self.candidate.name)

    def test_timeout_terminates_owned_tree_and_never_retries(self):
        process, tree = self.launch(blocked=True)
        original = process.wait
        def wait(timeout=None):
            if not tree.closed:
                raise rq.subprocess.TimeoutExpired('fixture', timeout)
            return original(timeout)
        process.wait = wait
        self.manager._observe(self.availability, False)
        self.assertTrue(tree.closed)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.spawn.call_count, 1)

    def test_corrupt_journal_never_relaunches(self):
        self.launch()
        self.manager._observe(self.availability, False)
        journal = next(self.manager.directory.glob('*.json'))
        journal.write_text('broken')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.assertEqual(self.spawn.call_count, 1)

    def test_existing_committed_report_reused_and_drift_refused(self):
        self.report(self.root / 'external')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'complete')
        self.binary.write_bytes(b'drift')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.spawn.assert_not_called()

    def test_source_and_contract_drift_invalidate_reports(self):
        for relative in ('scripts/check.py', 'assets/text-alpha-v1.json'):
            with self.subTest(file=relative):
                report = self.report(self.root / 'old-report')
                path = self.runtime / relative
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                with self.assertRaises(rq.StaleSource):
                    rq.report_result(report, self.candidate)
                self.manager._observe(self.availability, True)
                self.assertEqual(self.manager.result['state'], 'deferred')
                self.assertNotIn('admitted', self.manager.result)
                path.write_bytes(original)
        self.spawn.assert_not_called()

    def test_each_source_identity_gets_one_attempt_across_restart(self):
        self.launch()
        self.manager._observe(self.availability, False)
        first = self.manager.result['source_identity_sha256']
        self.manager._observe(self.availability, False)
        (self.runtime / 'scripts/check.py').write_text('# source two\n')
        self.launch()
        self.manager._observe(self.availability, False)
        second = self.manager.result['source_identity_sha256']
        self.assertNotEqual(first, second)
        self.assertEqual(self.manager.result['state'], 'complete')
        other = rq.Requalification()
        other._observe(self.availability, False)
        self.assertEqual(other.result['state'], 'complete')
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(len(list(self.manager.directory.glob('*.json'))), 2)
        # A failed new identity is also terminal, regardless of the older successful journal.
        (self.runtime / 'scripts/check.py').write_text('# source three\n')
        self.launch(code=1)
        other._observe(self.availability, False)
        other._observe(self.availability, False)
        self.assertEqual(other.result['state'], 'failed')
        self.assertEqual(self.spawn.call_count, 3)

    def test_report_source_drift_during_worker_never_displays_passed(self):
        self.launch()
        spawn = self.spawn.side_effect
        def drift(command):
            result = spawn(command)
            (self.runtime / 'scripts/check.py').write_text('# changed during review\n')
            return result
        self.spawn.side_effect = drift
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.assertNotIn('admitted', self.manager.result)

    def test_new_report_must_still_match_the_claimed_source(self):
        self.launch()
        spawn = self.spawn.side_effect
        def drift(command):
            (self.runtime / 'scripts/check.py').write_text('# changed before worker snapshot\n')
            return spawn(command)
        self.spawn.side_effect = drift
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.assertNotIn('admitted', self.manager.result)

    def test_revalidation_does_not_return_cached_passed_status(self):
        self.report(self.root / 'external')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'complete')
        (self.runtime / 'scripts/check.py').write_text('# drift after completed review\n')
        with patch.object(self.manager, '_observe'):
            status = self.manager.observe(self.availability)
            self.manager.thread.join(timeout=2)
        self.assertEqual(status['state'], 'checking')
        self.assertNotIn('admitted', status)
        self.spawn.assert_not_called()

    def test_legacy_journal_and_invalid_old_report_do_not_block_new_identity(self):
        report = self.report(self.root / 'legacy-report')
        report.write_text('broken old report')
        self.manager.directory.mkdir(parents=True)
        legacy_identity = str(self.candidate).casefold() + '|' + self.candidate.name
        legacy_key = rq.hashlib.sha256(legacy_identity.encode()).hexdigest()[:20]
        legacy = self.manager.directory / (legacy_key + '.json')
        legacy.write_text(json.dumps({'state': 'failed', 'report': str(report)}))
        (self.runtime / 'scripts/check.py').write_text('# updated source\n')
        self.launch()
        self.manager._observe(self.availability, False)
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'complete')
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(rq.read_json(legacy)['state'], 'failed')

    def test_simultaneous_empty_file_lock_waits_without_false_failure(self):
        process, tree = self.launch(blocked=True)
        managers = [self.manager] + [rq.Requalification() for _ in range(7)]
        barrier = threading.Barrier(len(managers))
        def observe(manager):
            barrier.wait(timeout=3)
            manager._observe(self.availability, False)
        threads = [threading.Thread(target=observe, args=(manager,)) for manager in managers]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(process.waiting.wait(3))
            # The seven contenders finish while the one owner holds the empty file lock.
            for thread in threads:
                thread.join(timeout=0.2)
            states = [m.result.get('state') for m in managers]
            self.assertEqual(states.count('running'), 1, states)
            self.assertEqual(states.count('waiting'), 7, states)
            self.assertEqual(self.spawn.call_count, 1)
            self.assertEqual((self.manager.directory / 'worker.lock').stat().st_size, 0)
        finally:
            process.ready.set()
            for thread in threads:
                thread.join(timeout=3)
            for manager in managers:
                manager.close()
        self.assertEqual(sum(m.result.get('state') == 'complete' for m in managers), 1)

    def test_source_inventory_covers_exact_runtime_contract(self):
        (self.runtime / 'scripts/ignore.txt').write_text('ignore')
        (self.runtime / 'scripts/nested').mkdir()
        (self.runtime / 'scripts/nested/ignore.py').write_text('ignore')
        (self.runtime / 'assets/ignore.txt').write_text('ignore')
        expected = {'scripts/check.py', 'scripts/worker.cjs', 'assets/text-alpha-v1.json'}
        self.assertEqual(set(rq.source_identity()), expected)
        self.assertEqual(rq.source_identity(), {p: rq.digest(self.runtime / p) for p in expected})

    def test_zero_missing_report_and_uncommitted_report_fail(self):
        self.spawn.side_effect = lambda command: (Process(), Tree(Process()))
        self.manager._observe(self.availability, False)
        self.assertEqual(self.manager.result['state'], 'failed')
        self.manager._observe(self.availability, False)
        self.assertEqual(self.spawn.call_count, 1)
        path = self.report(self.root / 'external')
        (self.root / 'admissions.json').unlink()
        with self.assertRaises(OSError):
            rq.report_result(path, self.candidate)

    def test_baseline_missing_and_incomplete_install_do_not_spawn(self):
        self.manager._observe(dict(self.availability, version=self.manager.baseline,
                                   path=str(self.root / self.manager.baseline / 'JianyingPro.exe')), False)
        self.assertEqual(self.manager.result, {})
        self.manager._observe(dict(self.availability, installed=False), False)
        self.assertEqual(self.manager.result, {})
        self.manager._observe(dict(self.availability, compatible=False), False)
        self.assertEqual(self.manager.result['state'], 'blocked')
        self.spawn.assert_not_called()

    def test_workbench_catalog_preserves_initial_selection_and_closes_owner(self):
        from jianying_engine import software_backends
        detected = {entry['id']: dict(self.availability) for entry in software_backends.catalog()}
        with patch('jianying_engine.workbench.detect_backends', return_value=detected), \
                patch.object(rq.Requalification, '_observe'):
            service = Workbench(self.root / 'wb', self.root)
            catalog = service.backend_catalog()
            jianying = next(x for x in catalog if x['id'] == 'jianying')
            self.assertTrue(jianying['selectable'])
            self.assertEqual(jianying['availability']['requalification']['state'], 'needed')
            asyncio.run(service.close())
            self.assertTrue(service.requalification.closed)
        self.spawn.assert_not_called()

    def test_catalog_selection_uses_base_detection_for_every_review_outcome(self):
        from jianying_engine import software_backends
        detected = {entry['id']: dict(self.availability) for entry in software_backends.catalog()}
        selection = {'mode': 'single', 'single_backend': 'jianying', 'roles': {}}
        with patch('jianying_engine.workbench.detect_backends', return_value=detected):
            service = Workbench(self.root / 'catalog', self.root)
            try:
                for state in ('needed', 'running', 'partial', 'failed', 'complete'):
                    for base_state in ('available', 'missing', 'incompatible'):
                        with self.subTest(review=state, installation=base_state):
                            detected['jianying'].update(installed=base_state != 'missing',
                                compatible=base_state == 'available', selectable=base_state == 'available',
                                state=base_state, label='基础状态', reason='基础原因')
                            with patch.object(service.requalification, 'observe', return_value={'state': state}):
                                catalog = service.backend_catalog(refresh=True)
                            item = next(x for x in catalog if x['id'] == 'jianying')
                            self.assertEqual(item['selectable'], base_state == 'available')
                            self.assertEqual(item['role_availability']['editing']['selectable'], base_state == 'available')
                            if base_state == 'available':
                                self.assertEqual(software_backends.require_available(selection, catalog)['single_backend'], 'jianying')
                            else:
                                with self.assertRaises(ValueError):
                                    software_backends.require_available(selection, catalog)
            finally:
                asyncio.run(service.close())
        self.spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
