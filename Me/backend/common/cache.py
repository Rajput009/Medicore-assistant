import os, asyncio, json, time
import asyncpg
from typing import Optional, Dict, Any

DATABASE_URL = os.getenv('DATABASE_URL') or f"postgresql://{os.getenv('POSTGRES_USER','medicore')}:{os.getenv('POSTGRES_PASSWORD','medicore_pw')}@{os.getenv('POSTGRES_HOST','postgres')}:{os.getenv('POSTGRES_PORT','5432')}/{os.getenv('POSTGRES_DB','medicore')}"

_pool: Optional[asyncpg.pool.Pool] = None

async def init_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        # ensure table exists
        async with _pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS fhir_cache (
                    key TEXT PRIMARY KEY,
                    resource TEXT,
                    params JSONB,
                    response JSONB,
                    fetched_at TIMESTAMP WITH TIME ZONE
                );''')
    return _pool

def _make_key(resource: str, params: Dict[str, Any]) -> str:
    # deterministic key from resource + sorted params
    if not params:
        return f"{resource}::"
    items = sorted(params.items())
    return f"{resource}::" + "&".join([f"{k}={v}" for k,v in items])

async def get_cached(resource: str, params: Dict[str, Any], max_age_seconds: int = 300) -> Optional[Dict[str, Any]]:
    pool = await init_pool()
    key = _make_key(resource, params)
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT response, fetched_at FROM fhir_cache WHERE key=$1', key)
        if not row:
            return None
        fetched_at = row['fetched_at']
        if (time.time() - fetched_at.timestamp()) > max_age_seconds:
            return None
        return row['response']

async def set_cached(resource: str, params: Dict[str, Any], response: Dict[str, Any]):
    pool = await init_pool()
    key = _make_key(resource, params)
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO fhir_cache(key, resource, params, response, fetched_at)
            VALUES($1,$2,$3,$4,now())
            ON CONFLICT (key) DO UPDATE SET response = EXCLUDED.response, fetched_at = EXCLUDED.fetched_at;
        ''', key, resource, json.dumps(params), json.dumps(response))


async def invalidate_cache(resource: str, patient_id: str = None):
    pool = await init_pool()
    async with pool.acquire() as conn:
        if patient_id:
            await conn.execute("DELETE FROM fhir_cache WHERE resource=$1 AND params->>'patient'=$2", resource, patient_id)
        else:
            await conn.execute("DELETE FROM fhir_cache WHERE resource=$1", resource)
