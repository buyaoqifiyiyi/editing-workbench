"""Offline separation regression: model introspection, ffmpeg resampling, the windowed stitch, and every loud refusal.

Runs real inference, so it needs onnxruntime in this interpreter and the demucs ONNX weights on disk; `separate`'s
own prerequisites are the caller's, and the check refuses/skips the same way the tool does. No desktop control and
no real song: the mixture is generated here, so what is asserted is our own code - chunking, window coverage,
silence in / silence out, the report, and that bad input fails loudly instead of writing empty stems.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import wave

import numpy as np

import editor
import separate

SEGMENT = 343980  # measured from the model's own input shape; asserted below, never assumed
SECONDS = 4.0
RATE = 44100
SILENCE_MAX = 0.05      # measured 0.0000 on a silent mixture; guard keeps the window from inventing signal
RECONSTRUCTION_GUARD = 0.2  # measured 0.0984 on this synthetic mix (the model is out of distribution here)


def write_wav(path, data, rate, channels=None):
    channels = channels or data.shape[1]
    pcm = np.clip(data, -1.0, 1.0)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm * 32767.0).astype('<i2').tobytes())
    return path


def write_fixture(path, rate, channels, speech_gain=1.6, music_gain=0.9):
    """A 48 kHz fixture on purpose: separation must resample it through ffmpeg on the way in."""
    rng = np.random.default_rng(11)
    count = int(SECONDS * rate)
    speech = rng.normal(0, 1e-4, count)
    for begin, end in ((0.5, 1.5), (2.5, 3.5)):
        span = slice(int(begin * rate), int(end * rate))
        speech[span] += rng.normal(0, 0.25, span.stop - span.start)
    t = np.arange(count) / rate
    music = 0.15 * np.sin(2 * np.pi * 330 * t) + 0.05 * np.sin(2 * np.pi * 660 * t)
    mono = music * music_gain + speech * speech_gain
    data = np.repeat(mono[:, None], channels, axis=1)
    return write_wav(path, data, rate)


def run_main(arguments):
    """CLI in-process: returns (returncode, report or refusal text); stdout stays out of the way."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = separate.main([str(item) for item in arguments])
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {'status': 'raw', 'text': text}


