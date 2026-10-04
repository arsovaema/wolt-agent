"""One-time independent food-assessment backfill; no plan publication or nutrition changes."""
import asyncio
import json
import sys
from pathlib import Path
from sqlalchemy import select
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


async def run():
    try:
        async with main.async_session() as db:
            rows = (await db.execute(select(main.MenuItem).where(main.MenuItem.is_available))).scalars().all()
            candidates = [dict(main.menu_item_to_candidate(m),id=m.id) for m in rows]
            print(json.dumps({'available_items': len(candidates)}), flush=True)
            _,info = await main.food_evaluation.assess_candidates(db,main.openai_client,candidates)
            print(json.dumps(info), flush=True)
    finally:
        await main.openai_client.close()
        await main.engine.dispose()


if __name__ == '__main__':
    asyncio.run(run())
