# Corpus

This directory is gitignored and holds real legacy applications used for evaluation.

Populate it with:

```bash
uv run python scripts/fetch_corpus.py --list
uv run python scripts/fetch_corpus.py --fetch php-legacy
```

Each entry is a pinned checkout under `corpus/<name>/`. Ground truth lives in
`evaluation/ground_truth/<name>.json`; results are written to `evaluation/results/`.
