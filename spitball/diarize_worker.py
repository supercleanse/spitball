"""The speaker-split worker: one sherpa-onnx diarization run, one JSON line.

Run only by spitball/diarize.py, under the live-engine venv's Python
(`spitball diarize setup` installs sherpa-onnx there) -- never imported by
the rest of Spitball, which stays standard-library only. Reads a mono
16 kHz PCM WAV (the far channel), prints one JSON object on stdout and
exits:

  {"segments": [[start, end, speaker], ...], "seconds": 1.9, "speakers": 2}
  or {"error": "..."} with exit 1

Usage: python diarize_worker.py <segmentation model> <embedding model>
       <wav> <num_clusters> <threshold> <threads> <min_on_s> <min_off_s>

`num_clusters` -1 lets the clustering pick a count under `threshold`.
"""
import json
import sys
import time
import wave


def _say(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _read_wav(path: str):
    import numpy as np
    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {8 * w.getsampwidth()}-bit")
        rate = w.getframerate()
        channels = w.getnchannels()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels)[:, 0]
    return samples, rate


def main(argv: list) -> int:
    if len(argv) != 9:
        _say({"error": "usage: diarize_worker.py <seg model> <emb model> <wav> <num_clusters> "
                       "<threshold> <threads> <min_on_s> <min_off_s>"})
        return 2
    seg, emb, wav = argv[1], argv[2], argv[3]
    try:
        num_clusters, threshold, threads = int(argv[4]), float(argv[5]), int(argv[6])
        min_on, min_off = float(argv[7]), float(argv[8])
        import sherpa_onnx
        samples, rate = _read_wav(wav)
        if rate != 16000:
            raise ValueError(f"expected a 16 kHz WAV, got {rate} Hz")
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg),
                num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=threshold),
            min_duration_on=min_on, min_duration_off=min_off)
        if not cfg.validate():
            raise ValueError("sherpa-onnx rejected the diarization config (models missing?)")
        started = time.monotonic()
        sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
        result = sd.process(samples).sort_by_start_time() if len(samples) else []
        segments = [[round(float(s.start), 3), round(float(s.end), 3), int(s.speaker)] for s in result]
        _say({"segments": segments, "seconds": round(time.monotonic() - started, 2),
              "speakers": len({s[2] for s in segments})})
        return 0
    except Exception as e:  # noqa: BLE001 -- any failure means "no split this time"
        _say({"error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
