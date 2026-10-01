# RNNoise model for ffmpeg's `arnndn` filter

`sh.rnnn` is the `somnolent-hogwash-2018-09-01` model from Gregor Richards'
[rnnoise-models](https://github.com/GregorR/rnnoise-models) (the trained networks
for rnnoise-nu, Xiph's RNNoise with model loading). It is the one trained for
"speech in a reasonable recording environment: fans, AC, computers", which is the
case Spitball's mic channel is in. ffmpeg's `arnndn` filter reads this text format
directly; the file is 298 KB.

- Source: https://github.com/GregorR/rnnoise-models/blob/master/somnolent-hogwash-2018-09-01/sh.rnnn
- SHA-256: `70bb6685eb0c2a1d18e2918dca3fbfbd39317010b1802eb1b6ea73a92f3fdec0`
- Training data and intent: `info.txt` next to it upstream (LibriVox readings and
  TSP speech as the signal, the `rnnoise_contributions` recordings as the noise).
- License: the repository's README states that, apart from its `tools/` directory,
  "none of this work is creative and thus none of it is subject to copyright"; the
  repository carries no other license file. Spitball redistributes the file unchanged
  with this attribution. RNNoise itself (the algorithm, implemented inside ffmpeg) is
  by Jean-Marc Valin and Xiph.Org, BSD-3-Clause.

Spitball uses it as `arnndn=m=<this file>:mix=0.7` on a temporary copy of the mic
channel only (see `spitball/denoise.py`). Delete the file and Spitball falls back to
ffmpeg's built-in `afftdn` filter with no other change.