def check(root, model):
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    session, segment, providers = separate.session_for(model)
    assert segment == SEGMENT, segment
    assert providers == ['CPUExecutionProvider'], providers
    checks.append(f'model: input segment {segment} read from the graph, 4 stems, CPU provider')

    mix_file = write_fixture(root / 'mix-48k.wav', 48000, 2)
    mix = separate.decode(mix_file)
    assert mix.ndim == 2 and mix.shape[1] == 2 and mix.dtype == np.float32, mix.shape
    expected = int(SECONDS * RATE)
    assert abs(mix.shape[0] - expected) <= 64, (mix.shape[0], expected)
    assert float(np.sqrt((mix ** 2).mean())) > 0.01
    checks.append(f'decode: 48 kHz stereo fixture resampled to 44.1 kHz float32 {mix.shape}, frame count within 64 samples')

    stems, chunks, seconds = separate.separate(mix, session, segment, 0.25, verbose=False)
    assert chunks == 1, chunks
    assert len(stems) == len(separate.STEM_NAMES), len(stems)
    for name, data in zip(separate.STEM_NAMES, stems):
        assert data.shape == mix.shape, (name, data.shape)
        assert float(np.sqrt((data ** 2).mean())) > 0, name
    reconstruction = float(np.linalg.norm((np.sum(stems, axis=0) - mix).ravel()) / np.linalg.norm(mix.ravel()))
    assert reconstruction < RECONSTRUCTION_GUARD, reconstruction
    checks.append(f'stitch: one chunk, four stems cover every frame, reconstruction error {reconstruction:.4f} '
                  f'< {RECONSTRUCTION_GUARD}')

    silent, _, _ = separate.separate(np.zeros((RATE, 2), dtype=np.float32), session, segment, 0.25, verbose=False)
    loudest = max(float(np.abs(data).max()) for data in silent)
    assert loudest < SILENCE_MAX, loudest
    checks.append(f'silence: a silent mixture yields near-zero stems (max {loudest:.4f} < {SILENCE_MAX})')

    silent_file = write_wav(root / 'silence.wav', np.zeros((RATE, 2)), RATE)
    silent_out = root / 'silent-report'
    code, silent_report = run_main(['--input', silent_file, '--out', silent_out, '--model', model])
    assert code == 0 and silent_report['status'] == 'passed', silent_report
    assert silent_report['reconstruction_rel_err'] is None, silent_report
    def reject_nonfinite(value):
        raise ValueError('Non-standard JSON number: ' + value)
    saved = json.loads((silent_out / 'separation.json').read_text(encoding='utf-8'),
                       parse_constant=reject_nonfinite)
    assert saved == silent_report and max(saved['stem_rms'].values()) < SILENCE_MAX
    checks.append('silent CLI report: undefined relative error is null, all numbers are standard JSON')

    out = root / 'stems'
    code, report = run_main(['--input', mix_file, '--out', out, '--model', model])
    assert code == 0 and report['status'] == 'passed', report
    vocals = out / 'vocals.wav'
    accompaniment = out / 'accompaniment.wav'
    assert vocals.is_file() and accompaniment.is_file() and (out / 'separation.json').is_file()
    with wave.open(str(vocals), 'rb') as handle:
        assert handle.getframerate() == RATE and handle.getnchannels() == 2, handle.getparams()
        frames = handle.getnframes()
    assert frames == mix.shape[0], (frames, mix.shape[0])
    assert vocals.stat().st_size == 44 + 4 * frames, vocals.stat().st_size
    assert report['model']['bytes'] == model.stat().st_size and len(report['model']['sha256']) == 64
    assert report['chunks'] == 1 and report['seconds'] == SECONDS and report['samples'] == frames
    assert report['providers'] == ['CPUExecutionProvider'] and len(report['boundaries']) >= 3
    assert not (out / 'stem-drums.wav').exists()
    checks.append(f'cli pair: {frames} frames at 44.1 kHz stereo, report matches disk '
                  f'({vocals.stat().st_size} B = 44 + 4 x frames)')

    all_stems = root / 'stems-all'
    code, report = run_main(['--input', mix_file, '--out', all_stems, '--model', model, '--stems', 'all'])
    assert code == 0 and report['status'] == 'passed', report
    assert sorted(path.name for path in all_stems.glob('*.wav')) == sorted(
        ['vocals.wav', 'accompaniment.wav'] + [f'stem-{name}.wav' for name in separate.STEM_NAMES]), \
        sorted(path.name for path in all_stems.glob('*.wav'))
    checks.append('cli all: four raw stems plus the pair, named by the demucs order')

    from_env = root / 'stems-env'
    os.environ['JIANYING_DEMUCS_MODEL'] = str(model)
    try:
        code, report = run_main(['--input', mix_file, '--out', from_env])
    finally:
        del os.environ['JIANYING_DEMUCS_MODEL']
    assert code == 0 and report['status'] == 'passed' and report['model']['path'] == str(model), report
    checks.append('model discovery: JIANYING_DEMUCS_MODEL is honored when --model is absent')

    poster = root / 'poster.png'
    editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'testsrc=s=640x360', '-frames:v', '1', str(poster)])
    refusals = {
        'no model at all': ['--input', mix_file, '--out', root / 'r1'],
        'model is not a file': ['--input', mix_file, '--out', root / 'r2', '--model', root / 'nope.onnx'],
        'output directory exists': ['--input', mix_file, '--out', out, '--model', model],
        'overlap too large': ['--input', mix_file, '--out', root / 'r3', '--model', model, '--overlap', '0.6'],
        'input is not a file': ['--input', root / 'nope.wav', '--out', root / 'r4', '--model', model],
        'input has no audio stream': ['--input', poster, '--out', root / 'r5', '--model', model],
    }
    for label, arguments in refusals.items():
        code, report = run_main(arguments)
        assert code == 1 and report['status'] == 'failed' and report.get('error'), (label, report)
        checks.append(f'refused: {label} -> {report["error"][:90]}')
    for name in ('r1', 'r2', 'r3', 'r4', 'r5'):
        assert not (root / name).exists(), name
    checks.append('refused runs wrote no output directory')

    result = {'status': 'passed', 'checks': checks, 'checks_count': len(checks),
              'separate_inputs': {'fixture': str(mix_file), 'stems': str(out), 'all_stems': str(all_stems),
                                  'from_env': str(from_env), 'model': str(model)},
              'measurements': {'segment': segment, 'chunks': chunks, 'inference_seconds': round(seconds, 2),
                               'reconstruction_rel_err': round(reconstruction, 6), 'silence_max': round(loudest, 5)},
              'ui_compatibility': 'unverified', 'visual_audio_review': 'unverified'}
    editor.write_json(root / 'separate-check.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True,
                        help='htdemucs ONNX weights; the Skill ships none')
    arguments = parser.parse_args()
    editor.sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    model = arguments.model.resolve()
    if not model.is_file():
        editor.emit({'status': 'failed',
                     'error': f'Demucs model is not a file: {model}; pass --demucs-model with the ONNX weights'})
        return 1
    try:
        import onnxruntime  # noqa: F401  - the tool refuses without it, and so does this check
    except ImportError:
        editor.emit({'status': 'failed',
                     'error': 'onnxruntime is not importable in this interpreter; run the separation check with a '
                              'Python that has it (regression skips it with the same reason)'})
        return 1
    report = check(arguments.out.resolve(), model)
    editor.emit(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
