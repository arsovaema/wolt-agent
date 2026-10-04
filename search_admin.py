"""Local maintenance CLI; no unauthenticated indexing endpoint."""
import argparse
import asyncio
import json

async def run(command, file=None):
    import main
    import meal_search
    from sqlalchemy import text
    try:
        async with main.engine.begin() as conn:
            await conn.run_sync(main.Base.metadata.create_all)
            await main.ensure_menu_item_columns(conn)
            await main.catalog.migrate(conn)
            await main.service_area.migrate(conn)
            await main.ingestion.migrate(conn)
            await main.auth.migrate(conn)
            await main.plan_jobs.migrate(conn)
            await meal_search.migrate(conn)
        if command == 'import-restaurants':
            from pathlib import Path
            async with main.async_session() as db:
                print({'restaurant_ids':await main.catalog.import_restaurants(db,json.loads(Path(file).read_text()))})
        if command == 'import-service':
            from pathlib import Path
            async with main.async_session() as db:
                print({'restaurants_updated':await main.service_area.import_service(db,json.loads(Path(file).read_text()))})
        if command == 'restaurants':
            async with main.async_session() as db:
                print(json.dumps([dict(r) for r in (await db.execute(text('SELECT * FROM restaurants ORDER BY id'))).mappings()],default=str))
        if command == 'retry-embeddings':
            async with main.async_session() as db:
                await db.execute(text('UPDATE meal_search_documents SET attempts=0,retry_at=now(),last_error=NULL WHERE attempts>=3'))
                await db.commit()
        if command == 'populate':
            print({'refresh_jobs_queued': await main.catalog.enqueue_due(main.async_session, force=True)})
        if command in {'populate', 'index'}:
            print({'indexed_this_run': await meal_search.index_pending(main.async_session, main.openai_client)})
        async with main.async_session() as db:
            result = (await db.execute(text('''SELECT count(*) AS total,
                count(*) FILTER (WHERE embedding IS NOT NULL AND embedding_version=:version) AS indexed,
                count(*) FILTER (WHERE attempts>=3) AS failed_embeddings
                FROM meal_search_documents'''), {'version': meal_search.VERSION})).mappings().one()
            print(json.dumps(dict(result)))
    finally:
        await main.engine.dispose()
        await main.openai_client.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'populate', 'index', 'status', 'restaurants', 'import-service', 'import-restaurants', 'retry-embeddings'])
    parser.add_argument('--file')
    args=parser.parse_args()
    if args.command in ('import-service','import-restaurants') and not args.file:
        parser.error('--file is required')
    asyncio.run(run(args.command,args.file))
