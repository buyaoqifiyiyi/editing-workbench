"""Track reuse safety, existing-plan preservation and real draft build regression."""
import argparse
import copy
import io
import json
import random
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import editor
import layout_tracks


class _FakeFilmCraft:
    def __init__(self, calls):
        self.calls = calls
        self.seq = {'video': [{'items': []}], 'audio': [{'items': []}], 'durationFrames': 120}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def call_tool(self, name, params=None):
        params = params or {}
        if name == 'media_import':
            self.names = [Path(p).name for p in params['paths']]
            return {'imported': len(self.names)}
        if name == 'project_inspect':
            return {'root': {'children': [{'item': f'it_{i}', 'name': n} for i, n in enumerate(self.names)]}}
        if name == 'sequence_inspect':
            return self.seq
        if name == 'command_run':
            cid, p = params['id'], params.get('params', {})
            self.calls.append((cid, p))
            if cid == 'file.newSequence':
                self.seq['video'] = [{'items': []} for _ in range(p.get('video', 3))]
                self.seq['audio'] = [{'items': []} for _ in range(p.get('audio', 2))]
            elif cid == 'timeline.place':
                target = self.seq['audio'] if p.get('video') is False else self.seq['video']
                idx = int(p['track'][1:]) - 1
                while len(target) <= idx:
                    target.append({'items': []})
                target[idx]['items'].append({'clip': f'c_{len(self.calls)}', 'start': round(p['seconds'] * editor.TICKS_PER_SEC), 'duration': p['duration']})
            elif cid == 'graphics.newText':
                return {'clip': f'g_{len(self.calls)}'}
            elif cid == 'file.saveAs':
                Path(p['path']).write_text('{}', encoding='utf-8')
        return {}


def clip(track,start,duration,**fields):
    return {'track':track,'start':start,'duration':duration,**fields}


def synthetic(clips,order,composites=None,tasks=None,effects=None):
    return {'clips':clips,'audio':[],'captions':[],'effects':effects or [],'filters':[],
            'composites':composites or [],'manual_tasks':tasks or [],'layer_order':order}


