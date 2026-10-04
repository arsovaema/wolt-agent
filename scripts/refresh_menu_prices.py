"""Refresh existing catalog prices and retire removed dishes, preserving nutrition data."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
import main
from wolt_scraper import scrape_wolt_menu


async def run(*, dispose_engine=True):
    async with main.async_session() as db:
        restaurants = (await db.execute(text('SELECT id,name,url FROM restaurants WHERE is_active'))).mappings().all()
    changed = []
    for restaurant in restaurants:
        items = await scrape_wolt_menu(restaurant['url'])
        if not items:
            raise ValueError(f"No menu found for {restaurant['name']}")
        prices = {}
        for item in items:
            prices.setdefault(item['item_name'], set()).add(item['price_mkd'])
        async with main.async_session() as db:
            rows = (await db.execute(text('SELECT id,item_name,price_mkd FROM menu_items WHERE restaurant_id=:rid'), {'rid':restaurant['id']})).mappings().all()
            for row in rows:
                values = prices.get(row['item_name'], set())
                if not values:
                    await db.execute(text('UPDATE menu_items SET is_available=false WHERE id=:id'), {'id':row['id']})
                    await db.execute(text('UPDATE nutrition_jobs SET present=false WHERE menu_item_id=:id'), {'id':row['id']})
                    print(f"Unavailable: {restaurant['name']}: {row['item_name']}", flush=True)
                    continue
                if len(values) != 1:
                    continue
                price = next(iter(values))
                # A successful live observation refreshes freshness even when
                # the price is unchanged. Missing/ambiguous items stay stale.
                await db.execute(text("UPDATE menu_items SET last_seen_at=now() AT TIME ZONE 'UTC' WHERE id=:id"), {'id':row['id']})
                if price == row['price_mkd']:
                    continue
                await db.execute(text('UPDATE menu_items SET price_mkd=:price WHERE id=:id'), {'price':price,'id':row['id']})
                # Keep queued nutrition jobs from restoring an obsolete price.
                await db.execute(text("UPDATE nutrition_jobs SET payload=jsonb_set(payload,'{price_mkd}',CAST(:price AS jsonb)) WHERE menu_item_id=:id"), {'price':str(price),'id':row['id']})
                changed.append(row['id'])
                print(f"{restaurant['name']}: {row['item_name']}: {row['price_mkd']} -> {price} MKD", flush=True)
            await db.commit()
    print(f'Updated {len(changed)} catalog prices.', flush=True)
    if dispose_engine:
        await main.engine.dispose()

if __name__ == '__main__':
    asyncio.run(run())
