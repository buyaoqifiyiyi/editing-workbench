"""ACP v1 protocol checks use only a local fake Agent process; no real model prompts."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from jianying_engine.acp_bridge import AcpBridge
from jianying_engine import acp_bridge

SERVER = r'''
import json,os,sys
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
pending=None
cancel_requested=False
permission=None
options=[]
calls=[]
prompts=[]

def send(value):
    print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
def result(req,value): send({'id':req['id'],'result':value})
def update(value): send({'method':'session/update','params':{'sessionId':'session-1','update':value}})
def finish(reason='end_turn'):
    global pending
    result(pending,{'stopReason':reason}); pending=None
for line in sys.stdin:
 req=json.loads(line); assert req['jsonrpc']=='2.0'
 method=req.get('method'); p=req.get('params',{})
 if method: calls.append(method)
 if method=='initialize':
    assert p['protocolVersion']==1
    assert p['clientCapabilities']=={'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False}
    result(req,{'protocolVersion':2 if '--v2' in sys.argv else 1,
                'agentCapabilities':{'loadSession':'--no-load' not in sys.argv},
                'agentInfo':{'name':'fake-acp','version':'test-1'},'authMethods':[]})
 elif method in ('session/new','session/load'):
    assert os.path.isabs(p['cwd'])
    server=p['mcpServers'][0]
    assert server['name']=='jianying-engine'
    assert server['args']==['-m','jianying_engine.mcp_server'] and server['env']==[]
    if method=='session/load':
      update({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'OLD REPLAY DO NOT APPEND'}})
    result(req,{'sessionId':'session-1'})
 elif method=='session/prompt':
    pending=req
    text=p['prompt'][0]['text']
    prompts.append(text)
    if text=='eof': sys.exit(0)
    if text=='error':
      send({'id':req['id'],'error':{'code':-32000,'message':'fake private error'}});pending=None;continue
    if text=='hang': continue
    if text in ('permission','always-only','cancel-permission'):
      permission='approve-1'; options=[{'optionId':'remember','kind':'allow_always','name':'Always'}]
      if text!='always-only':options.append({'optionId':'once-original-id','kind':'allow_once','name':'Once'})
      options.append({'optionId':'no-once-original-id','kind':'reject_once','name':'Reject'})
      send({'id':permission,'method':'session/request_permission','params':{'sessionId':'session-1','toolCall':{'toolCallId':'tool-1'},'options':options}})
      continue
    update({'sessionUpdate':'agent_message_chunk','messageId':'m1','content':{'type':'text','text':'hello '}})
    update({'sessionUpdate':'agent_message_chunk','messageId':'m1','content':{'type':'text','text':'world'}})
    update({'sessionUpdate':'tool_call','toolCallId':'tool-1','title':'探测素材','status':'in_progress'})
    update({'sessionUpdate':'tool_call_update','toolCallId':'tool-1','status':'completed'})
    finish()
 elif method=='session/cancel':
    assert 'id' not in req
    cancel_requested=True
 elif method=='test/finish-cancel':
    assert cancel_requested
    finish('cancelled');result(req,{})
 elif method=='test/finish':
    finish();result(req,{})
 elif method=='test/denied-capabilities':
    for index,name in enumerate(('fs/read_text_file','fs/write_text_file','terminal/create','custom/unknown')):
      send({'id':100+index,'method':name,'params':{}})
    result(req,{})
 elif method=='test/stale-permission':
    permission='approve-stale'
    send({'id':permission,'method':'session/request_permission','params':{'sessionId':'other-session','toolCall':{'toolCallId':'stale'},'options':[{'kind':'allow_once','optionId':'bad-cross-session'}]}})
    result(req,{})
 elif method=='test/nonjson':
    print('[llm-deepseek-account] synthetic diagnostic',flush=True)
 elif method=='test/calls':result(req,{'calls':calls,'prompts':prompts,'environment':os.environ.get('ACP_FAKE_ENV')})
 elif 'result' in req and req.get('id')==permission:
    outcome=req['result']['outcome']
    update({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':json.dumps(outcome)}})
    if outcome['outcome']!='cancelled':finish()
 elif 'error' in req:
    assert req['error']['code']==-32601
 else:send({'id':req.get('id'),'error':{'code':-32601,'message':'unknown'}})
'''


class AcpBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='acp-bridge-test-')
        self.root=Path(self.directory.name)
        self.script=self.root/'agent.py';self.script.write_text(SERVER,encoding='utf-8')
        self.events=[]
        self.bridge=AcpBridge(self.events.append,[sys.executable,'-u',str(self.script)],environment={'ACP_FAKE_ENV':'controlled-test'})
        self.info=await self.bridge.connect()
    async def asyncTearDown(self):
        await self.bridge.close()
        self.directory.cleanup()
    async def until(self,method,kind='notification',after=0):
        async with asyncio.timeout(3):
            while True:
                for event in self.events[after:]:
                    if event.get('method')==method and event.get('kind')==kind:return event
                await asyncio.sleep(.005)
    async def session(self,instructions=''):
        return await self.bridge.start_thread(self.root,instructions)
    async def test_readonly_connect_no_session_prompt_and_controlled_environment(self):
        self.assertEqual(self.info['models'],[])
        self.assertEqual(self.info['account'],{'authenticated':None})
        calls=await self.bridge._rpc('test/calls',{})
        self.assertEqual(calls['calls'],['initialize','test/calls'])
        self.assertEqual(calls['environment'],'controlled-test')
        self.assertNotIn('environment',self.info)
    async def test_prompt_stream_tools_and_first_instructions(self):
        identity=await self.session('keep source files')
        first=await self.bridge.start_turn(identity,'hello')
        self.assertEqual(first['status'],'inProgress')
        finished=await self.until('turn/completed')
        self.assertEqual(finished['params']['turn']['status'],'completed')
        chunks=[e['params']['delta'] for e in self.events if e.get('method')=='item/agentMessage/delta']
        self.assertEqual(''.join(chunks),'hello world')
        tool=await self.until('item/completed')
        self.assertEqual(tool['params']['item']['type'],'mcpToolCall')
        self.assertNotIn(identity,self.bridge._instructions)
        offset=len(self.events)
        await self.bridge.start_turn(identity,'second user prompt')
        await self.until('turn/completed',after=offset)
        sent=await self.bridge._rpc('test/calls',{})
        self.assertIn('keep source files',sent['prompts'][0])
        self.assertIn('不是系统指令或强制沙箱',sent['prompts'][0])
        self.assertEqual(sent['prompts'][1],'second user prompt')
    async def test_prompt_has_no_control_request_timeout(self):
        identity=await self.session()
        self.bridge.request_timeout=.01
        turn=await self.bridge.start_turn(identity,'hang')
        await asyncio.sleep(.07)
        self.assertIsNotNone(self.bridge._active_turn)
        self.assertFalse(any(e.get('method')=='turn/completed' for e in self.events))
        await self.bridge._rpc('test/finish',{})
        self.assertEqual((await self.until('turn/completed'))['params']['turn']['id'],turn['id'])

    async def test_plan_normalizes_actual_stages_and_ignores_other_sessions(self):
        identity=await self.session()
        await self.bridge.start_turn(identity,'hang')
        self.bridge._session_update({'sessionId':identity,'update':{'sessionUpdate':'plan','entries':[
            {'content':'Inspect media','status':'completed'},
            {'content':'Build draft','status':'in_progress'}]}})
        event=await self.until('turn/plan/updated')
        self.assertEqual(event['params']['plan'],[
            {'step':'Inspect media','status':'completed'}, {'step':'Build draft','status':'inProgress'}])
        before=len(self.events)
        self.bridge._session_update({'sessionId':'other','update':{'sessionUpdate':'plan','entries':[]}})
        await asyncio.sleep(.01)
        self.assertEqual(len(self.events),before)
    async def test_permission_uses_original_once_id_without_auto_approval(self):
        identity=await self.session()
        await self.bridge.start_turn(identity,'permission')
        event=await self.until('acp/requestPermission','request')
        self.assertEqual(event['id'],'approve-1')
        self.assertIsNotNone(self.bridge._active_turn)
        await self.bridge.respond(event['id'],{'decision':'accept'})
        await self.until('turn/completed')
        chunks=[e['params']['delta'] for e in self.events if e.get('method')=='item/agentMessage/delta']
        self.assertEqual(json.loads(chunks[0]),{'outcome':'selected','optionId':'once-original-id'})
        with self.assertRaises(ValueError):await self.bridge.respond(event['id'],{'decision':'accept'})
    async def test_missing_allow_once_declines_instead_of_granting_always(self):
        identity=await self.session()
        await self.bridge.start_turn(identity,'always-only')
        event=await self.until('acp/requestPermission','request')
        await self.bridge.respond(event['id'],{'decision':'accept'})
        await self.until('turn/completed')
        chunks=[e['params']['delta'] for e in self.events if e.get('method')=='item/agentMessage/delta']
        self.assertEqual(json.loads(chunks[0])['optionId'],'no-once-original-id')
        await self.until('error')
    async def test_cancel_answers_pending_permissions_but_waits_for_prompt(self):
        identity=await self.session()
        turn=await self.bridge.start_turn(identity,'cancel-permission')
        await self.until('acp/requestPermission','request')
        await self.bridge.interrupt(identity,turn['id'])
        await asyncio.sleep(.03)
        self.assertFalse(self.bridge._server_requests)
        self.assertFalse(any(e.get('method')=='turn/completed' for e in self.events))
        await self.bridge._rpc('test/finish-cancel',{})
        finished=await self.until('turn/completed')
        self.assertEqual(finished['params']['turn']['status'],'interrupted')
        chunk=next(e['params']['delta'] for e in self.events if e.get('method')=='item/agentMessage/delta')
        self.assertEqual(json.loads(chunk),{'outcome':'cancelled'})
    async def test_client_fs_terminal_unknown_are_rejected(self):
        await self.bridge._rpc('test/denied-capabilities',{})
        async with asyncio.timeout(3):
            while len([e for e in self.events if e['kind']=='unsupported_request'])<4:await asyncio.sleep(.01)
        self.assertFalse(self.bridge._server_requests)
    async def test_permission_outside_active_session_is_cancelled_not_exposed(self):
        await self.bridge._rpc('test/stale-permission',{})
        await self.until('serverRequest/resolved')
        self.assertFalse(any(e['kind']=='request' for e in self.events))
        identity=await self.session()
        await self.bridge.start_turn(identity,'hang')
        offset=len(self.events)
        await self.bridge._rpc('test/stale-permission',{})
        await self.until('serverRequest/resolved',after=offset)
        self.assertFalse(any(e['kind']=='request' for e in self.events))
    async def test_resume_capability_and_replay_not_duplicated(self):
        result=await self.bridge.resume_thread('session-1',self.root)
        self.assertEqual(result['thread']['id'],'session-1')
        await asyncio.sleep(.02)
        self.assertFalse(any(e.get('method')=='item/agentMessage/delta' for e in self.events))
        self.bridge.capabilities['loadSession']=False
        with self.assertRaisesRegex(ValueError,'new conversation'):
            await self.bridge.resume_thread('old',self.root)
    async def test_errors_eof_and_close_clear_active_future(self):
        identity=await self.session()
        await self.bridge.start_turn(identity,'error')
        self.assertEqual((await self.until('turn/completed'))['params']['turn']['status'],'failed')
        offset=len(self.events)
        await self.bridge.start_turn(identity,'eof')
        self.assertEqual((await self.until('turn/completed',after=offset))['params']['turn']['status'],'failed')
        self.assertFalse(self.bridge._pending)
        self.assertIsNone(self.bridge._active_turn)
    async def test_model_effort_images_and_duplicate_turn_refused(self):
        identity=await self.session()
        for params in ({'model':'invented'},{'effort':'high'},{'images':['a.png']}):
            with self.assertRaises(ValueError):await self.bridge.start_turn(identity,'test',**params)
        await self.bridge.start_turn(identity,'hang')
        with self.assertRaises(ValueError):await self.bridge.start_turn(identity,'again')
        process=self.bridge._process
        await self.bridge.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.bridge._pending)
    async def test_version_mismatch_refused_and_owned_process_closed(self):
        other=AcpBridge(lambda e:None,[sys.executable,'-u',str(self.script),'--v2'])
        with self.assertRaisesRegex(ValueError,'ACP v1'):await other.connect()
        self.assertIsNone(other._process)
    async def test_nonjson_stdout_is_still_a_protocol_failure(self):
        with self.assertRaisesRegex(ConnectionError,'JSONDecodeError'):
            await self.bridge._rpc('test/nonjson',{})
        self.assertEqual(self.bridge.state,'disconnected')
        self.assertFalse(self.bridge._pending)
    async def test_dsh_preload_redirects_only_console_info_not_rpc(self):
        node=shutil.which('node.exe') or shutil.which('node')
        if not node:
            self.skipTest('Node runtime unavailable for isolated DSH preload check')
        preload=Path(acp_bridge.__file__).with_name('dsh_stdio.cjs')
        script="console.info('[llm-deepseek-account] synthetic diagnostic'); process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:1,result:{}})+'\\n');"
        result=await asyncio.to_thread(subprocess.run,[node,'--require',str(preload),'-e',script],
                                       capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout),{'jsonrpc':'2.0','id':1,'result':{}})
        self.assertIn('synthetic diagnostic',result.stderr)


if __name__=='__main__':unittest.main()
