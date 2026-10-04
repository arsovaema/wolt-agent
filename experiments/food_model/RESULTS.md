# Food-model pilot results

Reference: 30 Nutrition5k test-split dishes, ten in each of three calorie strata. Ingredient quantities were withheld. Lower errors are better.

| Method | Valid / 30 | Calorie MAE (kcal) | Protein MAE (g) | Carb MAE (g) | Fat MAE (g) | Median latency (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Current GPT-4o-mini, ingredients only | 30 | 236.0 | 11.7 | 33.8 | 12.7 | 0.9 |
| Food model, image only | 30 | 219.0 | 13.4 | 19.3 | 16.7 | 7.3 |
| Food model, image + ingredients | 30 | 220.1 | 13.0 | 20.6 | 17.0 | 8.5 |
| GPT-4o-mini, image + ingredients | 30 | 217.5 | 11.5 | 30.9 | 13.5 | 1.2 |
| 50/50 average: current text + food image | 30 | 224.7 | 11.7 | 25.4 | 14.2 | 8.3 |

All-arm common successful subset: **30/30**. Per-arm table above uses each arm's successful predictions; use `metrics.json` common-subset results when coverage differs.

## Paired comparisons against the current text estimator

Negative differences favor the candidate. Intervals are exploratory 95% stratified paired-bootstrap intervals (10,000 resamples).

| Candidate | Calorie MAE difference (kcal), 95% interval | Protein MAE difference (g), 95% interval | Paired n |
| --- | ---: | ---: | ---: |
| Food model, image only | -17.0 [-86.9, +46.8] | +1.6 [-1.2, +4.7] | 30 |
| Food model, image + ingredients | -15.9 [-82.1, +44.6] | +1.3 [-1.3, +3.8] | 30 |
| GPT-4o-mini, image + ingredients | -18.5 [-82.0, +39.8] | -0.3 [-2.2, +1.7] | 30 |
| 50/50 average: current text + food image | -11.3 [-46.3, +20.6] | +0.0 [-1.9, +1.9] | 30 |

## Does combination improve on the food-image model?

| Candidate vs food-image model | Calorie MAE difference (kcal), 95% interval | Protein MAE difference (g), 95% interval | Paired n |
| --- | ---: | ---: | ---: |
| food image text vs food image | +1.1 [-15.6, +19.4] | -0.3 [-1.9, +1.0] | 30 |
| average vs food image | +5.7 [-26.8, +41.1] | -1.6 [-3.7, +0.2] | 30 |
| openai image text vs food image | -1.5 [-45.7, +43.9] | -1.9 [-4.7, +0.7] | 30 |

## Reliability checks

The energy check flags a >20% mismatch between predicted calories and 4×protein + 4×carbs + 9×fat. It is an approximate internal-consistency check, not proof of correctness. Flagged predictions remain in all metrics.

| Method | Calories within 20% | Protein within 20% | Energy mismatch flags | Calorie bias (kcal) | Protein bias (g) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current GPT-4o-mini, ingredients only | 9/30 | 6/30 | 0/30 | +134.1 | +0.3 |
| Food model, image only | 6/30 | 9/30 | 1/30 | -42.5 | -9.7 |
| Food model, image + ingredients | 5/30 | 7/30 | 5/30 | -44.2 | -9.0 |
| GPT-4o-mini, image + ingredients | 8/30 | 6/30 | 0/30 | +49.0 | -2.4 |
| 50/50 average: current text + food image | 6/30 | 7/30 | 0/30 | +45.8 | -4.7 |

## Errors by portion/calorie range

| Method | 50–350 kcal: calorie MAE | 350–650 kcal: calorie MAE | 650–1500 kcal: calorie MAE |
| --- | ---: | ---: | ---: |
| Current GPT-4o-mini, ingredients only | 312.0 (n=10) | 260.7 (n=10) | 135.3 (n=10) |
| Food model, image only | 182.0 (n=10) | 118.6 (n=10) | 356.3 (n=10) |
| Food model, image + ingredients | 172.0 (n=10) | 126.9 (n=10) | 361.3 (n=10) |
| GPT-4o-mini, image + ingredients | 216.5 (n=10) | 195.7 (n=10) | 240.3 (n=10) |
| 50/50 average: current text + food image | 247.0 (n=10) | 183.2 (n=10) | 243.8 (n=10) |

## Scope and reproducibility

- These are measured-ingredient reference estimates from Nutrition5k, not laboratory assays or verified Wolt meals.
- Thirty dishes with deliberately balanced calorie ranges are a pilot. Cafeteria overhead photographs and constructed ingredient descriptions differ from Wolt marketing images and menus.
- The adapter ran on official Qwen3-VL-2B-Instruct in FP16 on Apple MPS, not the author’s 4-bit bitsandbytes deployment. Revisions and versions are in `results/environment.json`.
- No prompt tuning used the evaluation outcomes. A separate training-split photo was used for smoke tests. Model pretraining exposure to this public dataset cannot be excluded.
- Bootstrap intervals treat sampled dishes as independent; the source dataset includes incremental dish scans, so possible related plates are an additional limitation.
- The 50/50 ensemble was fixed before evaluation; no fitted or selected weights.
- Failed API rate-limit attempts were retained and only transient failures retried. Raw responses, prompts, token counts and durations are saved in the JSONL files.
- API response model IDs: gpt-4o-mini-2024-07-18.
- API usage for recorded successful responses: 439,000 input tokens; 1,512 output tokens. Smoke calls excluded.
- Data selection skipped 188 unavailable overhead images before completing the fixed per-stratum sample and finding a smoke image. All skips are recorded.

Sources: [Nutrition5k](https://github.com/google-research-datasets/Nutrition5k), [food model](https://huggingface.co/Ateeqq/food-analysis).
