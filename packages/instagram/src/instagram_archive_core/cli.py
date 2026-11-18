"""Headless commands: setup, enqueue, schedule and run a single worker."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import queue
import signal
import threading
from . import core


@contextmanager
def worker_lock():
    with (core.DATA_DIR/'worker.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Another worker is already running') from None
        yield


def run(once=False):
    with worker_lock():
        core.initialize(recover=True)
        stopped=threading.Event()
        def stop(signum, frame):
            stopped.set()
            # stop_process in run_job's finally preserves incomplete downloads.
            raise KeyboardInterrupt
        old={s:signal.signal(s,stop) for s in (signal.SIGINT,signal.SIGTERM)}
        try:
            while not stopped.is_set():
                core.guard_disk()
                core.schedule_due_targets()
                # SQL is durable and sees jobs enqueued by separate CLI processes.
                with core.connect() as db:
                    row=db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY CASE WHEN batch_id LIKE 'auto-%' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
                while True:
                    try:core.JOB_QUEUE.get_nowait();core.JOB_QUEUE.task_done()
                    except queue.Empty:break
                if row:core.run_job(row['id'])
                if once:return
                if not row:stopped.wait(5)
        except KeyboardInterrupt:
            return
        finally:
            for s,handler in old.items():signal.signal(s,handler)


def _main(argv=None):
    parser=argparse.ArgumentParser(description='Instagram archive and fetch core (headless)')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init')
    account=commands.add_parser('account')
    account.add_argument('--label',required=True)
    account.add_argument('--cookie-file',required=True)
    for name in ('target','fetch'):
        command=commands.add_parser(name)
        command.add_argument('username')
        command.add_argument('--account',type=int,required=True)
        command.add_argument('--includes',default='posts,reels,stories,avatar')
        command.add_argument('--full-check',action='store_true')
        if name=='target':
            command.add_argument('--interval-hours',type=int,default=24)
            command.add_argument('--disabled',action='store_true')
    worker=commands.add_parser('run')
    worker.add_argument('--once',action='store_true',help='Schedule due targets, run at most one job, then exit')
    cancel=commands.add_parser('cancel');cancel.add_argument('job_id',type=int)
    commands.add_parser('status')
    args=parser.parse_args(argv)
    os.umask(0o077)
    core.configure()
    if args.command=='run':return run(args.once)
    core.initialize()
    if args.command=='account':print(json.dumps({'account_id':core.register_account(args.label,args.cookie_file)}))
    elif args.command in ('target','fetch'):
        includes=args.includes.split(',')
        if args.command=='target':core.set_target(args.username,args.account,includes,args.interval_hours,not args.disabled,args.full_check)
        else:print(json.dumps({'job_ids':core.fetch(args.username,args.account,includes,args.full_check)}))
    elif args.command=='cancel':print(json.dumps(core.cancel_job(args.job_id)))
    elif args.command=='status':
        with core.connect() as db:
            print(json.dumps({'jobs':[dict(row) for row in db.execute('SELECT id,username,status,progress,added_count FROM jobs ORDER BY id DESC LIMIT 100')],
                'targets':[dict(row) for row in db.execute('SELECT username,auto_enabled,auto_next_run_at FROM targets')]},ensure_ascii=False))


def main(argv=None):
    try:return _main(argv)
    except Exception as error:
        import sys
        print('Instagram core stopped: '+type(error).__name__+'; check configuration and storage',file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
