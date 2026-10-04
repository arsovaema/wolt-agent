"""Offline unit tests plus opt-in isolated-schema PostgreSQL integration tests.
RUN_SEARCH_DB_TESTS=1 .venv/bin/python -m unittest test_meal_search -v
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import meal_search as search


def fake_client():
    client = SimpleNamespace()
    client.with_options = lambda **kw: client
    async def embed(**kw):
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1.0] + [0.0] * (search.DIMENSIONS - 1)) for i in range(len(kw['input']))])
    client.embeddings = SimpleNamespace(create=AsyncMock(side_effect=embed))
    client.beta = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=AsyncMock())))
    return client


class UnitTests(unittest.IsolatedAsyncioTestCase):
    def test_macedonian_food_aliases_preserve_negation(self):
        self.assertEqual(search.semantic_query('пица со сирење'), 'pizza with cheese')
        self.assertEqual(search.semantic_query('салата без пилешко'), 'salad without chicken')
        self.assertEqual(search.semantic_query('chicken under 400 MKD'), 'chicken under 400 MKD')

    def test_request_limits(self):
        for kwargs in [{'query': ' '}, {'query': 'a', 'limit': 21}, {'query': 'a', 'min_protein': float('nan')}, {'query': 'a', 'max_price_mkd': -1}]:
            with self.assertRaises(ValidationError):
                search.SearchRequest(**kwargs)

    def test_vector_validation(self):
        for value in [[1.0], [float('nan')] * 512, [0.0] * 512, [float('inf')] * 512]:
            with self.assertRaises(ValueError):
                search.vector_literal(value)

    def test_citations_reject_unknown_ids_invented_quotes_duplicates(self):
        selection = search.GroundedSelection(recommendations=[
            search.Evidence(meal_id=1, quote='grilled chicken'),
            search.Evidence(meal_id=1, quote='grilled chicken'),
            search.Evidence(meal_id=999, quote='grilled chicken'),
            search.Evidence(meal_id=2, quote='allergen free'),
        ])
        result = search.validate_evidence(selection, [
            {'id': 1, 'description': 'Fresh grilled chicken with rice'},
            {'id': 2, 'description': 'Salad'},
        ])
        self.assertEqual(result, [{'meal_id': 1, 'quote': 'grilled chicken'}])

    async def test_embedding_response_must_cover_entire_batch(self):
        client = fake_client()
        client.embeddings.create.side_effect = None
        client.embeddings.create.return_value = SimpleNamespace(data=[])
        with self.assertRaises(ValueError):
            await search.embed(client, ['meal'])


@unittest.skipUnless(os.getenv('RUN_SEARCH_DB_TESTS') == '1', 'Opt-in PostgreSQL integration tests')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from dotenv import load_dotenv
        load_dotenv('.env')
        self.schema = 'test_search_' + uuid4().hex
        self.admin = create_async_engine(os.environ['DATABASE_URL'])
        async with self.admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {self.schema}'))
        self.engine = create_async_engine(os.environ['DATABASE_URL'], connect_args={'server_settings': {'search_path': f'{self.schema},public'}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.client = fake_client()
        async with self.engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE menu_items (
                id SERIAL PRIMARY KEY, restaurant_name TEXT, item_name TEXT, category TEXT,
                description TEXT, price_mkd INTEGER, estimated_calories INTEGER,
                estimated_protein FLOAT, estimated_carbs FLOAT DEFAULT 50, estimated_fat FLOAT DEFAULT 15,
                food_assessment JSON NOT NULL DEFAULT '{}', image_url TEXT, nutrition_confidence TEXT, nutrition_ranges JSON, analyzed_at TIMESTAMP, nutrition_source TEXT, estimator_version TEXT
            )'''))
            import catalog
            await conn.execute(text('CREATE TABLE user_profiles(id INTEGER PRIMARY KEY)'))
            await catalog.migrate(conn)
            import service_area
            await service_area.migrate(conn)
            await search.migrate(conn)
            await search.migrate(conn)
            await conn.execute(text("""INSERT INTO menu_items
                (restaurant_name,item_name,description,price_mkd,estimated_calories,estimated_protein)
                VALUES ('Alpha','Chicken bowl','grilled chicken with rice',250,500,40),
                       ('Beta','Pizza','cheese pizza',500,900,20),
                       ('Alpha','Chicken salad','chicken and vegetables',200,300,35)"""))
            await conn.execute(text("INSERT INTO restaurants(name,url,city) VALUES ('Alpha','https://wolt.com/alpha','Skopje'),('Beta','https://wolt.com/beta','Skopje')"))
            await conn.execute(text("UPDATE restaurants SET service_verified_until=now()+interval '30 days'"))
            await conn.execute(text("INSERT INTO restaurant_delivery_areas(restaurant_id,latitude,longitude,radius_km) SELECT id,42,21,10 FROM restaurants"))
            await conn.execute(text("INSERT INTO restaurant_opening_hours SELECT id,d,0,1440 FROM restaurants CROSS JOIN generate_series(0,6) d"))
            await conn.execute(text("UPDATE menu_items m SET restaurant_id=r.id,last_seen_at=now() AT TIME ZONE 'UTC' FROM restaurants r WHERE m.restaurant_name=r.name"))

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA {self.schema} CASCADE'))
        await self.admin.dispose()

    async def test_indexing_is_incremental_and_price_changes_do_not_reembed(self):
        self.assertEqual(await search.index_pending(self.sessions, self.client), 3)
        self.assertEqual(await search.index_pending(self.sessions, self.client), 0)
        async with self.engine.begin() as conn:
            await conn.execute(text('UPDATE menu_items SET price_mkd=260 WHERE id=1'))
        self.assertEqual(await search.index_pending(self.sessions, self.client), 0)
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE menu_items SET description='new chicken recipe' WHERE id=1"))
        self.assertEqual(await search.index_pending(self.sessions, self.client), 1)
        async with self.engine.begin() as conn:
            await conn.execute(text('DELETE FROM menu_items WHERE id=1'))
            self.assertEqual((await conn.execute(text('SELECT count(*) FROM meal_search_documents'))).scalar(), 2)

    async def test_hybrid_filters_and_keyword_fallback(self):
        await search.index_pending(self.sessions, self.client)
        req = search.SearchRequest(query='chicken', restaurant='Alpha', max_price_mkd=250, min_protein=30, max_calories=400, generate_answer=False)
        async with self.sessions() as db:
            result = await search.search(db, self.client, req)
        self.assertEqual([m['id'] for m in result['meals']], [3])
        self.assertEqual(result['retrieval']['mode'], 'hybrid')
        self.client.embeddings.create.side_effect = RuntimeError('Simulated provider outage')
        async with self.sessions() as db:
            result = await search.search(db, self.client, req)
        self.assertEqual([m['id'] for m in result['meals']], [3])
        self.assertEqual(result['retrieval']['mode'], 'keyword')

    async def test_rag_evidence_and_empty_results(self):
        await search.index_pending(self.sessions, self.client)
        self.client.beta.chat.completions.parse.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=search.GroundedSelection(recommendations=[search.Evidence(meal_id=1, quote='grilled chicken')])) )])
        async with self.sessions() as db:
            result = await search.search(db, self.client, search.SearchRequest(query='chicken'))
        self.assertEqual(result['answer_status'], 'grounded')
        self.assertEqual(result['recommendations'][0]['meal_id'], 1)
        self.assertEqual(result['meals'][0]['id'], 1)
        async with self.sessions() as db:
            empty = await search.search(db, self.client, search.SearchRequest(query='chicken', max_price_mkd=1))
        self.assertEqual(empty['meals'], [])
        self.assertEqual(empty['answer_status'], 'no_results')
        self.assertEqual(self.client.beta.chat.completions.parse.await_count, 1)

    async def test_weekly_retrieval_uses_hybrid_search_without_preselecting_a_plan(self):
        import weekly_retrieval
        await search.index_pending(self.sessions, self.client)
        profile = SimpleNamespace(latitude=42.,longitude=21.,delivery_minute=780,city='Skopje',goal="muscle gain", daily_budget_mkd=400,
            daily_calorie_target=2100, daily_protein_target=120, daily_carbs_target=240, daily_fat_target=60)
        async with self.sessions() as db:
            candidates, info = await weekly_retrieval.retrieve_candidates(db, self.client, profile, 100)
        self.assertEqual(info['mode'], 'hybrid')
        self.assertGreater(info['semantic_candidates'], 0)
        self.assertEqual(info['query_embeddings'], 3)
        self.assertLessEqual(len(candidates), weekly_retrieval.CANDIDATE_LIMIT)
        self.assertTrue(all(m['price_mkd'] <= 300 for m in candidates))
        self.assertEqual({m['id'] for m in candidates}, {1,3})
        self.assertEqual(len(self.client.embeddings.create.call_args.kwargs['input']), 3)
        async with self.engine.begin() as conn:
            await conn.execute(text('UPDATE menu_items SET food_assessment=CAST(:value AS json) WHERE id=1'),
                               {'value': '{"is_meal":false}'})
        async with self.sessions() as db:
            candidates, _ = await weekly_retrieval.retrieve_candidates(db, self.client, profile, 100)
        self.assertEqual({m['id'] for m in candidates}, {3})
        profile.goal = 'uncached test goal'
        self.client.embeddings.create.side_effect = RuntimeError('provider unavailable')
        async with self.sessions() as db:
            fallback, info = await weekly_retrieval.retrieve_candidates(db, self.client, profile, 100)
        self.assertEqual(info['mode'], 'nutrition_fallback')
        self.assertEqual({m['id'] for m in fallback}, {3})


    async def test_rag_failure_preserves_retrieved_matches(self):
        await search.index_pending(self.sessions, self.client)
        self.client.beta.chat.completions.parse.side_effect = RuntimeError('Simulated RAG outage')
        async with self.sessions() as db:
            result = await search.search(db, self.client, search.SearchRequest(query='chicken', limit=1))
        self.assertEqual(len(result['meals']), 1)
        self.assertEqual(result['answer_status'], 'unavailable')
        self.assertEqual(result['recommendations'], [])

    async def test_failed_embedding_batches_back_off_and_text_changes_reset_retries(self):
        self.client.embeddings.create.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            await search.index_pending(self.sessions,self.client)
        self.assertEqual(await search.index_pending(self.sessions,self.client),0)
        async with self.engine.begin() as conn:
            self.assertEqual((await conn.execute(text('SELECT min(attempts) FROM meal_search_documents'))).scalar(),1)
            await conn.execute(text("UPDATE menu_items SET description='updated ingredients' WHERE id=1"))
            self.assertEqual((await conn.execute(text('SELECT attempts FROM meal_search_documents WHERE menu_item_id=1'))).scalar(),0)
        self.client = fake_client()
        self.assertEqual(await search.index_pending(self.sessions,self.client),1)

    async def test_changed_document_during_embedding_is_not_overwritten(self):
        original = self.client.embeddings.create.side_effect
        async def changing(**kwargs):
            async with self.engine.begin() as conn:
                await conn.execute(text("UPDATE menu_items SET description='changed while embedding' WHERE id=1"))
            return await original(**kwargs)
        self.client.embeddings.create.side_effect = changing
        self.assertEqual(await search.index_pending(self.sessions, self.client, max_batches=1), 2)
        async with self.engine.connect() as conn:
            self.assertIsNone((await conn.execute(text('SELECT embedding FROM meal_search_documents WHERE menu_item_id=1'))).scalar())


if __name__ == '__main__':
    unittest.main()
