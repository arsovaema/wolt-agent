# Food nutrition experiment

An isolated experiment comparing the application's GPT-4o-mini ingredient-based estimator with the Ateeqq/food-analysis vision adapter and multimodal combinations. No app/database behavior is changed.

- [Protocol fixed before evaluation](PROTOCOL.md)
- [Measured results](RESULTS.md)
- [Machine-readable metrics and individual predictions](results/metrics.json)
- Raw outputs: `results/openai.jsonl`, `results/hf.jsonl`
- Selected dishes, ground truth, source hashes and image URLs: `data/manifest.json`
- Model revisions/runtime: `results/environment.json`, `models/revisions.json`

## Reproduce

The run uses an isolated `.venv` with exact package versions saved in `requirements.lock.txt`. The initial Apple M4 environment has 16 GB of memory. The local model is approximately 4–5 GB of weights. Public dataset selection is deterministic (seed 20260918).

```sh
python3 -m venv experiments/food_model/.venv
experiments/food_model/.venv/bin/python -m pip install -r experiments/food_model/requirements.lock.txt
experiments/food_model/.venv/bin/python experiments/food_model/prepare_data.py
experiments/food_model/.venv/bin/python experiments/food_model/download_models.py
experiments/food_model/.venv/bin/python experiments/food_model/run_benchmark.py hf --smoke --device mps
experiments/food_model/.venv/bin/python experiments/food_model/run_benchmark.py openai --smoke
experiments/food_model/.venv/bin/python experiments/food_model/run_benchmark.py hf --device mps
experiments/food_model/.venv/bin/python experiments/food_model/run_benchmark.py openai
experiments/food_model/.venv/bin/python experiments/food_model/run_benchmark.py openai --retry-transient
experiments/food_model/.venv/bin/python experiments/food_model/analyze.py
```

The API arms use the existing project `.env` key; model weights run locally. Only public benchmark images/ingredient lists go to the API. No profile or database access is used. Successful saved predictions are not rerun. `--retry-transient` only retries API network/rate/server failures, at ten-second intervals, preserving earlier attempts. Parsing/model failures remain failures. API model aliases may change over time; actual response model IDs are saved. The download script records revision hashes; reusing the downloaded model folders preserves this run. A fresh download must pin those hashes to reproduce the exact weights if upstream changes.

For CPU-only hardware, omit `--device mps`; the runner falls back to FP32 CPU, which is slower and uses more memory. The supplied lock file is for this macOS run; other platforms may need compatible PyTorch builds. Apple GPU access may require running outside an application sandbox.

Raw local-model outputs can contain ingredient portion guesses; these are predictions, not measurements. The benchmark inputs never include reference weights/macros or the dataset/dish identifiers. Input image bytes themselves are the original public dataset photographs.

## Verify the scoring code

```sh
experiments/food_model/.venv/bin/python -m unittest discover -s experiments/food_model -p test_benchmark.py -v
```

These tests verify schema normalization, invalid-output handling, MAE/bias/WAPE calculations and pairing by dish ID. They do not establish real-world nutrition accuracy.
