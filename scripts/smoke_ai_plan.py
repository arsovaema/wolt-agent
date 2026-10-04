"""Live planning check: uses catalog/profile and AI, caches food assessments but never publishes a plan.

Run explicitly with --user-id. Provider calls incur normal API usage.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


async def run(user_id):
    started = time.monotonic()
    async with main.async_session() as db:
        profile = await db.get(main.UserProfile, user_id)
        state = await db.get(main.DailyPlanState, user_id)
        previous = await db.get(main.WeeklyPlanState, user_id)
        snapshot = SimpleNamespace(**{c.key: getattr(profile, c.key) for c in main.UserProfile.__table__.columns})
        fee = state.daily_fee_allowance_mkd
        history = main.plan_history.prior_weeks(previous.result if previous else {})
        _, _, previous_ids = main.plan_history.usage_from_history(history)
        candidates, retrieval = await main.weekly_retrieval.retrieve_candidates(db, main.openai_client, snapshot, fee, previous_ids)
        candidates, evaluation = await main.food_evaluation.assess_candidates(db, main.openai_client, candidates)
        retrieval['food_evaluation'] = evaluation
    print(json.dumps({'stage': 'retrieved', 'seconds': round(time.monotonic()-started, 2), **retrieval}), flush=True)
    try:
        days, info = await main.ai_meal_planner.create_plan(main.openai_client, candidates, snapshot, fee, history)
        report = {'seconds': round(time.monotonic()-started, 2), 'planning': info,
                  'days': [main.summarize_plan(day, snapshot, fee) for day in days]}
        Path('/tmp/wolt-plan-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'seconds': report['seconds'], 'planning': info,
            'days': [{'restaurant': day[0]['restaurant_name'], 'ids': [m['id'] for m in day],
                      'status': report['days'][i]['status'], 'total_mkd': report['days'][i]['budget']['estimated_total_mkd']}
                     for i, day in enumerate(days)]}), flush=True)
    except main.ai_meal_planner.PlanRequirementsError as exc:
        print(json.dumps({'failed_requirements': exc.errors, 'seconds': round(time.monotonic()-started, 2)}), flush=True)
        raise
    finally:
        await main.openai_client.close()
        await main.engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', type=int, required=True)
    asyncio.run(run(parser.parse_args().user_id))
