import json
from pathlib import Path
import tempfile
import unittest

from jianying_engine import artifacts


class ArtifactClassification(unittest.TestCase):
    def test_standalone_effect_projects_and_exchange_files_remain_visible_without_binary_decoding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = ['shot.aep', 'scene.blend', 'cut.prproj', 'cut.kdenlive', 'cut.otio', 'shot.avi']
            for name in names:
                (root / name).write_bytes(b'\xff\x00native-or-media-data')
            (root / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
                {'path': name} for name in names]}), encoding='utf-8')
            entries = [item for item in artifacts.discover(root) if item['category'] == 'deliverable']
            self.assertEqual({entry['name'] for entry in entries}, set(names))
            for entry in entries:
                self.assertEqual(entry['preview_kind'], 'file')
                self.assertEqual(artifacts.registered_path(root, entries, entry['id'])[1].name, entry['name'])

    def test_resolve_project_is_delivered_as_native_file_without_text_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / 'project.drp'
            project.write_bytes(b'PK\x03\x04\xffbinary native project')
            (root / 'workbench-delivery.json').write_text(json.dumps({'schema_version': 1, 'items': [
                {'path': 'project.drp', 'title': '达芬奇可编辑工程'}]}), encoding='utf-8')
            entries = artifacts.discover(root)
            entry = next(e for e in entries if e['name'] == 'project.drp')
            self.assertEqual((entry['category'], entry['preview_kind'], entry['type']),
                             ('deliverable', 'file', '达芬奇工程'))
            self.assertEqual(artifacts.registered_path(root, entries, entry['id'])[1], project)

    def test_delivery_manifest_and_draft_bundle_exclude_work_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'film';project.mkdir()
            for relative in ['draft-v1/draft_content.json','draft-v2/draft_content.json',
                             'draft-v2/verification.json','draft-v2/subdraft/child/draft_content.json',
                             'scout/01.jpg','native-v2-final/contact.jpg','media.json','edit-plan.json','剪辑交付.md']:
                p=project/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}',encoding='utf-8')
            (project/'workbench-delivery.json').write_text(json.dumps({'schema_version':1,'items':[
                {'path':'draft-v2','title':'正式可编辑草稿'},{'path':'剪辑交付.md','title':'交付说明'}]}),encoding='utf-8')
            entries=artifacts.discover(root)
            delivered=[e for e in entries if e['category']=='deliverable']
            self.assertEqual({e['display_name'] for e in delivered},{'正式可编辑草稿','交付说明'})
            self.assertEqual(len([e for e in entries if e['type']=='draft']),2)
            self.assertFalse(any('subdraft' in e['path'] or e['name']=='verification.json' for e in entries))
            self.assertEqual(next(e for e in entries if e['name']=='01.jpg')['category'],'process')
            self.assertEqual(next(e for e in entries if e['name']=='media.json')['category'],'process')
            self.assertEqual(next(e for e in entries if e['name']=='edit-plan.json')['category'],'project')
            self.assertTrue(all(e['project']=='film' for e in entries))

    def test_undeclared_images_and_drafts_are_not_formal_deliverables(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ['01.jpg','media.json','draft/draft_content.json','draft/字幕.srt']:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
            entries=artifacts.discover(root)
            self.assertFalse(any(e['category']=='deliverable' for e in entries))
            self.assertEqual(len(entries),3)

    def test_manifest_cannot_register_outside_source_or_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';workspace.mkdir()
            outside=root/'outside.md';outside.write_text('private')
            source=workspace/'input.mp4';source.write_text('source')
            (workspace/'empty.srt').write_text('')
            (workspace/'workbench-delivery.json').write_text(json.dumps({'schema_version':1,'items':[
                {'path':'../outside.md'},{'path':str(outside)},{'path':'input.mp4'},{'path':'empty.srt'},{'path':''}]}),encoding='utf-8')
            entries=artifacts.discover(workspace,[{'path':str(source),'kind':'video'}])
            self.assertFalse(any(e['category']=='deliverable' for e in entries))
            self.assertFalse(any(e['path']==str(outside) for e in entries))

    def test_existing_cached_entries_are_reclassified_and_bundle_members_collapsed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);draft=root/'draft';draft.mkdir()
            (draft/'draft_content.json').write_text('{}');(draft/'verification.json').write_text('{}')
            cached=[{'id':str(i),'name':p.name,'path':str(p),'type':'draft' if p.name=='draft_content.json' else '数据'}
                    for i,p in enumerate(draft.iterdir())]
            entries=artifacts.categorize(root,cached)
            self.assertEqual(len(entries),1)
            self.assertEqual(entries[0]['category'],'project')

    def test_deep_project_declarations_remain_local_to_each_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for relative in ['collection/season/film-a','collection/season/film-b']:
                project=root/relative;project.mkdir(parents=True)
                (project/'交付说明.md').write_text('delivery',encoding='utf-8')
                (project/'workbench-delivery.json').write_text(json.dumps({'schema_version':1,'items':[{'path':'交付说明.md'}]}),encoding='utf-8')
            delivered=[e for e in artifacts.discover(root) if e['category']=='deliverable']
            self.assertEqual(len(delivered),2)
            self.assertEqual(len({e['project'] for e in delivered}),2)


if __name__=='__main__':unittest.main()