class LayoutTests(unittest.TestCase):
    def test_disjoint_overlays_merge_and_keep_time_and_payload(self):
        plan=synthetic([clip('video',0,8),clip('a',0,2,mask={'name':'rect'}),
                        clip('b',2,2,keyframes=[{'property':'alpha','value':.5}])],['video','a','b'])
        before=copy.deepcopy(plan)
        result,report=layout_tracks.compact(plan)
        self.assertEqual(plan,before)
        self.assertEqual(result['layer_order'],['video','复用叠层1'])
        self.assertEqual(result['clips'][2]['track'],'复用叠层1')
        self.assertEqual(result['clips'][2]['keyframes'],before['clips'][2]['keyframes'])
        self.assertEqual(report['status'],'compacted')

    def test_crossing_intermediate_active_layer_cannot_merge(self):
        plan=synthetic([clip('video',0,8),clip('a',0,2),clip('middle',0,2),
                        clip('middle',4,2),clip('upper',4,2)],['video','a','middle','upper'])
        result,report=layout_tracks.compact(plan)
        self.assertEqual(result['layer_order'],plan['layer_order'])
        self.assertEqual(report['status'],'unchanged')

    def test_effect_between_tracks_is_a_fixed_barrier(self):
        plan=synthetic([clip('video',0,8),clip('a',0,2),clip('b',4,2)],
                       ['video','a','fx','b'],effects=[clip('fx',4,1)])
        result,_=layout_tracks.compact(plan)
        self.assertIn('b',result['layer_order'])
        plan['effects'][0]['start']=2
        result,_=layout_tracks.compact(plan)
        self.assertIn('b',result['layer_order'])

    def test_group_task_and_transition_tracks_are_barriers(self):
        plan=synthetic([clip('video',0,12),clip('a',0,1),clip('group_a',1,1),
                        clip('group_b',1,1),clip('z',4,1),clip('task',6,1),
                        clip('transition',8,1,transition={'name':'x'})],
                       ['video','a','group_a','group_b','z','task','transition'],
                       composites=[{'tracks':['group_a','group_b']}],tasks=[{'track':'task'}])
        result,_=layout_tracks.compact(plan)
        for name in ['group_a','group_b','task','transition']:
            self.assertIn(name,result['layer_order'])
        self.assertIn('z',result['layer_order']) # moving across group is refused

    def test_local_group_outer_range_protects_empty_inner_intervals(self):
        plan=synthetic([clip('video',0,12),clip('a',0,1),clip('group_a',1,.2),
                        clip('group_b',2.8,.2),clip('z',1.4,.5)],
                       ['video','a','group_a','group_b','z'],
                       composites=[{'tracks':['group_a','group_b'],'start':1,'duration':2}])
        result,_=layout_tracks.compact(plan)
        self.assertIn('z',result['layer_order'])
        plan['clips'][-1]['start']=4
        result,_=layout_tracks.compact(plan)
        self.assertNotIn('z',result['layer_order'])
        self.assertEqual(result['composites'],plan['composites'])
        self.assertEqual(result['layer_order'][2:4],['group_a','group_b'])

    def test_overlap_one_microsecond_and_exact_touch(self):
        plan=synthetic([clip('video',0,8),clip('a',0,1),clip('b',.999999,1)],['video','a','b'])
        result,_=layout_tracks.compact(plan)
        self.assertIn('b',result['layer_order'])
        plan['clips'][-1]['start']=1
        result,_=layout_tracks.compact(plan)
        self.assertNotIn('b',result['layer_order'])

    def test_default_layer_mode_untouched(self):
        plan=synthetic([clip('video',0,8),clip('a',0,1),clip('b',2,1)],['video','a','b'])
        del plan['layer_order']
        result,report=layout_tracks.compact(plan)
        self.assertEqual(result,plan)
        self.assertEqual(report['status'],'unchanged')

    def test_existing_multi_clip_role_keeps_its_name_and_members(self):
        plan=synthetic([clip('video',0,10),clip('开场窗',0,1),
                        clip('左页',2,1),clip('左页',4,1),clip('休息幅',7,1)],
                       ['video','开场窗','左页','休息幅'])
        result,_=layout_tracks.compact(plan)
        self.assertIn('左页',result['layer_order'])
        self.assertEqual([c['track'] for c in result['clips'][2:4]],['左页','左页'])
        self.assertLess(len(result['layer_order']),len(plan['layer_order']))

    def test_randomized_overlapping_order_and_source_items_preserved(self):
        randomizer=random.Random(6127)
        merged_runs = 0
        for run in range(100):
            clips=[clip('video',0,30)]
            order=['video']
            for lane in range(6):
                name=f'lane{lane}';order.append(name)
                # Mix candidates with protected pre-existing reusable lanes.
                starts=sorted(randomizer.sample(range(0,28,2),1 if lane<4 else 3))
                clips.extend(clip(name,start,1,token=f'{lane}/{start}') for start in starts)
            plan=synthetic(clips,order)
            result,report=layout_tracks.compact(plan)
            merged_runs += bool(report['merges'])
            self.assertEqual(layout_tracks.ordering_pairs(plan),layout_tracks.ordering_pairs(result))
            for before,after in zip(plan['clips'],result['clips']):
                self.assertEqual({k:v for k,v in before.items() if k!='track'},
                                 {k:v for k,v in after.items() if k!='track'},run)
        self.assertGreater(merged_runs,0,'Random cases must exercise actual merges')

    def test_real_validate_build_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='track-layout-check-') as temp:
            root=Path(temp); image=root/'frame.png'
            Image.new('RGB',(640,360),(75,42,92)).save(image)
            music=root/'music.wav'
            with wave.open(str(music),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000)
                audio.writeframes(b'\x00\x00'*(8000*4))
            output_dir=root/'separate-output';output_dir.mkdir()
            plan={'schema_version':1,'name':'track-layout-fixture','mode':'script',
                  'canvas':{'width':640,'height':360,'fps':30},
                  'clips':[{'path':image.name,'start':0,'duration':4,'track':'video'},
                           {'path':image.name,'start':0,'duration':1,'track':'a'},
                           {'path':image.name,'start':2,'duration':1,'track':'b'}],
                  'audio':[{'path':music.name,'start':0,'duration':4,'track':'music','volume':.7}],
                  'captions':[{'text':'测试字幕','start':1,'duration':1,'track':'captions'}],
                  'layer_order':['video','a','b','captions','music']}
            source=root/'original.json';editor.write_json(source,plan);before=source.read_bytes()
            target=output_dir/'new.json'
            result=layout_tracks.run(source,target)
            self.assertEqual((result['tracks_before'],result['tracks_after']),(5,4))
            self.assertEqual(source.read_bytes(),before)
            normalized,_,_=editor.validate_plan(target)
            self.assertEqual(normalized['layer_order'],['video','复用叠层1','captions','music'])
            self.assertEqual(Path(normalized['clips'][0]['path']),image.resolve())
            self.assertEqual(Path(normalized['audio'][0]['path']),music.resolve())
            calls = []
            with patch('filmcraft_backend.find_cli', return_value='filmcraft-cli'), \
                    patch('craft_rpc.CraftMcpClient', side_effect=lambda *a, **k: _FakeFilmCraft(calls)):
                built=editor.build_draft(SimpleNamespace(plan=str(target),out=str(root/'project')))
            self.assertEqual(built['status'],'passed')
            self.assertEqual(built['video_tracks'],3)
            self.assertTrue(any(cid=='graphics.newText' and p.get('text')=='测试字幕' for cid,p in calls))
            repeated=layout_tracks.run(target,root/'repeat.json')
            self.assertEqual(repeated['status'],'unchanged')
            self.assertIsNone(repeated['output'])
            self.assertFalse((root/'repeat.json').exists())
            with self.assertRaises(ValueError):
                layout_tracks.run(source,target)
            with self.assertRaises(ValueError):
                layout_tracks.run(source,source)
            occupied_report=output_dir/'occupied.layout.json'
            occupied_report.write_text('keep',encoding='utf-8')
            with self.assertRaises(ValueError):
                layout_tracks.run(source,output_dir/'occupied.json')
            self.assertEqual(occupied_report.read_text(encoding='utf-8'),'keep')
            self.assertFalse((output_dir/'occupied.json').exists())
            self.assertEqual(source.read_bytes(),before)

    def test_overlays_track_blocks_overlapping_cross_and_allows_disjoint_merge(self):
        plan=synthetic([clip('video',0,8),clip('a',0,2),clip('b',4,2)],
                       ['video','a','overlay','b'])
        plan['overlays']=[{'name':'fx','track':'overlay','start':4,'duration':2}]
        result,report=layout_tracks.compact(plan)
        self.assertIn('b',result['layer_order'])
        self.assertIn('overlay',result['layer_order'])
        self.assertEqual(report['status'],'unchanged')
        plan['overlays'][0]['start']=2
        plan['overlays'][0]['duration']=1
        result,report=layout_tracks.compact(plan)
        self.assertEqual(report['status'],'compacted')
        self.assertEqual(result['layer_order'],['video','复用叠层1','overlay'])
        self.assertEqual(result['clips'][2]['track'],'复用叠层1')

    def test_blend_mode_track_is_protected(self):
        plan=synthetic([clip('video',0,8),clip('a',0,2),clip('b',4,2,blend_mode='screen')],
                       ['video','a','b'])
        result,report=layout_tracks.compact(plan)
        self.assertEqual(result['layer_order'],['video','a','b'])
        self.assertEqual(report['status'],'unchanged')
        plan2=synthetic([clip('video',0,8),clip('a',0,2,blend_mode='screen'),clip('b',4,2)],
                        ['video','a','b'])
        result2,report2=layout_tracks.compact(plan2)
        self.assertEqual(result2['layer_order'],['video','a','b'])
        self.assertEqual(report2['status'],'unchanged')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=False)
    stream=io.StringIO();suite=unittest.defaultTestLoader.loadTestsFromTestCase(LayoutTests)
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    editor.write_json(args.out/'layout-tracks-self-check.json',
        {'status':'passed' if result.wasSuccessful() else 'failed','tests':result.testsRun,
         'failures':len(result.failures),'errors':len(result.errors),'log':stream.getvalue()})
    editor.emit({'status':'passed' if result.wasSuccessful() else 'failed',
                 'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)})
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
