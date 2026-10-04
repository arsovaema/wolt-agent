"""Restaurant catalog and durable refresh queue for small multi-worker deployments."""
import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

log = logging.getLogger(__name__)


def seed_restaurants():
    return json.loads(Path(__file__).with_name('restaurants.json').read_text())


async def migrate(conn):
    if conn.dialect.name != 'postgresql':
        return
    # Run once per migration version rather than backfilling every app startup.
    await conn.execute(text('CREATE TABLE IF NOT EXISTS app_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())'))
    await conn.execute(text('SELECT pg_advisory_xact_lock(78149322)'))
    if (await conn.execute(text("SELECT 1 FROM app_migrations WHERE version='catalog-v1'"))).scalar():
        return
    await conn.execute(text('''CREATE TABLE restaurants (
        id SERIAL PRIMARY KEY, name VARCHAR(150) NOT NULL, url TEXT UNIQUE NOT NULL,
        city VARCHAR(100) NOT NULL, is_active BOOLEAN NOT NULL DEFAULT true,
        last_success_at TIMESTAMPTZ, last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )'''))
    await conn.execute(text('CREATE INDEX restaurants_city_active ON restaurants (lower(city), id) WHERE is_active'))
    for restaurant in seed_restaurants():
        await conn.execute(text('INSERT INTO restaurants(name,url,city) VALUES (:name,:url,:city)'), restaurant)
    await conn.execute(text('ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS restaurant_id INTEGER REFERENCES restaurants(id)'))
    await conn.execute(text('ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS is_available BOOLEAN NOT NULL DEFAULT true'))
    await conn.execute(text('ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP'))
    await conn.execute(text("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS city VARCHAR(100) NOT NULL DEFAULT 'Skopje'"))
    await conn.execute(text('UPDATE menu_items m SET restaurant_id=r.id FROM restaurants r WHERE m.restaurant_name=r.name AND m.restaurant_id IS NULL'))
    await conn.execute(text('UPDATE menu_items SET last_seen_at=analyzed_at WHERE last_seen_at IS NULL'))
    await conn.execute(text('''UPDATE restaurants r SET last_success_at=(
        SELECT max(last_seen_at) AT TIME ZONE 'UTC' FROM menu_items m WHERE m.restaurant_id=r.id)'''))
    await conn.execute(text('CREATE INDEX menu_restaurant_available_price ON menu_items (restaurant_id, price_mkd, id) WHERE is_available'))
    await conn.execute(text('CREATE INDEX menu_available_price ON menu_items (price_mkd, id) WHERE is_available'))
    await conn.execute(text('CREATE INDEX menu_available_calories ON menu_items (estimated_calories, id) WHERE is_available'))
    await conn.execute(text('CREATE INDEX menu_available_protein ON menu_items (estimated_protein, id) WHERE is_available'))
    await conn.execute(text('''CREATE TABLE catalog_jobs (
        id BIGSERIAL PRIMARY KEY, restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_until TIMESTAMPTZ, token TEXT, error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ
    )'''))
    await conn.execute(text("CREATE UNIQUE INDEX catalog_one_active_job ON catalog_jobs(restaurant_id) WHERE status IN ('queued','running')"))
    await conn.execute(text("CREATE INDEX catalog_pending ON catalog_jobs(available_at,id) WHERE status IN ('queued','running')"))
    await conn.execute(text("INSERT INTO app_migrations(version) VALUES ('catalog-v1')"))


async def enqueue_due(session_factory, force=False):
    async with session_factory() as db:
        result = await db.execute(text('''INSERT INTO catalog_jobs(restaurant_id)
            SELECT r.id FROM restaurants r WHERE r.is_active
            AND (:force OR r.last_success_at IS NULL OR r.last_success_at < now()-interval '24 hours')
            AND NOT EXISTS (SELECT 1 FROM catalog_jobs j WHERE j.restaurant_id=r.id
                AND (j.status IN ('queued','running') OR j.created_at > now()-interval '24 hours'))
            ON CONFLICT DO NOTHING'''), {'force': force}) if not force else await db.execute(text('''
            INSERT INTO catalog_jobs(restaurant_id) SELECT id FROM restaurants WHERE is_active
            ON CONFLICT DO NOTHING'''))
        await db.commit()
        return result.rowcount


async def claim_job(session_factory):
    async with session_factory() as db:
        await db.execute(text("UPDATE catalog_jobs SET status='failed',error='retry_limit',finished_at=now() WHERE attempts>=3 AND (status='queued' OR (status='running' AND lease_until<now()))"))
        job = (await db.execute(text('''WITH next_job AS (
            SELECT j.id FROM catalog_jobs j JOIN restaurants r ON r.id=j.restaurant_id
            WHERE r.is_active AND j.attempts<3 AND ((j.status='queued' AND j.available_at<=now())
                OR (j.status='running' AND j.lease_until<now()))
            ORDER BY j.available_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1
        ) UPDATE catalog_jobs j SET status='running', attempts=attempts+1,
            token=:token,lease_until=now()+interval '20 minutes'
            FROM next_job n WHERE j.id=n.id RETURNING j.*'''), {'token':str(uuid4())})).mappings().first()
        await db.commit()
        return dict(job) if job else None


