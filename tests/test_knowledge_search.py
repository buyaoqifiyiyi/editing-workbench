"""Exercise the bundled pure open-source knowledge library through CLI and MCP interfaces."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from openedit_engine import api  # noqa: E402
from openedit_engine.mcp_server import create_server  # noqa: E402

GUIDES = (
    'reference/creative-knowledge',
    'reference/creative-knowledge-story',
    'reference/creative-knowledge-techniques',
    'reference/creative-knowledge-audio',
    'reference/creative-knowledge-workflows',
    'reference/creative-knowledge-sources',
)


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
                'from openedit_engine.cli import main;'
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
        for query in ('Whisper', 'Demucs', 'FilmCraft', 'EffectCraft', '绿幕'):
            with self.subTest(query=query):
                self.assertTrue(api.search_knowledge(query, 3)['results'], query)

    def test_usage_command_does_not_outrank_the_requested_story_method(self):
        first = api.search_knowledge('剧情不清 人物目标与代价', 1)['results'][0]
        self.assertNotEqual((first['guide'], first['section']),
                            ('reference/creative-knowledge', '如何调用'))
        self.assertIn(first['guide'], {'reference/creative-knowledge-story',
                                      'reference/general-editing'})

    def test_named_backend_and_sound_method_rank_useful_guides_first(self):
        self.assertEqual(api.search_knowledge('EffectCraft', 1)['results'][0]['guide'],
                         'reference/openedit')
        self.assertNotEqual(api.search_knowledge('J-cut', 1)['results'][0]['guide'],
                            'reference/creative-knowledge-sources')

    def test_plain_text_templates_are_searchable(self):
        results = api.search_knowledge('前镜 后镜 接管', 3)['results']
        self.assertIn('reference/creative-knowledge-workflows', {x['guide'] for x in results})

    def test_registered_operational_guides_are_also_searchable(self):
        results = api.search_knowledge('素材路径迁移', 5)['results']
        self.assertIn('migration', {x['guide'] for x in results})
        self.assertEqual(api.search_knowledge('开源剪辑步骤', 1)['results'][0]['guide'], 'editing')

    def test_cli_search_uses_same_local_api(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from openedit_engine.cli import main;'
                'raise SystemExit(main(["knowledge","剧情不清人物目标与代价","--limit","3"]))')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(root / 'src')], cwd=directory,
                                    capture_output=True, text=True, encoding='utf8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['results'])

    def test_capability_anchors_and_search_hints(self):
        text = api.read_guide('capabilities')
        for anchor_id in (
            'filmcraft-editing', 'effectcraft-overlays', 'filmcraft-grading', 'filmcraft-blend',
            'text-highlights', 'media-prep', 'transcription', 'separation', 'ducking',
            'offline-denoise', 'beat-editing', 'offline-tracking',
        ):
            self.assertIn(f'<a id="{anchor_id}"></a>', text)
        cap_lines = text.splitlines()
        hints = [h for item in api.search_knowledge('ProRes 透明通道动效', 5)['results']
                 for h in item.get('capability_hints', [])]
        for hint in hints:
            self.assertEqual(hint['guide'], 'capabilities')
            self.assertIn(f'<a id="{hint["anchor"]}"></a>', '\n'.join(cap_lines[max(0, hint['line'] - 3):hint['line']]))

    def test_all_runtime_markdown_links_and_anchors_resolve(self):
        runtime_root = root / 'src' / 'openedit_engine' / 'runtime'
        md_files = sorted(runtime_root.rglob('*.md'))

        def _slug(head):
            s = head.strip().lower()
            s = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', s)
            return re.sub(r'\s+', '-', s).strip('-')

        anchors = {}
        for md in md_files:
            ids = set()
            text = md.read_text(encoding='utf-8')
            ids.update(re.findall(r'<a\s+id=["\']([^"\']+)["\']', text))
            for line in text.splitlines():
                m = re.match(r'^#{1,6}\s+(.+?)\s*$', line)
                if m:
                    ids.add(_slug(m.group(1)))
            anchors[md.resolve()] = ids

        dead = []
        for md in md_files:
            rel = md.relative_to(runtime_root)
            for lineno, line in enumerate(md.read_text(encoding='utf-8').splitlines(), 1):
                for m in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', line):
                    href = m.group(2).strip()
                    if href.startswith(('http://', 'https://', 'mailto:', '#')):
                        continue
                    target_part, _, frag = href.partition('#')
                    target = (md.parent / target_part).resolve()
                    if not target.is_file():
                        dead.append(f'{rel}:{lineno} -> {href} (missing file)')
                    elif frag and frag not in anchors.get(target, set()) and _slug(frag) not in anchors.get(target, set()):
                        dead.append(f'{rel}:{lineno} -> {href} (missing anchor)')
        self.assertEqual(dead, [])

    def test_recipes_guide_is_discoverable_with_capability_hints(self):
        res_recipe = api.search_knowledge('标题卡 动效配方', 5)['results']
        self.assertIn('reference/recipes', {r['guide'] for r in res_recipe})
        hints = [h['anchor'] for r in res_recipe for h in r.get('capability_hints', [])]
        self.assertIn('effectcraft-overlays', hints)

        res_chroma = api.search_knowledge('绿幕抠像 ultra_key', 5)['results']
        self.assertTrue({'reference/green-screen', 'reference/recipes'}.intersection(r['guide'] for r in res_chroma))

    def test_openedit_course_manual_is_registered_untruncated_and_searchable(self):
        self.assertIn('reference/course-techniques-openedit', api.paths()['guides'])
        manual_text = api.read_guide('reference/course-techniques-openedit')
        self.assertLess(len(manual_text), 131072)
        self.assertIn('已学课程实战总手册', manual_text)

        queries = [
            ('动漫排版 人物重影', {'reference/course-techniques-openedit'}, 'ai-matting'),
            ('超燃卡点 四角小窗', {'reference/course-techniques-openedit'}, None),
            ('关键帧曲线 缓动 挤压拉伸', {'reference/course-techniques-openedit'}, None),
            ('字中视频 局部调色 三色轮', {'reference/course-techniques-openedit'}, 'filmcraft-grading'),
            ('横转竖 人脸跟拍', {'reference/course-techniques-openedit', 'reference/tutorial-library', 'capabilities'}, None),
            ('口播一键粗剪 字幕', {'reference/course-techniques-openedit', 'reference/general-editing', 'capabilities'}, None),
            ('响度母带 EBU R128', {'reference/course-techniques-openedit', 'reference/creative-knowledge-workflows', 'capabilities'}, None),
            ('非绿幕 AI 抠像', {'reference/course-techniques-openedit', 'reference/green-screen', 'reference/layered-reveal', 'capabilities'}, None),
        ]
        for query, expected_guides, expected_hint in queries:
            with self.subTest(query=query):
                res = api.search_knowledge(query, 5)['results']
                self.assertTrue(expected_guides.intersection(r['guide'] for r in res), f'{query}: {res}')
                if expected_hint is not None:
                    hints = {h['anchor'] for r in res for h in r.get('capability_hints', [])}
                    self.assertIn(expected_hint, hints, f'{query}: {hints}')

    def test_no_obsolete_jianying_or_stale_capability_phrases_in_runtime_docs(self):
        runtime_root = root / 'src' / 'openedit_engine' / 'runtime'
        stale_phrases = (
            'preview-sequence',
            '会员效果已授权',
            '红色HSL/三点L曲线',
            '非绿幕自动轮廓抠像当前未内置',
            'EffectCraft 6 套声明式配方',
            '内置 6 个分辨率自适应配方',
            '没有可靠的原生动态压低接口',
            '已有登记但封面异常',
        )
        hits = []
        for md in sorted(runtime_root.rglob('*.md')):
            rel = md.relative_to(runtime_root).as_posix()
            text = md.read_text(encoding='utf-8-sig')
            for phrase in stale_phrases:
                if phrase in text:
                    hits.append(f'{rel}: {phrase}')
        self.assertEqual(hits, [])


class KnowledgeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_retrieves_knowledge_and_explains_endpoints(self):
        with tempfile.TemporaryDirectory() as state:
            async with Client(create_server(state)) as client:
                self.assertIn('FilmCraft', client.instructions)
                info = (await client.call_tool('describe_operations', {})).structured_content
                self.assertIn('reference/creative-knowledge', info['guides'])
                self.assertIn('reference/course-techniques-openedit', info['guides'])
                self.assertIn('multimodal', info['operations']['music-edit'])
                for name in GUIDES + ('reference/course-techniques-openedit',):
                    result = (await client.call_tool('read_guide', {'name': name})).structured_content
                    self.assertFalse(result.get('truncated'), name)
                    self.assertTrue(result['text'].strip(), name)
                capability = (await client.call_tool('read_guide', {'name': 'capabilities'})).structured_content
                self.assertFalse(capability['truncated'])
                self.assertIn('<a id="effectcraft-overlays"></a>', capability['text'])
                denied = (await client.call_tool('read_guide', {'name': '../../private'})).structured_content
                self.assertIn('error', denied)

    async def test_fresh_stdio_mcp_can_discover_and_read_knowledge(self):
        with tempfile.TemporaryDirectory() as state:
            env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LOCALAPPDATA',
                                             'USERPROFILE', 'TEMP', 'TMP') if k in os.environ}
            env['PYTHONPATH'] = str(root / 'src')
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'openedit_engine.mcp_server', '--state-dir', state], env=env)
            async with Client(params) as client:
                self.assertIn('FilmCraft', client.instructions)
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
