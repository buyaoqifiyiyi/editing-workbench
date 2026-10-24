"""Deterministic ACP test peer. Calls the real local engine through stdio MCP; no model."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
session = None


def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False), flush=True)


def update(value):
    send({'method': 'session/update', 'params': {'sessionId': 'integration-session', 'update': value}})


async def execute(prompt):
    cwd = Path(session['cwd'])
    source = cwd / 'incoming/shot.png'
    text = '\n'.join(item.get('text', '') for item in prompt if item.get('type') == 'text')
    assert '[文件夹]' in text and str(source.parent) in text, 'Workbench must forward the chosen local folder'
    output = cwd / 'output'
    output.mkdir()
    plan = output / 'edit-plan.json'
    plan.write_text(json.dumps({'schema_version': 1, 'name': '接入链路验证', 'mode': 'hybrid',
                               'canvas': {'width': 320, 'height': 240, 'fps': 30},
                               'clips': [{'path': str(source), 'start': 0, 'duration': 1}],
                               'audio': [], 'captions': [], 'notes': ['合成测试素材，无模型调用'], 'missing_assets': []},
                              ensure_ascii=False), encoding='utf-8')
    engine = next(item for item in session['mcpServers'] if item['name'] == 'jianying-engine')
    params = StdioServerParameters(command=engine['command'],
                                  args=[*engine['args'], '--state-dir', str(cwd / '.job-state')],
                                  env={item['name']: item['value'] for item in engine['env']})
    async with Client(params) as client:
        discovered = await client.call_tool('describe_operations', {})
        assert 'build' in discovered.structured_content['operations']
        captioned = output / 'captioned-plan.json'
        word_plan = output / 'word-plan.json'
        word_input = output / 'word-input'
        tracked_plan = output / 'tracked-plan.json'
        for step, (operation, arguments) in enumerate([('reverse', ['--input', str(source.parent / 'reverse-source.mov'),
                                                 '--start', '0.5', '--duration', '1', '--audio', 'reverse',
                                                 '--out', str(output / 'reverse')]),
                                     ('denoise-audio', ['--input', str(output / 'reverse/reversed.mov'), '--duration', '1',
                                                        '--noise-floor', '-40', '--reduction', '12', '--out', str(output / 'denoise')]),
                                     ('captions', [str(plan), '--srt', str(source.parent / 'corrected.srt'),
                                                  '--timebase', 'timeline', '--index', '0', '1', '--out', str(captioned)]),
                                     ('captions', [str(captioned), '--transcript', str(word_input / 'transcript.json'),
                                                   '--level', 'word', '--index', '0', '1', '--track', 'words', '--out', str(word_plan)]),
                                     ('tracking-apply', [str(word_plan), '--tracking', str(output / 'tracking.json'),
                                                         '--source-index', '0', '--caption-index', '6', '--offset-y', '30',
                                                         '--out', str(tracked_plan)]),
                                     ('validate', ['--plan', str(tracked_plan)]),
                                     ('build', ['--plan', str(tracked_plan), '--out', str(output / 'draft')])]):
            call_id = f'{operation}-{step}'
            update({'sessionUpdate': 'tool_call', 'toolCallId': call_id, 'title': 'jianying-engine · ' + operation, 'status': 'in_progress'})
            job = (await client.call_tool('start_job', {'operation': operation, 'arguments': arguments})).structured_content
            assert job['job_id'], job
            async with asyncio.timeout(30):
                while True:
                    status = (await client.call_tool('job_status', {'job_id': job['job_id']})).structured_content
                    if status['finished'] is not None:
                        break
                    await asyncio.sleep(.02)
            assert (status['state'], status['returncode']) == ('succeeded', 0), status
            if operation == 'reverse':
                data = json.loads(plan.read_text(encoding='utf-8'))
                report = json.loads((output / 'reverse/reverse-report.json').read_text(encoding='utf-8'))
                data['clips'] = [
                    {'path': report['output_media'], 'start': 0, 'duration': .5, 'source_in': 0, 'volume': 1},
                    {'path': report['output_media'], 'start': .5, 'duration': .5, 'source_in': .5, 'volume': 1}]
                plan.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                # Synthetic interval contract fixture, not model recognition. Real ASR is checked externally.
                media = Path(report['output_media'])
                (output / 'tracking.json').write_text(json.dumps({
                    'schema_version': 1, 'method': 'opencv_csrt', 'status': 'tracked', 'lost_at': None,
                    'source': {'path': str(media.resolve()), 'bytes': media.stat().st_size,
                               'sha256': hashlib.sha256(media.read_bytes()).hexdigest(), 'width': 320, 'height': 240},
                    'coverage_start': 0, 'coverage_end': 1,
                    'samples': [{'time': t, 'bbox': [70 + t * 40, 110, 20, 20]} for t in (0, .25, .5, .75)],
                    'test_fixture': 'synthetic trajectory; real CSRT checked externally'}, ensure_ascii=False), encoding='utf-8')
                segments = [
                    {'start': .1, 'end': .4, 'text': '合成词测试', 'words': [
                        {'word': '合成', 'start': .1, 'end': .2, 'probability': .9},
                        {'word': '词测试', 'start': .2, 'end': .4, 'probability': .9}]},
                    {'start': .6, 'end': .9, 'text': '第二镜头', 'words': [
                        {'word': '第二', 'start': .6, 'end': .7, 'probability': .9},
                        {'word': '镜头', 'start': .7, 'end': .9, 'probability': .9}]}]
                word_input.mkdir()
                (word_input / 'transcript.json').write_text(json.dumps({'segments': segments}, ensure_ascii=False), encoding='utf-8')
                (word_input / 'transcript-report.json').write_text(json.dumps({
                    'status': 'passed', 'input': str(media), 'segments': segments,
                    'params': {'word_timestamps': True, 'level': 'word'},
                    'audio': {'bytes': media.stat().st_size, 'seconds': 1, 'sha256': hashlib.sha256(media.read_bytes()).hexdigest()},
                    'test_fixture': 'synthetic word intervals; no ASR inference'}, ensure_ascii=False), encoding='utf-8')
            if operation == 'denoise-audio':
                data = json.loads(plan.read_text(encoding='utf-8'))
                report = json.loads((output / 'denoise/denoise-report.json').read_text(encoding='utf-8'))
                for clip in data['clips']:
                    clip['volume'] = 0
                data['audio'] = [{'path': report['output_media'], 'start': 0, 'source_in': 0, 'duration': 1, 'volume': 1}]
                plan.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            if operation == 'tracking-apply':
                data = json.loads(tracked_plan.read_text(encoding='utf-8'))
                data['clips'][0]['mask'] = {'name': '矩形', 'experimental': True, 'size': .6, 'rect_width': .45,
                                           'feather': 20, 'rendering': 'red_coverage_v1'}
                tracked_plan.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            if operation == 'captions' and word_plan.exists():
                data = json.loads(word_plan.read_text(encoding='utf-8'))
                for cue in data['captions']:
                    if cue['track'] == 'words':
                        cue['y'] = 0
                assert len(data['captions']) == 6
                data['captions'].append({'text': '跟随标签', 'start': 0, 'duration': .5, 'x': 0, 'y': 0,
                                         'size': 10, 'track': 'follow-label', 'style': {'auto_wrapping': False}})
                word_plan.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            update({'sessionUpdate': 'tool_call_update', 'toolCallId': call_id, 'status': 'completed'})
    (output / '交付说明.md').write_text('本机引擎已生成可编辑草稿，仅通过结构验证；未做剪映UI重开、视听或导出。', encoding='utf-8')
    (cwd / 'workbench-delivery.json').write_text(json.dumps({'schema_version':1,'items':[
        {'path':'output/draft','title':'可编辑草稿'}, {'path':'output/交付说明.md','title':'交付说明'}]},ensure_ascii=False),encoding='utf-8')
    update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '测试Peer通过真实MCP完成reverse/denoise-audio/captions/tracking-apply/validate/build；草稿见output/draft。'}})


for line in sys.stdin:
    request = json.loads(line)
    method, identity, data = request.get('method'), request.get('id'), request.get('params', {})
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {}, 'agentInfo': {'name': 'test-peer', 'version': 'test-only'}}
    elif method == 'session/new':
        session = data
        result = {'sessionId': 'integration-session'}
    elif method == 'session/prompt':
        try:
            asyncio.run(execute(data['prompt']))
            result = {'stopReason': 'end_turn'}
        except Exception as error:
            send({'id': identity, 'error': {'code': -32603, 'message': str(error)}})
            continue
    else:
        send({'id': identity, 'error': {'code': -32601, 'message': 'Test peer method not supported'}})
        continue
    send({'id': identity, 'result': result})
