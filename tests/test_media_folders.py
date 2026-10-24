from pathlib import Path
import tempfile
import unittest
from jianying_engine.workbench import Workbench


class MediaFolders(unittest.TestCase):
    def test_folder_reference_dedup_and_explicit_file_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / 'media'
            (media / 'nested').mkdir(parents=True)
            clip = media / 'clip.mp4'
            clip.write_bytes(b'original')
            (media / 'notes.txt').write_text('not media')
            (media / 'nested/picture.PNG').write_bytes(b'picture')
            service = Workbench(root / 'state', root)
            service.add_media([str(media), str(media), str(clip)])
            self.assertEqual([item['kind'] for item in service.media], ['folder', 'video'])
            self.assertEqual(service.media[0]['path'], str(media))
            self.assertIsNone(service.media[0]['url'])
            self.assertTrue(service.media[1]['url'].startswith('/api/media/'))
            self.assertEqual(clip.read_bytes(), b'original')

    def test_empty_folder_is_valid_and_missing_path_does_not_partially_add(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            empty = root / 'empty'
            empty.mkdir()
            service = Workbench(root / 'state', root)
            service.add_media([str(empty)])
            self.assertEqual(service.media[0]['kind'], 'folder')
            before = list(service.media)
            clip = root / 'clip.mp4'
            clip.write_bytes(b'x')
            with self.assertRaises(ValueError):
                service.add_media([str(clip), str(root / 'absent')])
            self.assertEqual(service.media, before)


if __name__ == '__main__':
    unittest.main()
