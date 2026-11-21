"""Isolated unit tests for upstream_guard (upstream-check, contract-check, upstream-upgrade) and EffectCraft footage import."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from openedit_engine import api

SCRIPTS = str(api.RUNTIME / 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import effectcraft_backend
import upstream_guard


class _FakeHttpResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class _FakeContractMcp:
    def __init__(self, tools, commands):
        self._tools = list(tools)
        self._commands = list(commands)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def list_tools(self):
        return [{'name': name} for name in self._tools]

    def call_tool(self, name, arguments=None):
        del arguments
        if name in ('command_list', 'list_commands'):
            return [{'id': cid} for cid in self._commands]
        return {}


class UpstreamGuardTests(unittest.TestCase):
    def _mock_backends(self, fc_ver='0.20.1', ec_ver='0.24.1'):
        return {
            'filmcraft': {
                'id': 'filmcraft', 'installed': True, 'version': fc_ver,
                'path': 'C:/tools/filmcraft-cli.exe',
                'verified_baseline': '0.20.1', 'verified_range': '>=0.20.0,<0.21.0',
                'version_status': 'verified', 'warning': None,
            },
            'effectcraft': {
                'id': 'effectcraft', 'installed': True, 'version': ec_ver,
                'path': 'C:/tools/effectcraft-cli.exe',
                'verified_baseline': '0.24.1', 'verified_range': '>=0.24.0,<0.25.0',
                'version_status': 'verified', 'warning': None,
            },
            'ffmpeg': {'id': 'ffmpeg', 'installed': True, 'version': '9.0.2', 'path': 'C:/tools/ffmpeg.exe'},
        }

    def test_upstream_check_up_to_date_and_read_only_state(self):
        def fake_urlopen(req, timeout=15):
            del timeout
            if 'storytold/filmcraft' in req.full_url:
                return _FakeHttpResponse({
                    'tag_name': 'v0.20.1', 'name': 'FilmCraft v0.20.1',
                    'published_at': '2026-11-12T00:26:09Z',
                    'html_url': 'https://github.com/storytold/filmcraft/releases/tag/v0.20.1',
                    'body': 'Patch notes',
                })
            return _FakeHttpResponse({
                'tag_name': 'v0.24.1', 'name': 'EffectCraft v0.24.1',
                'published_at': '2026-11-12T00:23:40Z',
                'html_url': 'https://github.com/storytold/effectcraft/releases/tag/v0.24.1',
                'body': 'Patch notes',
            })

        with tempfile.TemporaryDirectory(prefix='oe-upstream-') as tmp:
            state_file = Path(tmp) / 'state.json'
            with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                    patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                res = upstream_guard.upstream_check(state_path=state_file, save_state=False)
            self.assertEqual(res['status'], 'ok')
            self.assertFalse(res['any_update_available'])
            self.assertEqual(res['apps']['filmcraft']['update_kind'], 'none')
            self.assertEqual(res['apps']['effectcraft']['update_kind'], 'none')
            self.assertEqual(res['recommended_next_steps'], [])
            self.assertFalse(res['state']['saved'])
            self.assertFalse(state_file.exists())

    def test_upstream_check_patch_minor_and_state_change_tracking(self):
        releases = {
            'filmcraft': {'tag_name': 'v0.20.2', 'published_at': '2026-11-21T00:00:00Z', 'body': 'Fix alpha'},
            'effectcraft': {'tag_name': 'v0.25.0', 'published_at': '2026-11-21T00:00:00Z', 'body': 'Breaking MCP'},
        }

        def fake_urlopen(req, timeout=15):
            del timeout
            key = 'filmcraft' if 'storytold/filmcraft' in req.full_url else 'effectcraft'
            item = releases[key]
            return _FakeHttpResponse({
                'tag_name': item['tag_name'],
                'name': item['tag_name'],
                'published_at': item['published_at'],
                'html_url': f'https://github.com/storytold/{key}/releases/tag/{item["tag_name"]}',
                'body': item['body'],
            })

        with tempfile.TemporaryDirectory(prefix='oe-upstream-state-') as tmp:
            state_file = Path(tmp) / 'nested' / 'state.json'
            with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                    patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                first = upstream_guard.upstream_check(state_path=state_file, save_state=True)
            self.assertTrue(first['state']['saved'])
            self.assertTrue(state_file.is_file())
            self.assertTrue(first['any_update_available'])
            self.assertEqual(first['apps']['filmcraft']['update_kind'], 'patch')
            self.assertEqual(first['apps']['filmcraft']['latest_vs_baseline']['version_status'], 'verified')
            self.assertEqual(first['apps']['effectcraft']['update_kind'], 'minor')
            self.assertEqual(first['apps']['effectcraft']['latest_vs_baseline']['version_status'], 'unverified_newer')
            self.assertTrue(first['apps']['effectcraft']['latest_vs_baseline']['warning'])
            self.assertTrue(first['apps']['filmcraft']['first_recorded'])
            self.assertFalse(first['any_changed_since_last_check'])
            self.assertTrue(first['recommended_next_steps'])

            releases['filmcraft']['tag_name'] = 'v0.20.3'
            with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                    patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                second = upstream_guard.upstream_check(state_path=state_file, save_state=True)
            self.assertTrue(second['any_changed_since_last_check'])
            self.assertTrue(second['apps']['filmcraft']['changed_since_last_check'])
            self.assertFalse(second['apps']['effectcraft']['changed_since_last_check'])

    def test_upstream_check_handles_network_errors_without_clobbering_state(self):
        with tempfile.TemporaryDirectory(prefix='oe-upstream-err-') as tmp:
            state_file = Path(tmp) / 'state.json'
            state_file.write_text(json.dumps({
                'schema_version': 1,
                'checked_at': '2026-11-20T00:00:00Z',
                'last_seen_tags': {'filmcraft': 'v0.20.1', 'effectcraft': 'v0.24.1'},
            }), encoding='utf-8')

            def fail_filmcraft(req, timeout=15):
                del timeout
                if 'storytold/filmcraft' in req.full_url:
                    raise urllib.error.HTTPError(req.full_url, 503, 'Unavailable', {}, io.BytesIO(b''))
                return _FakeHttpResponse({'tag_name': 'v0.24.2', 'name': 'v0.24.2', 'published_at': '2026-11-21Z'})

            with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                    patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fail_filmcraft):
                partial = upstream_guard.upstream_check(state_path=state_file, save_state=True)
            self.assertEqual(partial['status'], 'partial')
            self.assertFalse(partial['apps']['filmcraft']['fetch_ok'])
            self.assertTrue(partial['apps']['effectcraft']['fetch_ok'])
            saved = json.loads(state_file.read_text(encoding='utf-8'))
            self.assertEqual(saved['last_seen_tags']['filmcraft'], 'v0.20.1')
            self.assertEqual(saved['last_seen_tags']['effectcraft'], 'v0.24.2')

            with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                    patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')):
                failed = upstream_guard.upstream_check(state_path=state_file, save_state=True)
            self.assertEqual(failed['status'], 'failed')
            self.assertFalse(failed['state']['saved'])

    def test_contract_check_detects_missing_tools_and_commands(self):
        def fake_mcp(cmd, **kwargs):
            del kwargs
            if 'filmcraft' in cmd[0]:
                return _FakeContractMcp(upstream_guard.FILMCRAFT_REQUIRED_TOOLS, upstream_guard.FILMCRAFT_REQUIRED_COMMANDS)
            return _FakeContractMcp(upstream_guard.EFFECTCRAFT_REQUIRED_TOOLS, upstream_guard.EFFECTCRAFT_REQUIRED_COMMANDS)

        with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                patch.object(upstream_guard.filmcraft_backend, 'find_cli', return_value='C:/tools/filmcraft-cli.exe'), \
                patch.object(upstream_guard.effectcraft_backend, 'find_cli', return_value='C:/tools/effectcraft-cli.exe'), \
                patch.object(upstream_guard.craft_rpc, 'CraftMcpClient', side_effect=fake_mcp):
            ok_res = upstream_guard.contract_check(skip_e2e=True)
        self.assertEqual(ok_res['status'], 'passed')
        self.assertEqual(ok_res['filmcraft']['missing_tools'], [])
        self.assertEqual(ok_res['effectcraft']['missing_commands'], [])

        def broken_mcp(cmd, **kwargs):
            del kwargs
            if 'filmcraft' in cmd[0]:
                tools = [t for t in upstream_guard.FILMCRAFT_REQUIRED_TOOLS if t != 'render_frame']
                return _FakeContractMcp(tools, upstream_guard.FILMCRAFT_REQUIRED_COMMANDS)
            cmds = [c for c in upstream_guard.EFFECTCRAFT_REQUIRED_COMMANDS if c != 'layer.addItem']
            return _FakeContractMcp(upstream_guard.EFFECTCRAFT_REQUIRED_TOOLS, cmds)

        with patch.object(upstream_guard.software_detection, 'detect_backends', return_value=self._mock_backends()), \
                patch.object(upstream_guard.filmcraft_backend, 'find_cli', return_value='C:/tools/filmcraft-cli.exe'), \
                patch.object(upstream_guard.effectcraft_backend, 'find_cli', return_value='C:/tools/effectcraft-cli.exe'), \
                patch.object(upstream_guard.craft_rpc, 'CraftMcpClient', side_effect=broken_mcp):
            bad_res = upstream_guard.contract_check(skip_e2e=True)
        self.assertEqual(bad_res['status'], 'failed')
        self.assertEqual(bad_res['filmcraft']['missing_tools'], ['render_frame'])
        self.assertEqual(bad_res['effectcraft']['missing_commands'], ['layer.addItem'])

    def test_effectcraft_footage_layer_uses_file_import_and_add_item(self):
        calls = []

        class _FakeClient:
            def call_tool(self, name, params=None):
                calls.append((name, params))
                cmd = (params or {}).get('command')
                if cmd == 'file.import':
                    return {'items': [7], 'errors': []}
                if cmd == 'layer.addItem':
                    return {'layer': 'L_9'}
                return {}

        with tempfile.TemporaryDirectory(prefix='oe-footage-') as tmp:
            clip = Path(tmp) / 'plate.png'
            clip.write_bytes(b'png')
            layer_id = effectcraft_backend._add_declarative_layer(
                _FakeClient(), 'Main', {'type': 'footage', 'path': str(clip)}, 640, 360, 'Noto Sans SC',
            )
        self.assertEqual(layer_id, 'L_9')
        self.assertEqual(calls[0], ('execute_command', {'command': 'file.import', 'params': {'paths': [str(clip.resolve())]}}))
        self.assertEqual(calls[1], ('execute_command', {'command': 'layer.addItem', 'params': {'item': 7}}))

        class _ErrClient:
            def call_tool(self, name, params=None):
                del name, params
                return {'items': [], 'errors': ['bad file']}

        with self.assertRaisesRegex(RuntimeError, 'file.import failed'):
            effectcraft_backend._add_declarative_layer(
                _ErrClient(), 'Main', {'type': 'footage', 'path': 'plate.png'}, 640, 360, 'Noto Sans SC',
            )

    def test_api_schemas_and_commands_for_new_operations(self):
        cc_args = api.arguments_from_params('contract-check', {'out': 'out-dir', 'skip_e2e': True})
        self.assertEqual(cc_args, ['--out', 'out-dir', '--skip-e2e'])
        self.assertTrue(api.command_for('contract-check', cc_args))

        uc_args = api.arguments_from_params('upstream-check', {'state': 's.json', 'save_state': True, 'timeout': 10})
        self.assertEqual(uc_args, ['--state', 's.json', '--save-state', '--timeout', '10'])
        self.assertTrue(api.command_for('upstream-check', uc_args))

        uu_args = api.arguments_from_params('upstream-upgrade', {
            'state': 's.json', 'tools_dir': 'tools', 'min_age_hours': 24,
            'keep_versions': 2, 'dry_run': True, 'retry_failed': True, 'timeout': 30,
        })
        self.assertEqual(uu_args, [
            '--state', 's.json', '--tools-dir', 'tools', '--min-age-hours', '24',
            '--keep-versions', '2', '--dry-run', '--retry-failed', '--timeout', '30',
        ])
        self.assertTrue(api.command_for('upstream-upgrade', uu_args))

    @staticmethod
    def _make_portable_zip(app_id, version):
        exe_name = f'{app_id}-cli.exe' if os.name == 'nt' else f'{app_id}-cli'
        arch = upstream_guard._windows_arch()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr(f'{app_id}-{version}-windows-{arch}-portable/{exe_name}', b'fake-cli')
        raw = buf.getvalue()
        return raw, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _seed_tools(root, fc_versions=('0.19.0', '0.20.1'), ec_versions=('0.24.1',)):
        fc_exe = 'filmcraft-cli.exe' if os.name == 'nt' else 'filmcraft-cli'
        ec_exe = 'effectcraft-cli.exe' if os.name == 'nt' else 'effectcraft-cli'
        for ver in fc_versions:
            p = root / f'filmcraft-{ver}' / f'filmcraft-{ver}-windows-x64-portable' / fc_exe
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'old')
        for ver in ec_versions:
            p = root / f'effectcraft-{ver}' / f'effectcraft-{ver}-windows-x64-portable' / ec_exe
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'old')

    def test_safe_extract_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory(prefix='oe-zipslip-') as tmp:
            zpath = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(zpath, 'w') as zf:
                zf.writestr('../escape.txt', b'evil')
            with self.assertRaisesRegex(RuntimeError, '路径穿越'):
                upstream_guard._safe_extract_zip(zpath, Path(tmp) / 'out')

    def test_upstream_upgrade_happy_path_prunes_and_records_rollback(self):
        zip_bytes, zip_sha = self._make_portable_zip('filmcraft', '0.21.0')
        arch = upstream_guard._windows_arch()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')

        class _BinaryResp:
            def __init__(self, data):
                self._buf = io.BytesIO(data)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, n=-1):
                return self._buf.read(n)

        def fake_urlopen(req, timeout=30):
            del timeout
            if req.full_url.endswith('.zip'):
                return _BinaryResp(zip_bytes)
            if 'storytold/filmcraft' in req.full_url:
                return _FakeHttpResponse({
                    'tag_name': 'v0.21.0', 'name': 'v0.21.0', 'published_at': old_time,
                    'html_url': 'https://github.com/storytold/filmcraft/releases/tag/v0.21.0',
                    'assets': [{
                        'name': f'filmcraft-0.21.0-windows-{arch}-portable.zip',
                        'digest': f'sha256:{zip_sha}',
                        'browser_download_url': f'https://github.com/storytold/filmcraft/releases/download/v0.21.0/filmcraft-0.21.0-windows-{arch}-portable.zip',
                    }],
                })
            return _FakeHttpResponse({
                'tag_name': 'v0.24.1', 'name': 'v0.24.1', 'published_at': old_time,
                'html_url': 'https://github.com/storytold/effectcraft/releases/tag/v0.24.1',
                'assets': [],
            })

        with tempfile.TemporaryDirectory(prefix='oe-upgrade-ok-') as tmp:
            tools_dir = Path(tmp) / 'tools'
            self._seed_tools(tools_dir)
            state_file = tools_dir / 'upstream-state.json'
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    patch.object(upstream_guard, 'contract_check', return_value={'status': 'passed', 'e2e': {'elapsed_seconds': 1.2}}):
                res = upstream_guard.upstream_upgrade(
                    state_path=state_file, tools_dir=tools_dir, min_age_hours=24, keep_versions=2,
                )
            self.assertEqual(res['status'], 'upgraded')
            self.assertEqual(res['upgraded'], ['filmcraft'])
            self.assertEqual(res['blocked'], [])
            self.assertTrue((tools_dir / 'filmcraft-0.21.0').is_dir())
            self.assertTrue((tools_dir / 'filmcraft-0.20.1').is_dir())
            self.assertFalse((tools_dir / 'filmcraft-0.19.0').exists())
            self.assertEqual(res['rollback_versions']['filmcraft'], ['filmcraft-0.21.0', 'filmcraft-0.20.1'])
            state_data = json.loads(state_file.read_text(encoding='utf-8'))
            self.assertEqual(state_data['verified_versions']['filmcraft']['version'], '0.21.0')
            self.assertEqual(state_data['verified_versions']['filmcraft']['zip_sha256'], zip_sha)

    def test_upstream_upgrade_checksum_mismatch_aborts_before_install(self):
        zip_bytes, _ = self._make_portable_zip('filmcraft', '0.21.0')
        arch = upstream_guard._windows_arch()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')

        class _BinaryResp:
            def __init__(self, data):
                self._buf = io.BytesIO(data)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, n=-1):
                return self._buf.read(n)

        def fake_urlopen(req, timeout=30):
            del timeout
            if req.full_url.endswith('.zip'):
                return _BinaryResp(zip_bytes)
            if 'storytold/filmcraft' in req.full_url:
                return _FakeHttpResponse({
                    'tag_name': 'v0.21.0', 'published_at': old_time,
                    'assets': [{
                        'name': f'filmcraft-0.21.0-windows-{arch}-portable.zip',
                        'digest': 'sha256:' + ('0' * 64),
                        'browser_download_url': f'https://github.com/storytold/filmcraft/releases/download/v0.21.0/filmcraft-0.21.0-windows-{arch}-portable.zip',
                    }],
                })
            return _FakeHttpResponse({'tag_name': 'v0.24.1', 'published_at': old_time, 'assets': []})

        with tempfile.TemporaryDirectory(prefix='oe-upgrade-sha-') as tmp:
            tools_dir = Path(tmp) / 'tools'
            self._seed_tools(tools_dir, fc_versions=('0.20.1',))
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                res = upstream_guard.upstream_upgrade(tools_dir=tools_dir)
            self.assertEqual(res['status'], 'blocked')
            self.assertEqual(res['apps']['filmcraft']['action'], 'error_checksum')
            self.assertFalse((tools_dir / 'filmcraft-0.21.0').exists())

    def test_upstream_upgrade_blocks_contract_failure_skips_repeat_and_supports_cooldown(self):
        zip_bytes, zip_sha = self._make_portable_zip('filmcraft', '0.21.0')
        arch = upstream_guard._windows_arch()
        fresh_time = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
        pub_time = fresh_time

        class _BinaryResp:
            def __init__(self, data):
                self._buf = io.BytesIO(data)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, n=-1):
                return self._buf.read(n)

        def fake_urlopen(req, timeout=30):
            del timeout
            if req.full_url.endswith('.zip'):
                return _BinaryResp(zip_bytes)
            if 'storytold/filmcraft' in req.full_url:
                return _FakeHttpResponse({
                    'tag_name': 'v0.21.0', 'published_at': pub_time,
                    'assets': [{
                        'name': f'filmcraft-0.21.0-windows-{arch}-portable.zip',
                        'digest': f'sha256:{zip_sha}',
                        'browser_download_url': f'https://github.com/storytold/filmcraft/releases/download/v0.21.0/filmcraft-0.21.0-windows-{arch}-portable.zip',
                    }],
                })
            return _FakeHttpResponse({'tag_name': 'v0.24.1', 'published_at': old_time, 'assets': []})

        with tempfile.TemporaryDirectory(prefix='oe-upgrade-blk-') as tmp:
            tools_dir = Path(tmp) / 'tools'
            self._seed_tools(tools_dir, fc_versions=('0.20.1',))
            state_file = tools_dir / 'upstream-state.json'

            # 1) Dry run writes zero files.
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                dry = upstream_guard.upstream_upgrade(state_path=state_file, tools_dir=tools_dir, dry_run=True)
            self.assertEqual(dry['status'], 'dry_run_updates_available')
            self.assertFalse(state_file.exists())

            # 2) Cooldown defers recent release without downloading.
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen):
                deferred = upstream_guard.upstream_upgrade(state_path=state_file, tools_dir=tools_dir, min_age_hours=24)
            self.assertEqual(deferred['status'], 'deferred')
            self.assertEqual(deferred['apps']['filmcraft']['action'], 'deferred_cooldown')

            # 3) Contract failure blocks install and persists failed_versions.
            pub_time = old_time
            failed_report = {
                'status': 'failed',
                'filmcraft': {'missing_tools': ['render_frame'], 'missing_commands': []},
                'e2e': {'status': 'skipped'},
            }
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    patch.object(upstream_guard, 'contract_check', return_value=failed_report):
                blocked = upstream_guard.upstream_upgrade(state_path=state_file, tools_dir=tools_dir, min_age_hours=24)
            self.assertEqual(blocked['status'], 'blocked')
            self.assertEqual(blocked['apps']['filmcraft']['action'], 'blocked_contract_failed')
            self.assertFalse((tools_dir / 'filmcraft-0.21.0').exists())

            # 4) Subsequent run skips re-download without calling contract_check.
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    patch.object(upstream_guard, 'contract_check', side_effect=AssertionError('should skip')):
                skipped = upstream_guard.upstream_upgrade(state_path=state_file, tools_dir=tools_dir, min_age_hours=24)
            self.assertEqual(skipped['status'], 'blocked')
            self.assertEqual(skipped['apps']['filmcraft']['action'], 'skipped_known_failure')

            # 5) retry_failed=True re-runs contract_check and upgrades if passing.
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    patch.object(upstream_guard, 'contract_check', return_value={'status': 'passed', 'e2e': {'elapsed_seconds': 0.5}}):
                retried = upstream_guard.upstream_upgrade(
                    state_path=state_file, tools_dir=tools_dir, min_age_hours=24, retry_failed=True,
                )
            self.assertEqual(retried['status'], 'upgraded')
            self.assertTrue((tools_dir / 'filmcraft-0.21.0').is_dir())

    def test_upstream_upgrade_partial_fallback_upgrades_only_passing_backend(self):
        fc_bytes, fc_sha = self._make_portable_zip('filmcraft', '0.21.0')
        ec_bytes, ec_sha = self._make_portable_zip('effectcraft', '0.25.0')
        arch = upstream_guard._windows_arch()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')

        class _BinaryResp:
            def __init__(self, data):
                self._buf = io.BytesIO(data)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, n=-1):
                return self._buf.read(n)

        def fake_urlopen(req, timeout=30):
            del timeout
            if 'filmcraft-0.21.0' in req.full_url:
                return _BinaryResp(fc_bytes)
            if 'effectcraft-0.25.0' in req.full_url:
                return _BinaryResp(ec_bytes)
            if 'storytold/filmcraft' in req.full_url:
                return _FakeHttpResponse({
                    'tag_name': 'v0.21.0', 'published_at': old_time,
                    'assets': [{
                        'name': f'filmcraft-0.21.0-windows-{arch}-portable.zip',
                        'digest': f'sha256:{fc_sha}',
                        'browser_download_url': f'https://github.com/storytold/filmcraft/releases/download/v0.21.0/filmcraft-0.21.0-windows-{arch}-portable.zip',
                    }],
                })
            return _FakeHttpResponse({
                'tag_name': 'v0.25.0', 'published_at': old_time,
                'assets': [{
                    'name': f'effectcraft-0.25.0-windows-{arch}-portable.zip',
                    'digest': f'sha256:{ec_sha}',
                    'browser_download_url': f'https://github.com/storytold/effectcraft/releases/download/v0.25.0/effectcraft-0.25.0-windows-{arch}-portable.zip',
                }],
            })

        def fake_contract_check(*, filmcraft_cli=None, effectcraft_cli=None, skip_e2e=False, out_dir=None):
            del skip_e2e, out_dir
            if effectcraft_cli is not None:
                return {
                    'status': 'failed',
                    'effectcraft': {'missing_tools': [], 'missing_commands': ['layer.newText']},
                    'e2e': {'status': 'skipped'},
                }
            return {'status': 'passed', 'e2e': {'elapsed_seconds': 0.9}}

        with tempfile.TemporaryDirectory(prefix='oe-upgrade-partial-') as tmp:
            tools_dir = Path(tmp) / 'tools'
            self._seed_tools(tools_dir, fc_versions=('0.20.1',), ec_versions=('0.24.1',))
            with patch.object(upstream_guard.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    patch.object(upstream_guard, 'contract_check', side_effect=fake_contract_check):
                res = upstream_guard.upstream_upgrade(tools_dir=tools_dir)
            self.assertEqual(res['status'], 'partial')
            self.assertEqual(res['upgraded'], ['filmcraft'])
            self.assertEqual(res['blocked'], ['effectcraft'])
            self.assertTrue((tools_dir / 'filmcraft-0.21.0').is_dir())
            self.assertFalse((tools_dir / 'effectcraft-0.25.0').exists())


if __name__ == '__main__':
    unittest.main()
