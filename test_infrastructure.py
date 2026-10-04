"""Isolated PostgreSQL tests; never scrape or call paid providers."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import service_area
from test_ai_meal_planner import assessment


class ServiceValidationTests(unittest.TestCase):
    def test_overnight_hours_must_be_split(self):
        with self.assertRaises(ValueError):
            service_area.OpeningWindow(weekday=0,start_minute=1200,end_minute=120)

    def test_invalid_delivery_coordinates_rejected(self):
        with self.assertRaises(ValueError):
            service_area.DeliveryArea(latitude=91,longitude=0,radius_km=5)


@unittest.skipUnless(os.getenv('RUN_SEARCH_DB_TESTS')=='1','Opt-in PostgreSQL integration tests')
class InfrastructureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from dotenv import load_dotenv
        load_dotenv('.env')
        import main
        self.main=main
        self.schema='test_infra_'+uuid4().hex
        self.admin=create_async_engine(os.environ['DATABASE_URL'])
        async with self.admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {self.schema}'))
        self.engine=create_async_engine(os.environ['DATABASE_URL'],connect_args={'server_settings':{'search_path':f'{self.schema},public'}})
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: main.Base.metadata.create_all(sync, checkfirst=False))
            for _ in range(2):
                await main.catalog.migrate(conn)
                await service_area.migrate(conn)
                await main.ingestion.migrate(conn)
                await main.auth.migrate(conn)
                await main.plan_jobs.migrate(conn)
                await main.meal_search.migrate(conn)
        async with self.sessions() as db:
            for uid in (1,2):
                db.add(main.UserProfile(id=uid,city='Skopje',latitude=42,longitude=21,delivery_minute=780,
                    current_weight_kg=70,target_weight_kg=70,height_cm=175,age=30,sex='male',
                    activity_level='sedentary',goal='maintenance',daily_budget_mkd=1000,
                    daily_calorie_target=2100,daily_protein_target=150,daily_carbs_target=240,daily_fat_target=60))
            await db.commit()
        self.profile=SimpleNamespace(city='Skopje',latitude=42.,longitude=21.,delivery_minute=780)
        self.service={'restaurant_id':1,'latitude':42,'longitude':21,'source':'test fixture',
            'verified_until':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),
            'delivery_areas':[{'latitude':42,'longitude':21,'radius_km':5}],
            'opening_hours':[{'weekday':d,'start_minute':0,'end_minute':1440} for d in range(7)]}
        async with self.sessions() as db:
            await service_area.import_service(db,[self.service])

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA {self.schema} CASCADE'))
        await self.admin.dispose()

    async def test_delivery_hours_distance_availability_and_verification(self):
        async with self.sessions() as db:
            self.assertEqual((await service_area.eligible_restaurants(db,self.profile,require_verified=True))[0],[1])
            self.profile.longitude=23
            self.assertEqual((await service_area.eligible_restaurants(db,self.profile,require_verified=True))[0],[])
            self.profile.longitude=21
            await db.execute(text('UPDATE restaurants SET available=false WHERE id=1'))
            self.assertEqual((await service_area.eligible_restaurants(db,self.profile,require_verified=True))[0],[])
            await db.execute(text("UPDATE restaurants SET available=true,service_verified_until=now()-interval '1 hour' WHERE id=1"))
            self.assertEqual((await service_area.eligible_restaurants(db,self.profile,require_verified=True))[0],[])
            await db.rollback()
            await db.execute(text('DELETE FROM restaurant_opening_hours WHERE restaurant_id=1 AND weekday=2'))
            self.assertEqual((await service_area.eligible_restaurants(db,self.profile,require_verified=True))[0],[])

    async def test_city_planning_needs_no_coordinates_or_verified_coverage(self):
        self.profile.latitude=None
        self.profile.longitude=None
        async with self.sessions() as db:
            await db.execute(text('UPDATE restaurants SET service_verified_until=NULL'))
            ids,status=await service_area.eligible_restaurants(db,self.profile)
            self.assertIn(1,ids)
            self.assertEqual(status,'city_only_unverified')
            await db.execute(text('UPDATE restaurants SET available=false WHERE id=1'))
            ids,_=await service_area.eligible_restaurants(db,self.profile)
            self.assertNotIn(1,ids)

    async def test_unknown_location_never_claims_service(self):
        self.profile.latitude=None
        async with self.sessions() as db:
            self.assertEqual(await service_area.eligible_restaurants(db,self.profile,require_verified=True),([], 'location_required'))

    async def test_two_users_cannot_read_or_replace_each_others_plans(self):
        import auth
        async with self.sessions() as db:
            tokens={}
            for uid in (1,2):
                identity=auth.user_context.set(uid)
                try:
                    profile,state=await self.main.locked_profile_and_state(db)
                    state.daily_fee_allowance_mkd=100
                    await db.commit()
                    _,tokens[uid],_,_=await self.main.begin_plan_refresh(db)
                    await self.main.publish_plan_result(db,tokens[uid],{'status':'no_plan','message':f'user {uid}'})
                finally:
                    auth.user_context.reset(identity)
            identity=auth.user_context.set(2)
            try:
                result=await self.main.publish_plan_result(db,tokens[1],{'status':'no_plan','message':'wrong'})
                self.assertEqual(result['status'],'superseded')
                self.assertEqual((await self.main.current_daily_plan(db))['message'],'user 2')
            finally:
                auth.user_context.reset(identity)

    async def test_plan_job_deduplication_and_crash_retry_limit(self):
        import plan_jobs
        async with self.sessions() as db:
            await plan_jobs.enqueue(db,1)
            await plan_jobs.enqueue(db,1)
        first=await plan_jobs.claim(self.sessions)
        self.assertEqual(first['attempts'],1)
        self.assertIsNone(await plan_jobs.claim(self.sessions))
        async with self.sessions() as db:
            await db.execute(text("UPDATE weekly_plan_jobs SET attempts=3,lease_until=now()-interval '1 second'"))
            await db.commit()
        self.assertIsNone(await plan_jobs.claim(self.sessions))
        async with self.sessions() as db:
            self.assertEqual((await plan_jobs.status(db,1))['status'],'refresh_failed')
            await plan_jobs.enqueue(db,1)
        self.assertEqual((await plan_jobs.claim(self.sessions))['attempts'],1)

    async def test_scrape_and_estimate_are_separate_and_stale_estimate_is_rejected(self):
        import ingestion
        item={'item_name':'Chicken rice','category':'Meals','description':'chicken','price_mkd':200,'image_url':None}
        async def scrape(items):
            async with self.sessions() as db:
                with patch('wolt_scraper.scrape_wolt_menu',AsyncMock(return_value=items)):
                    await ingestion.scrape_restaurant(db,restaurants_override=[{'id':1,'name':'Picerija 511','url':'https://test.invalid'}])
        await scrape([item])
        job=await ingestion.claim(self.sessions)
        await scrape([item|{'description':'changed ingredients'}])
        macros=self.main.complete_nutrition_uncertainty(self.main.AssessedNutrition(calories=600,protein_grams=40,carbs_grams=70,fat_grams=20, food_assessment=assessment("test dish")))
        self.assertFalse(await ingestion.publish(self.sessions,job,macros))
        new=await ingestion.claim(self.sessions)
        self.assertTrue(await ingestion.publish(self.sessions,new,macros))
        await scrape([item|{'description':'changed ingredients','price_mkd':250}])
        self.assertIsNone(await ingestion.claim(self.sessions))
        async with self.sessions() as db:
            row=(await db.execute(text('SELECT price_mkd,is_available FROM menu_items'))).one()
            self.assertEqual(tuple(row),(250,True))
            stored=(await db.execute(text('SELECT food_assessment FROM menu_items'))).scalar_one()
            self.assertEqual(stored, dict(macros.food_assessment.model_dump(), assessment_version=__import__('food_evaluation').ASSESSMENT_VERSION))
            with patch('wolt_scraper.scrape_wolt_menu',AsyncMock(return_value=[])):
                with self.assertRaises(ValueError):
                    await ingestion.scrape_restaurant(db,restaurants_override=[{'id':1,'name':'Picerija 511','url':'https://test.invalid'}])
            self.assertTrue((await db.execute(text('SELECT is_available FROM menu_items'))).scalar())

    async def test_authentication_origin_sessions_and_logout(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport
        import auth
        app=FastAPI()
        auth.install(app,self.sessions)
        @app.get('/api/private')
        async def private():
            return {'uid':auth.current_user_id()}
        with patch.dict(os.environ,{'WOLT_AUTH_REQUIRED':'true'}):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                self.assertEqual((await client.get('/api/private')).status_code,401)
                result=await client.post('/api/auth/register',json={'username':'alice','password':'a strong password here'})
                self.assertEqual(result.status_code,200)
                uid=result.json()['user_id']
                self.assertGreater(uid,1)
                self.assertEqual((await client.get('/api/private')).json()['uid'],uid)
                self.assertEqual((await client.post('/api/auth/logout',headers={'origin':'http://evil.test'})).status_code,403)
                await client.post('/api/auth/logout')
                self.assertEqual((await client.get('/api/private')).status_code,401)
                self.assertEqual((await client.post('/api/auth/login',json={'username':'alice','password':'incorrect password here'})).status_code,401)
                self.assertEqual((await client.post('/api/auth/login',json={'username':'alice','password':'a strong password here'})).status_code,200)

    async def test_catalog_import_preserves_ids_and_city_changes_clear_verification(self):
        import catalog
        venue={'name':'Picerija 511','url':'https://wolt.com/en/mkd/skopje/restaurant/picerija-511','city':'Skopje'}
        async with self.sessions() as db:
            self.assertEqual(await catalog.import_restaurants(db,[venue]),[1])
            self.assertIsNotNone((await db.execute(text('SELECT service_verified_until FROM restaurants WHERE id=1'))).scalar())
            self.assertEqual(await catalog.import_restaurants(db,[venue|{'city':'Elsewhere'}]),[1])
            self.assertIsNone((await db.execute(text('SELECT service_verified_until FROM restaurants WHERE id=1'))).scalar())

    async def test_cached_plan_rejects_changed_price_or_service(self):
        from main import MenuItem
        async with self.sessions() as db:
            item=MenuItem(restaurant_id=1,restaurant_name='Fixture',item_name='Chicken',price_mkd=200,
                estimated_calories=600,estimated_protein=40,estimated_carbs=70,estimated_fat=20,
                is_available=True,last_seen_at=datetime.now(timezone.utc).replace(tzinfo=None))
            db.add(item)
            await db.commit()
            meals=[{'id':item.id,'price_mkd':200}]
            self.assertTrue(await self.main.cached_meals_valid(db,meals))
            item.price_mkd=250
            await db.commit()
            self.assertFalse(await self.main.cached_meals_valid(db,meals))
            item.price_mkd=200
            await db.execute(text('UPDATE restaurants SET available=false WHERE id=1'))
            await db.commit()
            self.assertFalse(await self.main.cached_meals_valid(db,meals))

    async def test_order_preview_scopes_users_and_revalidates_saved_day(self):
        import ordering
        import auth
        from fastapi import HTTPException
        today=self.main.planner_today()
        generation=str(uuid4())
        async with self.sessions() as db:
            meals=[]
            for i in range(3):
                item=self.main.MenuItem(restaurant_id=1,restaurant_name='Fixture',item_name=f'Order meal {i}',
                    price_mkd=200,estimated_calories=600,estimated_protein=40,estimated_carbs=70,
                    estimated_fat=20,is_available=True,last_seen_at=datetime.now(timezone.utc).replace(tzinfo=None))
                db.add(item)
                await db.flush()
                meals.append({'id':item.id,'item_name':item.item_name,'price_mkd':200, 'restaurant_id':1, 'restaurant_name':'Fixture'})
            db.add(self.main.WeeklyPlanState(user_id=1,generation=generation,start_date=today,
                result={'status':'ready','weekly_plan':[{'status':'ready','day_number':1,
                    'date':today.isoformat(),'meals':meals,'budget':{'estimated_total_mkd':700}}]}))
            await db.commit()
            identity=auth.user_context.set(1)
            try:
                with patch.object(self.main.meal_planning, 'MIN_WEEKLY_RESTAURANTS', 1), patch('wolt_scraper.scrape_wolt_menu', AsyncMock(return_value=[{'item_name':m['item_name'],'price_mkd':m['price_mkd']} for m in meals])):
                    result=await ordering.preview(db,1,today,generation)
                self.assertEqual(result['food_subtotal_mkd'],600)
                self.assertFalse(result['payment_automatic'])
                for day,planned_date,version in ((2,today,generation),(1,today+timedelta(days=1),generation),(1,today,'stale')):
                    with self.assertRaises(HTTPException):
                        await ordering.preview(db,day,planned_date,version)
                identity2=auth.user_context.set(2)
                try:
                    with self.assertRaises(HTTPException):
                        await ordering.preview(db,1,today,generation)
                finally:
                    auth.user_context.reset(identity2)
                item.price_mkd=250
                await db.commit()
                with self.assertRaises(HTTPException):
                    await ordering.preview(db,1,today,generation)
            finally:
                auth.user_context.reset(identity)

    async def test_request_limits_are_isolated_by_user(self):
        import query_cache
        from fastapi import HTTPException
        async with self.sessions() as db:
            for _ in range(10):
                await query_cache.throttle(db,1,'/api/user/profile')
            with self.assertRaises(HTTPException) as error:
                await query_cache.throttle(db,1,'/api/user/profile')
            self.assertEqual(error.exception.status_code,429)
            await query_cache.throttle(db,2,'/api/user/profile')

    async def test_query_cache_and_expiry(self):
        import query_cache
        from test_meal_search import fake_client
        client=fake_client()
        async with self.sessions() as db:
            first=await query_cache.embed(db,client,['chicken'])
            self.assertEqual(first,await query_cache.embed(db,client,['chicken']))
            self.assertEqual(client.embeddings.create.await_count,1)
            await db.execute(text("UPDATE query_embedding_cache SET expires_at=now()-interval '1 second'"))
            await db.commit()
            await query_cache.embed(db,client,['chicken'])
            self.assertEqual(client.embeddings.create.await_count,2)


if __name__=='__main__':
    unittest.main()
