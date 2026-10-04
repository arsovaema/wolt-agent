"""Isolated, reproducible scale/quality benchmark using bootstrapped real menu seeds.

No paid APIs. Never modifies public application tables. Fixtures and their indexes
are dropped in finally; reports are retained. Not a human relevance evaluation.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from dotenv import load_dotenv
import weekly_retrieval
import service_area
from meal_planning import select_best_daily_meals, summarize_plan


def percentiles(values):
    values=sorted(values)
    if not values:
        return {}
    return {f'p{p}_ms':round(values[min(len(values)-1,math.ceil(len(values)*p/100)-1)]*1000,2) for p in (50,95,99)}


async def run(args):
    load_dotenv()
    schema='benchmark_'+uuid4().hex
    admin=create_async_engine(os.environ['DATABASE_URL'])
    engine=create_async_engine(os.environ['DATABASE_URL'],pool_size=args.concurrency,max_overflow=0,
        connect_args={'server_settings':{'search_path':schema+',public'}})
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    report={'schema':schema,'rows':args.rows,'concurrency':args.concurrency,'requests':args.requests,'cities':args.cities,'restaurants':10000,'nominal_meals_per_city':args.rows/args.cities,
            'fixture':'Bootstrapped observed menu nutrition/prices and stored real embeddings; synthetic cities, service areas, freshness and availability',
            'limitations':['No live provider latency or spend','No HTTP/TLS/network or login load included',
                           'Not millions of distinct real menus; no human relevance labels',
                           'Unused global HNSW index omitted; this measures exact local retrieval, not ingestion throughput'],
            'environment':{'platform':platform.platform(),'python':sys.version.split()[0]},'errors':[]}
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {schema}'))
        async with engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE seeds AS SELECT row_number() OVER(ORDER BY m.id)::integer AS seed_id,
                m.item_name,m.category,m.description,m.price_mkd,m.estimated_calories,m.estimated_protein,
                m.estimated_carbs,m.estimated_fat,m.nutrition_ranges,m.nutrition_confidence,m.nutrition_source,
                m.estimator_version,d.embedding FROM public.menu_items m JOIN public.meal_search_documents d ON d.menu_item_id=m.id
                WHERE m.price_mkd>0 AND m.estimated_calories>0 AND d.embedding IS NOT NULL LIMIT 1000'''))
            count=(await conn.execute(text('SELECT count(*) FROM seeds'))).scalar()
            if not count:
                raise RuntimeError('Need at least one observed menu with a stored embedding for representative seed data')
            report['observed_seed_meals']=count
            await conn.execute(text('CREATE UNIQUE INDEX seed_number ON seeds(seed_id)'))
            report['environment']['postgres']=(await conn.execute(text('SELECT version()'))).scalar()
            await conn.execute(text('''CREATE TABLE restaurants (
                id INTEGER PRIMARY KEY,name TEXT,city TEXT,is_active BOOLEAN DEFAULT true,available BOOLEAN DEFAULT true,
                timezone TEXT DEFAULT 'Europe/Skopje',service_verified_until TIMESTAMPTZ DEFAULT now()+interval '30 days')'''))
            await conn.execute(text("INSERT INTO restaurants(id,name,city) SELECT n,'Venue '||n,'City '||((n-1)/:per_city) FROM generate_series(1,10000) n"),{'per_city':10000//args.cities})
            await conn.execute(text('CREATE INDEX restaurant_city ON restaurants(lower(city),id)'))
            await conn.execute(text('''CREATE TABLE restaurant_delivery_areas (
                restaurant_id INTEGER,latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,radius_km DOUBLE PRECISION)'''))
            await conn.execute(text('INSERT INTO restaurant_delivery_areas SELECT id,42,21,5 FROM restaurants'))
            await conn.execute(text('CREATE INDEX area_restaurant ON restaurant_delivery_areas(restaurant_id)'))
            await conn.execute(text('CREATE TABLE restaurant_opening_hours(restaurant_id INTEGER,weekday INTEGER,start_minute INTEGER,end_minute INTEGER,PRIMARY KEY(restaurant_id,weekday,start_minute))'))
            await conn.execute(text('INSERT INTO restaurant_opening_hours SELECT id,d,0,1440 FROM restaurants CROSS JOIN generate_series(0,6) d'))
            await conn.execute(text('''CREATE TABLE menu_items (
                id INTEGER PRIMARY KEY,restaurant_id INTEGER,restaurant_name TEXT,item_name TEXT,category TEXT,description TEXT,
                price_mkd INTEGER,estimated_calories INTEGER,estimated_protein DOUBLE PRECISION,estimated_carbs DOUBLE PRECISION,
                estimated_fat DOUBLE PRECISION,image_url TEXT,nutrition_ranges JSON,nutrition_confidence TEXT,nutrition_source TEXT,
                estimator_version TEXT,is_available BOOLEAN,last_seen_at TIMESTAMP,food_assessment JSON DEFAULT '{}'::json)'''))
            await conn.execute(text("CREATE TABLE meal_search_documents(menu_item_id INTEGER PRIMARY KEY,embedding vector(512),embedding_version TEXT,search_text tsvector DEFAULT ''::tsvector)"))
        started=time.perf_counter()
        for start in range(1,args.rows+1,50000):
            async with engine.begin() as conn:
                await conn.execute(text('''INSERT INTO menu_items
                    SELECT n,1+(n-1)%10000,'Venue '||(1+(n-1)%10000),s.item_name||' #'||n,s.category,s.description,
                    greatest(1,round(s.price_mkd*(0.8+(n%41)*0.01)))::integer,
                    s.estimated_calories,s.estimated_protein,s.estimated_carbs,s.estimated_fat,NULL,
                    s.nutrition_ranges,s.nutrition_confidence,s.nutrition_source,s.estimator_version,
                    n%17<>0, (now() AT TIME ZONE 'UTC') - CASE WHEN n%23=0 THEN interval '96 hours' ELSE interval '1 hour' END
                    FROM generate_series(CAST(:start AS integer),CAST(:end AS integer)) n JOIN seeds s ON s.seed_id=1+(n-1)%:count'''),
                    {'start':start,'end':min(args.rows,start+49999),'count':count})
                await conn.execute(text('''INSERT INTO meal_search_documents(menu_item_id,embedding,embedding_version) SELECT n,s.embedding,:version
                    FROM generate_series(CAST(:start AS integer),CAST(:end AS integer)) n JOIN seeds s ON s.seed_id=1+(n-1)%:count'''),
                    {'start':start,'end':min(args.rows,start+49999),'count':count,'version':weekly_retrieval.VERSION})
            if start==1 or start%250000==1:
                print(f'Loaded {min(args.rows,start+49999):,}/{args.rows:,} meals',flush=True)
        print('Building local nutrition and price indexes',flush=True)
        async with engine.begin() as conn:
            for key in ('price_mkd','estimated_calories','estimated_protein'):
                await conn.execute(text(f'CREATE INDEX local_{key} ON menu_items(restaurant_id,{key},id) WHERE is_available'))
            await conn.execute(text('ANALYZE menu_items'))
            await conn.execute(text('ANALYZE meal_search_documents'))
            await conn.execute(text('ANALYZE restaurants'))
            await conn.execute(text('ANALYZE restaurant_delivery_areas'))
            await conn.execute(text('ANALYZE restaurant_opening_hours'))
            vectors=(await conn.execute(text('SELECT embedding::text FROM seeds ORDER BY seed_id LIMIT 3'))).scalars().all()
            report['fixture_size_bytes']=(await conn.execute(text('''SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relkind='r' '''),{'schema':schema})).scalar()
        report['load_seconds']=round(time.perf_counter()-started,2)
        vectors=(vectors*3)[:3]
        async def cached_vectors(db,client,queries):
            return vectors
        semaphore=asyncio.Semaphore(args.concurrency)
        durations=[]; plan_times=[]; qualities=[]; recalls=[]; nutrition_recalls=[]; nutrition_regrets=[]; signature_recalls=[]; errors=[]
        async def request(i,quality=False):
            async with semaphore:
                profile=SimpleNamespace(city=f'City {i%args.cities}',latitude=42.,longitude=21.,delivery_minute=780,
                    goal=('maintenance','fat loss','muscle gain')[i%3],daily_budget_mkd=(700,1000,1400)[i%3],
                    daily_calorie_target=(1800,2200,2700)[i%3],daily_protein_target=(100,140,180)[i%3],
                    daily_carbs_target=(200,250,310)[i%3],daily_fat_target=(60,70,90)[i%3])
                try:
                    begin=time.perf_counter()
                    async with sessions() as db:
                        candidates,meta=await weekly_retrieval.retrieve_candidates(db,None,profile,100)
                    if not quality:
                        durations.append(time.perf_counter()-begin)
                    if quality:
                        ids={m['id'] for m in candidates}
                        async with sessions() as db:
                            await db.execute(text('SET LOCAL max_parallel_workers_per_gather=0'))
                            restaurants,_=await service_area.eligible_restaurants(db,profile)
                            where="m.restaurant_id=ANY(CAST(:ids AS integer[])) AND m.is_available AND m.last_seen_at>=(now() AT TIME ZONE 'UTC')-interval '72 hours' AND m.price_mkd>0 AND m.price_mkd<=:budget"
                            params={'ids':restaurants,'budget':profile.daily_budget_mkd-100}
                            expected=set()
                            for vector in vectors:
                                rows=(await db.execute(text(f'''SELECT m.id FROM menu_items m JOIN meal_search_documents d ON m.id=d.menu_item_id
                                    WHERE {where} ORDER BY d.embedding <=> CAST(:vector AS vector),m.id LIMIT 12'''),params|{'vector':vector})).scalars().all()
                                expected.update(rows)
                            recalls.append(len(expected & ids)/len(expected) if expected else 1)
                            targets={k:getattr(profile,'daily_'+('calorie' if k=='calories' else k)+'_target')/3 for k in ('calories','protein','carbs','fat')}
                            nearest=set((await db.execute(text(f'''SELECT m.id FROM menu_items m WHERE {where} ORDER BY
                                abs(estimated_calories-:calories)/:calories*0.35+abs(estimated_protein-:protein)/:protein*0.35+
                                abs(estimated_carbs-:carbs)/:carbs*0.15+abs(estimated_fat-:fat)/:fat*0.15,m.id LIMIT 20'''),params|targets)).scalars().all())
                            nutrition_recalls.append(len(nearest & ids)/len(nearest) if nearest else 1)
                            signature_rows=(await db.execute(text(f"""WITH signatures AS (
                                SELECT DISTINCT m.estimated_calories AS calories,m.estimated_protein AS protein,
                                    m.estimated_carbs AS carbs,m.estimated_fat AS fat
                                FROM menu_items m WHERE {where})
                                SELECT * FROM signatures ORDER BY
                                    abs(calories-:calories)/:calories*0.35+abs(protein-:protein)/:protein*0.35+
                                    abs(carbs-:carbs)/:carbs*0.15+abs(fat-:fat)/:fat*0.15,
                                    calories,protein,carbs,fat LIMIT 20"""),params|targets)).mappings().all()
                            keys=('calories','protein','carbs','fat')
                            expected_signatures={tuple(row[key] for key in keys) for row in signature_rows}
                            retrieved_signatures={tuple(row[key] for key in keys) for row in candidates}
                            signature_recalls.append(len(expected_signatures & retrieved_signatures)/len(expected_signatures) if expected_signatures else 1)
                            def distance(meal):
                                return sum(abs(meal[k]-targets[k])/targets[k]*weight for k,weight in
                                           (('calories',.35),('protein',.35),('carbs',.15),('fat',.15)))
                            exact_rows=(await db.execute(text("SELECT estimated_calories AS calories,estimated_protein AS protein,estimated_carbs AS carbs,estimated_fat AS fat FROM menu_items WHERE id=ANY(CAST(:ids AS integer[]))"),{'ids':list(nearest)})).mappings().all()
                            if exact_rows and candidates:
                                nutrition_regrets.append(max(0,min(map(distance,candidates))-min(map(distance,exact_rows))))
                        t=time.perf_counter()
                        selected,_=await asyncio.to_thread(select_best_daily_meals,candidates,profile,daily_fee_allowance_mkd=100)
                        plan_times.append(time.perf_counter()-t)
                        if selected:
                            summary=summarize_plan(selected,profile,100)
                            qualities.append({'served':True,'budget_ok':summary['budget']['estimated_total_mkd']<=profile.daily_budget_mkd,'fit':summary['status']=='ready'})
                        else:
                            qualities.append({'served':False,'budget_ok':None,'fit':False})
                    return len(candidates)
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                    return 0
        print(f'Measuring {args.requests} retrieval requests at concurrency {args.concurrency}',flush=True)
        with patch('weekly_retrieval.query_cache.embed',cached_vectors):
            begin=time.perf_counter()
            sizes=await asyncio.gather(*(request(i) for i in range(args.requests)))
            report['workload_seconds']=round(time.perf_counter()-begin,2)
            report['quality_phase']='separate from concurrent latency workload'
            print(f'Measuring {args.quality_samples} exact-reference quality cases',flush=True)
            for i in range(args.quality_samples):
                await request(i,quality=True)
        served=[q for q in qualities if q['served']]
        report.update(retrieval_latency=percentiles(durations),planner_latency=percentiles(plan_times),
            successful_requests=len(durations),errors=errors,candidate_max=max(sizes,default=0),
            semantic_recall_at_12=statistics.mean(recalls) if recalls else None,
            nutrition_recall_at_20=statistics.mean(nutrition_recalls) if nutrition_recalls else None,
            distinct_nutrition_recall_at_20=statistics.mean(signature_recalls) if signature_recalls else None,
            nutrition_best_score_regret=statistics.mean(nutrition_regrets) if nutrition_regrets else None,
            quality_samples=len(qualities),plan_coverage=len(served)/len(qualities) if qualities else None,
            budget_compliance=sum(q['budget_ok'] for q in served)/len(served) if served else None,
            nutrition_fit_rate=sum(q['fit'] for q in qualities)/len(qualities) if qualities else None)
        if args.http_users:
            print(f'Measuring {args.http_users} authenticated weekly-plan users',flush=True)
            from benchmarks.http_load import run as http_run
            with patch('weekly_retrieval.query_cache.embed',cached_vectors):
                report['http_load']=await http_run(engine,sessions,args.http_users)
        report['passed']=not errors and report['candidate_max']<=60 and report['budget_compliance']==1 and not report.get('http_load',{}).get('errors')
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        await admin.dispose()
        Path(args.output).write_text(json.dumps(report,indent=2,default=str)+'\n')
    print(json.dumps(report,indent=2,default=str),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=2_000_000)
    parser.add_argument('--cities',type=int,default=1000)
    parser.add_argument('--concurrency',type=int,default=20)
    parser.add_argument('--requests',type=int,default=100)
    parser.add_argument('--quality-samples',type=int,default=12)
    parser.add_argument('--http-users',type=int,default=0)
    parser.add_argument('--output',default='benchmarks/results.json')
    args=parser.parse_args()
    if not (1<=args.concurrency<=100 and 1<=args.rows<=10_000_000 and args.requests>0 and args.cities>0 and 10000%args.cities==0 and args.http_users<=args.cities):
        parser.error('Invalid workload size')
    asyncio.run(run(args))
