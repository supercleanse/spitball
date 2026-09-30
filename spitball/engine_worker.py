"""The live transcript's persistent speech-to-text worker.

Run only by spitball/live_engine.py, under the live-engine venv's own Python
(`spitball live setup` creates it) -- never imported by the rest of Spitball,
which stays standard-library only. It loads voxtype's active Parakeet model
ONCE with onnx-asr and then answers one request per stdin line for the rest
of the call, so each live segment costs a fraction of a second instead of
the ~2s a fresh `voxtype transcribe` spends reloading the model every time.

Protocol (one JSON object per line):
  startup  -> {"ready": true, "model": "<name>", "load_s": 1.5}
              or {"error": "..."} and exit 1
  request  <- {"id": 7, "wav": "/path/clip.wav"}   16 kHz mono PCM WAV
  response -> {"id": 7, "text": "..."}  or  {"id": 7, "error": "..."}

Usage: python engine_worker.py <voxtype model dir> <adapter cache dir> <threads>
"""
import json
import sys
import time
import wave
from pathlib import Path


def _say(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _adapter(model_dir: Path, cache: Path) -> tuple:
    """(onnx-asr model type, directory onnx-asr can load, quantization).

    voxtype's TDT downloads (istupakov's exports) already carry onnx-asr's
    own config.json and file names, so they load as they are. The streaming
    parakeet-unified export doesn't: it names its files differently and
    ships a sentencepiece tokenizer instead of onnx-asr's `token id` vocab.
    For that layout, build a small adapter directory of symlinks plus a
    generated vocab.txt and config.json -- never touching voxtype's own
    files -- and pick RNN-T vs TDT from the joint network's output width
    (vocab + blank means plain RNN-T; anything wider carries TDT durations).
    """
    config = model_dir / "config.json"
    if config.exists():
        kind = json.loads(config.read_text()).get("model_type") or "nemo-conformer-tdt"
        quant = "int8" if any(model_dir.glob("*.int8.onnx")) else None
        return kind, model_dir, quant

    out = cache / model_dir.name
    out.mkdir(parents=True, exist_ok=True)
    for dst, src in (("encoder-model.onnx", "encoder.onnx"),
                     ("encoder.onnx.data", "encoder.onnx.data"),
                     ("decoder_joint-model.onnx", "decoder_joint.onnx")):
        link, target = out / dst, model_dir / src
        if target.exists() and not link.exists():
            link.symlink_to(target)

    vocab = out / "vocab.txt"
    if not vocab.exists():
        import sentencepiece as spm
        sp = spm.SentencePieceProcessor(model_file=str(model_dir / "tokenizer.model"))
        n = sp.get_piece_size()
        lines = [f"{sp.id_to_piece(i)} {i}" for i in range(n)] + [f"<blk> {n}"]
        vocab.write_text("\n".join(lines) + "\n")
    n_tokens = sum(1 for _ in vocab.open())

    import onnxruntime as ort
    joint = ort.InferenceSession(str(model_dir / "decoder_joint.onnx"),
                                 providers=["CPUExecutionProvider"])
    width = joint.get_outputs()[0].shape[-1]
    kind = "nemo-conformer-rnnt" if width == n_tokens else "nemo-conformer-tdt"
    (out / "config.json").write_text(json.dumps(
        {"model_type": kind, "features_size": 128, "subsampling_factor": 8}))
    return kind, out, None


def _read_wav(path: Path):
    import numpy as np
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


def main(argv: list) -> int:
    if len(argv) != 4:
        _say({"error": "usage: engine_worker.py <model dir> <cache dir> <threads>"})
        return 2
    model_dir, cache, threads = Path(argv[1]), Path(argv[2]), int(argv[3])
    try:
        started = time.monotonic()
        import onnx_asr
        import onnxruntime as ort
        kind, path, quant = _adapter(model_dir, cache)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        model = onnx_asr.load_model(kind, str(path), quantization=quant, sess_options=opts)
        _say({"ready": True, "model": model_dir.name,
              "load_s": round(time.monotonic() - started, 2)})
    except Exception as e:  # noqa: BLE001 -- any load failure means "fall back to voxtype"
        _say({"error": f"{type(e).__name__}: {e}"})
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            samples = _read_wav(Path(req["wav"]))
            text = model.recognize(samples, sample_rate=16000) if len(samples) else ""
            _say({"id": req_id, "text": (text or "").strip()})
        except Exception as e:  # noqa: BLE001 -- one bad clip must not end the worker
            _say({"id": req_id, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
