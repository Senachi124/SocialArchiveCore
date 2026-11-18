"""Structured application events. Never accept upstream messages, URLs or cookies."""
import json
from psycopg.types.json import Jsonb
from . import db
from .common import now, HK

FIELDS = {'slice_id', 'post_id', 'file', 'bytes', 'total_bytes', 'elapsed_seconds',
          'attempt', 'account', 'endpoint', 'wait_until', 'seen', 'count', 'exception_type',
          'stage', 'category', 'http_status', 'retryable', 'resource_key',
          'errno', 'reason', 'operation', 'child_exit_code', 'diagnostic_present'}

def event(phase, message, *, job_id=None, **details):
    details = {key: value for key, value in details.items() if key in FIELDS}
    db.execute('INSERT INTO xl_events(job_id,phase,message,details) VALUES(%s,%s,%s,%s)',
               (job_id, phase, message, Jsonb(details)))
    print(json.dumps({'at': now().astimezone(HK).isoformat(), 'job': job_id,
                      'phase': phase, 'message': message, **details}, ensure_ascii=False), flush=True)
