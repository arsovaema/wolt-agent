import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from ai_meal_planner import create_plan, PlanProposal, DayChoice, MealChoice, PlanRequirementsError, RepairProposal, RepairChoice
from meal_eligibility import FoodAssessment


def assessment(identity='dish', **changes):
    return dict(is_meal=True, reason='Menu describes a complete dish', dish_identity=identity,
                dish_format='', protein_sources=[], preparation='', confidence='medium') | changes


def catalog(venues=7, per_venue=4):
    return [dict(id=v*per_venue+i, restaurant_id=v, restaurant_name=f'Venue {v}',
                 item_name=f'Dish {v}-{i}', description='Menu evidence', category='Meals',
                 price_mkd=200, calories=700, protein=50, carbs=80, fat=20,
                 food_assessment=assessment(f'dish {v}-{i}')) for v in range(venues) for i in range(per_venue)]


def proposal(rows, days=7):
    return PlanProposal(days=[DayChoice(day_number=v+1, meals=[MealChoice(meal_id=m['id'],
        food_assessment=FoodAssessment.model_validate(m['food_assessment'])) for m in rows if m['restaurant_id']==v][:3])
        for v in range(days)], reasoning='Varied menu choices')


def client_for(*responses):
    client = Mock()
    client.with_options.return_value = client
    responses = iter(responses)
    async def respond(**kwargs):
        result = next(responses)
        if callable(result):
            result = result(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=result))])
    client.beta.chat.completions.parse = AsyncMock(side_effect=respond)
    return client


def repair_with(chosen, *, judgments=()):
    def respond(kwargs):
        offered = json.loads(kwargs['messages'][-1]['content'])['repair_options']
        choices = []
        for day, ids in chosen.items():
            option_id = next(key for key, row in offered.items()
                             if row['day_number'] == day and set(row['meal_ids']) == set(ids))
            choices.append(RepairChoice(day_number=day, option_id=option_id, food_assessments=list(judgments)))
        return RepairProposal(choices=choices, reasoning='Targeted repair chosen by AI')
    return respond


def invalid_repair():
    return RepairProposal(choices=[RepairChoice(day_number=1, option_id='invented', food_assessments=[])], reasoning='Invalid repair')


def profile():
    return SimpleNamespace(daily_budget_mkd=700, daily_calorie_target=2100, daily_protein_target=150,
                           daily_carbs_target=240, daily_fat_target=60, goal='muscle gain')


class AIPlannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.review_mock = AsyncMock()
        review = patch('ai_meal_planner.review_dishes', self.review_mock)
        review.start()
        self.addCleanup(review.stop)

    async def test_valid_ai_choice_is_preserved_even_when_other_meal_scores_better(self):
        rows = catalog()
        rows[0]['calories'] = 670
        initial = proposal(rows)
        client = client_for(initial)
        days, info = await create_plan(client, rows, profile(), 100)
        self.assertEqual([m['id'] for m in days[0]], [0,1,2])
        self.assertEqual(info['attempts'], 1)
        self.assertEqual(client.beta.chat.completions.parse.await_count, 1)
        self.review_mock.assert_awaited_once()

    async def test_repairs_only_invalid_day_and_retains_valid_slots(self):
        rows = catalog()
        initial = proposal(rows)
        initial.days[0].meals[1].meal_id = 9999
        client = client_for(initial, repair_with({1: [0,1,2]}))
        days, info = await create_plan(client, rows, profile(), 100)
        self.assertEqual([m['id'] for m in days[0]], [0,1,2])
        self.assertEqual([m['id'] for m in days[1]], [4,5,6])
        self.assertEqual(info['attempts'], 2)
        self.review_mock.assert_awaited_once()

    async def test_negative_assessment_cannot_be_overridden_by_model(self):
        rows = catalog()
        rows[0]['food_assessment'] = assessment('sauce', is_meal=False)
        initial = proposal(rows)
        initial.days[0].meals[0].food_assessment.is_meal = True
        days, _ = await create_plan(client_for(initial, repair_with({1: [3,1,2]})), rows, profile(), 100)
        self.assertEqual({m['id'] for m in days[0]}, {3,1,2})

    async def test_no_silent_numerical_fallback_on_persistent_failure(self):
        rows = catalog()
        rows[0]['price_mkd'] = 1000
        invalid = proposal(rows)
        client = client_for(invalid, invalid_repair(), invalid_repair())
        with self.assertRaises(PlanRequirementsError) as caught:
            await create_plan(client, rows, profile(), 100)
        self.assertIn('Total including fees', str(client.beta.chat.completions.parse.call_args.kwargs['messages']))
        self.assertEqual(client.beta.chat.completions.parse.await_count, 3)
        self.review_mock.assert_not_awaited()

    async def test_rejects_nutrition_failure_and_repeated_restaurant(self):
        rows = catalog()
        invalid = proposal(rows)
        invalid.days[1].meals = invalid.days[0].meals
        rows[0]['protein'] = 0
        client = client_for(invalid, invalid_repair(), invalid_repair())
        with self.assertRaises(PlanRequirementsError):
            await create_plan(client, rows, profile(), 100)

    async def test_legacy_assessment_and_history_reach_planner(self):
        rows = catalog()
        initial = proposal(rows)
        for m in rows:
            m.pop('food_assessment')
        history = [[[{'item_name': 'Previous meal', 'food_assessment': assessment('previous')}]]]
        client = client_for(initial)
        days, _ = await create_plan(client, rows, profile(), 100, history)
        self.assertTrue(days[0][0]['food_assessment']['is_meal'])
        context = json.loads(client.beta.chat.completions.parse.call_args.kwargs['messages'][1]['content'])
        self.assertEqual(context['recent_weeks'], history)
        self.assertEqual(context['catalog'][0]['description'], 'Menu evidence')

    async def test_provider_refusal_and_outage_do_not_publish_plan(self):
        with self.assertRaises(RuntimeError):
            await create_plan(client_for(None), catalog(), profile(), 100)
        client = client_for()
        client.beta.chat.completions.parse.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            await create_plan(client, catalog(), profile(), 100)


    async def test_same_novel_dish_identity_is_rejected_without_name_keywords(self):
        rows = catalog()
        rows[1]['food_assessment']['dish_identity'] = rows[0]['food_assessment']['dish_identity']
        invalid = proposal(rows)
        client = client_for(invalid, invalid_repair(), invalid_repair())
        with self.assertRaises(PlanRequirementsError) as caught:
            await create_plan(client, rows, profile(), 100)
        self.assertIn('variants of the same dish', str(client.beta.chat.completions.parse.call_args.kwargs['messages']))

    async def test_newly_inferred_negative_assessment_stays_fixed_during_repair(self):
        rows = catalog()
        initial = proposal(rows)
        rows[0].pop('food_assessment')
        initial.days[0].meals[0].food_assessment.is_meal = False
        changed = initial.model_copy(deep=True)
        changed.days[0].meals[0].food_assessment.is_meal = True
        client = client_for(initial, invalid_repair(), invalid_repair())
        with self.assertRaises(PlanRequirementsError) as caught:
            await create_plan(client, rows, profile(), 100)
        self.assertIn('not a meal', str(client.beta.chat.completions.parse.call_args.kwargs['messages']))


    async def test_impossible_nutrition_returns_ai_tradeoff_without_retrying(self):
        rows = catalog()
        p = profile()
        p.daily_carbs_target = 1000
        client = client_for(proposal(rows))
        days, info = await create_plan(client, rows, p, 100)
        from meal_planning import summarize_plan
        self.assertEqual(len(days), 7)
        self.assertTrue(all(summarize_plan(day,p,100)['status']=='poor_fit' for day in days))
        self.assertEqual(info['attempts'], 1)

    async def test_repair_option_cannot_override_catalog_or_change_valid_days(self):
        rows = catalog()
        initial = proposal(rows)
        rows[0]['price_mkd'] = 800
        def repair(kwargs):
            context = json.loads(kwargs['messages'][-1]['content'])
            for option in context['repair_options'].values():
                self.assertLessEqual(option['estimated_total_mkd'], 700)
                self.assertNotIn(0, option['meal_ids'])
                self.assertTrue(all(mid//4 == 0 for mid in option['meal_ids']))
            result = repair_with({1: [1,2,3]})(kwargs)
            result.choices.append(RepairChoice(day_number=2, option_id=result.choices[0].option_id, food_assessments=[]))
            return result
        days, _ = await create_plan(client_for(initial, repair), rows, profile(), 100)
        self.assertEqual([m['id'] for m in days[1]], [4,5,6])
        self.assertEqual({m['id'] for m in days[0]}, {1,2,3})

    async def test_no_affordable_week_fails_before_provider_call(self):
        rows = catalog()
        for meal in rows: meal['price_mkd'] = 1000
        client = client_for()
        with self.assertRaises(PlanRequirementsError):
            await create_plan(client, rows, profile(), 100)
        client.beta.chat.completions.parse.assert_not_awaited()


    async def test_repair_options_reserve_distinct_venues_for_multiple_failed_days(self):
        rows = catalog()
        initial = proposal(rows)
        initial.days[0].meals = [initial.days[n].meals[0] for n in range(3)]
        initial.days[1].meals = [MealChoice(meal_id=i,food_assessment=None) for i in (1,5,9)]
        def repair(kwargs):
            offered = json.loads(kwargs['messages'][-1]['content'])['repair_options']
            by_day = {}
            for key,row in offered.items():
                by_day.setdefault(row['day_number'], []).append((key,row))
            self.assertEqual(set(by_day), {1,2})
            venues = [{tuple(row['restaurant']) for _,row in options} for options in by_day.values()]
            self.assertTrue(venues[0].isdisjoint(venues[1]))
            return RepairProposal(choices=[RepairChoice(day_number=n,option_id=options[0][0],food_assessments=[])
                for n,options in by_day.items()],reasoning='Choose a valid option for each failed day')
        days, _ = await create_plan(client_for(initial,repair),rows,profile(),100)
        self.assertEqual(len({day[0]['restaurant_id'] for day in days}),7)
        self.assertEqual([m['id'] for m in days[2]],[8,9,10])
