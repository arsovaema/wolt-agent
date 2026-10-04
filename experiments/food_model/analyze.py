"""Evaluate saved predictions against reference nutrition; no model calls."""
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARMS = ['openai_text', 'hf_image', 'hf_image_text', 'openai_image_text', 'ensemble_50_50']
LABELS = {'openai_text': 'Current GPT-4o-mini, ingredients only',
          'hf_image': 'Food model, image only',
          'hf_image_text': 'Food model, image + ingredients',
          'openai_image_text': 'GPT-4o-mini, image + ingredients',
          'ensemble_50_50': '50/50 average: current text + food image'}
KEYS = ('calories', 'protein', 'carbs', 'fat')


def metrics(records, truth):
    result = {'n': len(records)}
    if not records:
        return result
    for key in KEYS:
        errors = [row['prediction'][key] - truth[dish][key] for dish, row in records.items()]
        absolute = list(map(abs, errors))
        denominator = sum(truth[dish][key] for dish in records)
        eligible = [dish for dish in records if truth[dish][key] > 0]
        result[key] = {
            'mae': statistics.mean(absolute), 'median_absolute_error': statistics.median(absolute),
            'signed_bias': statistics.mean(errors),
            'wape_percent': 100 * sum(absolute) / denominator if denominator else None,
            'within_20_percent_n': sum(abs(records[dish]['prediction'][key] - truth[dish][key]) <= 0.2 * truth[dish][key] for dish in eligible),
            'within_20_percent_denominator': len(eligible),
        }
    seconds = [r['seconds'] for r in records.values()]
    result['median_seconds'] = statistics.median(seconds)
    result['macro_energy_discrepancy_over_20_percent_n'] = sum(
        abs(r['prediction']['calories'] - (4 * r['prediction']['protein'] + 4 * r['prediction']['carbs'] + 9 * r['prediction']['fat']))
        > 0.2 * r['prediction']['calories'] for r in records.values())
    return result


def paired_comparison(candidate, baseline, truth, strata, key, repeats=10000):
    common = sorted(candidate.keys() & baseline.keys())
    if not common:
        return {'n': 0}
    delta = {dish: abs(candidate[dish]['prediction'][key] - truth[dish][key])
             - abs(baseline[dish]['prediction'][key] - truth[dish][key]) for dish in common}
    groups = defaultdict(list)
    for dish in common:
        groups[tuple(strata[dish])].append(dish)
    rng = random.Random(20260918)
    bootstrap = []
    for _ in range(repeats):
        sampled = [rng.choice(ids) for ids in groups.values() for _ in ids]
        bootstrap.append(statistics.mean(delta[dish] for dish in sampled))
    bootstrap.sort()
    return {'n': len(common), 'mae_difference_candidate_minus_baseline': statistics.mean(delta.values()),
            'bootstrap_95_percent_interval': [bootstrap[int(.025 * repeats)], bootstrap[int(.975 * repeats)]],
            'candidate_better_n': sum(d < 0 for d in delta.values()),
            'ties_n': sum(d == 0 for d in delta.values())}


def load_rows():
    latest = {}
    all_rows = []
    for provider in ('openai', 'hf'):
        path = ROOT / f'results/{provider}.jsonl'
        if not path.exists():
            continue
        for row in map(json.loads, path.read_text().splitlines()):
            key = row['arm'], row['dish_id']
            if key in latest and latest[key]['status'] == 'ok':
                raise ValueError('Successful prediction was rerun: ' + str(key))
            latest[key] = row
            all_rows.append(row)
    return latest, all_rows


