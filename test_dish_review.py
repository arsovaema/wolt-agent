import json
import unittest
from dish_review import review_dishes, DishReview, DishGroup
from meal_diversity import combination_diversity
from test_ai_meal_planner import catalog, proposal, client_for


class DishReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_comparison_catches_variants_with_different_cached_labels(self):
        rows=catalog(venues=1)
        rows[1]['item_name']=rows[0]['item_name']+' + drink'
        result=DishReview(equivalent_dishes=[DishGroup(meal_ids=[0,1],canonical_label='same main dish')],shared_formats=[])
        client=client_for(result)
        assessments={}
        await review_dishes(client,proposal(rows,days=1),{m['id']:m for m in rows},assessments)
        selected=[dict(m,food_assessment=assessments.get(m['id'],m['food_assessment'])) for m in rows[:3]]
        self.assertTrue(combination_diversity(selected)['obvious_duplicate'])
        sent=json.loads(client.beta.chat.completions.parse.call_args.kwargs['messages'][1]['content'])
        self.assertNotIn('price_mkd',sent[0])

    async def test_formats_are_unified_from_model_comparison(self):
        rows=catalog(venues=1)
        result=DishReview(equivalent_dishes=[],shared_formats=[DishGroup(meal_ids=[0,1,2],canonical_label='rice dish')])
        assessments={}
        await review_dishes(client_for(result),proposal(rows,days=1),{m['id']:m for m in rows},assessments)
        self.assertTrue(combination_diversity([dict(m,food_assessment=assessments[m['id']]) for m in rows[:3]])['excessive_format_repetition'])

    async def test_invented_or_cross_restaurant_groups_do_not_modify_assessments(self):
        rows=catalog()
        for ids in ([0,9999],[0,4]):
            result=DishReview(equivalent_dishes=[DishGroup(meal_ids=ids,canonical_label='invented')],shared_formats=[])
            assessments={}
            await review_dishes(client_for(result),proposal(rows),{m['id']:m for m in rows},assessments)
            self.assertEqual(assessments,{})
