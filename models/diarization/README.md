# Speaker-split models (downloaded, not shipped)

`spitball diarize setup` downloads two model files into
`~/.local/share/spitball/live-engine/models/diarization/` and verifies each
against the SHA-256 below before it is used (`spitball/diarize.py`). Nothing
in this directory ships in the plugin; this file records where the models
come from and under what terms. No Hugging Face account or token is involved:
both are plain files from the k2-fsa project's GitHub releases.

| File on disk | Source | SHA-256 | License |
|---|---|---|---|
| `pyannote-segmentation-3.0.int8.onnx` | `model.int8.onnx` inside [`sherpa-onnx-pyannote-segmentation-3-0.tar.bz2`](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2) (tarball `24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488`) | `d582f4b4c6b48205de7e0643c57df0df5615a3c176189be3fc461e9d18827b5d` | MIT (Copyright (c) 2022 CNRS; the tarball's `LICENSE`). Converted by k2-fsa from [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0). |
| `3dspeaker-eres2net-en-voxceleb.onnx` | [`3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx`](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx) | `c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5` | Apache-2.0 ([3D-Speaker](https://github.com/modelscope/3D-Speaker), ERes2Net trained on VoxCeleb, exported by k2-fsa). |

The inference library is the [`sherpa-onnx`](https://github.com/k2-fsa/sherpa-onnx)
wheel (Apache-2.0), installed into the live-engine venv beside onnx-asr; it
bundles its own ONNX Runtime.

Why these two: the segmentation model is the same one pyannote 3.x uses (1.5 MB
quantized); the embedding model is the smallest English one k2-fsa publishes
(26 MB) and, on k2-fsa's three two-speaker English samples, was the more stable
of the two candidates when the speaker count is not known in advance (NeMo's
`titanet_small`, 40 MB, CC-BY-4.0, over-split at the default threshold).
