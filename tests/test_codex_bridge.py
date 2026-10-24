"""Protocol tests use an owned fake JSONL server; no real threads or paid turns are created."""
import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jianying_engine import codex_bridge

FAKE_SERVER = r'''
import json, sys

def send(value):
    print(json.dumps(value), flush=True)

def result(request, value):
    send({'id': request['id'], 'result': value})

initialized = False
pair = []
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        assert request['params']['clientInfo']['name'] == 'jianying_workbench'
        result(request, {'userAgent': 'fake-cli'})
    elif method == 'initialized':
        initialized = True
    elif method == 'model/list':
        assert initialized
        if request['params'].get('cursor') == 'page-two':
            result(request, {'data': [{'id': 'second', 'model': 'second'}], 'nextCursor': None})
        else:
            result(request, {'data': [{'id': 'first', 'model': 'first'}], 'nextCursor': 'page-two'})
    elif method == 'account/read':
        assert request['params'] == {'refreshToken': False}
        sys.stderr.write('Bearer fake-private-token email=secret@example.invalid\n'); sys.stderr.flush()
        result(request, {'account': {'type': 'chatgpt', 'email': 'secret@example.invalid'}, 'requiresOpenaiAuth': True})
    elif method in ('thread/start', 'thread/resume'):
        params = request['params']
        assert params['sandbox'] == 'danger-full-access'
        assert params['approvalPolicy'] == 'never'
        assert params['approvalsReviewer'] == 'user'
        if method == 'thread/start':
            assert params['developerInstructions'] == 'safe developer instructions'
        if method == 'thread/resume' and 'developerInstructions' in params:
            assert params['developerInstructions'] == 'editing only; background handoff'
        result(request, {'thread': {'id': 'fake-thread', 'turns': []}})
    elif method == 'turn/start':
        assert request['params']['approvalPolicy'] == 'never'
        assert request['params']['sandboxPolicy'] == {'type':'dangerFullAccess'}
        result(request, {'turn': {'id': 'fake-turn', 'status': 'inProgress'}})
        send({'id': 'approval-string', 'method': 'item/fileChange/requestApproval',
              'params': {'threadId': 'fake-thread', 'turnId': 'fake-turn', 'itemId': 'file-1'}})
        send({'id': 2, 'method': 'execCommandApproval', 'params': {'conversationId': 'fake-thread', 'callId': 'legacy'}})
        send({'method': 'test/input', 'params': request['params']})
        send({'method': 'item/agentMessage/delta', 'params': {'delta': 'hello'}})
    elif method == 'turn/interrupt':
        result(request, {})
        send({'method': 'turn/completed', 'params': {'threadId': 'fake-thread', 'turn': {'id': 'fake-turn', 'status': 'interrupted'}}})
    elif method == 'test/pair':
        pair.append(request)
        if len(pair) == 2:
            for pending in reversed(pair):
                result(pending, pending['params'])
            pair = []
    elif method == 'test/unknown':
        send({'id': 'unknown', 'method': 'account/chatgptAuthTokens/refresh', 'params': {'sensitive': 'never echoed'}})
        result(request, {})
    elif method == 'test/hang':
        pass
    elif method == 'test/writer-conflict':
        send({'id':request['id'], 'error':{'code':-32600, 'message':'thread 01a154c2-69b4-7463-98a1-6545542b17c8 already has an active writer'}})
    elif method == 'test/eof':
        sys.exit(0)
    elif method == 'test/malformed':
        print('SECRET not JSON', flush=True)
    elif method == 'test/account-notification':
        send({'method': 'account/updated', 'params': {'email': 'secret@example.invalid'}})
        result(request, {})
    elif 'error' in request:
        send({'method': 'test/unknownRejected', 'params': {'code': request['error']['code']}})
    elif 'result' in request:
        send({'method': 'test/response', 'params': request})
        send({'method': 'serverRequest/resolved', 'params': {'requestId': request['id']}})
    else:
        send({'id': request.get('id'), 'error': {'code': -32601, 'message': 'fake failure'}})
'''


class CodexBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='codex-bridge-test-')
        self.root = Path(self.directory.name)
        self.server = self.root / 'server.py'
        self.server.write_text(FAKE_SERVER, encoding='utf-8')
        original_spawn = codex_bridge._spawn
        self.spawn_patch = patch.object(codex_bridge, '_spawn', side_effect=lambda command: original_spawn([sys.executable, '-u', str(self.server)]))
        self.version_patch = patch.object(codex_bridge, '_version', return_value='mock-0.162.1')
        self.spawn_patch.start(); self.version_patch.start()
        self.events = []
        self.bridge = codex_bridge.CodexBridge(self.events.append, codex_path=sys.executable)
        self.info = await self.bridge.connect()

    async def asyncTearDown(self):
        await self.bridge.close()
        self.spawn_patch.stop(); self.version_patch.stop()
        self.directory.cleanup()

    async def event(self, method=None, kind=None):
        async with asyncio.timeout(3):
            while True:
                for event in self.events:
                    if (method is None or event.get('method') == method) and (kind is None or event.get('kind') == kind):
                        return event
                await asyncio.sleep(0.01)

    async def test_handshake_pagination_and_safe_account(self):
        self.assertEqual([m['id'] for m in self.info['models']], ['first', 'second'])
        self.assertEqual(self.info['account'], {'authenticated': True})
        self.assertNotIn('secret@example', json.dumps(self.info))
        await self.bridge._request('test/account-notification', {})
        self.assertEqual((await self.event('account/updated'))['params'], {})
        async with asyncio.timeout(3):
            while self.bridge.stderr_summary['bytes'] == 0:
                await asyncio.sleep(0.01)
        self.assertEqual(set(self.bridge.stderr_summary), {'bytes', 'lines'})
        self.assertEqual(await self.bridge.connect(), self.info)

    async def test_mock_turn_images_approval_and_interrupt(self):
        thread = await self.bridge.start_thread(self.root, 'safe developer instructions')
        self.assertEqual(thread, 'fake-thread')
        resumed = await self.bridge.resume_thread(thread, self.root)
        self.assertEqual(resumed['thread']['id'], thread)
        self.bridge.set_instructions(thread, 'editing only; background handoff')
        self.assertEqual((await self.bridge.resume_thread(thread, self.root))['thread']['id'], thread)
        image = self.root / 'fake.png'; image.write_bytes(b'fixture')
        turn = await self.bridge.start_turn(thread, 'hello', model='first', effort='high', images=[str(image)])
        self.assertEqual(turn['id'], 'fake-turn')
        request = await self.event('item/fileChange/requestApproval', 'request')
        self.assertEqual(request['id'], 'approval-string')
        await self.event('execCommandApproval', 'request')
        self.assertEqual(len(self.bridge._server_requests), 2)
        self.assertFalse(any(e.get('method') == 'test/response' for e in self.events))
        inputs = (await self.event('test/input'))['params']
        self.assertEqual(inputs['input'][1], {'type': 'localImage', 'path': str(image)})
        self.assertEqual(inputs['model'], 'first'); self.assertEqual(inputs['effort'], 'high')
        await self.bridge.respond(2, {'decision': 'denied'})
        reply = await self.event('test/response')
        self.assertEqual(reply['params']['id'], 2)
        with self.assertRaises(ValueError):
            await self.bridge.respond(2, {'decision': 'approved'})
        await self.bridge.interrupt(thread, turn['id'])
        await self.event('turn/completed')
        self.assertFalse(self.bridge._server_requests)

    async def test_approval_explicit_reply_and_async_consumer(self):
        handled = asyncio.Event()
        async def consumer(event):
            self.events.append(event)
            if event.get('kind') == 'request':
                await self.bridge.respond(event['id'], {'decision': 'decline'})
                handled.set()
        self.bridge.on_event = consumer
        await self.bridge.start_turn('fake-thread', 'fake turn only')
        await asyncio.wait_for(handled.wait(), 3)
        await self.event('test/response')

    async def test_out_of_order_independent_futures_and_timeout(self):
        one, two = await asyncio.gather(self.bridge._request('test/pair', {'value': 1}),
                                       self.bridge._request('test/pair', {'value': 2}))
        self.assertEqual((one, two), ({'value': 1}, {'value': 2}))
        self.bridge.request_timeout = 0.05
        with self.assertRaises(asyncio.TimeoutError):
            await self.bridge._request('test/hang', {})
        self.assertFalse(self.bridge._pending)
        self.bridge.request_timeout = 3
        with self.assertRaises(RuntimeError):
            await self.bridge._request('unavailable', {})
        self.assertFalse(self.bridge._pending)

    async def test_unknown_server_request_rejected_not_approved(self):
        await self.bridge._request('test/unknown', {})
        rejected = await self.event('test/unknownRejected')
        self.assertEqual(rejected['params']['code'], -32601)
        event = await self.event(kind='unsupported_request')
        self.assertNotIn('params', event)
        self.assertFalse(self.bridge._server_requests)

    async def test_writer_conflict_is_typed_and_actionable(self):
        with self.assertRaises(RuntimeError) as caught:
            await self.bridge._request('test/writer-conflict', {})
        self.assertTrue(getattr(caught.exception, 'active_writer_conflict', False))
        self.assertIn('占用', str(caught.exception))
        other = codex_bridge.CodexRpcError(-32600, 'provider error Bearer private-value')
        self.assertFalse(other.active_writer_conflict)
        self.assertNotIn('private-value', str(other))

    async def test_unattended_policy_covers_new_resumed_forked_and_existing_turns(self):
        calls=[]
        async def request(method,params):
            calls.append((method,params))
            return {'thread':{'id':'fake-thread'},'turn':{'id':'fake-turn'}}
        with patch.object(self.bridge,'_request',side_effect=request):
            await self.bridge.start_thread(self.root,'safe developer instructions')
            await self.bridge.resume_thread('fake-thread',self.root)
            await self.bridge.fork_thread('fake-thread',self.root)
            await self.bridge.start_turn('fake-thread','editing only')
        self.assertEqual(len(calls),4)
        for method,params in calls:
            self.assertEqual(params.get('approvalPolicy'),'never',method)
            if method=='turn/start':
                self.assertEqual(params['sandboxPolicy'],{'type':'dangerFullAccess'})
            else:
                self.assertEqual(params['sandbox'],'danger-full-access',method)

    async def test_eof_clears_pending_and_owned_process(self):
        process = self.bridge._process
        with self.assertRaises(ConnectionError):
            await self.bridge._request('test/eof', {})
        await self.event(kind='connection_closed')
        self.assertFalse(self.bridge._pending)
        self.assertFalse(self.bridge._server_requests)
        await self.bridge.close()
        self.assertIsNotNone(process.poll())

    async def test_invalid_json_disconnects_without_echoing_data(self):
        with self.assertRaises(ConnectionError):
            await self.bridge._request('test/malformed', {})
        closed = await self.event(kind='connection_closed')
        self.assertNotIn('SECRET', json.dumps(closed))

    async def test_cancellation_cleans_rpc_and_close_is_idempotent(self):
        pending = asyncio.create_task(self.bridge._request('test/hang', {}))
        await asyncio.sleep(0.02)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.bridge._pending)
        process = self.bridge._process
        await asyncio.gather(self.bridge.close(), self.bridge.close())
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
