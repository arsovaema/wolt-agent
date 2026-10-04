"""Shared, versioned embedding cache; caches vectors, never user plans or credentials."""
import hashlib
import json
from sqlalchemy import text


async def migrate(conn):
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS query_embedding_cache (
        key TEXT PRIMARY KEY, vectors JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL)'''))
    await conn.execute(text('CREATE INDEX IF NOT EXISTS query_cache_expiry ON query_embedding_cache(expires_at)'))
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS api_request_limits (
        user_id BIGINT NOT NULL, route TEXT NOT NULL, window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
        count INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id,route))'''))


async def embed(db, client, inputs):
    from meal_search import embed as provider_embed, VERSION
    key=hashlib.sha256(json.dumps([VERSION,inputs],ensure_ascii=False).encode()).hexdigest()
    row=(await db.execute(text('SELECT vectors FROM query_embedding_cache WHERE key=:key AND expires_at>now()'),{'key':key})).scalar()
    await db.rollback()
    if row is not None:
        return row
    vectors=await provider_embed(client,inputs)
    await db.execute(text("""INSERT INTO query_embedding_cache VALUES (:key,CAST(:vectors AS jsonb),now()+interval '24 hours')
        ON CONFLICT(key) DO UPDATE SET vectors=excluded.vectors,expires_at=excluded.expires_at"""),{'key':key,'vectors':json.dumps(vectors)})
    await db.commit()
    return vectors


async def throttle(db, uid, route):
    from fastapi import HTTPException
    count=(await db.execute(text('''INSERT INTO api_request_limits(user_id,route) VALUES (:uid,:route)
        ON CONFLICT(user_id,route) DO UPDATE SET
        count=CASE WHEN api_request_limits.window_start<now()-interval '1 minute' THEN 1 ELSE api_request_limits.count+1 END,
        window_start=CASE WHEN api_request_limits.window_start<now()-interval '1 minute' THEN now() ELSE api_request_limits.window_start END
        RETURNING count'''),{'uid':uid,'route':route})).scalar()
    await db.commit()
    if count>10:
        raise HTTPException(429,'Too many requests; try again in a minute')
