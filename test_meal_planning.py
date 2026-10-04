"""Offline regression tests. Run: .venv/bin/python -m unittest test_meal_planning"""
import os
import hashlib
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from meal_planning import estimated_cost, select_best_daily_meals, summarize_plan
from wolt_scraper import extract_image_url
from meal_eligibility import is_meal_candidate

# Import the app without using real credentials or opening any connections.
with patch.dict(os.environ, {
    "OPENAI_API_KEY": "test-placeholder",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
}):
    import main


def profile(**changes):
    return SimpleNamespace(**({
        "daily_budget_mkd": 700, "daily_calorie_target": 2100,
        "daily_protein_target": 150, "daily_carbs_target": 240,
        "daily_fat_target": 60,
    } | changes))


def meals(**changes):
    return [dict(item_name=f"Meal {i}", restaurant_name=f"Restaurant {i}",
                 price_mkd=200, calories=700, protein=50, carbs=80, fat=20) | changes
            for i in range(3)]


class PlanningTests(unittest.TestCase):
    def test_main_dishes_with_sauce_are_searchable(self):
        self.assertTrue(is_meal_candidate("Пилешко во кари сос 300gr", "Главни јадења"))
        self.assertTrue(is_meal_candidate("Chicken with sauce", "Meals"))
        self.assertTrue(is_meal_candidate("Кари сос", "Extras / sauces"))  # Unknown items need model review.

    def test_image_url_is_extracted_from_lazy_or_srcset_images(self):
        from bs4 import BeautifulSoup
        lazy = BeautifulSoup('<div><img data-src="https://img.test/a.jpg"></div>', "html.parser").div
        srcset = BeautifulSoup('<div><img srcset="https://img.test/small.jpg 1x, https://img.test/large.jpg 2x"></div>', "html.parser").div
        self.assertEqual(extract_image_url(lazy), "https://img.test/a.jpg")
        self.assertEqual(extract_image_url(srcset), "https://img.test/large.jpg")

    def test_uncertainty_defaults_are_explicit_and_validated(self):
        macro = main.MacroNutrients(calories=500, protein_grams=20, carbs_grams=60, fat_grams=15)
        completed = main.complete_nutrition_uncertainty(macro)
        self.assertEqual((completed.calories_low, completed.calories_high), (400, 600))
        with self.assertRaises(ValueError):
            main.complete_nutrition_uncertainty(main.MacroNutrients(
                calories=500, protein_grams=20, carbs_grams=60, fat_grams=15,
                calories_low=600, calories_high=700,
            ))

    def test_allowance_is_reserved_once_across_restaurants(self):
        cost = estimated_cost(meals(), 100)
        self.assertEqual(cost["estimated_total_mkd"], 700)
        self.assertEqual(cost["daily_fee_allowance_mkd"], 100)

    def test_budget_boundary_includes_allowance(self):
        found, score = select_best_daily_meals(meals(), profile(), daily_fee_allowance_mkd=100)
        self.assertEqual(len(found), 3)
        self.assertEqual(score, 10)
        self.assertEqual(select_best_daily_meals(meals(), profile(daily_budget_mkd=699), daily_fee_allowance_mkd=100), ([], -1))

    def test_unknown_fees_are_not_assumed_zero(self):
        with self.assertRaises(ValueError):
            select_best_daily_meals(meals(), profile(), daily_fee_allowance_mkd=None)
        found, _ = select_best_daily_meals(meals(), profile(daily_budget_mkd=600), daily_fee_allowance_mkd=0)
        self.assertEqual(len(found), 3)

    def test_cheaper_equally_suitable_plan_wins(self):
        pool = meals() + [meals()[0] | {"item_name": "Cheap", "price_mkd": 100}]
        found, _ = select_best_daily_meals(pool, profile(), daily_fee_allowance_mkd=100)
        self.assertEqual(sum(m["price_mkd"] for m in found), 500)

    def test_duplicates_do_not_fill_three_slots(self):
        self.assertEqual(select_best_daily_meals([meals()[0]] * 3, profile(), daily_fee_allowance_mkd=0), ([], -1))

    def test_poor_fit_is_explicit(self):
        result = summarize_plan(meals(calories=100, protein=1, carbs=1, fat=1), profile(), 100)
        self.assertEqual(result["status"], "poor_fit")
        self.assertEqual(result["nutrition"]["differences_from_targets"]["calories"], -1800)

    def test_ready_summary_has_prices_totals_and_remaining_budget(self):
        result = summarize_plan(meals(), profile(), 100)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["budget"]["estimated_remaining_mkd"], 0)
        self.assertEqual(result["nutrition"]["estimated_totals"]["calories"], 2100)
        self.assertTrue(result["budget"]["is_estimate"])

    def test_wide_ranges_reduce_plan_score_and_are_reported(self):
        narrow = meals()
        wide = [meal | {"nutrition_ranges": {
            "calories": {"low": 100, "high": 1300},
            "protein": {"low": 1, "high": 99},
            "carbs": {"low": 1, "high": 159},
            "fat": {"low": 1, "high": 39},
        }} for meal in meals()]
        self.assertLess(summarize_plan(wide, profile(), 100)["daily_plan_score"],
                        summarize_plan(narrow, profile(), 100)["daily_plan_score"])
        self.assertGreater(summarize_plan(wide, profile(), 100)["uncertainty"]["average_relative_range_width"], 0)

    def test_invalid_targets_and_estimates(self):
        with self.assertRaises(ValueError):
            select_best_daily_meals(meals(), profile(daily_fat_target=0), daily_fee_allowance_mkd=0)
        self.assertEqual(select_best_daily_meals(meals(protein=-5), profile(), daily_fee_allowance_mkd=0), ([], -1))

    def test_acceptable_plan_takes_priority_over_higher_scoring_poor_fit(self):
        acceptable = meals(calories=595, protein=42.5, carbs=64, fat=16)
        alternative = meals()[0] | {"item_name": "Alternative", "calories": 910,
                                    "protein": 65, "carbs": 61.6, "fat": 28}
        poor = summarize_plan([acceptable[0], acceptable[1], alternative], profile(), 100)
        good = summarize_plan(acceptable, profile(), 100)
        self.assertEqual(poor["status"], "poor_fit")
        self.assertGreater(poor["daily_plan_score"], good["daily_plan_score"])
        selected, _ = select_best_daily_meals(acceptable + [alternative], profile(), daily_fee_allowance_mkd=100)
        self.assertEqual(summarize_plan(selected, profile(), 100)["status"], "ready")


