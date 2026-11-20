"""Workbench-owned review, once per install and runtime source identity. No native code runs in the request thread.

The durable claim is written before spawn: a crash is an interrupted attempt, never
an automatic retry. An OS lock spans the worker lifetime, including pipe draining.
Only the Popen/tree created here may be terminated; saved PIDs are never killed.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from .api import RUNTIME, command_for
from .jobs import _spawn, _capture
from .runtime.scripts import file_hash, runtime_common

FEATURES = set(runtime_common.RUNTIME_FEATURES)

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


digest = file_hash.sha256


def source_identity():
    """Same inventory as runtime_admission.source_identity/requalify.source_snapshot."""
    return runtime_common.runtime_source_identity(RUNTIME)


def source_digest(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def reference_inventory(candidate, baseline):
    """Scheduling hint only; the worker verifies actual reference identities before use."""
    data = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'JianyingEngine'
    paths = [candidate.parent / baseline, data / 'references' / baseline]
    required = set()
    for contract in (RUNTIME / 'assets').glob('*.json'):
        try:
            spec = read_json(contract)
            if spec.get('runtime_version') == baseline:
                required.update(spec.get('runtime_sha256', {}))
                if spec.get('font_relative'):
                    required.add(spec['font_relative'])
            if contract.name == 'native-preview-' + baseline + '.json':
                required.update(spec.get('files', {}))
                if spec.get('runtime', {}).get('file'):
                    required.add(spec['runtime']['file'])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    try:
        recorded = read_json(RUNTIME / 'assets' / ('native-preview-' + baseline + '.json')).get('install')
        if recorded:
            paths.append(Path(recorded))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    inventory = []
    for path in paths:
        for rel in [None, *sorted(value for value in required if isinstance(value, str))]:
            if rel is not None and (Path(rel).is_absolute() or '..' in Path(rel).parts):
                continue
            target = path if rel is None else path / rel
            try:
                info = target.stat()
                inventory.append((str(target.resolve()).casefold(), info.st_mtime_ns, info.st_ctime_ns,
                                  info.st_size, target.is_dir()))
            except OSError:
                inventory.append((str(target.absolute()).casefold(), None, None, None, False))
    return inventory


def execution_busy(store):
    """Check the manual/worker execution lock without keeping it or touching another process."""
    lock_path = Path(store).resolve().parent / 'requalification-execution.lock'
    return runtime_common.is_file_locked(lock_path)


class StaleSource(ValueError):
    pass


def report_result(report_path, candidate):
    """Read eligibility separately from the committed admission transaction."""
    report = read_json(report_path)
    current_source = source_identity()
    if report.get('source_identity') != current_source:
        raise StaleSource('报告源码或合同身份已失效，需要按当前代码重新复核。')
    features = report['features']
    if (Path(report['candidate']).resolve() != candidate.resolve()
            or report.get('admission_state') != 'eligible_not_committed'
            or not isinstance(features, dict) or set(features) != FEATURES):
        raise ValueError('复核报告身份或结构无效')
    admitted = report['admitted']
    expected = {f for f, value in features.items() if value['status'] == 'admitted'}
    if not isinstance(admitted, list) or set(admitted) != expected:
        raise ValueError('复核报告子集清单不一致')
    refused = {f: v.get('reason', '未通过') for f, v in features.items() if f not in expected}
    if admitted:
        store = read_json(report['store'])
        if store.get('schema_version') != 1:
            raise ValueError('准入记录结构版本无效')
        record = store['versions'][candidate.name]
        if (Path(record['report']).resolve() != Path(report_path).resolve()
                or record['report_sha256'] != digest(report_path)):
            raise ValueError('报告未对应已提交的准入记录')
        for feature in admitted:
            files = report['identities'][feature]
            if not files or record['features'][feature] != files:
                raise ValueError('准入文件身份不一致')
            for relative, value in files.items():
                path = (candidate / relative).resolve()
                if not path.is_relative_to(candidate.resolve()) or digest(path) != value:
                    raise ValueError('已复核的运行文件发生变化')
            absent = report['features'][feature].get('absent_contract_files', [])
            if absent != record.get('absent_contract_files', {}).get(feature, []):
                raise ValueError('已替代的合同文件记录不一致')
            for missing in absent:
                missing_path = (candidate / missing).resolve()
                if not missing_path.is_relative_to(candidate.resolve()) or missing_path.exists():
                    raise ValueError('已替代的合同文件位置发生变化')
                directory = missing_path.parent
                actual = {p.relative_to(candidate.resolve()).as_posix() for p in directory.iterdir() if p.is_file()}
                expected_files = {str(Path(rel)).replace('\\', '/') for rel in files
                                  if (candidate / rel).resolve().parent == directory}
                if actual != expected_files:
                    raise ValueError('已复核的替代文件目录发生变化')
    return {'state': 'complete' if admitted and not refused else 'partial' if admitted else 'failed',
            'admitted': admitted, 'refused': refused, 'sequence': report.get('sequence'),
            'scope': report.get('preview_scope'), 'report': str(report_path),
            'source_identity_sha256': source_digest(current_source)}


class Requalification:
    def __init__(self):
        store = Path(os.environ.get('JIANYING_RUNTIME_ADMISSIONS') or
                     Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) /
                     'JianyingEngine/runtime-admissions.json')
        # All workbenches using one admission store share claims, even with different session roots.
        self.directory = store.parent / 'workbench-requalification'
        # Short, durable native paths; deleting these reports invalidates admission.
        self.evidence_directory = store.parent / 'jq'
        self.mutex = threading.RLock()
        self.result = {}
        self.thread = None
        self.process = self.tree = None
        self.closed = False
        self.checked_at = 0
        self.baseline = read_json(RUNTIME / 'assets/text-alpha-v1.json')['runtime_version']

    def observe(self, availability, busy=False):
        """Schedule a cheap observation; filesystem hashes and spawn stay off-loop."""
        with self.mutex:
            result = copy.deepcopy(self.result)
            if availability.get('installed') and availability.get('path') and availability.get('version'):
                if availability['version'] == self.baseline and Path(availability['path']).parent.name == self.baseline:
                    result = {}
                elif result.get('version') != availability['version'] or result.get('candidate') != str(Path(availability['path']).resolve().parent):
                    result = dict(state='needed', version=availability['version'], candidate=str(Path(availability['path']).resolve().parent))
            else:
                result = {}
                if availability.get('installed') and availability.get('path'):
                    result = dict(state='blocked', reason='无法读取剪映版本，不能确认准入；不会自动复核。')
            if not self.closed and time.monotonic() - self.checked_at >= 10 and (self.thread is None or not self.thread.is_alive()):
                if result.get('state') in ('complete', 'partial'):
                    result = {k: v for k, v in result.items() if k not in ('admitted', 'refused', 'sequence', 'scope')}
                    result.update(state='checking', reason='正在核对当前源码、合同和报告指纹。')
                    self.result = copy.deepcopy(result)
                self.checked_at = time.monotonic()
                self.thread = threading.Thread(target=self._observe, args=(dict(availability), busy), daemon=True)
                self.thread.start()
        return result

    def _publish(self, **result):
        with self.mutex:
            self.result = result

    def _observe(self, availability, busy):
        lock = None
        try:
            if not availability.get('installed') or not availability.get('path') or not availability.get('version'):
                self._publish()
                return
            candidate = Path(availability['path']).resolve().parent
            baseline = self.baseline
            if candidate.name == baseline and availability['version'] == baseline:
                self._publish()
                return
            source = source_digest(source_identity())
            identity = json.dumps([str(candidate).casefold(), availability['version'], source,
                                   reference_inventory(candidate, baseline)], ensure_ascii=False)
            key = hashlib.sha256(identity.encode()).hexdigest()[:20]
            self.directory.mkdir(parents=True, exist_ok=True)
            journal = self.directory / (key + '.json')
            status = dict(version=availability['version'], candidate=str(candidate), state='needed',
                          source_identity_sha256=source)
            # The lock covers all workbenches sharing this machine-local admission store.
            lock = runtime_common.acquire_file_lock(self.directory / 'worker.lock', blocking=False, pad_empty=False)
            if lock is None:
                self._publish(**dict(status, state='waiting', reason='已有工作台后台复核占用，等待其结果。'))
                return
            # Holding worker.lock proves no workbench-owned worker is still live.
            # Reconcile every old key before considering any committed report.
            for previous in self.directory.glob('*.json'):
                try:
                    prior = read_json(previous)
                    if prior.get('state') in ('running', 'claiming'):
                        prior.update(state='interrupted', reason='上次复核已中断；不会自动重试。')
                        self._mark_rejected_report(prior)
                        self._save(previous, prior)
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            store_path = Path(os.environ.get('JIANYING_RUNTIME_ADMISSIONS') or
                              Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') /
                              'JianyingEngine/runtime-admissions.json')
            def committed():
                if not store_path.exists():
                    return None
                record = read_json(store_path).get('versions', {}).get(candidate.name)
                if not record or self._rejected_report(record['report']):
                    return None
                return dict(status, **report_result(record['report'], candidate))

            if journal.exists():
                saved = read_json(journal)
                if saved['state'] in ('failed', 'blocked', 'waiting', 'interrupted'):
                    try:
                        result = committed()
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        result = None
                    if result and Path(result['report']).resolve() != Path(saved.get('report', '')).resolve():
                        self._save(journal, result)
                        self._publish(**result)
                        return
                if (saved['state'] == 'waiting' and not busy and availability.get('compatible')
                        and candidate.name == availability['version'] and not execution_busy(store_path)):
                    if saved.get('lock_retry_count', 0) >= 1:
                        saved.update(state='blocked', reason='复核执行锁再次被占用；等待其他复核完成或环境变化。')
                        self._save(journal, saved)
                    else:
                        saved.update(state='claiming', lock_retry_count=1, started=time.time(),
                                     report=str(self.evidence_directory / (key[:12] + '-retry') / 'requalify-report.json'))
                        self.evidence_directory.mkdir(parents=True, exist_ok=True)
                        self._save(journal, saved)
                        self._publish(**saved)
                        self._run(candidate, journal, saved, key + '-retry')
                        return
                # Revalidate completed results after restart or runtime drift.
                if saved['state'] in ('complete', 'partial'):
                    saved.update(report_result(saved['report'], candidate))
                self._publish(**saved)
                return
            # An existing externally committed review is evidence, not a new attempt.
            if store_path.exists():
                try:
                    result = committed()
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    status['reason'] = '既有复核报告无效，需要重新复核：' + str(exc)
                    self._publish(**status)
                else:
                    if result:
                        self._save(journal, result)
                        self._publish(**result)
                        return
            if busy:
                self._publish(**dict(status, state='deferred', reason='当前制作任务进行中，空闲后发起一次复核。'))
                return
            if candidate.name != availability['version'] or not availability.get('compatible'):
                self._publish(**dict(status, state='blocked', reason='安装版本或基础条件不满足，无法自动复核。'))
                return
            self.evidence_directory.mkdir(parents=True, exist_ok=True)
            status.update(state='claiming', started=time.time(), report=str(self.evidence_directory / key[:12] / 'requalify-report.json'))
            self._save(journal, status)
            self._publish(**status)
            self._run(candidate, journal, status, key)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._publish(state='failed', reason=str(exc), version=availability.get('version'),
                          candidate=str(Path(availability['path']).resolve().parent) if availability.get('path') else None)
        finally:
            runtime_common.release_file_lock(lock)

    @staticmethod
    def _save(path, status):
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(status, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    @staticmethod
    def _mark_rejected_report(status):
        try:
            report = Path(status['report'])
            if report.is_file():
                status['rejected_report_sha256'] = digest(report)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _rejected_report(self, report):
        path = Path(report).resolve()
        report_hash = digest(path)
        for journal in self.directory.glob('*.json'):
            try:
                saved = read_json(journal)
                if (saved.get('rejected_report_sha256') == report_hash and
                        Path(saved.get('report', '')).resolve() == path):
                    return True
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return False

    def _run(self, candidate, journal, status, key):
        readers = []
        try:
            command = command_for('requalify', ['--candidate', str(candidate), '--out', str(Path(status['report']).parent)])
            with self.mutex:
                if self.closed:
                    raise ValueError('工作台已退出，复核未启动。')
                self.process, self.tree = _spawn(command)
                process = self.process
            for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
                thread = threading.Thread(target=_capture, args=(stream, self.directory / (key + '.' + name), 1024 * 1024), daemon=True)
                thread.start()
                readers.append(thread)
            status.update(state='running')
            self._save(journal, status)
            self._publish(**status)
            try:
                code = process.wait(timeout=8 * 3600)
            except subprocess.TimeoutExpired:
                raise ValueError('后台复核超过八小时，已回收自有进程；不会自动重试。')
            status['returncode'] = code
            if code != 0:
                self._mark_rejected_report(status)
                for thread in readers:
                    thread.join()
                error_path = self.directory / (key + '.stderr')
                error = error_path.read_text(encoding='utf-8', errors='replace') if error_path.is_file() else ''
                if 'no verified measured install directory was found' in error:
                    status.update(state='blocked', reason='找不到已核验的剪映旧版本参考副本；恢复参考副本后可重新复核。')
                    return
                stdout_path = self.directory / (key + '.stdout')
                stdout = stdout_path.read_text(encoding='utf-8', errors='replace') if stdout_path.is_file() else ''
                if 'Another Jianying version review is already running' in stdout:
                    status.update(state='waiting', reason='手动或其他进程正在复核，等待其结果。')
                    return
                raise ValueError('复核子进程失败，退出码：' + str(code))
            result = report_result(status['report'], candidate)
            if result['source_identity_sha256'] != status['source_identity_sha256']:
                raise StaleSource('复核期间源码或合同发生变化，本次结果不能用于当前认领。')
            status.pop('reason', None)
            status.update(result)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._mark_rejected_report(status)
            status.update(state='interrupted' if self.closed else 'failed', reason=str(exc))
        finally:
            with self.mutex:
                if self.tree is not None:
                    self.tree.close()
                process = self.process
                self.process = self.tree = None
            if process is not None:
                process.wait()
            for thread in readers:
                thread.join()
            status['finished'] = time.time()
            self._save(journal, status)
            self._publish(**status)

    def close(self):
        with self.mutex:
            self.closed = True
            if self.tree is not None:
                self.tree.close()
            thread = self.thread
        if thread is not None:
            thread.join(timeout=10)


def reviewed_availability(availability, review):
    result = dict(availability)
    if review:
        state = review['state']
        labels = {'needed': '需要按当前源码复核', 'checking': '核对源码与报告中', 'claiming': '后台复核准备中', 'running': '后台复核中',
                  'waiting': '等待后台复核', 'deferred': '等待任务空闲后复核', 'blocked': '无法自动复核',
                  'interrupted': '后台复核已中断', 'failed': '后台复核失败',
                  'partial': '后台复核部分通过', 'complete': '后台复核完整子集通过'}
        detail = copy.deepcopy(review)
        detail['label'] = labels.get(state, '需要复核')
        detail['reason'] = review.get('reason') or '复核仅覆盖报告中的原生静帧子集；连续视听与导出未验证。'
        measured = 'admitted' in review and 'refused' in review
        detail['subsets'] = {
            feature: {'status': ('admitted' if feature in review['admitted'] else 'refused') if measured else 'unverified',
                      'reason': review.get('refused', {}).get(feature, '') if measured else '尚无有效复核结论'}
            for feature in sorted(FEATURES)
        }
        # Review is advisory UI data; installation checks and native guards retain authority.
        result['requalification'] = detail
    return result
