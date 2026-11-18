"""Pure clock, refusal and encoder checks for preview_sequence; never starts Jianying."""
import argparse
from fractions import Fraction
import inspect
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from unittest.mock import Mock, patch
import wave

from PIL import Image

import editor
import file_hash
import native_preview
import preview_sequence


def _reject(checks, label, expected, action):
    try:
        action()
    except (OSError, ValueError, TypeError) as error:
        if expected.lower() not in str(error).lower():
            raise AssertionError(f'{label}: unexpected error {error}') from error
        checks.append(label)
        return
    raise AssertionError('Accepted: ' + label)


def _write_wav(path, seconds=1.0, sample_rate=48000):
    frames = round(seconds * sample_rate)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b''.join(
            int(8000 * math.sin(2 * math.pi * 220 * index / sample_rate)).to_bytes(
                2, 'little', signed=True)
            for index in range(frames)
        ))


def _write_frames(root, count):
    root.mkdir()
    for index in range(count):
        Image.new('RGB', (320, 180), (20 + index * 25, 40, 180 - index * 15)).save(
            root / f'frame-{index}.png')


def _native_boundary_checks(work, draft, checks):
    """Exercise the Python supervisor with synthetic VEF packets, never an NAPI process."""
    install = work / 'mock-install'
    install.mkdir()
    runtime = install / 'bun.exe'
    runtime.write_bytes(b'synthetic worker; not executable')
    manifest = {'version': install.name, 'files': {'bun.exe': file_hash.sha256(runtime)},
                'runtime': {'file': 'bun.exe', 'sha256': file_hash.sha256(runtime)}}
    sequence, times = native_preview.sequence_timing(30, 0, .2, 30)
    mode = 'valid'
    children = []

    def worker(command, **kwargs):
        output = Path(command[-1]).parent
        request = editor.read_json(command[-1])
        frames, steps = [], []
        for index, value in enumerate(request['times']):
            target = round(value * 1_000_000)
            timestamp = round(request['times'][0] * 1_000_000) if mode == 'stale' else target
            packet = struct.pack('<4sHHIIIIQQ', b'VEF1', 1, 40, 320, 180, 1280, 1,
                                 timestamp, index) + bytes([index, 40, 80, 255]) * (320 * 180)
            (output / f'frame-{index}.vef').write_bytes(packet)
            frames.append({'file': f'frame-{index}.vef', 'targetUs': target, 'timestampUs': target})
            steps.append({'phase': 'frame', 'index': index, 'timestampUs': target,
                          'stableReads': 2 if mode == 'unstable' else 3})
        editor.write_json(output / 'checkpoint.json', {
            'success': True, 'frames': frames,
            'steps': steps + [{'phase': 'calling_shipped_self_termination'}],
        })
        child = Mock(pid=1234, returncode=0)
        if mode == 'timeout':
            child.communicate.side_effect = [subprocess.TimeoutExpired(command, 1), (b'', b'')]
        else:
            child.communicate.return_value = (b'', b'')
        children.append(child)
        return child

    with patch.object(native_preview, 'preview_manifest', return_value=manifest), \
            patch.object(native_preview.subprocess, 'Popen', side_effect=worker) as process:
        # The original five positional arguments, including runtime, remain valid.
        legacy = native_preview.preview(install, draft, work / 'legacy-eight', [.1] * 8, runtime)
        assert len(legacy['frames']) == 8 and legacy['source_draft_unchanged']
        for index, positions in enumerate(([], [.1] * 9, [float('nan')], [-.1], [1])):
            before = process.call_count
            _reject(checks, f'legacy invalid positions {index} refused before worker', '1-8',
                    lambda: native_preview.preview(install, draft, work / f'bad-legacy-{index}', positions))
            assert process.call_count == before
        checks.append('legacy eight-frame positional call remains compatible with a simulated worker')

        valid = native_preview.preview(install, draft, work / 'native-mock-valid', times, sequence=sequence)
        assert len(valid['frames']) == 6 and valid['runtime_unchanged']
        assert valid['visual_review'] == 'unverified'
        for mode in ('stale', 'unstable', 'timeout'):
            _reject(checks, f'{mode} native evidence refused despite successful checkpoint', 'Native preview failed',
                    lambda: native_preview.preview(
                        install, draft, work / f'native-mock-{mode}', times, sequence=sequence))
            if mode == 'timeout':
                children[-1].kill.assert_called_once()
        mode = 'valid'

        native_render = native_preview.preview

        def tampered(*args, **kwargs):
            report = native_render(*args, **kwargs)
            report['frames'][0]['sha256'] = '0' * 64
            return report

        with patch.object(native_preview, 'preview', side_effect=tampered), \
                patch.object(preview_sequence, 'encode_video') as encode:
            _reject(checks, 'self-reported PNG hash cannot admit different bytes', 'PNG identity differs',
                    lambda: preview_sequence.render_sequence(install, draft, work / 'tampered-png', 0, .2))
            encode.assert_not_called()

        # These are real admission guards on the shared path, without patching them away.
        guarded = work / 'unadmitted.json'
        content = editor.read_json(draft)
        content['materials']['drafts'] = [{'chromas': [{}]}]
        editor.write_json(guarded, content)
        before = process.call_count
        _reject(checks, 'sequence preserves nested chroma admission refusal', 'unadmitted nested chroma',
                lambda: native_preview.preview(install, guarded, work / 'bad-chroma', times, sequence=sequence))
        assert process.call_count == before
        runtime.write_bytes(b'changed runtime')
        _reject(checks, 'sequence refuses changed installed runtime identity', 'Unverified',
                lambda: native_preview.preview(install, draft, work / 'bad-runtime', times, sequence=sequence))
        assert process.call_count == before
        runtime.write_bytes(b'synthetic worker; not executable')

    # A success-only receipt must never reach the encoder.
    with patch.object(native_preview, 'preview', return_value={
            'status': 'passed', 'source_draft_unchanged': True}), \
            patch.object(preview_sequence, 'encode_video') as encode:
        _reject(checks, 'success-only native receipt cannot admit a sequence', 'evidence is incomplete',
                lambda: preview_sequence.render_sequence(install, draft, work / 'receipt-only', 0, .2))
        encode.assert_not_called()

