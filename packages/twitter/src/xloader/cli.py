"""Headless management commands; credentials are read only from configured files."""
import argparse
import json
import os
from pathlib import Path


def run(argv=None):
    parser = argparse.ArgumentParser(description='Twitter/X capture and automatic archive worker')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Initialize tables in the pre-created dedicated PostgreSQL schema')
    commands.add_parser('check', help='Validate configured media filesystem and reserve')
    commands.add_parser('worker', help='Run the single capture scheduler/worker')
    commands.add_parser('jobs', help='List recent job status without credentials')
    target = commands.add_parser('target', help='Add a capture target; automation is opt-in')
    target.add_argument('value', help='Account, HTTPS post URL or search query')
    target.add_argument('--kind', choices=['account', 'search', 'post'], default='account')
    target.add_argument('--since')
    target.add_argument('--until', help='Exclusive date, YYYY-MM-DD')
    target.add_argument('--label')
    target.add_argument('--interval-hours', type=int, default=6)
    target.add_argument('--enable', action='store_true')
    enqueue = commands.add_parser('enqueue', help='Queue a manual job for an existing target')
    enqueue.add_argument('target_id', type=int)
    enqueue.add_argument('--mode', choices=['incremental', 'full', 'search'], default='incremental')
    control = commands.add_parser('control', help='Pause, resume, retry or permanently cancel a job')
    control.add_argument('job_id', type=int)
    control.add_argument('action', choices=['pause', 'resume', 'retry', 'cancel'])
    enabled = commands.add_parser('automation', help='Enable or disable automatic capture for a target')
    enabled.add_argument('target_id', type=int)
    enabled.add_argument('state', choices=['enable', 'disable'])
    args = parser.parse_args(argv)
    # No configuration is needed merely to display --help.
    from . import db
    from .storage import check_storage
    from .search import normalize, target_spec
    from psycopg.types.json import Jsonb
    if args.command == 'check':
        check_storage(force=True)
        print('Storage identity and 5 GiB reserve verified')
    elif args.command == 'init':
        db.migrate()
        print('Capture schema initialized')
    elif args.command == 'worker':
        check_storage(force=True)
        os.umask(0o077)
        from .worker import main as worker
        worker()
    elif args.command == 'target':
        if not 1 <= args.interval_hours <= 720:
            parser.error('--interval-hours must be between 1 and 720')
        spec = normalize({'kind': args.kind, 'value': args.value,
                          'since': args.since, 'until': args.until,
                          'workflow_version': 2, 'auto_mode': 'incremental',
                          'stop_on_known': True})
        if spec['kind'] == 'post' and args.enable:
            parser.error('Single posts cannot be recurring targets')
        row = db.execute('INSERT INTO xl_targets(label,spec,enabled,interval_hours) VALUES(%s,%s,%s,%s) RETURNING id',
                         (args.label or args.value, Jsonb(spec), args.enable, args.interval_hours), one=True)
        print(json.dumps(row))
    elif args.command == 'enqueue':
        row = db.execute('SELECT * FROM xl_targets WHERE id=%s AND NOT archived', (args.target_id,), one=True)
        if not row:
            parser.error('Target not found')
        job = db.create_job(target_spec(row, args.mode), row['id'], 'manual')
        print(json.dumps({'id': job['id'], 'status': job['status']}))
    elif args.command == 'control':
        from .jobs import control as change
        change(args.job_id, args.action)
    elif args.command == 'automation':
        row = db.execute('UPDATE xl_targets SET enabled=%s,next_run_at=NULL WHERE id=%s AND NOT archived AND spec->>\'kind\'<>\'post\' RETURNING id',
                         (args.state == 'enable', args.target_id), one=True)
        if not row:
            parser.error('Recurring target not found')
        print(json.dumps(row))
    elif args.command == 'jobs':
        print(json.dumps(db.execute('SELECT id,target_id,trigger,status,added_posts,added_files,message,created_at FROM xl_jobs ORDER BY id DESC LIMIT 100'), default=str, ensure_ascii=False))


def main(argv=None):
    try:
        run(argv)
    except (Exception,) as error:
        # Never echo credential-bearing database/client exceptions.
        raise SystemExit(f'Command failed ({type(error).__name__}); check configuration and job events')


if __name__ == '__main__':
    main()
