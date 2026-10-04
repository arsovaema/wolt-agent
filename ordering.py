"""Read-only, per-user Wolt handoff. Never calls Wolt order/payment APIs."""
from datetime import date
from urllib.parse import urlsplit, urlunsplit
import re
from fastapi import Depends, HTTPException, Query
from sqlalchemy import text


def restaurant_url(value):
    try:
        parsed=urlsplit(value or '')
        port=parsed.port
    except ValueError:
        raise HTTPException(409,'This restaurant does not have a supported Wolt link.')
    if (parsed.scheme!='https' or parsed.hostname!='wolt.com' or parsed.username or parsed.password
            or port not in (None,443) or not re.fullmatch(r'/[^/]+/[^/]+/[^/]+/(restaurant|venue)/[^/]+/?',parsed.path)):
        raise HTTPException(409,'This restaurant does not have a supported Wolt link.')
    return urlunsplit(('https','wolt.com',parsed.path.rstrip('/'),'',''))


def group_meals(meals, rows):
    records={row['id']:row for row in rows}
    groups={}
    if len(meals)!=3 or len({meal.get('id') for meal in meals})!=3:
        raise HTTPException(409,'This day must contain three distinct saved meals. Generate a new plan.')
    for meal in meals:
        item=records.get(meal.get('id'))
        if not item or not item['is_available'] or item['price_mkd']!=meal.get('price_mkd') or item['item_name']!=meal.get('item_name'):
            raise HTTPException(409,'A meal or price changed. Refresh your plan before ordering.')
        rid=item['restaurant_id']
        group=groups.setdefault(rid,{'restaurant_id':rid,'restaurant_name':item['restaurant_name'],
            'url':restaurant_url(item['url']),'meals':[],'food_subtotal_mkd':0})
        group['meals'].append({'menu_item_id':item['id'],'name':item['item_name'],
            'category':item['category'],'quantity':1,'price_mkd':item['price_mkd']})
        group['food_subtotal_mkd']+=item['price_mkd']
    return list(groups.values())


async def verify_live_menu(groups):
    """Read-only preflight before opening a helper tab; never trust old prices alone."""
    from wolt_scraper import scrape_wolt_menu
    for group in groups:
        try:
            items = await scrape_wolt_menu(group['url'])
        except Exception as exc:
            raise HTTPException(503, 'Wolt menu could not be checked. Try again shortly.') from exc
        if not items:
            raise HTTPException(503, 'Wolt menu could not be checked. Try again shortly.')
        available = {(item['item_name'], item['price_mkd']) for item in items}
        for meal in group['meals']:
            if (meal['name'], meal['price_mkd']) not in available:
                raise HTTPException(409, f"{meal['name']} is no longer listed at {meal['price_mkd']} MKD. Refresh the catalog and generate a new week before ordering.")


async def preview(db, day_number, planned_date, generation=None):
    import main
    import auth
    state=await db.get(main.WeeklyPlanState,auth.current_user_id())
    if state is None or (generation is not None and state.generation!=generation):
        raise HTTPException(409,'Your week changed. Reopen the day to review the current meals.')
    plan=await main.current_weekly_plan(db)
    if plan.get('status') not in ('ready','poor_fit'):
        raise HTTPException(409,plan.get('message','Generate a current weekly plan before ordering.'))
    day=next((day for day in plan.get('weekly_plan',[]) if day.get('day_number')==day_number and day.get('date')==planned_date.isoformat()),None)
    if day is None or day.get('status') not in ('ready','poor_fit'):
        raise HTTPException(409,'This day is no longer available. Reload the weekly plan.')
    ids=[meal.get('id') for meal in day.get('meals',[])]
    if not ids or any(not isinstance(mid,int) for mid in ids):
        raise HTTPException(409,'Generate a new week to link these meals to the catalog.')
    rows=(await db.execute(text('''SELECT m.id,m.restaurant_id,m.item_name,m.category,m.price_mkd,m.is_available,
        r.name AS restaurant_name,r.url FROM menu_items m JOIN restaurants r ON r.id=m.restaurant_id
        WHERE m.id=ANY(CAST(:ids AS integer[])) AND r.is_active'''),{'ids':ids})).mappings().all()
    groups=group_meals(day['meals'],rows)
    await verify_live_menu(groups)
    return {'day_number':day_number,'date':day['date'],'generation':state.generation,'restaurants':groups,
        'food_subtotal_mkd':sum(group['food_subtotal_mkd'] for group in groups),
        'estimated_total_mkd':day.get('budget',{}).get('estimated_total_mkd'),
        'separate_checkouts':len(groups)>1,'payment_automatic':False,
        'message':'Prepare the meals in Wolt, then review delivery details, fees and payment there. This does not schedule a future delivery.'}


def register_routes(app,get_db):
    @app.get('/api/agent/weekly-plan/{day_number}/order-preview')
    async def order_preview(day_number:int, planned_date:date, generation:str|None=Query(default=None,max_length=36),db=Depends(get_db)):
        if not 1<=day_number<=7:
            raise HTTPException(422,'Choose a day from 1 to 7.')
        return await preview(db,day_number,planned_date,generation)
