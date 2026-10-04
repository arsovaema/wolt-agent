import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from food_evaluation import assess_candidates, ASSESSMENT_VERSION, AssessmentBatch, AssessedItem
from test_ai_meal_planner import assessment


class FoodEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, parsed):
        client=Mock()
        client.with_options.return_value=client
        client.beta.chat.completions.parse=AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(parsed=parsed))]))
        db=AsyncMock()
        db.execute.return_value=SimpleNamespace(rowcount=1)
        return db,client

    async def test_independent_assessment_persists_negative_and_positive_judgments(self):
        parsed=AssessmentBatch(items=[AssessedItem(meal_id=1,food_assessment=assessment('side',is_meal=False)),
                                      AssessedItem(meal_id=2,food_assessment=assessment('stew'))])
        db,client=self.setup(parsed)
        candidates=[dict(id=1,item_name='Side',description='A small side',category='Sides',price_mkd=50),
                    dict(id=2,item_name='Stew',description='Main dish',category='Meals',price_mkd=300)]
        rows,info=await assess_candidates(db,client,candidates)
        self.assertFalse(rows[0]['food_assessment']['is_meal'])
        self.assertTrue(rows[1]['food_assessment']['is_meal'])
        self.assertEqual(info['evaluated'],2)
        self.assertEqual(rows[0]['food_assessment']['assessment_version'],ASSESSMENT_VERSION)
        evidence=json.loads(client.beta.chat.completions.parse.call_args.kwargs['messages'][1]['content'])
        self.assertNotIn('price_mkd',evidence[0])
        self.assertNotIn('daily_targets',str(evidence))
        self.assertEqual(db.execute.await_count,2)
        db.commit.assert_awaited_once()

    async def test_versioned_cache_avoids_provider_and_db_work(self):
        db,client=self.setup(None)
        rows=[dict(id=1,food_assessment=dict(assessment('meal'),assessment_version=ASSESSMENT_VERSION))]
        returned,info=await assess_candidates(db,client,rows)
        self.assertIs(returned,rows)
        self.assertEqual(info,{'evaluated':0,'cached':1})
        client.beta.chat.completions.parse.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_incomplete_or_duplicate_ids_are_not_cached(self):
        for ids in ([1],[1,1],[1,99]):
            db,client=self.setup(AssessmentBatch(items=[AssessedItem(meal_id=i,food_assessment=assessment(str(i))) for i in ids]))
            with self.assertRaises(ValueError):
                await assess_candidates(db,client,[dict(id=1,item_name='One'),dict(id=2,item_name='Two')])
            db.execute.assert_not_awaited()

    async def test_changed_evidence_is_not_used_or_committed(self):
        db,client=self.setup(AssessmentBatch(items=[AssessedItem(meal_id=1,food_assessment=assessment('meal'))]))
        db.execute.return_value=SimpleNamespace(rowcount=0)
        with self.assertRaises(RuntimeError):
            await assess_candidates(db,client,[dict(id=1,item_name='Old name')])
        db.commit.assert_not_awaited()
