"""Pure unit boundaries; native UI is deliberately outside this candidate."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts'))
import native_beat


class NativeBeatTests(unittest.TestCase):
    def test_no_rounding_or_coercion(self):
        self.assertEqual(native_beat.micros(1.25), 1250000)
        self.assertEqual(native_beat.micros(.000001), 1)
        for value in (True, False, '1', None, .0000001, 1.2500001, float('nan'), float('inf'), -1, 10**1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native_beat.micros(value)

    def test_absent_field_does_not_require_runtime(self):
        with patch.object(native_beat, 'runtime', side_effect=AssertionError('legacy runtime check')):
            native_beat.validate({'audio': []})
            self.assertEqual(native_beat.verify({'materials': {}, 'tracks': []}, {'audio': []}), 0)

    def test_unplanned_nested_beats_refused(self):
        with self.assertRaises(ValueError):
            native_beat.verify({'materials': {'drafts': [{'materials': {'beats': [{'type':'beats'}]}}]}}, {'audio': []})

    def test_generated_material_id_collision_is_retried(self):
        content = {'materials': {'audios': [{'id': 'COLLISION'}]},
                   'tracks': [{'type': 'audio', 'name': 'music', 'segments': [{'extra_material_refs': []}]}]}
        with patch.object(native_beat.uuid, 'uuid4', side_effect=['COLLISION', 'NEW']):
            native_beat.apply(content, {'audio': [{'beat_marks': [.5]}]})
        self.assertEqual(content['materials']['beats'][0]['id'], 'NEW')
        self.assertEqual(content['tracks'][0]['segments'][0]['extra_material_refs'], ['NEW'])

    def test_variable_pts_rejected_even_when_summary_claims_30fps(self):
        import editor
        with tempfile.TemporaryDirectory(prefix='native-beat-vfr-') as directory:
            root = Path(directory)
            video = root / 'variable.mp4'
            editor.run([editor.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=s=320x180:r=30:d=4', '-vf',
                        r'settb=1/90000,setpts=N/(30*TB)+if(eq(mod(N\,2)\,0)\,0\,0.01/TB)',
                        '-fps_mode', 'vfr', '-enc_time_base', '1/90000', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(video)])
            audio = root / 'synthetic.wav'
            plan = {'canvas': {'width': 320, 'height': 180, 'fps': 30},
                    'clips': [{'path': str(video)}], 'audio': [{'path': str(audio), 'beat_marks': [0.5]}]}
            probes = {str(video): {'kind': 'video', 'duration': 4, 'fps': 30, 'codec': 'h264',
                                   'width': 320, 'height': 180, 'has_audio': False},
                      str(audio): {'kind': 'audio', 'duration': 4, 'sample_rate': 44100,
                                   'channels': 1, 'audio_codec': 'pcm_s16le'}}
            with self.assertRaisesRegex(ValueError, 'VFR or discontinuous'):
                native_beat.validate_media(plan, probes)


if __name__ == '__main__':
    unittest.main()