async def heartbeat(session_factory, job):
    while True:
        await asyncio.sleep(30)
        async with session_factory() as db:
            await db.execute(text("UPDATE catalog_jobs SET lease_until=now()+interval '20 minutes' WHERE id=:id AND token=:token AND status='running'"),job)
            await db.commit()


async def finish_job(session_factory, job, error=None):
    async with session_factory() as db:
        status = 'succeeded' if error is None else ('queued' if job['attempts'] < 3 else 'failed')
        result = await db.execute(text('''UPDATE catalog_jobs SET status=:status, error=:error,
            lease_until=NULL, available_at=now()+(:delay * interval '1 second'),
            finished_at=CASE WHEN :status IN ('succeeded','failed') THEN now() ELSE NULL END
            WHERE id=:id AND token=:token AND status='running' RETURNING restaurant_id'''),
            job | {'status':status, 'error':error, 'delay':min(3600,60*2**min(job['attempts'],6))})
        rid = result.scalar()
        if rid is not None:
            await db.execute(text('''UPDATE restaurants SET last_error=:error,
                last_success_at=CASE WHEN :success THEN now() ELSE last_success_at END WHERE id=:rid'''),
                {'error':error,'success':error is None,'rid':rid})
        await db.commit()


async def worker(session_factory, refresh):
    while True:
        job = None
        try:
            job = await claim_job(session_factory)
            if job is None:
                await asyncio.sleep(10)
                continue
            async with session_factory() as db:
                restaurant = (await db.execute(text('SELECT id,name,url,city FROM restaurants WHERE id=:id'), {'id':job['restaurant_id']})).mappings().one()
                restaurant = dict(restaurant)
            pulse = asyncio.create_task(heartbeat(session_factory,job))
            try:
                # Dedicated connection holds a per-restaurant lock. It also
                # prevents overlap with manual refreshes and expires on crash.
                async with session_factory() as lock_db:
                    locked = (await lock_db.execute(text('SELECT pg_try_advisory_xact_lock(78149,:rid)'), {'rid':restaurant['id']})).scalar()
                    if not locked:
                        raise RuntimeError('Restaurant refresh already running')
                    async with session_factory() as db:
                        await refresh(db, catalog_only=True, restaurants_override=[restaurant])
                await finish_job(session_factory,job)
            finally:
                pulse.cancel()
                try:
                    await pulse
                except asyncio.CancelledError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('Catalog refresh failed')
            if job:
                await finish_job(session_factory, job, type(exc).__name__)
            await asyncio.sleep(2)


async def import_restaurants(db, records):
    """Local operator import. Existing URLs retain their internal restaurant IDs."""
    from pydantic import BaseModel, Field, field_validator
    from urllib.parse import urlsplit

    class Restaurant(BaseModel):
        name: str = Field(min_length=1,max_length=150)
        url: str = Field(max_length=1000)
        city: str = Field(min_length=1,max_length=100)
        is_active: bool = True

        @field_validator('name','city')
        @classmethod
        def nonblank(cls,value):
            if not value.strip():
                raise ValueError('Name and city cannot be blank')
            return value.strip()

        @field_validator('url')
        @classmethod
        def wolt_url(cls,value):
            parsed=urlsplit(value)
            if parsed.scheme!='https' or parsed.hostname!='wolt.com' or parsed.username or parsed.password or parsed.port not in (None,443) or '/restaurant/' not in parsed.path:
                raise ValueError('Use an HTTPS wolt.com restaurant URL')
            return value.rstrip('/')

    entries=[Restaurant.model_validate(record) for record in records]
    ids=[]
    for entry in entries:
        rid=(await db.execute(text('''INSERT INTO restaurants(name,url,city,is_active)
            VALUES (:name,:url,:city,:is_active) ON CONFLICT(url) DO UPDATE SET name=excluded.name,
            city=excluded.city,is_active=excluded.is_active,
            service_verified_until=CASE WHEN restaurants.city<>excluded.city THEN NULL ELSE restaurants.service_verified_until END
            RETURNING id'''),entry.model_dump())).scalar()
        await db.execute(text('UPDATE menu_items SET restaurant_name=:name WHERE restaurant_id=:rid'),{'name':entry.name,'rid':rid})
        ids.append(rid)
    await db.commit()
    return ids
