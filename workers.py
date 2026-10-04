"""Run dedicated ingestion or planning processes: python workers.py all."""
import argparse
import asyncio
import logging
import signal


async def supervise(factory, *, name):
    """Recover a crashed loop without terminating unrelated worker roles."""
    delay=1
    while True:
        try:
            await factory()
            logging.warning('%s worker returned unexpectedly; restarting',name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('%s worker stopped; restarting in %s seconds',name,delay)
        await asyncio.sleep(delay)
        delay=min(30,delay*2)


async def run(kind, concurrency):
    import main
    import ingestion
    tasks=[]
    loop=asyncio.get_running_loop()
    root=asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM,root.cancel)
    def start(factory,name):
        tasks.append(asyncio.create_task(supervise(factory,name=name),name=name))
    async def cleanup():
        from sqlalchemy import text
        while True:
            async with main.async_session() as db:
                await db.execute(text('DELETE FROM query_embedding_cache WHERE expires_at<now()'))
                await db.execute(text('DELETE FROM auth_sessions WHERE expires_at<now()'))
                await db.execute(text("DELETE FROM auth_attempts WHERE window_start<now()-interval '1 day'"))
                await db.execute(text("DELETE FROM api_request_limits WHERE window_start<now()-interval '1 day'"))
                await db.execute(text("DELETE FROM catalog_jobs WHERE status IN ('succeeded','failed') AND finished_at<now()-interval '30 days'"))
                await db.commit()
            await asyncio.sleep(3600)
    start(cleanup,'cleanup')
    if kind == 'prices':
        async def refresh_existing_catalog():
            from scripts.refresh_menu_prices import run as refresh
            while True:
                await refresh(dispose_engine=False)
                await asyncio.sleep(3600)
        start(refresh_existing_catalog,'catalog-prices-and-availability')
    if kind in ('scrape','all'):
        async def schedule():
            while True:
                try:
                    await main.catalog.enqueue_due(main.async_session)
                except Exception:
                    logging.exception('Catalog scheduling failed')
                await asyncio.sleep(900)
        start(schedule,'catalog-schedule')
        for index in range(concurrency):
            start(lambda:main.catalog.worker(main.async_session,ingestion.scrape_restaurant),f'scrape-{index}')
    if kind in ('nutrition','all'):
        for index in range(concurrency):
            start(lambda:ingestion.worker(main.async_session,main.openai_client),f'nutrition-{index}')
    if kind in ('embedding','all'):
        start(lambda:main.meal_search.indexing_worker(main.async_session,main.openai_client),'embedding')
    if kind in ('plans','all'):
        for index in range(concurrency):
            start(lambda:main.plan_jobs.worker(main.async_session,main.generate_weekly_plan),f'plans-{index}')
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await main.openai_client.close()
        await main.engine.dispose()
        loop.remove_signal_handler(signal.SIGTERM)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['scrape','nutrition','embedding','plans','prices','all'])
    parser.add_argument('--concurrency',type=int,choices=range(1,9),default=2)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(args.kind,args.concurrency))
    except (KeyboardInterrupt,asyncio.CancelledError):
        pass
