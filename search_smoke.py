"""Live retrieval smoke evaluation. Uses existing catalog and paid query APIs."""
import asyncio
import json
from pathlib import Path
from time import perf_counter

async def run():
    import main
    import meal_search
    queries = [
        {'query': 'grilled chicken with vegetables', 'max_price_mkd': 400},
        {'query': 'пица со сирење', 'max_price_mkd': 400},
        {'query': 'vegetarian rice bowl', 'restaurant': 'Healthy Junk Food'},
        {'query': 'burger', 'max_price_mkd': 1},
    ]
    results = []
    try:
        for query in queries:
            started = perf_counter()
            async with main.async_session() as db:
                result = await meal_search.search(db, main.openai_client, meal_search.SearchRequest(**query, limit=5))
            for meal in result['meals']:
                assert meal['price_mkd'] <= query.get('max_price_mkd', float('inf'))
                assert not query.get('restaurant') or meal['restaurant_name'] == query['restaurant']
            by_id = {m['id']: m for m in result['meals']}
            for citation in result['recommendations']:
                assert citation['meal_id'] in by_id
                assert any(citation['quote'] in (by_id[citation['meal_id']].get(key) or '') for key in ['item_name', 'category', 'description'])
            row = {'query': query, 'seconds': round(perf_counter()-started, 3), 'retrieval': result['retrieval'],
                   'answer_status': result['answer_status'], 'matches': [{k: m[k] for k in ['id','item_name','restaurant_name','price_mkd']} for m in result['meals']],
                   'recommendations': result['recommendations']}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        Path('search_smoke_results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await main.engine.dispose()
        await main.openai_client.close()

if __name__ == '__main__':
    asyncio.run(run())
