import unittest
from types import SimpleNamespace
from meal_diversity import dish_identity, combination_diversity, features
from meal_planning import select_best_daily_meals
from test_ai_meal_planner import assessment


def meal(name, **changes):
    return dict(item_name=name, restaurant_name='Venue', price_mkd=200,
                calories=600, protein=40, carbs=70, fat=20) | changes


class DiversityTests(unittest.TestCase):
    def test_model_identity_groups_sizes_and_bundles(self):
        a = meal('Пилешки врап M', food_assessment=assessment('chicken wrap'))
        b = meal('House bundle with drink', food_assessment=assessment('chicken wrap'))
        c = meal('Special', food_assessment=assessment('chicken curry'))
        self.assertEqual(dish_identity(a), dish_identity(b))
        self.assertNotEqual(dish_identity(a), dish_identity(c))
        self.assertTrue(combination_diversity([a,b,c])['obvious_duplicate'])

    def test_unlisted_protein_and_formats_are_supported(self):
        meals = [meal(str(i), food_assessment=assessment(str(i), dish_format='dumplings',
                 protein_sources=['duck'])) for i in range(3)]
        self.assertEqual(features(meals[0]), (frozenset(['dumplings']), frozenset(['duck'])))
        self.assertTrue(combination_diversity(meals)['excessive_format_repetition'])
        self.assertEqual(combination_diversity(meals)['similarity_penalty'], 1)

    def test_unknown_names_do_not_invent_features(self):
        for name in ['House special', 'Chicken rice', 'Galaxy']:
            self.assertEqual(features(meal(name)), (frozenset(), frozenset()))
        self.assertEqual(combination_diversity([meal('House'), meal('Galaxy')])['similarity_penalty'], 0)

    def test_offline_scorer_respects_stored_eligibility(self):
        p = SimpleNamespace(daily_calorie_target=1800,daily_protein_target=120,
                            daily_carbs_target=210,daily_fat_target=60,daily_budget_mkd=700)
        rows = [meal(str(i), food_assessment=assessment(str(i))) for i in range(3)]
        rows[0]['food_assessment']['is_meal'] = False
        self.assertEqual(select_best_daily_meals(rows,p,daily_fee_allowance_mkd=0), ([], -1))