def main():
    manifest = json.loads((ROOT / 'data/manifest.json').read_text())
    truth = {d['dish_id']: d['reference'] for d in manifest['samples']}
    strata = {d['dish_id']: d['stratum_kcal'] for d in manifest['samples']}
    latest, all_rows = load_rows()
    records = {arm: {} for arm in ARMS}
    failures = {arm: [] for arm in ARMS}
    for (arm, dish), row in latest.items():
        if dish not in truth:
            raise ValueError('Non-evaluation dish in result file')
        if row['status'] == 'ok':
            records[arm][dish] = row
        else:
            failures[arm].append({'dish_id': dish, 'error_type': row.get('error_type')})
    for dish in sorted(records['openai_text'].keys() & records['hf_image'].keys()):
        a, b = records['openai_text'][dish], records['hf_image'][dish]
        records['ensemble_50_50'][dish] = {'prediction': {k: (a['prediction'][k] + b['prediction'][k]) / 2 for k in KEYS},
                                         'seconds': a['seconds'] + b['seconds']}
    common = set(truth)
    for arm in ARMS:
        common &= records[arm].keys()
    results = {'sample_size': len(truth), 'common_successful_n': len(common),
               'coverage': {arm: {'success': len(records[arm]), 'failures': failures[arm],
                                  'missing': len(truth) - len(records[arm]) - len(failures[arm])} for arm in ARMS},
               'per_arm': {arm: metrics(records[arm], truth) for arm in ARMS},
               'common_subset': {arm: metrics({d: r for d, r in records[arm].items() if d in common}, truth) for arm in ARMS},
               'strata': {f'{lo}-{hi}': {arm: metrics({d:r for d,r in records[arm].items() if strata[d] == [lo, hi]}, truth) for arm in ARMS}
                           for lo, hi in ((50,350),(350,650),(650,1500))},
               'paired_vs_current_text': {arm: {key: paired_comparison(records[arm], records['openai_text'], truth, strata, key)
                                                for key in ('calories', 'protein')} for arm in ARMS[1:]},
               'transient_error_attempts': dict(Counter(r.get('error_type') for r in all_rows if r['status'] == 'error')),
               'openai_usage': {key: sum(r.get('usage', {}).get(key, 0) for r in all_rows) for key in ('prompt_tokens','completion_tokens','total_tokens')},
               'api_models': sorted({r['model_returned'] for r in all_rows if 'model_returned' in r}),
               'per_dish': {dish: {'reference': truth[dish], 'predictions': {arm: records[arm].get(dish, {}).get('prediction') for arm in ARMS}}
                            for dish in truth}}
    results['combination_comparisons'] = {
        label: {key: paired_comparison(records[candidate], records[baseline], truth, strata, key)
                for key in ('calories', 'protein')}
        for label, candidate, baseline in (
            ('food_image_text_vs_food_image', 'hf_image_text', 'hf_image'),
            ('average_vs_food_image', 'ensemble_50_50', 'hf_image'),
            ('openai_image_text_vs_food_image', 'openai_image_text', 'hf_image'),
        )}
    (ROOT / 'results/metrics.json').write_text(json.dumps(results, indent=2, allow_nan=False))
    lines = ['# Food-model pilot results', '',
             'Reference: 30 Nutrition5k test-split dishes, ten in each of three calorie strata. Ingredient quantities were withheld. Lower errors are better.', '',
             '| Method | Valid / 30 | Calorie MAE (kcal) | Protein MAE (g) | Carb MAE (g) | Fat MAE (g) | Median latency (s) |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for arm in ARMS:
        m = results['per_arm'][arm]
        if m['n']:
            lines.append(f"| {LABELS[arm]} | {m['n']} | " + ' | '.join(f"{m[k]['mae']:.1f}" for k in KEYS) + f" | {m['median_seconds']:.1f} |")
        else:
            lines.append(f"| {LABELS[arm]} | 0 | — | — | — | — | — |")
    lines += ['', f"All-arm common successful subset: **{len(common)}/30**. Per-arm table above uses each arm's successful predictions; use `metrics.json` common-subset results when coverage differs.", '',
              '## Paired comparisons against the current text estimator', '',
              'Negative differences favor the candidate. Intervals are exploratory 95% stratified paired-bootstrap intervals (10,000 resamples).', '',
              '| Candidate | Calorie MAE difference (kcal), 95% interval | Protein MAE difference (g), 95% interval | Paired n |',
              '| --- | ---: | ---: | ---: |']
    for arm in ARMS[1:]:
        values = results['paired_vs_current_text'][arm]
        cells = []
        for key in ('calories', 'protein'):
            v = values[key]
            cells.append(f"{v['mae_difference_candidate_minus_baseline']:+.1f} [{v['bootstrap_95_percent_interval'][0]:+.1f}, {v['bootstrap_95_percent_interval'][1]:+.1f}]" if v['n'] else '—')
        lines.append(f"| {LABELS[arm]} | {' | '.join(cells)} | {values['calories']['n']} |")
    lines += ['', '## Does combination improve on the food-image model?', '',
              '| Candidate vs food-image model | Calorie MAE difference (kcal), 95% interval | Protein MAE difference (g), 95% interval | Paired n |',
              '| --- | ---: | ---: | ---: |']
    for label, values in results['combination_comparisons'].items():
        cells = []
        for key in ('calories', 'protein'):
            v = values[key]
            cells.append(f"{v['mae_difference_candidate_minus_baseline']:+.1f} [{v['bootstrap_95_percent_interval'][0]:+.1f}, {v['bootstrap_95_percent_interval'][1]:+.1f}]" if v['n'] else '—')
        lines.append(f"| {label.replace('_', ' ')} | {' | '.join(cells)} | {values['calories']['n']} |")
    lines += ['', '## Reliability checks', '',
              'The energy check flags a >20% mismatch between predicted calories and 4×protein + 4×carbs + 9×fat. It is an approximate internal-consistency check, not proof of correctness. Flagged predictions remain in all metrics.', '',
              '| Method | Calories within 20% | Protein within 20% | Energy mismatch flags | Calorie bias (kcal) | Protein bias (g) |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for arm in ARMS:
        m = results['per_arm'][arm]
        if not m['n']:
            continue
        lines.append(f"| {LABELS[arm]} | {m['calories']['within_20_percent_n']}/{m['calories']['within_20_percent_denominator']} | {m['protein']['within_20_percent_n']}/{m['protein']['within_20_percent_denominator']} | {m['macro_energy_discrepancy_over_20_percent_n']}/{m['n']} | {m['calories']['signed_bias']:+.1f} | {m['protein']['signed_bias']:+.1f} |")
    lines += ['', '## Errors by portion/calorie range', '',
              '| Method | 50–350 kcal: calorie MAE | 350–650 kcal: calorie MAE | 650–1500 kcal: calorie MAE |',
              '| --- | ---: | ---: | ---: |']
    for arm in ARMS:
        cells = []
        for stratum in ('50-350', '350-650', '650-1500'):
            value = results['strata'][stratum][arm]
            cells.append(f"{value['calories']['mae']:.1f} (n={value['n']})" if value['n'] else '—')
        lines.append(f"| {LABELS[arm]} | {' | '.join(cells)} |")
    lines += ['', '## Scope and reproducibility', '',
              '- These are measured-ingredient reference estimates from Nutrition5k, not laboratory assays or verified Wolt meals.',
              '- Thirty dishes with deliberately balanced calorie ranges are a pilot. Cafeteria overhead photographs and constructed ingredient descriptions differ from Wolt marketing images and menus.',
              '- The adapter ran on official Qwen3-VL-2B-Instruct in FP16 on Apple MPS, not the author’s 4-bit bitsandbytes deployment. Revisions and versions are in `results/environment.json`.',
              '- No prompt tuning used the evaluation outcomes. A separate training-split photo was used for smoke tests. Model pretraining exposure to this public dataset cannot be excluded.',
              '- Bootstrap intervals treat sampled dishes as independent; the source dataset includes incremental dish scans, so possible related plates are an additional limitation.',
              '- The 50/50 ensemble was fixed before evaluation; no fitted or selected weights.',
              '- Failed API rate-limit attempts were retained and only transient failures retried. Raw responses, prompts, token counts and durations are saved in the JSONL files.',
              f"- API response model IDs: {', '.join(results['api_models'])}.",
              f"- API usage for recorded successful responses: {results['openai_usage']['prompt_tokens']:,} input tokens; {results['openai_usage']['completion_tokens']:,} output tokens. Smoke calls excluded.",
              f"- Data selection skipped {len(manifest['skipped'])} unavailable overhead images before completing the fixed per-stratum sample and finding a smoke image. All skips are recorded.",
              '', 'Sources: [Nutrition5k](https://github.com/google-research-datasets/Nutrition5k), [food model](https://huggingface.co/Ateeqq/food-analysis).', '']
    (ROOT / 'RESULTS.md').write_text('\n'.join(lines))
    print('\n'.join(lines[:12]))


if __name__ == '__main__':
    main()
