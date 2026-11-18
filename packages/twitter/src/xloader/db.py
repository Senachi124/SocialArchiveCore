import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Bound per-process connections; each caller uses a separate transaction.
_connections = threading.BoundedSemaphore(4)

@contextmanager
def connection():
    schema = os.environ.get('XLOADER_SCHEMA', 'xloader')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema) or schema in ('public', 'pg_catalog', 'information_schema'): raise ValueError('Invalid XLoader schema')
    config_path = os.environ.get('XLOADER_DB_CONFIG')
    if not config_path or not Path(config_path).is_absolute():
        raise ValueError('XLOADER_DB_CONFIG must name an absolute credentials file')
    config = json.loads(Path(config_path).read_text())
    with _connections, psycopg.connect(**config['database'], autocommit=True, row_factory=dict_row,
                         options=f'-c search_path={schema},pg_catalog -c statement_timeout=30000') as conn:
        yield conn

def execute(sql, params=(), one=False):
    with connection() as c:
        cur = c.execute(sql, params)
        if cur.description:
            return cur.fetchone() if one else cur.fetchall()

def migrate():
    with connection() as c:
        c.execute(Path(__file__).with_name('schema.sql').read_text())

def create_job(spec, target_id=None, trigger='manual'):
    from .search import plan
    with connection() as c, c.transaction():
        if target_id is not None:
            c.execute('SELECT pg_advisory_xact_lock(89272,%s)', (target_id,))
            target = c.execute('SELECT archived FROM xl_targets WHERE id=%s', (target_id,)).fetchone()
            if not target or target['archived']: raise ValueError('此用戶已移出清單，請重新新增')
            existing = c.execute("SELECT * FROM xl_jobs WHERE target_id=%s AND status IN ('queued','running','paused') ORDER BY id LIMIT 1", (target_id,)).fetchone()
            if existing: return existing
        job = c.execute("INSERT INTO xl_jobs(spec,target_id,trigger) VALUES(%s,%s,%s) RETURNING *", (Jsonb(spec), target_id, trigger)).fetchone()
        for start, end, url in plan(spec):
            c.execute('INSERT INTO xl_slices(job_id,since_date,until_date,url) VALUES(%s,%s,%s,%s)', (job['id'], start, end, url))
        return job