def check(root):
    root.mkdir(parents=True, exist_ok=False)
    work = root / 'fixtures'
    work.mkdir()
    checks = []

    parameters = list(inspect.signature(native_preview.preview).parameters.values())
    assert [item.name for item in parameters] == [
        'install', 'draft', 'output', 'times', 'runtime', 'sequence']
    assert parameters[-1].default is None and parameters[-2].default is None
    checks.append('native preview keeps its original positional signature and optional sequence tail')

    sequence, times = native_preview.sequence_timing(30, .5, 1, 30)
    assert sequence == {
        'schema_version': 1, 'start_frame': 15, 'duration_frames': 30,
        'draft_fps': 30, 'sample_fps': 30, 'frame_step': 1, 'frame_count': 30,
    }
    admitted, admitted_times, tolerance, timeout = native_preview.validate_sequence(
        sequence, times, 2_000_000, 30)
    assert admitted == sequence and admitted_times == times
    assert tolerance == 1 and timeout == 80
    gap_us = round(times[1] * 1_000_000) - round(times[0] * 1_000_000)
    assert gap_us > tolerance
    ideal_error = max(
        abs(Fraction(round(value * 1_000_000))
            - Fraction((2 * (sequence['start_frame'] + index) + 1) * 1_000_000, 60))
        for index, value in enumerate(times)
    )
    assert ideal_error <= 1
    checks.append('30fps sequence samples project-frame centers, 1us tolerance and proportional timeout')

    fractional, fractional_times = native_preview.sequence_timing(30, 1 / 30, 7 / 30, 30)
    assert fractional['start_frame'] == 1 and fractional['duration_frames'] == 7
    assert len(fractional_times) == 7 and round(fractional_times[0] * 1_000_000) == 50000
    checks.append('CLI-compatible floating whole-frame fractions normalize to the exact project grid')

    edge, edge_times = native_preview.sequence_timing(30, 0, 7 / 30, 30)
    native_preview.validate_sequence(edge, edge_times, 233333, 30)
    _reject(checks, 'one additional frame beyond a rounded draft end refused', 'exceeds',
            lambda: native_preview.validate_sequence(edge, edge_times, 200000, 30))
    checks.append('exclusive sequence end follows the same rounded-microsecond project clock')

    sequence_15, times_15 = native_preview.sequence_timing(30, 0, 1, 15)
    assert sequence_15['frame_step'] == 2 and sequence_15['frame_count'] == 15
    assert all(round(b * 1_000_000) - round(a * 1_000_000) >= 66_666
               for a, b in zip(times_15, times_15[1:]))
    sequence_60, _ = native_preview.sequence_timing(60, 0, 1, 30)
    assert sequence_60['frame_step'] == 2 and sequence_60['frame_count'] == 30
    sequence_max, times_max = native_preview.sequence_timing(30, 0, 20, 30)
    assert sequence_max['frame_count'] == len(times_max) == 600
    checks.append('15fps divisor sampling, 60fps drafts and the 600-frame ceiling pass')

    mutated = list(times)
    mutated[1] += 1 / 1_000_000
    _reject(checks, 'mutated sequence time refused', 'does not match',
            lambda: native_preview.validate_sequence(sequence, mutated, 2_000_000, 30))
    missing = dict(sequence)
    del missing['frame_step']
    _reject(checks, 'incomplete sequence specification refused', 'complete',
            lambda: native_preview.validate_sequence(missing, times, 2_000_000, 30))
    _reject(checks, 'sample fps not dividing draft fps refused', 'divide',
            lambda: native_preview.sequence_timing(25, 0, 1, 15))
    _reject(checks, 'fractional start outside the project grid refused', 'whole draft frame',
            lambda: native_preview.sequence_timing(30, .01, 1, 30))
    _reject(checks, 'duration beyond 20 seconds refused', '20-second',
            lambda: native_preview.sequence_timing(30, 0, 20.000001, 30))

    draft = work / 'draft_content.json'
    editor.write_json(draft, {
        'duration': 1_000_000,
        'fps': 30,
        'materials': {'videos': [], 'audios': []},
    })
    original_draft = draft.read_bytes()
    prepared = preview_sequence.prepare_sequence(draft, 0, .2, 30)
    assert prepared['sequence']['frame_count'] == 6
    assert draft.read_bytes() == original_draft
    checks.append('plaintext draft clock and source bytes pass without native rendering')
    _native_boundary_checks(work, draft, checks)

    existing = work / 'existing'
    existing.mkdir()
    _reject(checks, 'existing sequence output refused before native start', 'already exists',
            lambda: preview_sequence.render_sequence(
                work / 'missing-install', draft, existing, 0, .2, 30))
    _reject(checks, 'partial audio arguments refused', 'requires explicit',
            lambda: preview_sequence.prepare_sequence(
                draft, 0, .2, 30, audio='not-a-real.wav', audio_source_in=0))
    _reject(checks, 'credential URL audio refused', 'URL',
            lambda: preview_sequence.prepare_sequence(
                draft, 0, .2, 30, audio='https://user:pass@example.test/a.wav',
                audio_source_in=0, audio_volume=1))
    _reject(checks, 'relative audio path refused', 'absolute',
            lambda: preview_sequence.prepare_sequence(
                draft, 0, .2, 30, audio='audio.wav', audio_source_in=0, audio_volume=1))
    _reject(checks, 'audio-only flags without audio refused', 'require audio',
            lambda: preview_sequence.prepare_sequence(
                draft, 0, .2, 30, audio_source_in=0, audio_volume=1))

    ciphertext = work / 'ciphertext.json'
    ciphertext.write_bytes(b'Q0lQSEVSVEVYVA==')
    _reject(checks, 'ciphertext draft refused', 'ciphertext',
            lambda: preview_sequence.prepare_sequence(ciphertext, 0, .2, 30))

    audio = work / 'tone.wav'
    _write_wav(audio)
    audio_before = file_hash.sha256(audio)
    audio_prepared = preview_sequence.prepare_audio(
        str(audio), .2, .5, Fraction(1, 5))
    assert audio_prepared['sha256'] == audio_before
    assert abs(audio_prepared['source_out_seconds'] - .4) < 1e-12
    _reject(checks, 'audio source range overflow refused', 'exceeds',
            lambda: preview_sequence.prepare_audio(
                str(audio), .9, 1, Fraction(1, 5)))
    _reject(checks, 'audio gain above fixed range refused', '0 to 4',
            lambda: preview_sequence.prepare_audio(
                str(audio), 0, 4.1, Fraction(1, 5)))

    video = work / 'not-audio.mp4'
    editor.run([
        editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=30:d=0.5',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video),
    ])
    _reject(checks, 'video used as audio refused', 'single audio file',
            lambda: preview_sequence.prepare_audio(
                str(video), 0, 1, Fraction(1, 5)))

    encode_sequence, _ = native_preview.sequence_timing(30, 0, .2, 30)
    silent_frames = work / 'silent-frames'
    _write_frames(silent_frames, encode_sequence['frame_count'])
    silent = preview_sequence.encode_video(silent_frames, encode_sequence)
    assert silent['technical_decode']['full_decode'] == 'passed'
    assert silent['technical_decode']['frames'] == 6
    assert silent['technical_decode']['audio_codec'] is None
    checks.append('six real PNG files encode to a decoded silent 320x180 H264 review video')

    audio_frames = work / 'audio-frames'
    _write_frames(audio_frames, encode_sequence['frame_count'])
    with_audio = preview_sequence.encode_video(
        audio_frames, encode_sequence, audio_prepared)
    assert with_audio['technical_decode']['audio_codec'] == 'aac'
    assert with_audio['technical_decode']['audio_sample_rate'] == 48000
    assert file_hash.sha256(audio) == audio_before
    checks.append('explicit single-audio range mixes at fixed gain, decodes and remains hash-identical')

    report = {
        'status': 'passed',
        'checks_count': len(checks),
        'checks': checks,
        'clock': {
            'tolerance_us': tolerance,
            'adjacent_frame_gap_us': gap_us,
            'max_abs_project_grid_error_us': float(ideal_error),
            'method': 'exact project-frame center rounded to nearest microsecond',
        },
        'outputs': {
            'silent_review': silent['path'],
            'audio_review': with_audio['path'],
        },
        'native_render': 'not_run_by_self_check',
        'encoder_fixture': 'synthetic PNG sequence; encoder path only',
        'real_native_png_sequence': 'requires coordinated Jianying acceptance run',
        'human_audiovisual_review': 'not_performed',
        'visual_review': 'unverified',
    }
    editor.write_json(root / 'preview-sequence-self-check.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New self-check output directory')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        editor.emit(check(args.out.resolve()))
    except (OSError, ValueError, TypeError, AssertionError) as error:
        editor.emit({'status': 'failed', 'error': str(error)})
        raise SystemExit(1)
