import unittest
from meal_eligibility import is_meal_candidate
from test_ai_meal_planner import assessment


class EligibilityTests(unittest.TestCase):
    def test_unknown_items_are_kept_for_model_review(self):
        for name in ['Кремаст Бафало сос 🌶️', 'Unknown special', 'Chicken with BBQ sauce']:
            self.assertTrue(is_meal_candidate(name, 'Extras / sauces'))
        self.assertFalse(is_meal_candidate('  '))
        self.assertFalse(is_meal_candidate(None))

    def test_model_judgment_controls_eligibility_not_name_or_category(self):
        self.assertFalse(is_meal_candidate('House special', 'Meals', assessment('dip', is_meal=False)))
        self.assertTrue(is_meal_candidate('Sauce special', 'Extras', assessment('stew')))
