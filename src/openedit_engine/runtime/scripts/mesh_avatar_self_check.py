import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import mesh_avatar

class MeshAvatarChecks(unittest.TestCase):
    def setUp(self):
        self.holder=tempfile.TemporaryDirectory(); self.root=Path(self.holder.name)
    def tearDown(self): self.holder.cleanup()
    def test_keyframes_reject_unknown_out_of_range_and_unsorted(self):
        p=self.root/'motion.json'
        for obj in ({'bogus':[{'time':0,'value':0}]},{'mouthOpen':[{'time':0,'value':2}]},{'angleX':[{'time':1,'value':0},{'time':0,'value':0}]}):
            p.write_text(json.dumps(obj),encoding='utf-8')
            with self.assertRaises(ValueError): mesh_avatar.read_keys(p,2)
    def test_configured_studio_and_missing_install(self):
        studio=self.root/'studio'; (studio/'packages/runtime/dist').mkdir(parents=True)
        (studio/'packages/runtime/dist/index.js').write_text('',encoding='utf-8')
        (studio/'node_modules/playwright').mkdir(parents=True)
        with patch.dict('os.environ', {'LOCALAPPDATA':str(self.root)}):
            with self.assertRaisesRegex(ValueError,'not configured'): mesh_avatar.studio_path(None)
            config=mesh_avatar.config_path(); config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'studio':str(studio)}),encoding='utf-8')
            self.assertEqual(mesh_avatar.studio_path(None),studio)
            (studio/'packages/runtime/dist/index.js').unlink()
            with self.assertRaisesRegex(ValueError,'built runtime'): mesh_avatar.studio_path(None)
    def test_attachment_preserves_original_and_rejects_tamper(self):
        from PIL import Image
        Image.new('RGB',(1280,720),(10,20,40)).save(self.root/'bg.png')
        video=self.root/'avatar.mov'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red@0.5:s=1280x720:r=30:d=1,format=rgba','-c:v','png','-pix_fmt','rgba',str(video)],check=True,capture_output=True)
        receipt={'video':str(video),'video_sha256':mesh_avatar.digest(video),'duration':1}
        (self.root/'receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
        plan={'schema_version':1,'name':'mesh-check','mode':'script','canvas':{'width':1280,'height':720,'fps':30},'layer_order':['video'],'clips':[{'path':'bg.png','start':0,'duration':1}]}
        source=self.root/'base.json'; source.write_text(json.dumps(plan),encoding='utf-8'); before=source.read_bytes()
        out=self.root/'new.json'; args=SimpleNamespace(plan=source,video=video,out=out,start=0,track='avatar')
        result=mesh_avatar.attach(args)
        self.assertEqual(result['status'],'validated'); self.assertEqual(source.read_bytes(),before)
        self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['layer_order'],['video','avatar'])
        with self.assertRaises(FileExistsError): mesh_avatar.attach(args)
        video.write_bytes(video.read_bytes()+b'changed'); args.out=self.root/'tampered.json'
        with self.assertRaisesRegex(ValueError,'receipt'): mesh_avatar.attach(args)
        self.assertFalse(args.out.exists())
    def test_video_inspection_is_bounded_and_records_timed_frames(self):
        from PIL import Image
        from unittest.mock import patch
        source=self.root/'source.mp4'; source.write_bytes(b'video')
        def run(cmd,**kwargs):
            target=Path(cmd[-1]); Image.new('RGB',(640,360),(10,20,30)).save(target)
            return SimpleNamespace(returncode=0,stderr='')
        args=SimpleNamespace(video=source,start=1.0,duration=2.0,samples=3,out=self.root/'inspection')
        with patch.object(mesh_avatar,'video_info',return_value=10.0), patch.object(mesh_avatar.shutil,'which',return_value='ffmpeg'), patch.object(mesh_avatar.subprocess,'run',side_effect=run):
            report=mesh_avatar.inspect_video(args)
        self.assertEqual([f['time'] for f in report['frames']],[1.0,2.0,3.0])
        self.assertEqual(len(report['frames']),3)
        self.assertTrue((args.out/'contact-sheet.jpg').is_file())
        args.samples=21; args.out=self.root/'bad'
        with patch.object(mesh_avatar,'video_info',return_value=10.0), patch.object(mesh_avatar.shutil,'which',return_value='ffmpeg'):
            with self.assertRaisesRegex(ValueError,'3 to 20'): mesh_avatar.inspect_video(args)
    def test_status_prores_1080p_attach_and_render_bounds(self):
        from PIL import Image
        with patch.dict('os.environ', {'LOCALAPPDATA': str(self.root)}):
            rep = mesh_avatar.status(SimpleNamespace(studio=None))
            self.assertEqual(rep['status'], 'unconfigured')
            self.assertFalse(rep['available'])
        Image.new('RGB', (1920, 1080), (10, 20, 40)).save(self.root / 'bg_hd.png')
        hd_mov = self.root / 'avatar_hd.mov'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue@0.5:s=1920x1080:r=30:d=1,format=rgba',
            '-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuva444p12le', str(hd_mov),
        ], check=True, capture_output=True)
        hd_receipt = self.root / 'hd_dir'
        hd_receipt.mkdir()
        hd_video = hd_receipt / 'avatar.mov'
        hd_mov.replace(hd_video)
        (hd_receipt / 'receipt.json').write_text(json.dumps({
            'video': str(hd_video), 'video_sha256': mesh_avatar.digest(hd_video), 'duration': 1.0,
        }), encoding='utf-8')
        plan_hd = {
            'schema_version': 2, 'name': 'mesh-hd',
            'canvas': {'width': 1920, 'height': 1080, 'fps': 60},
            'layer_order': ['video'],
            'clips': [{'path': 'bg_hd.png', 'start': 0, 'duration': 1}],
        }
        src_hd = self.root / 'hd.json'
        src_hd.write_text(json.dumps(plan_hd), encoding='utf-8')
        out_hd = self.root / 'hd_out.json'
        res = mesh_avatar.attach(SimpleNamespace(plan=src_hd, video=hd_video, out=out_hd, start=0, track='avatar'))
        self.assertEqual(res['status'], 'validated')
        self.assertEqual(json.loads(out_hd.read_text(encoding='utf-8'))['layer_order'], ['video', 'avatar'])

        studio = self.root / 'studio_v'
        (studio / 'packages/runtime/dist').mkdir(parents=True)
        (studio / 'packages/runtime/dist/index.js').write_text('', encoding='utf-8')
        (studio / 'node_modules/playwright').mkdir(parents=True)
        av = self.root / 'dummy.mavatar'
        av.write_bytes(b'mavatar')
        for bad_kw in (
            {'width': 1921, 'height': 1080, 'fps': 30, 'codec': 'prores_4444', 'fit': 'contain', 'frames': 10},
            {'width': 1920, 'height': 1080, 'fps': 30, 'codec': 'mp4', 'fit': 'contain', 'frames': 10},
            {'width': 1920, 'height': 1080, 'fps': 60, 'codec': 'png', 'fit': 'stretch', 'frames': 10},
        ):
            r_args = SimpleNamespace(studio=studio, avatar=av, out=self.root / 'ro', keyframes=None, browser=None, **bad_kw)
            with self.assertRaises(ValueError):
                mesh_avatar.render(r_args)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(MeshAvatarChecks)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    (a.out/'mesh-avatar-check.json').write_text(json.dumps({'tests':result.testsRun,'successful':result.wasSuccessful()}),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
