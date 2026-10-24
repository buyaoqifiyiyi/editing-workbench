import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from jianying_engine.workbench import Workbench, create_app
from test_workbench import available_backends


@patch('jianying_engine.workbench.detect_backends', new=available_backends)
class BulkMedia(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Workbench(self.root / 'state', self.root)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def files(self, count):
        folder = self.root / 'media'
        folder.mkdir()
        paths = []
        for index in range(count):
            path = folder / (f'{index:04}-original-video.mp4')
            path.write_bytes(b'original')
            paths.append(str(path))
        return folder, paths

    async def test_500_files_have_no_quantity_cap_and_reopen_without_session(self):
        _, paths = self.files(500)
        self.service.add_media(paths + paths)
        self.assertEqual(len(self.service.media), 500)
        restored = Workbench(self.root / 'state', self.root)
        self.assertEqual(restored.media, self.service.media)
        self.service.new_session()
        self.assertEqual(len(Workbench(self.root / 'state', self.root).media), 500)
        self.assertTrue(all(Path(path).read_bytes() == b'original' for path in paths))

    async def test_folder_is_one_reference_and_prompt_sends_only_its_path(self):
        folder, paths = self.files(200)
        with patch('jianying_engine.workbench.os.walk', side_effect=AssertionError('Must not enumerate folder')):
            self.service.add_media([str(folder), str(folder)])
        self.assertEqual(len(self.service.media), 1)
        self.assertEqual(self.service.media[0]['kind'], 'folder')
        self.assertIsNone(self.service.media[0]['url'])

        class Bridge:
            async def start_thread(self, *args):
                return 'fake-thread'
            async def start_turn(self, identity, text, **kwargs):
                self.text = text
                return {'id': 'fake-turn'}
            async def close(self):
                pass

        bridge = Bridge()
        self.service.bridge = bridge
        self.service.agent.update(state='connected', protocol='acp')
        await self.service.send('读取这个文件夹，然后按要求剪辑；仅模拟传输。')
        self.assertIn(str(folder), bridge.text)
        self.assertIn('[文件夹]', bridge.text)
        self.assertIn('尚未扫描', bridge.text)
        self.assertNotIn(paths[0], bridge.text)
        # Software routing now contributes its own bounded context; folder references stay compact.
        self.assertLess(len(self.service._media_prompt()), 1500)

    async def test_manifest_keeps_all_explicit_file_paths_without_replay(self):
        _, paths = self.files(500)
        self.service.add_media(paths)
        prompt = self.service._media_prompt()
        first = next((self.service.directory / 'media-manifests').glob('*.json'))
        data = json.loads(first.read_text(encoding='utf-8'))
        self.assertEqual(data['count'], 500)
        self.assertEqual([item['path'] for item in data['media']], paths)
        self.assertIn(str(first), prompt)
        self.assertLess(len(prompt), 1500)
        self.service._media_prompt()
        self.assertEqual(len(list(first.parent.glob('*.json'))), 2)
        self.assertEqual(json.loads(first.read_text(encoding='utf-8')), data)

    async def test_folder_reference_api_does_not_stream_directory_and_removal_keeps_source(self):
        folder, paths = self.files(200)
        app = create_app(self.service, 'token', 8765)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://127.0.0.1:8765',
                                     headers={'X-Workbench-Token': 'token'}) as client:
            added = await client.post('/api/media', json={'paths': [str(folder)]})
            self.assertEqual(added.status_code, 200)
            media = added.json()['media']
            self.assertEqual(len(media), 1)
            identity = media[0]['id']
            self.assertEqual((await client.get('/api/media/' + identity)).status_code, 404)
            self.assertEqual((await client.delete('/api/media/' + identity)).status_code, 200)
        self.assertTrue(folder.is_dir())
        self.assertEqual(len(list(folder.iterdir())), 200)
        self.assertEqual(Path(paths[0]).read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