class AsyncSessionAdapter:
    """Run app state transitions against real isolated SQLite tables.

    This exercises persistence/rollback behavior, but not PostgreSQL row locks.
    """
    def __init__(self, session):
        self.session = session

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    def add(self, value):
        self.session.add(value)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()

    async def refresh(self, value):
        self.session.refresh(value)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        main.Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = AsyncSessionAdapter(self.session)
        self.session.add(main.UserProfile(
            id=1, current_weight_kg=70, target_weight_kg=75, height_cm=175,
            age=30, sex="male", activity_level="sedentary", goal="muscle gain",
            **vars(profile()),
        ))
        self.session.add(main.DailyPlanState(
            user_id=1, daily_fee_allowance_mkd=100, generation="initial",
            plan_date=main.planner_today(), result=main.plan_status("refresh_required", "test"),
        ))
        self.session.commit()
        self.ai_patch = patch.object(main.openai_client, 'with_options', return_value=main.openai_client)
        self.ai_patch.start()
        self.parse_patch = patch.object(main.openai_client.beta.chat.completions, 'parse', AsyncMock(side_effect=self.propose))
        self.parse_patch.start()
        self.addCleanup(self.ai_patch.stop)
        self.addCleanup(self.parse_patch.stop)
        async def fixture_assess(db, client, candidates):
            from test_ai_meal_planner import assessment
            return [dict(m,food_assessment=assessment(m['item_name'])) for m in candidates], {'evaluated':0}
        self.assessment_patch = patch.object(main.food_evaluation, 'assess_candidates', AsyncMock(side_effect=fixture_assess))
        self.assessment_patch.start()
        self.addCleanup(self.assessment_patch.stop)
        review_patch = patch('ai_meal_planner.review_dishes', AsyncMock())
        review_patch.start()
        self.addCleanup(review_patch.stop)

    async def propose(self, **kwargs):
        from ai_meal_planner import PlanProposal, DayChoice, MealChoice
        from test_ai_meal_planner import assessment
        context = json.loads(kwargs['messages'][1]['content'])
        recent = {m.get('id') for week in context['recent_weeks'] for day in week for m in day}
        groups = {}
        for m in sorted(context['catalog'], key=lambda m: m['id'] in recent):
            groups.setdefault(m['restaurant_name'], []).append(m)
        days = []
        for rows in groups.values():
            if len(rows) >= 3 and len(days) < context['requirements']['days']:
                days.append(DayChoice(day_number=len(days)+1, meals=[MealChoice(meal_id=m['id'],
                    food_assessment=assessment(m['item_name'])) for m in rows[:3]]))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=PlanProposal(days=days, reasoning='Fixture AI plan')))])

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    async def ready(self):
        snapshot, token, fee, _ = await main.begin_plan_refresh(self.db)
        return await main.publish_plan_result(self.db, token, summarize_plan(meals(), snapshot, fee))

    def queue(self):
        return self.session.scalars(select(main.DailyMealQueue)).all()

    async def test_ready_plan_persists_and_queue_exposes_prices(self):
        result = await self.ready()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(self.queue()), 3)
        queue = await main.get_meal_queue(self.db)
        self.assertEqual(queue[0].price_mkd, 200)

    async def test_refresh_clears_old_queue_before_external_work(self):
        await self.ready()
        await main.begin_plan_refresh(self.db)
        self.assertEqual(self.queue(), [])
        self.assertEqual(await main.get_meal_queue(self.db), [])
        self.assertEqual((await main.current_daily_plan(self.db))["status"], "refreshing")



    async def test_poor_fit_stays_visible_only_as_alternative(self):
        snapshot, token, fee, _ = await main.begin_plan_refresh(self.db)
        result = summarize_plan(meals(calories=100), snapshot, fee)
        await main.publish_plan_result(self.db, token, result)
        self.assertEqual(len((await main.get_daily_plan(self.db))["meals"]), 3)
        self.assertEqual(await main.get_meal_queue(self.db), [])
        self.assertEqual(self.queue(), [])

    async def test_newer_refresh_cannot_be_overwritten(self):
        snapshot, old_token, fee, _ = await main.begin_plan_refresh(self.db)
        _, new_token, _, _ = await main.begin_plan_refresh(self.db)
        await main.publish_plan_result(self.db, new_token, main.plan_status("no_plan", "newer"))
        result = await main.publish_plan_result(self.db, old_token, summarize_plan(meals(), snapshot, fee))
        self.assertEqual(result["status"], "superseded")
        self.assertEqual((await main.current_daily_plan(self.db))["message"], "newer")

    async def test_expired_plan_is_hidden(self):
        await self.ready()
        with patch.object(main, "planner_today", return_value=main.planner_today() + timedelta(days=1)):
            self.assertEqual((await main.current_daily_plan(self.db))["status"], "refresh_required")
            self.assertEqual(await main.get_meal_queue(self.db), [])

    async def test_midnight_during_generation_invalidates_result(self):
        snapshot, token, fee, _ = await main.begin_plan_refresh(self.db)
        with patch.object(main, "planner_today", return_value=main.planner_today() + timedelta(days=1)):
            result = await main.publish_plan_result(self.db, token, summarize_plan(meals(), snapshot, fee))
        self.assertEqual(result["status"], "refresh_required")
        self.assertEqual(self.queue(), [])


    async def test_profile_update_invalidates_inflight_plan_and_preserves_allowance(self):
        snapshot, token, fee, _ = await main.begin_plan_refresh(self.db)
        data = main.UserProfileCreate(current_weight_kg=70, target_weight_kg=75, height_cm=175,
            age=30, sex="male", activity_level="sedentary", goal="muscle gain", daily_budget_mkd=500)
        plan = main.CalculatedNutritionalPlan(daily_calorie_target=2200, daily_protein_target=140,
            daily_carbs_target=275, daily_fat_target=60, reasoning="test")
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=plan))])
        with patch.object(main.openai_client.beta.chat.completions, "parse", AsyncMock(return_value=completion)):
            response = await main.create_or_update_profile(data, self.db)
        self.assertEqual(response["profile"]["daily_fee_allowance_mkd"], 100)
        self.assertEqual((await main.current_daily_plan(self.db))["status"], "refresh_required")
        self.assertEqual((await main.publish_plan_result(self.db, token, summarize_plan(meals(), snapshot, fee)))["status"], "superseded")

    async def test_catalog_daily_plan_respects_fee_budget_without_scraping(self):
        for meal in meals(restaurant_name="Picerija 511"):
            self.session.add(main.MenuItem(
                restaurant_name=meal["restaurant_name"], item_name=meal["item_name"],
                category="Meals", description="test", price_mkd=200,
                estimated_calories=700, estimated_protein=50,
                estimated_carbs=80, estimated_fat=20,
                nutrition_ranges={"calories": {"low": 560, "high": 840}, "protein": {"low": 40, "high": 60}, "carbs": {"low": 64, "high": 96}, "fat": {"low": 16, "high": 24}},
                nutrition_confidence="medium", nutrition_source="fixture",
                estimator_version=main.ESTIMATOR_VERSION,
                source_fingerprint=hashlib.sha256(json.dumps({
                    "restaurant": "Picerija 511", "item": meal["item_name"],
                    "category": "Meals", "description": "test", "image_url": None,
                    "estimator_version": main.ESTIMATOR_VERSION,
                }, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            ))
        self.session.commit()
        with patch("wolt_scraper.scrape_wolt_menu", AsyncMock(side_effect=AssertionError("Daily plans must not scrape"))) as scrape:
            result = await main.fetch_meals_now(self.db)
        scrape.assert_not_awaited()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["budget"]["estimated_total_mkd"], 700)
        self.session.get(main.UserProfile, 1).daily_budget_mkd = 699
        self.session.commit()
        with patch("wolt_scraper.scrape_wolt_menu", AsyncMock(side_effect=AssertionError("Daily plans must not scrape"))) as scrape:
            result = await main.fetch_meals_now(self.db)
        scrape.assert_not_awaited()
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(result["budget"]["available_for_food_mkd"], 599)
        self.assertEqual(self.queue(), [])


    async def test_daily_retrieval_failure_persists_failed_status(self):
        await self.ready()
        with patch.object(main, "weekly_candidates", AsyncMock(side_effect=RuntimeError("offline"))):
            result = await main.fetch_meals_now(self.db)
        self.assertEqual(result["status"], "refresh_failed")
        self.assertEqual((await main.current_daily_plan(self.db))["status"], "refresh_failed")
        self.assertEqual(await main.get_meal_queue(self.db), [])

    async def test_new_profile_can_set_zero_allowance_then_clear_it(self):
        self.session.delete(self.session.get(main.DailyPlanState, 1))
        self.session.delete(self.session.get(main.UserProfile, 1))
        self.session.commit()
        for fee in (0, None):
            data = main.UserProfileCreate(current_weight_kg=70, target_weight_kg=75, height_cm=175,
                age=30, sex="male", activity_level="sedentary", goal="muscle gain",
                daily_budget_mkd=700, daily_fee_allowance_mkd=fee)
            plan = main.CalculatedNutritionalPlan(daily_calorie_target=2200, daily_protein_target=140,
                daily_carbs_target=275, daily_fat_target=60, reasoning="test")
            completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=plan))])
            with patch.object(main.openai_client.beta.chat.completions, "parse", AsyncMock(return_value=completion)):
                result = await main.create_or_update_profile(data, self.db)
            self.assertEqual(result["profile"]["daily_fee_allowance_mkd"], fee)
            self.assertEqual(self.session.get(main.DailyPlanState, 1).daily_fee_allowance_mkd, fee)

    async def test_weekly_plan_has_seven_days_and_prioritizes_unique_meals(self):
        for index in range(42):
            self.session.add(main.MenuItem(
                restaurant_name=f"Restaurant {index // 3}", item_name=f"Weekly meal {index}",
                category="Meals", description="test", price_mkd=200,
                estimated_calories=700, estimated_protein=50,
                estimated_carbs=80, estimated_fat=20,
                nutrition_ranges={}, nutrition_confidence="medium",
                nutrition_source="fixture", estimator_version=main.ESTIMATOR_VERSION,
            ))
        self.session.commit()
        result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["weekly_plan"]), 7)
        scheduled = [meal["item_name"] for day in result["weekly_plan"] for meal in day["meals"]]
        self.assertEqual(len(scheduled), 21)
        self.assertEqual(len(set(scheduled)), 21)
        venues=[{meal['restaurant_name'] for meal in day['meals']} for day in result['weekly_plan']]
        self.assertTrue(all(len(group)==1 for group in venues))
        self.assertEqual(len(set(next(iter(group)) for group in venues)),7)
        self.assertEqual((await main.current_weekly_plan(self.db))["status"], "ready")
        second = await main.generate_weekly_plan(self.db)
        second_scheduled = {meal["item_name"] for day in second["weekly_plan"] for meal in day["meals"]}
        self.assertEqual(len(second_scheduled), 21)
        self.assertTrue(set(scheduled).isdisjoint(second_scheduled))
        self.assertEqual(len(second['selection_history']),1)
        with patch.object(main, 'weekly_candidates', AsyncMock(side_effect=RuntimeError('offline'))):
            failed = await main.generate_weekly_plan(self.db)
        self.assertEqual(failed['status'],'refresh_failed')
        self.assertEqual(len(failed['selection_history']),2)
        persisted = await main.current_weekly_plan(self.db)
        self.assertEqual(persisted['selection_history'],failed['selection_history'])


    async def test_weekly_avoids_identical_days_and_labels_nutrition_tradeoffs(self):
        candidates = [dict(m,id=i) for i,m in enumerate(meals())]
        candidates += [dict(meals(calories=100,protein=1,carbs=1,fat=1)[0],id=i,
                           item_name=f'Poor alternative {i}',restaurant_name=f'Venue {i // 3}') for i in range(3,24)]
        with patch.object(main,'weekly_candidates',AsyncMock(return_value=(candidates,{'mode':'sqlite'}))):
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result['status'], 'poor_fit')
        self.assertEqual(len(result['weekly_plan']), 7)
        self.assertTrue(all(day['budget']['estimated_total_mkd'] <= 700 for day in result['weekly_plan']))
        self.assertTrue(all(day['nutrition']['outside_tolerance'] for day in result['weekly_plan']))

    async def test_weekly_never_merges_undersized_restaurants(self):
        candidates=[dict(m,id=i,restaurant_id=i) for i,m in enumerate(meals())]
        with patch.object(main,'weekly_candidates',AsyncMock(return_value=(candidates,{'mode':'sqlite'}))):
            result=await main.generate_weekly_plan(self.db)
        self.assertEqual(result['status'],'no_plan')
        self.assertEqual(result['weekly_plan'],[])

    async def test_weekly_rejects_fewer_than_seven_venues(self):
        for count in (2,5,6):
            candidates=[dict(m,id=venue*3+i,restaurant_id=venue,restaurant_name=f'Venue {venue}')
                        for venue in range(count) for i,m in enumerate(meals())]
            with patch.object(main,'weekly_candidates',AsyncMock(return_value=(candidates,{'mode':'sqlite'}))):
                result=await main.generate_weekly_plan(self.db)
            self.assertEqual(result['status'],'no_plan')
            self.assertEqual(result['weekly_plan'],[])
            self.assertIn('7 different restaurants',result['message'])

    async def test_weekly_seven_venues_is_hard_constraint_despite_nutrition_scores(self):
        from collections import Counter
        candidates=[dict(m,id=venue*3+i,restaurant_id=venue,restaurant_name=f'Venue {venue}')
                    for venue in range(7) for i,m in enumerate(meals(calories=700 if venue==0 else 650))]
        with patch.object(main,'weekly_candidates',AsyncMock(return_value=(candidates,{'mode':'sqlite'}))):
            result=await main.generate_weekly_plan(self.db)
        venues=[day['meals'][0]['restaurant_id'] for day in result['weekly_plan']]
        self.assertEqual(len(venues),7)
        self.assertEqual(len(set(venues)),7)
        self.assertEqual(max(Counter(venues).values()),1)
        self.assertTrue(all(a!=b for a,b in zip(venues,venues[1:])))

    async def test_weekly_generation_rejects_fee_allowance_exceeding_budget(self):
        state = self.session.get(main.DailyPlanState, 1)
        state.daily_fee_allowance_mkd = 1200
        self.session.commit()
        with patch.object(main, "weekly_candidates", AsyncMock()) as retrieval:
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result["status"], "budget_invalid")
        retrieval.assert_not_awaited()

    async def test_weekly_empty_catalog_does_not_scrape(self):
        with patch("wolt_scraper.scrape_wolt_menu", AsyncMock()) as scrape:
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result["status"], "no_plan")
        scrape.assert_not_awaited()

    async def test_weekly_failure_is_persisted_and_not_reported_as_expired(self):
        with patch.object(main, "weekly_candidates", AsyncMock(side_effect=RuntimeError("offline"))):
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result["status"], "refresh_failed")
        self.assertEqual((await main.current_weekly_plan(self.db))["status"], "refresh_failed")

    async def test_saved_profile_can_be_restored(self):
        result = await main.get_user_profile(self.db)
        self.assertEqual(result["profile"]["goal"], "muscle gain")
        self.assertEqual(result["profile"]["daily_fee_allowance_mkd"], 100)

    async def test_weekly_selection_rejects_over_budget_candidates(self):
        candidates = [dict(m, id=venue*3+i, restaurant_name=f"Venue {venue}") for venue in range(7) for i, m in enumerate(meals())]
        candidates.append(dict(meals()[0], id=99, item_name="Too expensive", price_mkd=2000))
        with patch.object(main, "weekly_candidates", AsyncMock(return_value=(candidates, {"mode": "vector_goal"}))):
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(len(result["weekly_plan"]), 7)
        self.assertTrue(all(d["budget"]["estimated_total_mkd"] <= 700 for d in result["weekly_plan"]))
        self.assertNotIn(99, [m["id"] for d in result["weekly_plan"] for m in d["meals"]])

    async def test_weekly_rechecks_catalog_after_ai_before_publication(self):
        candidates = [dict(m, id=v*3+i, restaurant_name=f"Venue {v}")
                      for v in range(7) for i, m in enumerate(meals())]
        with patch.object(main, "weekly_candidates", AsyncMock(return_value=(candidates, {}))), \
             patch.object(main, "cached_meals_valid", AsyncMock(return_value=False)):
            result = await main.generate_weekly_plan(self.db)
        self.assertEqual(result['status'], 'refresh_failed')
        self.assertEqual(result['weekly_plan'], [])

    async def test_weekly_publication_cannot_overwrite_newer_generation(self):
        self.session.add(main.WeeklyPlanState(user_id=1, generation="newer", start_date=main.planner_today(), result={"status": "refreshing"}))
        self.session.commit()
        result = await main.publish_weekly_result(self.db, "older", main.planner_today(), {"status": "ready"})
        self.assertEqual(result["status"], "superseded")
        self.assertEqual((await main.current_weekly_plan(self.db))["status"], "refreshing")

    async def test_weekly_plan_requires_new_generation_after_expiry(self):
        weekly = main.WeeklyPlanState(
            user_id=1, generation="expired", start_date=main.planner_today() - timedelta(days=7),
            result={"status": "ready", "weekly_plan": []},
        )
        self.session.add(weekly)
        self.session.commit()
        result = await main.current_weekly_plan(self.db)
        self.assertEqual(result["status"], "expired")
        self.assertIn("finished", result["message"])

    async def test_legacy_queue_without_state_is_not_shown(self):
        await self.ready()
        self.session.delete(self.session.get(main.DailyPlanState, 1))
        self.session.commit()
        self.assertEqual(len(self.queue()), 3)
        self.assertEqual(await main.get_meal_queue(self.db), [])

    async def test_missing_profile_returns_explicit_status(self):
        self.session.delete(self.session.get(main.DailyPlanState, 1))
        self.session.delete(self.session.get(main.UserProfile, 1))
        self.session.commit()
        result = await main.fetch_meals_now(self.db)
        self.assertEqual(result["status"], "profile_required")


if __name__ == "__main__":
    unittest.main()
