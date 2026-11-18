import argparse
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_avatar

class MeshAvatarProjectChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspection_rejects_bad_span_or_sampling(self):
        src = self.root / 'video.mp4'
        src.write_bytes(b'video')
        args = SimpleNamespace(video=src, start=0.0, duration=1.0, samples=21, out=self.root / 'out')
        with patch.object(mesh_avatar, 'video_info', return_value=5.0), patch.object(mesh_avatar.shutil, 'which', return_value='ffmpeg'):
            with self.assertRaisesRegex(ValueError, '3 to 20'):
                mesh_avatar.inspect_video(args)
            args.samples, args.duration = 3, 6.0
            with self.assertRaisesRegex(ValueError, 'inside the video'):
                mesh_avatar.inspect_video(args)

    def test_inspection_records_timed_frames_and_video_identity(self):
        src = self.root / 'video.mp4'
        src.write_bytes(b'video')
        out = self.root / 'inspection'
        def run(cmd, **kwargs):
            Image.new('RGB', (64, 48), (30, 40, 50)).save(cmd[-1])
            return SimpleNamespace(returncode=0, stderr='')
        args = SimpleNamespace(video=src, start=1.0, duration=2.0, samples=3, out=out)
        with patch.object(mesh_avatar, 'video_info', return_value=8.0), patch.object(mesh_avatar.shutil, 'which', return_value='ffmpeg'), patch.object(mesh_avatar.subprocess, 'run', side_effect=run):
            report = mesh_avatar.inspect_video(args)
        self.assertEqual([x['time'] for x in report['frames']], [1.0, 2.0, 3.0])
        self.assertTrue(all(Path(x['path']).is_relative_to(out) for x in report['frames']))
        self.assertTrue((out / 'contact-sheet.jpg').is_file())
        stored = json.loads((out / 'video-inspection.json').read_text())
        self.assertEqual(stored['video_sha256'], mesh_avatar.digest(src))

    def fixture(self, root):
        studio = root / 'studio'
        project = studio / 'projects' / 'p'
        req = project / 'variant-requests'
        work = project / 'work' / 'video-context'
        (req / 'mouth_a').mkdir(parents=True)
        work.mkdir(parents=True)
        (project / 'variants').mkdir()
        source = project / 'source.png'
        identity = project / 'variants' / 'base.png'
        source.write_bytes(b'source')
        identity.write_bytes(b'source')
        (project / 'work').mkdir(parents=True, exist_ok=True)
        (project / 'work' / 'identity-base.sha256').write_text(mesh_avatar.digest(source) + '\n', encoding='utf-8')
        video = root / 'source.mp4'
        video.write_bytes(b'video-original')
        frame0, frame1 = work / 'frame0.png', work / 'frame1.png'
        frame0.write_bytes(b'frame-zero')
        frame1.write_bytes(b'frame-one')
        mask, prompt = req / 'mouth_a' / 'mask.png', req / 'mouth_a' / 'prompt.md'
        mask.write_bytes(b'mask')
        prompt.write_text('Open A mouth')
        context = {'project':str(project), 'source_image':str(source), 'video':str(video), 'video_sha256':mesh_avatar.digest(video),
                   'source_file_sha256':mesh_avatar.digest(source),
                   'selected_frames':[{'time':1.25,'path':str(frame0),'sha256':mesh_avatar.digest(frame0)},
                                      {'time':7.0,'path':str(frame1),'sha256':mesh_avatar.digest(frame1)}]}
        (work / 'context.json').write_text(json.dumps(context), encoding='utf-8')
        bundle = {'project':str(project), 'source_image':str(source), 'identity_base':str(identity), 'video':str(video), 'video_sha256':mesh_avatar.digest(video), 'source_sha256':mesh_avatar.digest(source), 'identity_base_sha256':mesh_avatar.digest(identity), 'identity_manifest_sha256':mesh_avatar.digest(project / 'work' / 'identity-base.sha256'), 'variants':[{'name':'mouth_a','mask':str(mask),
                  'mask_sha256':mesh_avatar.digest(mask), 'identity_sha256':mesh_avatar.digest(identity),
                  'prompt':str(prompt), 'output':str(project / 'variants' / 'mouth_a.png')}] }
        (req / 'video-context-requests.json').write_text(json.dumps(bundle), encoding='utf-8')
        selection = root / 'selection.json'
        selection.write_text(json.dumps([{'name':'mouth_a','context_frame_indices':[1],
                                         'observed_expression':'wide vowel at selected moment'}]), encoding='utf-8')
        return SimpleNamespace(studio=None, project=project, selection=selection), project, video, frame0, frame1, mask

    def test_variant_assignment_is_one_mask_plus_only_selected_frames(self):
        args, project, video, frame0, frame1, mask = self.fixture(self.root)
        with patch.object(mesh_avatar, 'studio_path', return_value=project.parents[1]):
            result = mesh_avatar.plan_variants(args)
        assignments = json.loads(Path(result['assignments']).read_text(encoding='utf-8'))
        variant = assignments['variants'][0]
        self.assertEqual(variant['video_timestamps'], [7.0])
        self.assertEqual(variant['inputs'], [str(project / 'source.png'), str(mask), str(frame1)])
        self.assertEqual(len(variant['input_sha256']), 3)
        with self.assertRaises(FileExistsError):
            with patch.object(mesh_avatar, 'studio_path', return_value=project.parents[1]):
                mesh_avatar.plan_variants(args)

    def test_tampered_context_inputs_are_rejected_before_assignment(self):
        for kind in ('video', 'frame', 'source', 'identity', 'mask'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                args, project, video, frame0, frame1, mask = self.fixture(Path(temp))
                target = {'video':video,'frame':frame1,'source':project/'source.png','identity':project/'variants'/'base.png','mask':mask}[kind]
                target.write_bytes(b'tampered')
                with patch.object(mesh_avatar, 'studio_path', return_value=project.parents[1]):
                    with self.assertRaisesRegex(ValueError, 'changed after'):
                        mesh_avatar.plan_variants(args)
                self.assertFalse((project / 'work' / 'video-context' / 'imagegen-assignments.json').exists())

    def test_pack_requires_image_mask_and_pose_review_receipt(self):
        args, project, *_ = self.fixture(self.root)
        (project / 'avatar.json').write_text('{}')
        (project / 'rig.json').write_text('{}')
        (project / 'built').mkdir()
        (project / 'built' / 'layers.json').write_text('{}')
        (project / 'built' / 'sprites').mkdir()
        (project / 'built' / 'sprites' / 'sprites.json').write_text('{"layers":{"mouth_a":[0,0,1,1]}}')
        source_video=self.root/'source.mp4'; source_video.write_bytes(b'video-original')
        assignments = {'video':str(source_video),'video_sha256':mesh_avatar.digest(source_video),
                       'source_sha256':mesh_avatar.digest(project/'source.png'),'identity_base_sha256':mesh_avatar.digest(project/'variants'/'base.png'),
                       'variants':[{'name':'mouth_a','inputs':[str(project/'source.png'),str(project/'variant-requests'/'mouth_a'/'mask.png'),str(project/'work'/'video-context'/'frame0.png')],
                                    'input_sha256':[mesh_avatar.digest(project/'source.png'),mesh_avatar.digest(project/'variant-requests'/'mouth_a'/'mask.png'),mesh_avatar.digest(project/'work'/'video-context'/'frame0.png')],
                                    'mask':str(project/'variant-requests'/'mouth_a'/'mask.png'),'output':str(project/'variants'/'mouth_a.png'),
                                    'acceptance_receipt':str(project/'work'/'acceptance'/'mouth_a.json'),
                                    'mask_sha256':'mask-hash'}]}
        manifest=project/'work'/'video-context'/'imagegen-assignments.json'
        manifest.write_text(json.dumps(assignments))
        pack_args=SimpleNamespace(studio=None,project=project,out=self.root/'out.mavatar',require_sprites=True)
        with patch.object(mesh_avatar,'studio_path',return_value=project.parents[1]):
            with self.assertRaisesRegex(ValueError,'missing'):
                mesh_avatar.pack_character(pack_args)
            output=project/'variants'/'mouth_a.png'; output.write_bytes(b'generated')
            receipt=project/'work'/'acceptance'/'mouth_a.json'; receipt.parent.mkdir()
            receipt.write_text(json.dumps({'variant':'mouth_a','image_sha256':mesh_avatar.digest(output),
                'mask_sha256':'mask-hash','mesh_build_sha256':mesh_avatar.digest(project/'built'/'sprites'/'sprites.json'),
                'accepted_outside_mask':True,'reviewed_pose':True}))
            with patch.object(mesh_avatar.subprocess,'run',side_effect=[SimpleNamespace(returncode=1,stderr='stop',stdout='')]):
                with self.assertRaisesRegex(RuntimeError,'pack failed'):
                    mesh_avatar.pack_character(pack_args)

    def test_prepare_variants_requires_built_mesh_layers_before_mask_generation(self):
        _, project, *_ = self.fixture(self.root)
        args=SimpleNamespace(studio=None,project=project)
        with patch.object(mesh_avatar,'studio_path',return_value=project.parents[1]), patch.object(mesh_avatar.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'complete and review Mesh rig/layer build'):
                mesh_avatar.prepare_variants(args)
            run.assert_not_called()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MeshAvatarProjectChecks))
    (args.out / 'mesh-avatar-project-check.json').write_text(json.dumps({'tests':result.testsRun,'successful':result.wasSuccessful()}), encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
