import unittest
from analyze import metrics, paired_comparison
from run_benchmark import normalize_output, text_prompt


class BenchmarkTests(unittest.TestCase):
    def test_documented_and_app_schemas_normalize_equally(self):
        a = normalize_output('{"nutritional_summary":{"calories_kcal":400,"protein_g":20,"carbohydrate_g":50,"fat_g":10}}')
        b = normalize_output('{"calories":400,"protein_grams":20,"carbs_grams":50,"fat_grams":10}')
        self.assertEqual(a, b)

    def test_invalid_values_and_non_json_are_failures(self):
        for raw in ('Here is your answer: {}', '{"calories":NaN,"protein":1,"carbs":1,"fat":1}',
                    '{"calories":400,"protein":-1,"carbs":1,"fat":1}', '{}'):
            with self.assertRaises(ValueError):
                normalize_output(raw)

    def test_mae_bias_and_wape_are_distinct(self):
        truth = {'a':dict(calories=100,protein=10,carbs=10,fat=10), 'b':dict(calories=300,protein=30,carbs=30,fat=30)}
        rows = {dish:{'prediction':{k:v+offset for k,v in value.items()},'seconds':1}
                for (dish,value),offset in zip(truth.items(), (10,-10))}
        result = metrics(rows, truth)
        self.assertEqual(result['calories']['mae'], 10)
        self.assertEqual(result['calories']['signed_bias'], 0)
        self.assertEqual(result['calories']['wape_percent'], 5)

    def test_pairing_uses_dish_identity(self):
        truth = {'a':{'calories':100}, 'b':{'calories':200}}
        baseline = {'a':{'prediction':{'calories':120}}, 'b':{'prediction':{'calories':180}}}
        candidate = {'b':{'prediction':{'calories':200}}, 'a':{'prediction':{'calories':100}}}
        result = paired_comparison(candidate,baseline,truth,{'a':[0,300],'b':[0,300]},'calories',repeats=100)
        self.assertEqual(result['mae_difference_candidate_minus_baseline'], -20)
        self.assertEqual(result['bootstrap_95_percent_interval'], [-20,-20])

    def test_prompt_contains_no_reference_fields(self):
        prompt = text_prompt(['eggs', 'rice'])
        self.assertIn('eggs, rice', prompt)
        self.assertNotIn('dish_', prompt)
        self.assertNotIn('reference', prompt)


if __name__ == '__main__':
    unittest.main()
