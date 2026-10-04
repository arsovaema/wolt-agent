"""Durable per-user weekly generation jobs; browser polls the existing GET route."""
import asyncio
import logging
from uuid import uuid4
from sqlalchemy import text
from auth import user_context

log=logging.getLogger(__name__)


async def migrate(conn):
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS weekly_plan_jobs (
        user_id INTEGER PRIMARY KEY REFERENCES user_profiles(id) ON DELETE CASCADE,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        token TEXT, lease_until TIMESTAMPTZ, error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )'''))


async def enqueue(db,user_id):
    await db.execute(text('''INSERT INTO weekly_plan_jobs(user_id,status) VALUES (:uid,'queued')
        ON CONFLICT(user_id) DO UPDATE SET status='queued',attempts=0,error=NULL,updated_at=now()
        WHERE weekly_plan_jobs.status NOT IN ('queued','running')'''),{'uid':user_id})
    await db.commit()
    return {'status':'refreshing','message':'Building your seven-day plan. You can leave this page and return later.','weekly_plan':[]}


async def status(db,user_id):
    row=(await db.execute(text('SELECT status,error FROM weekly_plan_jobs WHERE user_id=:uid'),{'uid':user_id})).mappings().first()
    if row and row['status'] in ('queued','running'):
        return {'status':'refreshing','message':'Finding meals for your goal and building your week.','weekly_plan':[]}
    if row and row['status']=='failed':
        return {'status':'refresh_failed','message':'Plan generation was interrupted repeatedly. Please try again.','weekly_plan':[]}
    return None


async def claim(session_factory):
    async with session_factory() as db:
        # Failed processes relinquish jobs after the lease expires.
        await db.execute(text("UPDATE weekly_plan_jobs SET status='failed',error='retry_limit' WHERE attempts>=3 AND (status='queued' OR (status='running' AND lease_until<now()))"))
        row=(await db.execute(text('''WITH next_job AS (
            SELECT user_id FROM weekly_plan_jobs WHERE attempts<3
            AND (status='queued' OR (status='running' AND lease_until<now()))
            ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE weekly_plan_jobs j SET status='running',attempts=attempts+1,
            token=:token,lease_until=now()+interval '5 minutes',updated_at=now()
            FROM next_job n WHERE j.user_id=n.user_id RETURNING j.*'''),{'token':str(uuid4())})).mappings().first()
        await db.commit()
        return dict(row) if row else None


async def worker(session_factory,generate):
    while True:
        job=None
        try:
            job=await claim(session_factory)
            if not job:
                await asyncio.sleep(1)
                continue
            identity=user_context.set(job['user_id'])
            try:
                # Budgeted execution is shorter than its lease; no duplicate
                # lease takeover during a normally running generation.
                async with asyncio.timeout(180):
                    async with session_factory() as db:
                        result=await generate(db)
                state=('queued' if job['attempts']<3 else 'failed') if result['status'] in ('superseded','refresh_failed') else 'succeeded'
                async with session_factory() as db:
                    await db.execute(text('''UPDATE weekly_plan_jobs SET status=:state,lease_until=NULL,updated_at=now()
                        WHERE user_id=:user_id AND token=:token'''),job|{'state':state})
                    await db.commit()
            finally:
                user_context.reset(identity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('Weekly job failed')
            if job:
                async with session_factory() as db:
                    await db.execute(text('''UPDATE weekly_plan_jobs SET status=:status,lease_until=NULL,error=:error,updated_at=now()
                        WHERE user_id=:user_id AND token=:token'''),job|{'status':'queued' if job['attempts']<3 else 'failed','error':type(exc).__name__})
                    await db.commit()
            await asyncio.sleep(1)
