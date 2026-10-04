"""Authenticated ASGI + durable job load against the benchmark's isolated schema.

Uses actual routes, auth middleware, PostgreSQL jobs and weekly planner. Provider
responses are stubbed by the scale runner. Excludes TCP/TLS and provider latency.
"""
import asyncio
import hashlib
import time
from unittest.mock import patch
from fastapi import FastAPI, Depends
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
import auth
import plan_jobs
import query_cache


async def run(engine,sessions,concurrency):
    import main
    from benchmarks.scale import percentiles
    async with engine.begin() as conn:
        tables=[main.UserProfile.__table__,main.DailyPlanState.__table__,main.WeeklyPlanState.__table__,main.DailyMealQueue.__table__,main.WeightLog.__table__]
        await conn.run_sync(lambda sync:main.Base.metadata.create_all(sync,tables=tables,checkfirst=False))
        await auth.migrate(conn)
        await plan_jobs.migrate(conn)
        await query_cache.migrate(conn)
    async with sessions() as db:
        for i in range(concurrency):
            uid=i+100
            db.add(main.UserProfile(id=uid,city=f'City {i}',latitude=42,longitude=21,delivery_minute=780,
                current_weight_kg=70,target_weight_kg=70,height_cm=175,age=30,sex='male',activity_level='sedentary',goal='maintenance',
                daily_budget_mkd=1400,daily_calorie_target=2200,daily_protein_target=140,daily_carbs_target=250,daily_fat_target=70))
            await db.flush()
            db.add(main.DailyPlanState(user_id=uid,daily_fee_allowance_mkd=100,generation=str(uid),plan_date=main.planner_today(),result={'status':'refresh_required'}))
            await db.execute(text("INSERT INTO accounts(id,username,password_hash) VALUES (:id,:name,'unused benchmark session')"),{'id':uid,'name':f'load-{uid}'})
            await db.execute(text("INSERT INTO auth_sessions VALUES (:token,:id,now()+interval '1 hour')"),
                {'id':uid,'token':hashlib.sha256(f'benchmark-{uid}'.encode()).hexdigest()})
        await db.commit()
    app=FastAPI()
    auth.install(app,sessions)
    async def db_dependency():
        async with sessions() as db:
            yield db
    @app.get('/api/user/profile')
    async def profile(db=Depends(db_dependency)):
        return await main.get_user_profile(db)
    @app.post('/api/agent/weekly-plan')
    async def enqueue(db=Depends(db_dependency)):
        return await main.create_weekly_plan(db)
    @app.get('/api/agent/weekly-plan')
    async def result(db=Depends(db_dependency)):
        return await main.get_weekly_plan(db)
    submissions=[]; reads=[]; completions=[]; errors=[]; days=[]
    async def user(i):
        uid=i+100
        async with AsyncClient(transport=ASGITransport(app=app),base_url='https://load.test',cookies={'wolt_session':f'benchmark-{uid}'}) as client:
            begin=time.perf_counter()
            response=await client.get('/api/user/profile')
            if response.status_code!=200 or response.json()['profile']['id']!=uid:
                raise AssertionError('Profile isolation failed')
            start=time.perf_counter()
            response=await client.post('/api/agent/weekly-plan')
            submissions.append(time.perf_counter()-start)
            response.raise_for_status()
            async with asyncio.timeout(240):
                while True:
                    start=time.perf_counter()
                    response=await client.get('/api/agent/weekly-plan')
                    reads.append(time.perf_counter()-start)
                    response.raise_for_status()
                    result=response.json()
                    if result['status']!='refreshing':
                        if result['status'] not in ('ready','poor_fit','no_plan'):
                            errors.append(result['status'])
                        days.extend(result.get('weekly_plan',[]))
                        completions.append(time.perf_counter()-begin)
                        break
                    await asyncio.sleep(.25)
    with patch.dict('os.environ',{'WOLT_AUTH_REQUIRED':'true'}):
        workers=[asyncio.create_task(plan_jobs.worker(sessions,main.generate_weekly_plan)) for _ in range(2)]
        try:
            results=await asyncio.gather(*(user(i) for i in range(concurrency)),return_exceptions=True)
            errors.extend(str(r) for r in results if isinstance(r,Exception))
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers,return_exceptions=True)
    return {'concurrent_authenticated_users':concurrency,'plan_workers':2,'submitted_requests':len(submissions),
            'poll_requests':len(reads),'completed_users':len(completions),'errors':errors,
            'submit_latency':percentiles(submissions),'poll_latency':percentiles(reads),
            'queue_to_completion_latency':percentiles(completions),'planned_days':len(days),
            'budget_compliance':sum(day['budget']['estimated_total_mkd']<=1400 for day in days)/len(days) if days else None,
            'nutrition_fit_rate':sum(day['status']=='ready' for day in days)/len(days) if days else None,
            'transport':'ASGI in-process; excludes TCP/TLS and external providers'}
