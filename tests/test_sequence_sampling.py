"""Frame-centre seeks must stay inside their selected project frames."""
from fractions import Fraction
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

root = Path(__file__).resolve().parents[1]
scripts = root / 'src/jianying_engine/runtime/scripts'
if not scripts.is_dir():
    scripts = root / 'jianying_engine/runtime/scripts'
sys.path.insert(0, str(scripts))
import native_preview
import preview_sequence


class SequenceSamplingTests(unittest.TestCase):
    def test_sampled_frames_are_sought_at_their_centres(self):
        for project_fps, sample_fps in ((30, 30), (30, 15), (60, 30), (120, 15)):
            with self.subTest(project_fps=project_fps, sample_fps=sample_fps):
                sequence, times = native_preview.sequence_timing(project_fps, 0, 1, sample_fps)
                for index, value in enumerate(times):
                    frame = sequence['start_frame'] + index * sequence['frame_step']
                    actual = Fraction(round(value * 1_000_000))
                    centre = Fraction((2 * frame + 1) * 1_000_000, 2 * project_fps)
                    self.assertLessEqual(abs(actual - centre), Fraction(1, 2))
                    self.assertGreater(actual, Fraction(frame * 1_000_000, project_fps))
                    self.assertLess(actual, Fraction((frame + 1) * 1_000_000, project_fps))
                native_preview.validate_sequence(sequence, times, 1_000_000, project_fps)

    def test_exact_pcm_end_is_accepted_but_one_sample_beyond_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b'\0\0' * 1_744_000)
            exact_duration = Fraction(592, 30)
            preview_sequence.prepare_audio(str(path), 16.6, 1, exact_duration)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                preview_sequence.prepare_audio(str(path), 16.6, 1,
                                               exact_duration + Fraction(1, 48000))

    def test_missing_audio_clock_keeps_duration_based_range_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b'\0\0' * 48000)
            metadata = {'streams': [{'codec_type': 'audio', 'sample_rate': '48000',
                                     'channels': 1, 'duration': '1.0'}],
                        'format': {'duration': '1.0'}}
            with patch.object(preview_sequence.editor, 'run',
                              return_value=SimpleNamespace(stdout=json.dumps(metadata))):
                preview_sequence.prepare_audio(str(path), 0, 1, Fraction(1, 2))
                with self.assertRaisesRegex(ValueError, 'exceeds'):
                    preview_sequence.prepare_audio(str(path), 0, 1, Fraction(1_000_001, 1_000_000))


if __name__ == '__main__':
    unittest.main()
