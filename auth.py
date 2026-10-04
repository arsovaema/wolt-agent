"""Opaque, database-backed sessions. Local mode is restricted to loopback clients."""
import asyncio
from contextvars import ContextVar
import hashlib
import hmac
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

user_context = ContextVar('user_id', default=1)


def current_user_id():
    return user_context.get()


def enabled():
    return os.getenv('WOLT_AUTH_REQUIRED', 'false').lower() == 'true'


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + result


def verify_password(password, stored):
    salt, _ = stored.split(':', 1)
    return hmac.compare_digest(password_hash(password, salt), stored)


def check_origin(request):
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
        parsed = urlsplit(origin)
        if parsed.netloc != request.headers.get('host') or parsed.scheme != request.url.scheme:
            raise HTTPException(403, 'Cross-origin writes are not allowed')


async def migrate(conn):
    if conn.dialect.name != 'postgresql':
        return
    await conn.execute(text('CREATE TABLE IF NOT EXISTS app_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())'))
    await conn.execute(text('SELECT pg_advisory_xact_lock(78149322)'))
    if (await conn.execute(text("SELECT 1 FROM app_migrations WHERE version='auth-v1'"))).scalar():
        return
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS accounts (
        id BIGSERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
    )'''))
    # Reserve ID 1 for the pre-existing local profile; registration never claims it.
    await conn.execute(text("SELECT setval(pg_get_serial_sequence('accounts','id'), GREATEST((SELECT COALESCE(max(id),1) FROM accounts), (SELECT COALESCE(max(id),1) FROM user_profiles)), true)"))
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash TEXT PRIMARY KEY, user_id BIGINT REFERENCES accounts(id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL
    )'''))
    await conn.execute(text('CREATE INDEX IF NOT EXISTS auth_session_expiry ON auth_sessions(expires_at)'))
    await conn.execute(text('''CREATE TABLE IF NOT EXISTS auth_attempts (
        ip TEXT PRIMARY KEY, window_start TIMESTAMPTZ NOT NULL DEFAULT now(), count INTEGER NOT NULL DEFAULT 1
    )'''))

    await conn.execute(text("INSERT INTO app_migrations(version) VALUES ('auth-v1')"))


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    password: str = Field(min_length=12, max_length=128)


async def throttle(db, ip):
    count = (await db.execute(text('''INSERT INTO auth_attempts(ip) VALUES (:ip)
        ON CONFLICT(ip) DO UPDATE SET
        count=CASE WHEN auth_attempts.window_start < now()-interval '1 minute' THEN 1 ELSE auth_attempts.count+1 END,
        window_start=CASE WHEN auth_attempts.window_start < now()-interval '1 minute' THEN now() ELSE auth_attempts.window_start END
        RETURNING count'''), {'ip':ip})).scalar()
    await db.commit()
    if count > 10:
        raise HTTPException(429, 'Too many attempts; try again in a minute')


def install(app, session_factory):
    @app.middleware('http')
    async def identity(request, call_next):
        if not request.url.path.startswith('/api/'):
            return await call_next(request)
        try:
            check_origin(request)
            if not enabled():
                host = request.client.host if request.client else ''
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host == 'testclient'
                hostname = request.url.hostname
                trusted_host = hostname in ('localhost','testserver','test')
                try:
                    trusted_host = trusted_host or ipaddress.ip_address(hostname).is_loopback
                except ValueError:
                    pass
                if not local or not trusted_host or request.headers.get('forwarded') or request.headers.get('x-forwarded-for'):
                    raise HTTPException(403, 'Enable authenticated mode before accessing this app remotely')
                uid = 1
            elif request.url.path.startswith('/api/auth/'):
                return await call_next(request)
            else:
                token = request.cookies.get('wolt_session', '')
                async with session_factory() as db:
                    uid = (await db.execute(text('SELECT user_id FROM auth_sessions WHERE token_hash=:token AND expires_at>now()'),
                            {'token':hashlib.sha256(token.encode()).hexdigest()})).scalar()
                if uid is None:
                    raise HTTPException(401, 'Sign in to continue')
            if request.method == 'POST' and not request.url.path.startswith('/api/auth/'):
                import query_cache
                async with session_factory() as db:
                    await query_cache.throttle(db, uid, request.url.path)
            context = user_context.set(uid)
            try:
                return await call_next(request)
            finally:
                user_context.reset(context)
        except HTTPException as exc:
            return JSONResponse({'detail':exc.detail},status_code=exc.status_code)

    async def establish_session(db, uid, request, response):
        token = secrets.token_urlsafe(32)
        await db.execute(text("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES (:token,:uid,now()+interval '7 days')"),
                         {'token':hashlib.sha256(token.encode()).hexdigest(),'uid':uid})
        await db.execute(text('DELETE FROM auth_sessions WHERE expires_at<now()'))
        await db.commit()
        response.set_cookie('wolt_session',token,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=604800)
        return {'user_id':uid}

    @app.get('/api/auth/config')
    async def config():
        return {'required':enabled()}

    @app.post('/api/auth/register')
    async def register(data: Credentials, request: Request, response: Response):
        if not enabled():
            raise HTTPException(409,'Accounts are disabled in local mode')
        async with session_factory() as db:
            await throttle(db,request.client.host)
            hashed = await asyncio.to_thread(password_hash,data.password)
            try:
                uid = (await db.execute(text('INSERT INTO accounts(username,password_hash) VALUES (:username,:password) RETURNING id'),
                      {'username':data.username.lower(),'password':hashed})).scalar()
                return await establish_session(db,uid,request,response)
            except IntegrityError:
                await db.rollback()
                raise HTTPException(409,'Username is unavailable')

    @app.post('/api/auth/login')
    async def login(data: Credentials, request: Request, response: Response):
        if not enabled():
            raise HTTPException(409,'Accounts are disabled in local mode')
        async with session_factory() as db:
            await throttle(db,request.client.host)
            account = (await db.execute(text('SELECT id,password_hash FROM accounts WHERE username=:username'),{'username':data.username.lower()})).mappings().first()
            # Perform a hash for missing accounts too, avoiding a cheap timing oracle.
            stored = account['password_hash'] if account else '00'*16 + ':' + '00'*64
            valid = await asyncio.to_thread(verify_password,data.password,stored)
            if not account or not valid:
                raise HTTPException(401,'Invalid username or password')
            return await establish_session(db,account['id'],request,response)

    @app.post('/api/auth/logout')
    async def logout(request: Request, response: Response):
        async with session_factory() as db:
            await db.execute(text('DELETE FROM auth_sessions WHERE token_hash=:token'),
                {'token':hashlib.sha256(request.cookies.get('wolt_session','').encode()).hexdigest()})
            await db.commit()
        response.delete_cookie('wolt_session')
        return {'status':'signed_out'}
