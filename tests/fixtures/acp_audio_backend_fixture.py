"""Deterministic ACP peer exercising standalone local audio through real stdio MCP."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import wave

from mcp import Client, StdioServerParameters


sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
session = None


def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False), flush=True)


def update(value):
    send({'method': 'session/update', 'params': {'sessionId': 'audio-integration-session', 'update': value}})


async def execute(prompt):
    cwd = Path(session['cwd'])
    source = cwd / 'incoming/source.wav'
    text = '\n'.join(item.get('text', '') for item in prompt if item.get('type') == 'text')
    assert '本次为单独调用：剪映专业版' in text and '工作台内部工具' in text, 'Missing selected software/internal audio context'
    assert 'denoise-audio' in text and str(source) in text, 'Missing audio operation or selected source'
    assert 'resolve-workflow' not in text and 'adobe-workflow' not in text, 'Unselected editor leaked into prompt'
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    output = cwd / 'output'
    output.mkdir()
    engine = next(item for item in session['mcpServers'] if item['name'] == 'jianying-engine')
    params = StdioServerParameters(command=engine['command'],
                                  args=[*engine['args'], '--state-dir', str(cwd / '.job-state')],
                                  env={item['name']: item['value'] for item in engine['env']})
    arguments = ['--input', str(source), '--start', '0', '--duration', '1',
                 '--noise-floor', '-40', '--reduction', '12', '--out', str(output / 'denoise')]
    async with Client(params) as client:
        discovered = (await client.call_tool('describe_operations', {})).structured_content
        assert 'denoise-audio' in discovered['operations']
        update({'sessionUpdate': 'tool_call', 'toolCallId': 'audio-denoise',
                'title': 'jianying-engine · denoise-audio', 'status': 'in_progress'})
        job = (await client.call_tool('start_job', {'operation': 'denoise-audio', 'arguments': arguments})).structured_content
        assert job['job_id'], job
        async with asyncio.timeout(30):
            while True:
                status = (await client.call_tool('job_status', {'job_id': job['job_id']})).structured_content
                if status['finished'] is not None:
                    break
                await asyncio.sleep(.02)
        assert (status['state'], status['returncode']) == ('succeeded', 0), status
        update({'sessionUpdate': 'tool_call_update', 'toolCallId': 'audio-denoise', 'status': 'completed'})
    report = json.loads((output / 'denoise/denoise-report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'passed' and report['source_sha256'] == original
    assert report['source_modified'] is False and hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert report['sample_count'] == 48000 and report['first_sample'] == 0
    assert report['params']['noise_floor_db'] == -40 and report['params']['reduction_db'] == 12
    for key, filename in [('dry', 'dry.wav'), ('denoised', 'denoised.wav')]:
        assert (report[key]['sample_rate'], report[key]['channels'], report[key]['sample_width'], report[key]['frames']) == (48000, 2, 2, 48000)
        with wave.open(str(output / 'denoise' / filename), 'rb') as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (48000, 2, 2, 48000)
    proof = {'backend': 'jianying', 'internal_tool': 'denoise-audio', 'mode': 'single', 'prompt': text,
             'operations_started': ['denoise-audio'], 'job': status,
             'source_sha256': original, 'model_calls': 0, 'native_editor_started': False}
    (output / 'protocol-proof.json').write_text(json.dumps(proof, ensure_ascii=False), encoding='utf-8')
    (output / '交付说明.md').write_text('已生成1秒48kHz立体声PCM16音频，核验帧数、文件身份和显式参数；未做试听或音质验收。未创建视频时间轴。', encoding='utf-8')
    (cwd / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
        {'path': 'output/denoise/denoised.wav', 'title': '独立降噪音频'},
        {'path': 'output/交付说明.md', 'title': '交付说明'}]}, ensure_ascii=False), encoding='utf-8')
    update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text',
            'text': '确定性测试通过真实MCP生成独立降噪音频；已核帧数与源身份，未做试听。'}})


for line in sys.stdin:
    request = json.loads(line)
    method, identity, data = request.get('method'), request.get('id'), request.get('params', {})
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {},
                  'agentInfo': {'name': 'audio-test-peer', 'version': 'test-only'}}
    elif method == 'session/new':
        session = data
        result = {'sessionId': 'audio-integration-session'}
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
