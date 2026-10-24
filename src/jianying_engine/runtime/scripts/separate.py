"""Offline human-voice separation for a plan's audio: htdemucs ONNX, no torch, no host token.

Reads any media ffmpeg can decode, resamples it to 44.1 kHz stereo, runs the demucs ONNX model in
overlapping chunks, and writes `vocals.wav` plus `accompaniment.wav` (optionally all four stems) into
a new output directory, with a `separation.json` report. The model and `onnxruntime` are the caller's
prerequisites: this Skill ships neither and never installs packages.

Chunking lives here because the model has no front end: the input is the raw waveform and the segment
length comes from the model's own input shape. Overlapping chunks are combined with a triangular
window; the zero-padded tail of the last chunk is discarded rather than extrapolated.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import editor

RATE = 44100
CHANNELS = 2
STEM_NAMES = ('drums', 'bass', 'other', 'vocals')
MODEL_NAME = 'htdemucs_fp16weights.onnx'
MODEL_BYTES = 165612636
MODEL_SOURCE = 'hf-mirror StemSplitio/htdemucs-onnx'


def decode(path):
    """Decode any media to (samples, 2) float32 at 44.1 kHz through ffmpeg, then read it with the stdlib."""
    with tempfile.TemporaryDirectory(prefix='separation-') as tmp:
        target = Path(tmp) / 'decoded.wav'
        editor.run([editor.tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-i', str(path), '-vn', '-ac', str(CHANNELS), '-ar', str(RATE),
                    '-c:a', 'pcm_s16le', str(target)], timeout=600)
        with wave.open(str(target), 'rb') as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != CHANNELS:
                raise ValueError('ffmpeg did not deliver 16-bit stereo PCM for separation')
            raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2')
    samples = raw.reshape(-1, CHANNELS).astype(np.float32) / 32768.0
    if samples.shape[0] == 0:
        raise ValueError(f'{Path(path).name} decoded to 0 audio samples; separation needs audible audio')
    return samples


def model_path(explicit):
    """--model, then JIANYING_DEMUCS_MODEL; the Skill does not ship the weights."""
    candidate = explicit or os.environ.get('JIANYING_DEMUCS_MODEL')
    if not candidate:
        raise ValueError(
            f'Offline separation needs the demucs ONNX model: pass --model PATH or set JIANYING_DEMUCS_MODEL. '
            f'This Skill ships no weights; the file measured here is {MODEL_NAME} '
            f'({MODEL_BYTES:,} B) from {MODEL_SOURCE}.')
    path = Path(os.path.expanduser(os.path.expandvars(str(candidate)))).resolve()
    if not path.is_file():
        raise ValueError(f'Demucs model is not a file: {path}')
    return path


def session_for(model):
    """Open the model, and take the segment length from its own input instead of assuming 7.8 s."""
    try:
        import onnxruntime as ort
    except ImportError:
        raise ValueError(
            'Human voice separation needs onnxruntime in the interpreter running this script; '
            'install it there (pip install onnxruntime). This Skill never installs packages itself, '
            'and torch is not required.')
    try:
        session = ort.InferenceSession(str(model), providers=['CPUExecutionProvider'])
    except Exception as error:  # onnxruntime raises its own RuntimeError family, not OSError
        raise ValueError(f'onnxruntime could not initialize {model.name}: {error}. '
                         'A "bad allocation" here is memory pressure: close other applications and retry.')
    waveform = session.get_inputs()[0]
    stems = session.get_outputs()[0]
    if len(waveform.shape) != 3 or waveform.shape[1] != CHANNELS:
        raise ValueError(f'Demucs model input must be [1, {CHANNELS}, samples], got {waveform.shape}')
    if len(stems.shape) != 4 or stems.shape[1] != len(STEM_NAMES):
        raise ValueError(f'Demucs model must return {len(STEM_NAMES)} stems, got {stems.shape}')
    return session, int(waveform.shape[2]), session.get_providers()


def separate(mix, session, segment, overlap, verbose):
    """Overlapping chunk inference + triangular window; the padded tail is zeroed, never extrapolated."""
    samples = mix.shape[0]
    hop = max(1, int(segment * (1.0 - overlap)))
    window = np.concatenate([np.arange(1, segment // 2 + 1),
                             np.arange(segment - segment // 2, 0, -1)]).astype(np.float32)
    window /= window.max()
    accumulated = np.zeros((len(STEM_NAMES), samples, CHANNELS), dtype=np.float64)
    weight = np.zeros(samples, dtype=np.float64)
    chunks = 0
    started = time.time()
    for start in range(0, samples, hop):
        chunk = mix[start:start + segment]
        if chunk.shape[0] < segment:
            chunk = np.pad(chunk, ((0, segment - chunk.shape[0]), (0, 0)))
        stems = session.run(None, {'mix': chunk.T[None].astype(np.float32)})[0][0]
        take = min(segment, samples - start)
        for index in range(len(STEM_NAMES)):
            accumulated[index, start:start + take] += stems[index].T[:take] * window[:take, None]
        weight[start:start + take] += window[:take]
        chunks += 1
        if verbose:
            print(f'chunk {chunks}: start={start} samples={take}', file=sys.stderr, flush=True)
    quiet = weight < 0.01
    weight = np.maximum(weight, 1e-6)
    return ([np.where(quiet[:, None], 0.0, accumulated[index] / weight[:, None])
             for index in range(len(STEM_NAMES))], chunks, time.time() - started)


def write_wav(path, data):
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype('<i2')
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(data.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            checksum.update(block)
    return checksum.hexdigest().upper()


def run_separation(args):
    source = Path(os.path.expanduser(os.path.expandvars(args.input)))
    if not source.is_file():
        raise ValueError(f'Input media is not a file: {source}')
    out = Path(args.out).resolve()
    if out.exists():
        raise ValueError(f'Output directory already exists; build a new named version: {out}')
    if not 0.0 <= args.overlap < 0.5:
        raise ValueError('--overlap must be in [0, 0.5): the window needs more than half a segment')

    model = model_path(args.model)
    session, segment, providers = session_for(model)
    mix = decode(source)
    stems, chunks, elapsed = separate(mix, session, segment, args.overlap, args.verbose)

    out.mkdir(parents=True)
    vocals = stems[STEM_NAMES.index('vocals')]
    accompaniment = np.sum([stems[index] for index in range(len(STEM_NAMES))
                            if STEM_NAMES[index] != 'vocals'], axis=0)
    write_wav(out / 'vocals.wav', vocals)
    write_wav(out / 'accompaniment.wav', accompaniment)
    if args.stems == 'all':
        for name, data in zip(STEM_NAMES, stems):
            write_wav(out / f'stem-{name}.wav', data)

    reconstruction = np.sum(stems, axis=0)
    mix_norm = float(np.linalg.norm(mix.ravel()))
    report = {
        'status': 'passed',
        'input': str(source),
        'out': str(out),
        'model': {'path': str(model), 'bytes': model.stat().st_size, 'sha256': digest(model),
                  'segment': segment, 'stems': list(STEM_NAMES)},
        'providers': list(providers),
        'sample_rate': RATE,
        'channels': CHANNELS,
        'samples': int(mix.shape[0]),
        'seconds': round(mix.shape[0] / RATE, 3),
        'overlap': args.overlap,
        'chunks': chunks,
        'inference_seconds': round(elapsed, 2),
        # Relative error is undefined for silence; RMS below still measures any output signal.
        'reconstruction_rel_err': (round(float(np.linalg.norm((reconstruction - mix).ravel())) /
                                         mix_norm, 6) if mix_norm else None),
        'stem_rms': {name: round(float(np.sqrt((stems[index] ** 2).mean())), 5)
                     for index, name in enumerate(STEM_NAMES)},
        'files': {path.name: path.stat().st_size for path in sorted(out.glob('*.wav'))},
        'boundaries': [
            'CPU only, fp16 weights; both the model and onnxruntime are caller-supplied.',
            'Stems sum back to the mixture inside the windowed region; the padded tail is discarded.',
            'Real music was not measured here: stem order follows the demucs convention and was only '
            'cross-checked by energy on a synthetic mix (no drums, no bass).',
        ],
    }
    (out / 'separation.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', required=True, help='media ffmpeg can decode')
    parser.add_argument('--out', required=True, help='new directory for the stems and report')
    parser.add_argument('--model', help=f'demucs ONNX weights, else JIANYING_DEMUCS_MODEL (e.g. {MODEL_NAME})')
    parser.add_argument('--stems', default='pair', choices=('pair', 'all'),
                        help="'pair' writes vocals + accompaniment, 'all' adds the four raw stems")
    parser.add_argument('--overlap', type=float, default=0.25, help='chunk overlap fraction, default 0.25')
    parser.add_argument('--verbose', action='store_true', help='chunk progress on stderr')
    args = parser.parse_args(argv)
    try:
        report = run_separation(args)
    except Exception as error:  # keep the JSON-only stdout contract, like editor.py does
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
