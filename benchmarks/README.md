# Performance and quality evaluation

Latest run: [results-2m-dense.json](results-2m-dense.json). Earlier reports are diagnostic baselines with different query/selection implementations and, in some cases, quality work interleaved with latency requests. They are not a controlled before/after speed comparison.

## Workload and results

Measured against PostgreSQL 16.15 in the local Docker environment on macOS/ARM64 with Python 3.13.9. The temporary fixture held **2,000,000 meals across 10,000 restaurants and 100 synthetic cities**, nominally 20,000 meals per city. It occupied about 6.96 GB including tables/indexes and was removed after the run.

The fixture bootstraps **295 observed restaurant meals and stored 512-dimensional embeddings**. It varies prices, restaurant IDs, location groups, availability and freshness. It does not contain two million independently collected menus. Query vectors reuse observed embeddings; no paid providers are called. The unused global HNSW index is omitted because the measured path uses exact local vector comparisons.

| Measurement | Result |
| --- | --- |
| Retrieval requests / concurrency | 100 / 20 |
| Retrieval p50 / p95 / p99 | 2075.14 / 3643.59 / 3728.64 ms |
| Failed requests | 0 |
| Maximum planner candidate pool | 60 |
| Exact-reference vector candidate recall@12 | 100% |
| Raw nutrition meal-ID recall@20 | 2.5% |
| Distinct nutrition-signature recall@20 | 90% |
| Mean best nutrition-score regret vs exact reference | 0 |
| Mixed-target daily plan samples | 20 |
| Daily sample coverage / budget compliance / nutrition fit | 100% / 100% / 100% |
| Concurrent authenticated users / plan workers | 20 / 2 |
| Weekly enqueue p95 | 31.49 ms |
| Weekly polling p95 | 601.75 ms |
| Weekly queue-to-completion p95 | 12339.11 ms |
| Completed users / planned days | 20 / 140 |
| Weekly estimated-budget compliance / nutrition fit | 100% / 100% |

Latency requests run concurrently. Exact-reference quality calculations run in a separate phase, followed by the authenticated HTTP/job workload. HTTP measurements use ASGI transport and the real auth middleware, route functions, database sessions, durable queue and weekly planner. They exclude TCP/TLS, external-provider latency and account-registration load. The worker process and HTTP tasks share a Python process in this harness, unlike the separate production worker commands.

Nutrition-ID recall is intentionally reported even though it is low. The fixture has many replicated dishes with identical nutrient vectors. Retrieval retains cheaper representatives of identical nutrition signatures, while the exact ID reference breaks equal-score ties by meal ID. This loses most exact reference IDs without losing the best nutrient match in these samples. **This is not evidence that real-world retrieval recall is high.** The additional distinct-signature metric measures overlap with the exact top 20 different calorie/protein/carbohydrate/fat tuples across all eligible dishes, rather than arbitrary replicated meal IDs. Its 90% result shows that some top nutrition alternatives are still missed. The zero best-score regret and fit rates are complementary measurements, not replacements for human relevance evaluation.

`passed` in the report means no recorded errors, bounded candidate count, and complete estimated-budget compliance; it is not a latency SLA or a human-quality approval. Dense service areas remain more expensive to search. The fixture is too small in independent food diversity, and the evaluation has too few profiles, to predict production nutrition-fit rates.

## Findings that changed the implementation

- Concurrent parallel PostgreSQL queries exhausted Docker shared memory in the first dense run. Retrieval now disables per-query parallel workers while allowing request-level concurrency.
- Repeated nutrient values crowded out complementary meals. Indexed neighborhoods now retain cheaper representatives of distinct nutrition signatures before the bounded personalized scoring step.
- Weekly selection previously favored novel poor-fit dishes over repeated fitting dishes. It now prioritizes nutrition fit and caches repeated combination searches within a generation.
- The vector stage now resolves eligible meal IDs first, fetches documents by primary key and materializes the local vector set once for the three query vectors.

## Reproduce

```sh
.venv/bin/python benchmarks/scale.py --rows 2000000 --cities 100 --concurrency 20 --requests 100 --quality-samples 20 --http-users 20 --output benchmarks/results-2m-dense.json
```

Use a local database containing observed seed meals and embeddings. The runner creates a uniquely named temporary schema, reads seed data from `public`, and removes only its own schema in `finally`. It does not modify application rows. Results include the fixture size, environment, parameters, errors, and percentile samples. The final saved report also records hashes of the implementation files measured in this session.

## Independently labeled quality evaluation

No independently reviewed relevance labels or verified service metadata were supplied, so this evaluation has **not** been run. Once those exist, provide a JSON array of cases containing `case_id`, `profile_id`, `query`, `relevant_meal_ids`, and optional `limit` (1–20). Labels must refer to menu IDs in the target installation and be reviewed independently of model results. Use profiles with verified delivery coverage.

```sh
# Opt-in: makes paid query embedding calls against the real catalog.
.venv/bin/python benchmarks/evaluate_quality.py --cases reviewed-cases.json --output benchmarks/labeled-quality.json
```

The runner records recall, precision, NDCG and latency. Real production load with actual providers, realistic geographic skew, larger independent food diversity, and human-reviewed relevance remains future validation; no fabricated labels or unmeasured production claims are included here.
