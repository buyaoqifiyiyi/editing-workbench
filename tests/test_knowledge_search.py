"""Exercise the bundled knowledge library through the actual guide and MCP interfaces."""
from pathlib import Path
import os
import subprocess
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from jianying_engine import api
from jianying_engine.mcp_server import create_server

GUIDES = ('reference/creative-knowledge', 'reference/creative-knowledge-story',
          'reference/creative-knowledge-techniques', 'reference/creative-knowledge-audio',
          'reference/creative-knowledge-workflows', 'reference/creative-knowledge-sources')


class KnowledgeInterfaceTests(unittest.TestCase):
    def test_fresh_guide_registry_resolves_all_knowledge_resources(self):
        for name in GUIDES:
            with self.subTest(name=name):
                self.assertIn(name, api.paths()['guides'])
                self.assertTrue(api.read_guide(name).strip())
        with self.assertRaises(ValueError):
            api.read_guide('../../unrelated-private-file')

    def test_cli_reads_the_candidate_from_an_unrelated_directory(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from jianying_engine.cli import main;'
                'raise SystemExit(main(["guide","reference/creative-knowledge"]))')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(root / 'src')], cwd=directory,
                                    capture_output=True, text=True, encoding='utf8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('剪辑创作知识库', result.stdout)

    def test_search_routes_realistic_questions_and_returns_valid_source_lines(self):
        cases = [
            ('剧情不清人物目标与代价', {'reference/creative-knowledge-story', 'reference/general-editing'}),
            ('声音先入画面后入', {'reference/creative-knowledge-audio', 'reference/general-editing', 'planning'}),
            ('画中画重复如何全画幅接管', {'reference/creative-knowledge-techniques', 'reference/technique-combinations'}),
            ('对白含音乐如何分离人声', {'reference/creative-knowledge-audio', 'reference/styles/anime-mix'}),
            ('J-cut', {'reference/creative-knowledge-audio', 'reference/creative-knowledge-techniques'}),
            ('音画对不上', {'reference/creative-knowledge-audio', 'reference/creative-knowledge-techniques'}),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                result = api.search_knowledge(query, 3)
                self.assertTrue(expected.intersection(item['guide'] for item in result['results']))
                for item in result['results']:
                    lines = api.read_guide(item['guide']).splitlines()
                    self.assertTrue(lines[item['line'] - 1].strip().startswith(item['excerpt']))
        self.assertEqual(api.search_knowledge('火山岩浆地质年代')['results'], [])

    def test_search_rejects_invalid_input_and_never_reads_an_outside_path(self):
        for query, limit in (('', 5), ('x' * 257, 5), ('剧情', True), ('剧情', 21)):
            with self.assertRaises(ValueError):
                api.search_knowledge(query, limit)
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'fixture.md'
            secret.write_text('# 火山岩浆地质年代\n测试私有内容', encoding='utf8')
            with patch.dict(api._GUIDES, {'reference/outside-fixture': str(secret)}):
                self.assertEqual(api.search_knowledge('火山岩浆地质年代')['results'], [])

    def test_single_tool_names_and_concrete_methods_are_retrievable(self):
        for query in ('Whisper', 'Demucs', 'OpenAI', 'Resolve', '绿幕'):
            with self.subTest(query=query):
                self.assertTrue(api.search_knowledge(query, 3)['results'])

    def test_json_parameters_are_searchable_without_english_substring_false_matches(self):
        for query in ('effects_adjust_blur', 'effects_adjust_speed'):
            with self.subTest(query=query):
                self.assertTrue(api.search_knowledge(query, 5)['results'])
        for query in ('RAM', 'LAN'):
            with self.subTest(query=query):
                self.assertEqual(api.search_knowledge(query)['results'], [])

    def test_usage_command_does_not_outrank_the_requested_story_method(self):
        first = api.search_knowledge('剧情不清 人物目标与代价', 1)['results'][0]
        self.assertNotEqual((first['guide'], first['section']),
                            ('reference/creative-knowledge', '如何调用'))
        self.assertIn(first['guide'], {'reference/creative-knowledge-story',
                                      'reference/general-editing'})

    def test_named_backend_and_sound_method_rank_useful_guides_first(self):
        self.assertEqual(api.search_knowledge('Resolve', 1)['results'][0]['guide'],
                         'reference/resolve-workflow')
        self.assertNotEqual(api.search_knowledge('J-cut', 1)['results'][0]['guide'],
                            'reference/creative-knowledge-sources')

    def test_plain_text_templates_are_searchable(self):
        results = api.search_knowledge('前镜 后镜 接管', 3)['results']
        self.assertIn('reference/creative-knowledge-workflows', {x['guide'] for x in results})

    def test_registered_operational_guides_are_also_searchable(self):
        results = api.search_knowledge('草稿路径迁移', 5)['results']
        self.assertIn('migration', {x['guide'] for x in results})
        self.assertEqual(api.search_knowledge('剪映剪辑步骤', 1)['results'][0]['guide'], 'editing')

    def test_cli_search_uses_same_local_api(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from jianying_engine.cli import main;'
                'raise SystemExit(main(["knowledge","剧情不清人物目标与代价","--limit","3"]))')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(root / 'src')], cwd=directory,
                                    capture_output=True, text=True, encoding='utf8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['results'])

    def test_restored_capability_anchors_and_current_mesh_paragraph_exist(self):
        text = api.read_guide('capabilities')
        for anchor in ('<a id="alpha-video"></a>', '<a id="mask-rect"></a>',
                       '<a id="separation"></a>', '<a id="native-beat"></a>'):
            self.assertIn(anchor, text)
        self.assertIn('Mesh Avatar 素材制作', text)
        self.assertIn('ImageGen 每次只接收原图', text)

    def test_search_returns_section_context_anchors_and_capability_hints(self):
        cap_lines = api.read_guide('capabilities').splitlines()
        pip_results = api.search_knowledge('画中画重复如何全画幅接管', 5)['results']
        self.assertTrue(pip_results)
        for item in pip_results:
            self.assertGreaterEqual(item['section_start_line'], 1)
            self.assertIn(item['excerpt'], item['context'])
        hints = [hint for item in pip_results for hint in item.get('capability_hints', [])]
        self.assertTrue(hints)
        for hint in hints:
            self.assertEqual(hint['guide'], 'capabilities')
            self.assertIn(f'<a id="{hint["anchor"]}"></a>', '\n'.join(cap_lines[max(0, hint['line'] - 3):hint['line']]))

        chroma_results = api.search_knowledge('Chroma2 色键绿幕', 5)['results']
        self.assertTrue(any(item['guide'] == 'capabilities' and item['anchor'] == 'installed-chroma2'
                            for item in chroma_results))

        speed_results = api.search_knowledge('曲线变速 变速', 5)['results']
        self.assertTrue(speed_results)
        self.assertTrue(any(
            (item['guide'] == 'capabilities' and item['anchor'] == 'speed-curve')
            or any(h['anchor'] == 'speed-curve' for h in item.get('capability_hints', []))
            for item in speed_results
        ))


class KnowledgeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_retrieves_knowledge_and_explains_both_media_calls(self):
        with tempfile.TemporaryDirectory() as state:
            async with Client(create_server(state)) as client:
                instructions = client.instructions
                self.assertIn('analyze and review', instructions)
                info = (await client.call_tool('describe_operations', {})).structured_content
                self.assertIn('reference/creative-knowledge', info['guides'])
                self.assertIn('analyze and review', info['operations']['music-edit'])
                for name in GUIDES:
                    result = (await client.call_tool('read_guide', {'name': name})).structured_content
                    self.assertFalse(result.get('truncated'), name)
                    self.assertTrue(result['text'].strip(), name)
                capability = (await client.call_tool('read_guide', {'name': 'capabilities'})).structured_content
                self.assertFalse(capability['truncated'])
                self.assertIn('<a id="mask-rect"></a>', capability['text'])
                self.assertIn('<a id="native-beat"></a>', capability['text'])
                denied = (await client.call_tool('read_guide', {'name': '../../private'})).structured_content
                self.assertIn('error', denied)

    async def test_fresh_stdio_mcp_can_discover_and_read_knowledge(self):
        with tempfile.TemporaryDirectory() as state:
            env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA',
                                             'USERPROFILE', 'TEMP', 'TMP') if k in os.environ}
            env['PYTHONPATH'] = str(root / 'src')
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'jianying_engine.mcp_server', '--state-dir', state], env=env)
            async with Client(params) as client:
                self.assertIn('analyze and review', client.instructions)
                info = (await client.call_tool('describe_operations', {})).structured_content
                self.assertIn('reference/creative-knowledge', info['guides'])
                guide = (await client.call_tool('read_guide',
                         {'name': 'reference/creative-knowledge-story'})).structured_content
                self.assertFalse(guide['truncated'])
                self.assertIn('剧情', guide['text'])
                found = (await client.call_tool('search_knowledge',
                         {'query': '画中画重复如何全画幅接管', 'limit': 3})).structured_content
                self.assertTrue(found['results'])
                self.assertIn('reference/creative-knowledge-techniques',
                              {item['guide'] for item in found['results']})


if __name__ == '__main__':
    unittest.main()
