import unittest
from plan_history import prior_weeks, usage_from_history
from meal_planning import select_best_daily_meals
from test_meal_diversity import meal
from types import SimpleNamespace

class HistoryTests(unittest.TestCase):
    def test_history_is_bounded_and_failures_preserve_it(self):
        previous={}
        for i in range(5):
            history=prior_weeks(previous)
            previous={'status':'ready','selection_history':history,'weekly_plan':[
                {'meals':[{'id':i,'restaurant_name':'Venue','item_name':f'Dish {i}'}]}]}
        history=prior_weeks(previous)
        self.assertEqual([week[0][0]['id'] for week in history],[2,3,4])
        self.assertEqual(prior_weeks({'status':'refresh_failed','selection_history':history}),history)
        usage,combos,ids=usage_from_history(history)
        self.assertEqual(ids,[2,3,4])
        self.assertGreater(usage[('Venue','Dish 4')],usage[('Venue','Dish 2')])

    def test_repeated_generations_change_equal_quality_meals(self):
        p=SimpleNamespace(daily_calorie_target=1800,daily_protein_target=120,daily_carbs_target=210,daily_fat_target=60,daily_budget_mkd=700)
        pool=[meal(name) for name in ['Beef rice','Chicken wrap','Bean soup','Fish pasta','Greek salad','Cheese sandwich']]
        first,_=select_best_daily_meals(pool,p,daily_fee_allowance_mkd=0)
        keys=frozenset((m['restaurant_name'],m['item_name']) for m in first)
        second,_=select_best_daily_meals(pool,p,daily_fee_allowance_mkd=0,meal_usage={key:3 for key in keys},used_combinations={keys})
        self.assertTrue({m['item_name'] for m in first}.isdisjoint({m['item_name'] for m in second}))
