# Preregistered pilot protocol

Compare nutrition estimates on 30 images from the official Nutrition5k RGB test split. Use seed 20260918 and sample 10 dishes each from three calorie strata (50–350, 350–650, 650–1500 kcal), without looking at predictions. Require valid positive reference values and an available overhead image. Record every skipped sample and replacement. This is a deliberately balanced pilot, not a representative Wolt sample. The public dataset may have appeared in model pretraining; absence of leakage cannot be guaranteed.

Input: ingredient names, alphabetically sorted to avoid providing weight-order information; omit masses, calorie/macronutrient values, dataset name, and dish IDs from model prompts. Image arms see the same photo. The textual description is a constructed ingredient list, not an actual Wolt menu description. Individual tiny ingredients are retained, and total serving weight is withheld.

Arms:
1. Current gpt-4o-mini text-only estimator, using the app's instruction and schema with neutral restaurant/category/name fields.
2. Ateeqq/food-analysis, image only.
3. Ateeqq/food-analysis, image plus the same ingredient list.
4. gpt-4o-mini, image plus the same ingredient list (controls for model identity).
5. An explicitly exploratory, fixed 50/50 arithmetic mean of arms 1 and 2, no fitted weights. This tests averaging rather than assuming it helps.

The food adapter will be applied to official Qwen/Qwen3-VL-2B-Instruct in BF16/FP16 for Apple MPS or CPU. This differs from the author's bitsandbytes 4-bit base, so results are for this local deployment. Record model revision hashes, package versions, hardware, raw outputs, latency, token counts and errors. Do not execute remote model code. Use deterministic greedy decoding locally and temperature 0 for API calls. Compare one prediction per dish per arm; never select the best of retries. Only transient network failures may be retried. Keep malformed outputs as failures.

Primary metrics: mean absolute error (MAE) in kcal and protein grams. Secondary: MAE for carbs/fat, signed bias, WAPE (sum absolute errors / sum true values), and fraction within 20% for calories/protein. Include full denominators and parsing failures. Compare on common successful dish IDs as well as per-arm coverage. Use paired bootstrap intervals for MAE differences; 30 samples support a pilot conclusion only. Do not tune prompts or ensemble weights on the evaluation dishes. Smoke-test a disjoint training-split image first.

No model is judged against another model's estimates. No benchmark results will change the planner automatically. Keep downloaded data/models and predictions outside the application tables.

Reference dataset: https://github.com/google-research-datasets/Nutrition5k
Food model: https://huggingface.co/Ateeqq/food-analysis
