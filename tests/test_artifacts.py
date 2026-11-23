import json
from pathlib import Path
import tempfile
import unittest

from openedit_engine import artifacts


class ArtifactClassification(unittest.TestCase):
    def test_open_source_projects_and_exchange_files_remain_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = ['timeline.fcproj', 'overlay.ecproj', 'cut.otio', 'cut.fcpxml', 'shot.avi']
            for name in names:
                (root / name).write_bytes(b'\xff\x00native-or-media-data')
            (root / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
                {'path': name} for name in names]}), encoding='utf-8')
            entries = [item for item in artifacts.discover(root) if item['category'] == 'deliverable']
            self.assertEqual({entry['name'] for entry in entries}, set(names))
            by_name = {entry['name']: entry for entry in entries}
            self.assertEqual(by_name['timeline.fcproj']['type_label'], 'FilmCraft工程')
            self.assertEqual(by_name['overlay.ecproj']['type_label'], 'EffectCraft工程')
            self.assertEqual(by_name['cut.otio']['type_label'], '时间轴交换文件')
            self.assertEqual(by_name['cut.fcpxml']['type_label'], 'FCPXML时间轴')
            for entry in entries:
                self.assertEqual(entry['preview_kind'] if 'preview_kind' in entry else 'file', 'file')

    def test_delivery_manifest_and_process_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / 'film'
            project.mkdir()
            for relative in [
                'out/project.fcproj',
                'out/edit-plan.json',
                'out/previews/frame-0.png',
                'out/overlays/title.ecproj',
                'scout/01.jpg',
                'media.json',
                '剪辑交付.md',
            ]:
                p = project / relative
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('{}', encoding='utf-8')
            (project / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
                {'path': 'out/project.fcproj', 'title': 'FilmCraft工程'},
                {'path': '剪辑交付.md', 'title': '交付说明'},
            ]}), encoding='utf-8')
            entries = artifacts.discover(root)
            delivered = [e for e in entries if e['category'] == 'deliverable']
            self.assertEqual({e['display_name'] for e in delivered}, {'FilmCraft工程', '交付说明'})
            self.assertEqual(next(e for e in entries if Path(e['name']).name == 'frame-0.png')['category'], 'process')
            self.assertEqual(next(e for e in entries if Path(e['name']).name == 'title.ecproj')['category'], 'process')
            self.assertEqual(next(e for e in entries if Path(e['name']).name == '01.jpg')['category'], 'process')
            self.assertEqual(next(e for e in entries if Path(e['name']).name == 'media.json')['category'], 'process')
            self.assertEqual(next(e for e in entries if Path(e['name']).name == 'edit-plan.json')['category'], 'project')
            self.assertTrue(all(e['project'] == 'film' for e in entries))

    def test_manifest_cannot_register_outside_source_or_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            outside = root / 'outside.md'
            outside.write_text('private')
            source = workspace / 'input.mp4'
            source.write_text('source')
            (workspace / 'empty.srt').write_text('')
            (workspace / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
                {'path': '../outside.md'}, {'path': str(outside)}, {'path': 'input.mp4'},
                {'path': 'empty.srt'}, {'path': ''},
            ]}), encoding='utf-8')
            entries = artifacts.discover(workspace, [{'path': str(source), 'kind': 'folder'}])
            self.assertFalse(any(e['category'] == 'deliverable' for e in entries))
            self.assertFalse(any(e['path'] == str(outside) for e in entries))
            with self.assertRaises(ValueError):
                artifacts.resolve_file(workspace, '../outside.md')

    def test_deep_project_declarations_remain_local_to_each_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative in ['collection/season/film-a', 'collection/season/film-b']:
                project = root / relative
                project.mkdir(parents=True)
                (project / '交付说明.md').write_text('delivery', encoding='utf-8')
                (project / 'workbench-delivery.json').write_text(
                    json.dumps({'schema_version': 1, 'items': [{'path': '交付说明.md'}]}), encoding='utf-8')
            delivered = [e for e in artifacts.discover(root) if e['category'] == 'deliverable']
            self.assertEqual(len(delivered), 2)
            self.assertEqual(len({e['project'] for e in delivered}), 2)


if __name__ == '__main__':
    unittest.main()
