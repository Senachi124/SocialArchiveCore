import json
import shutil
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path
from . import db
from .archive import publish_post
from .common import CONTROL, DATA, now
from .process import run_child
from .search import split_slice, target_spec
from .diagnostics import failure_message
from . import jobs
from .storage import check_storage
from .events import event
from psycopg.types.json import Jsonb

class CaptureFailure(Exception): pass

STOP = False
def stopping(*_):
    global STOP
    STOP = True

def schedule():
    if CONTROL is None or not (CONTROL / 'cookies.txt').exists(): return
    for target in db.execute('SELECT * FROM xl_targets WHERE enabled AND NOT archived AND (next_run_at IS NULL OR next_run_at<=now()) ORDER BY id'):
        busy = db.execute("SELECT 1 FROM xl_jobs WHERE target_id=%s AND status IN ('queued','running','paused')", (target['id'],), one=True)
        if not busy:
            spec = target_spec(target, target['spec'].get('auto_mode', 'incremental'))
            db.create_job(spec, target['id'], 'scheduled')
        db.execute('UPDATE xl_targets SET next_run_at=%s WHERE id=%s', (now() + timedelta(hours=target['interval_hours']), target['id']))

def next_job():
    # Automatic capture is served first; IDs retain FIFO within each class.
    return db.execute("""SELECT * FROM xl_jobs WHERE status='queued'
      AND NOT cancel_requested AND NOT pause_requested
      ORDER BY trigger='scheduled' DESC,id LIMIT 1""", one=True)

def yield_to_scheduled(job):
    if job['trigger'] != 'manual': return False
    # Discover due work only between slices, never interrupt an in-flight file.
    # schedule() retains same-target queued/running/paused deduplication.
    schedule()
    pending = next_job()
    if not pending or pending['trigger'] != 'scheduled': return False
    # jobs.finish serializes against pause/cancel and preserves completed slices.
    jobs.finish(job['id'], 'queued', '自動工作優先；已保存分段進度，稍後繼續')
    return True

def consume(job):
    check_storage()
    if not jobs.claim(job['id']):
        time.sleep(3)
        return
    event('started', '開始／繼續擷取', job_id=job['id'])
    def check():
        if STOP or jobs.interrupted(job['id']): raise InterruptedError()
    try:
        if not (CONTROL / 'cookies.txt').exists(): raise PermissionError('Cookie required')
        while True:
            check()
            row = db.execute("SELECT * FROM xl_slices WHERE job_id=%s AND status='queued' ORDER BY until_date DESC NULLS FIRST,id LIMIT 1", (job['id'],), one=True)
            if not row: break
            if yield_to_scheduled(job): return
            check()
            check_storage()
            # Capture preserves local media independently of external backups.
            db.execute("UPDATE xl_slices SET status='running' WHERE id=%s", (row['id'],))
            event('slice', f'搜尋 {row["since_date"] or "單則貼文"} → {row["until_date"] or ""}', job_id=job['id'], slice_id=row['id'])
            last = [0.0]
            def cancelled():
                if STOP: return True
                return jobs.interrupted(job['id'])
            def tick():
                if time.monotonic() - last[0] < 2: return
                last[0] = time.monotonic()
                check_storage()
                base = DATA / 'staging' / str(job['id']) / str(row['id'])
                progress = base / 'progress.json'
                detail = json.loads(progress.read_text()) if progress.exists() else {}
                count = detail.get('seen', 0)
                db.execute('UPDATE xl_slices SET seen=%s WHERE id=%s', (count, row['id']))
                labels = {'alt_lookup':'圖片 ALT 補抓','search':'搜尋貼文','timeline':'補抓時間線／回覆','download':'下載媒體','downloaded':'媒體下載完成','references':'取得引用／回覆原文','publish':'驗證並保存貼文','profile':'擷取頭像','complete':'整理搜尋分段'}
                message = f'{labels.get(detail.get("phase"), "準備搜尋")}；已見 {count} 則'
                if detail.get('post_id'): message += f'；貼文 {detail["post_id"]}'
                if detail.get('file'): message += f'；檔案 {detail["file"]}'
                if detail.get('wait_until'):
                    message = f'X 限流冷卻至 {detail["wait_until"]}；之後自動續做；已見 {count} 則'
                db.execute("UPDATE xl_jobs SET heartbeat_at=now(),message=CASE WHEN cancel_requested OR pause_requested THEN message ELSE %s END,progress=%s WHERE id=%s", (message, Jsonb(detail), job['id']))
            module = 'xloader.alt_backfill' if job['spec'].get('alt_backfill') else 'xloader.capture'
            rc = run_child([sys.executable, '-m', module, str(row['id'])], cancel=cancelled, tick=tick)
            check()
            base = DATA / 'staging' / str(job['id']) / str(row['id'])
            if rc:
                error_file = base / 'error.json'
                detail = json.loads(error_file.read_text()) if error_file.exists() else {}
                raise CaptureFailure(failure_message(detail, rc))
            progress = base / 'progress.json'
            report = json.loads(progress.read_text()) if progress.exists() else {'seen': 0}
            seen = report['seen']
            for metadata in ([] if job['spec'].get('alt_backfill') else sorted(base.glob('*/record.json'))):
                check()
                publish_post(metadata.parent, job['id'])
            stopped = report.get('stopped_on_known', False)
            dense = seen >= 600 or report.get('capped', False)
            children = split_slice(job['spec']['query'], row['since_date'], row['until_date']) if dense and row['since_date'] and not stopped else []
            with db.connection() as c, c.transaction():
                for start, end, url in children:
                    c.execute('INSERT INTO xl_slices(job_id,since_date,until_date,url) VALUES(%s,%s,%s,%s)', (job['id'], start, end, url))
                warning = '單日結果密集，可能仍有搜尋缺口；請縮小搜尋條件' if dense and not children and not stopped else ''
                if job['spec'].get('alt_backfill'): warning = report.get('alt_warning', '')
                if report.get('missing_references'): warning += f' {len(report["missing_references"])} 則原貼文未取得（刪除、受限或限流）'
                c.execute('UPDATE xl_slices SET status=%s,seen=%s,warning=%s,finished_at=now() WHERE id=%s', ('split' if children else 'completed', seen, warning, row['id']))
                if stopped:
                    c.execute("UPDATE xl_slices SET status='skipped',finished_at=now() WHERE job_id=%s AND status='queued'", (job['id'],))
            if stopped:
                status = jobs.finish(job['id'], 'completed', '遇到已保存且檔案驗證通過的貼文；依設定停止增量抓取')
                if status == 'completed': record_success(job)
                return
        warnings = db.execute("SELECT 1 FROM xl_slices WHERE job_id=%s AND warning<>'' LIMIT 1", (job['id'],), one=True)
        message = '密集日期需要再檢查' if warnings else '分段搜尋完成；以 X 實際返回的結果為準'
        if job['spec'].get('alt_backfill'):
            message = '圖片 ALT 補抓完成；部分貼文無法驗證，請查看分段記錄' if warnings else '圖片 ALT 補抓完成；已保存來源回傳的描述，圖片原檔保持不變'
        status = jobs.finish(job['id'], 'partial' if warnings else 'completed', message)
        if status == 'completed': record_success(job)
    except InterruptedError:
        jobs.finish(job['id'], 'queued', '服務中斷，已保存分段進度')
    except Exception as error:
        message = str(error) if isinstance(error, CaptureFailure) else f'擷取未完成（{type(error).__name__}）；檢查 Cookie、X 限流及磁碟後可續做'
        jobs.finish(job['id'], 'failed', message)

def record_success(job):
    if job['spec'].get('alt_backfill'): return
    if not job['target_id'] or job['spec']['kind'] == 'post': return
    from datetime import datetime, timezone
    end = datetime.fromisoformat(job['spec']['until']).replace(tzinfo=timezone.utc)
    db.execute('UPDATE xl_targets SET last_success_at=GREATEST(last_success_at,%s) WHERE id=%s', (min(now(), end), job['target_id']))

def main():
    check_storage()
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    with db.connection() as lock:
        if not lock.execute('SELECT pg_try_advisory_lock(89272101) AS locked').fetchone()['locked']: return
        for row in db.execute("SELECT id FROM xl_jobs WHERE status='running'"):
            jobs.finish(row['id'], 'queued', '服務重新啟動，將續做未完成分段')
        db.execute("UPDATE xl_slices SET status='queued' WHERE status='running'")
        while not STOP:
            try:
                check_storage()
                schedule()
                job = next_job()
                if job: consume(job)
                else: time.sleep(3)
            except Exception as error:
                print('Capture worker retry: ' + type(error).__name__, flush=True)
                time.sleep(5)

if __name__ == '__main__': main()
