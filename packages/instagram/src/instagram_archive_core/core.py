"""Headless job/scheduler core extracted from SocialMedia_autoArchive."""
from __future__ import annotations
import time, hashlib, heapq, importlib.util, importlib.metadata, json, os, queue, re, sqlite3, subprocess, sys, threading, uuid
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import job_control, auto_recovery
from .instagram_policy import validate_rate_policy
from .public_time import hk_timestamp
from .gallery_pacing import speed_choice, gallery_options
from .storage import guard_disk, guard_identity
from .redaction import redact
ROOT=Path(__file__).resolve().parent
DATA_DIR=LIBRARY_DIR=RUNTIME_DIR=COOKIE_DIR=DB_PATH=DOWNLOAD_ARCHIVE=None
MEDIA_EXTENSIONS={'.jpg','.jpeg','.png','.webp','.gif','.mp4','.webm','.mov','.m4v'}
MEDIA_INCLUDES={'posts','reels','stories','highlights'}
DEFAULT_AUTO_INCLUDES=['posts','reels','stories','avatar']
RATE_LIMIT_COOLDOWN_MINUTES=15
WORK_LOCK=threading.Lock()
MUTATION_LOCK=threading.RLock()
class AutomaticFirstQueue(queue.Queue):
    """Automatic work first, FIFO within each class; items retain their public type."""

    def _init(self, maxsize):
        self.queue = []
        self.sequence = 0

    def put(self, item, block=True, timeout=None, *, automatic=False):
        super().put((0 if automatic else 1, item), block, timeout)

    def _put(self, entry):
        priority, item = entry
        heapq.heappush(self.queue, (priority, self.sequence, item))
        self.sequence += 1

    def _get(self):
        return heapq.heappop(self.queue)[2]

def enqueue_job(job_id, batch_id=''):
    if isinstance(JOB_QUEUE, AutomaticFirstQueue):
        JOB_QUEUE.put(job_id, automatic=str(batch_id or '').startswith('auto-'))
    else:
        JOB_QUEUE.put(job_id)

def wait_for_queued_job(timeout=30):
    with JOB_QUEUE.not_empty:
        return JOB_QUEUE.not_empty.wait_for(lambda: JOB_QUEUE._qsize() > 0, timeout)

def serialized(function):

    @wraps(function)
    def wrapped(*args, **kwargs):
        with MUTATION_LOCK:
            return function(*args, **kwargs)
    return wrapped

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connect():
    guard_identity()
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def initialize(recover=False):
    guard_disk()
    for directory in (DATA_DIR, LIBRARY_DIR, RUNTIME_DIR):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect() as db:
        targets_existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='targets'").fetchone() is not None
        db.executescript('\n        CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,label TEXT NOT NULL,browser TEXT NOT NULL,profile TEXT NOT NULL DEFAULT \'\',status TEXT NOT NULL DEFAULT \'configured\',last_used_at TEXT,created_at TEXT NOT NULL);\n        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL,account_id INTEGER NOT NULL,includes TEXT NOT NULL,captions INTEGER NOT NULL DEFAULT 1,max_rpm INTEGER NOT NULL,status TEXT NOT NULL DEFAULT \'queued\',progress INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT \'\',log_path TEXT NOT NULL DEFAULT \'\',batch_id TEXT NOT NULL DEFAULT \'\',created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,FOREIGN KEY(account_id) REFERENCES accounts(id));\n        CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL COLLATE NOCASE UNIQUE,numeric_id TEXT NOT NULL DEFAULT \'\',group_name TEXT NOT NULL DEFAULT \'未分組\',auto_enabled INTEGER NOT NULL DEFAULT 0,auto_account_id INTEGER,auto_interval_hours INTEGER NOT NULL DEFAULT 24,auto_includes TEXT NOT NULL DEFAULT \'["posts","reels","stories","avatar"]\',auto_captions INTEGER NOT NULL DEFAULT 1,auto_max_rpm INTEGER NOT NULL DEFAULT 12,auto_next_run_at TEXT,auto_last_run_at TEXT,auto_last_message TEXT NOT NULL DEFAULT \'\',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);\n        CREATE TABLE IF NOT EXISTS console_events(id INTEGER PRIMARY KEY AUTOINCREMENT,level TEXT NOT NULL,message TEXT NOT NULL,job_id INTEGER,account_id INTEGER,created_at TEXT NOT NULL);\n        CREATE TABLE IF NOT EXISTS safety_state(id INTEGER PRIMARY KEY CHECK(id=1),rate_limit_until TEXT);\n        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status,created_at DESC);\n        CREATE INDEX IF NOT EXISTS idx_accounts_created ON accounts(created_at DESC);\n        CREATE INDEX IF NOT EXISTS idx_console_created ON console_events(id DESC);\n        ')
        account_columns = {row['name'] for row in db.execute('PRAGMA table_info(accounts)')}
        migrations = {'cookie_file': "TEXT NOT NULL DEFAULT ''", 'cookie_source': "TEXT NOT NULL DEFAULT 'browser-live'", 'cookie_status': "TEXT NOT NULL DEFAULT 'not_saved'", 'cookie_count': 'INTEGER NOT NULL DEFAULT 0', 'cookie_names': "TEXT NOT NULL DEFAULT '[]'", 'session_hint': "TEXT NOT NULL DEFAULT ''", 'cookie_updated_at': 'TEXT', 'cookie_expires_at': 'TEXT', 'cookie_debug': "TEXT NOT NULL DEFAULT ''", 'rate_limit_until': 'TEXT'}
        for column, definition in migrations.items():
            if column not in account_columns:
                db.execute(f'ALTER TABLE accounts ADD COLUMN {column} {definition}')
        target_columns = {row['name'] for row in db.execute('PRAGMA table_info(targets)')}
        target_migrations = {'auto_gallery_speed': "TEXT NOT NULL DEFAULT 'conservative'", 'auto_rate_policy': "TEXT NOT NULL DEFAULT '{}'", 'auto_auth_mode': "TEXT NOT NULL DEFAULT 'authenticated'", 'auto_enabled': 'INTEGER NOT NULL DEFAULT 0', 'auto_account_id': 'INTEGER', 'auto_interval_hours': 'INTEGER NOT NULL DEFAULT 24', 'auto_includes': 'TEXT NOT NULL DEFAULT \'["posts","reels","stories","avatar"]\'', 'auto_captions': 'INTEGER NOT NULL DEFAULT 1', 'auto_max_rpm': 'INTEGER NOT NULL DEFAULT 12', 'auto_engine': "TEXT NOT NULL DEFAULT 'gallery-dl'", 'auto_full_archive': 'INTEGER NOT NULL DEFAULT 0', 'auto_full_check': 'INTEGER NOT NULL DEFAULT 0', 'auto_max_items': 'INTEGER NOT NULL DEFAULT 3', 'auto_next_run_at': 'TEXT', 'auto_last_run_at': 'TEXT', 'auto_last_message': "TEXT NOT NULL DEFAULT ''", 'avatar_initialized': 'INTEGER NOT NULL DEFAULT 0', 'avatar_last_saved_at': 'TEXT', 'avatar_last_message': "TEXT NOT NULL DEFAULT ''"}
        for column, definition in target_migrations.items():
            if column not in target_columns:
                db.execute(f'ALTER TABLE targets ADD COLUMN {column} {definition}')
        if 'auto_gallery_speed' not in target_columns:
            db.execute("UPDATE targets SET auto_gallery_speed='custom' WHERE auto_max_rpm<>12")
        job_columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
        job_migrations = {'gallery_speed': "TEXT NOT NULL DEFAULT 'legacy'", 'cancel_requested': 'INTEGER NOT NULL DEFAULT 0', 'rate_policy': "TEXT NOT NULL DEFAULT '{}'", 'auth_mode': "TEXT NOT NULL DEFAULT 'authenticated'", 'engine': "TEXT NOT NULL DEFAULT 'gallery-dl'", 'max_items': 'INTEGER NOT NULL DEFAULT 3', 'full_archive': 'INTEGER NOT NULL DEFAULT 0', 'full_check': 'INTEGER NOT NULL DEFAULT 0', 'refresh_discovery': 'INTEGER NOT NULL DEFAULT 0', 'result_json': "TEXT NOT NULL DEFAULT '{}'", 'target_id': 'INTEGER', 'avatar_requested': 'INTEGER NOT NULL DEFAULT 0', 'batch_id': "TEXT NOT NULL DEFAULT ''", 'added_count': 'INTEGER'}
        for column, definition in job_migrations.items():
            if column not in job_columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')
        if 'gallery_speed' not in job_columns:
            db.execute("UPDATE jobs SET gallery_speed=CASE WHEN max_rpm=12 THEN 'conservative' ELSE 'custom' END WHERE engine='gallery-dl' AND status='queued'")
        if 'added_count' not in job_columns:
            from instagram_archive_core.job_activity import historical_added_count
            for old_job in db.execute("SELECT id,log_path,message FROM jobs WHERE status NOT IN ('queued','running')").fetchall():
                count = historical_added_count(old_job, LIBRARY_DIR, RUNTIME_DIR, MEDIA_EXTENSIONS)
                if count is not None:
                    db.execute('UPDATE jobs SET added_count=? WHERE id=?', (count, old_job['id']))
        if 'auth_mode' not in job_columns:
            db.execute("UPDATE jobs SET status='cancelled',message='匿名優先更新：請確認存取方式後重新建立工作',finished_at=? WHERE status IN ('queued','running')", (now(),))
        if 'auto_auth_mode' not in target_columns:
            db.execute("UPDATE targets SET auto_enabled=0,auto_next_run_at=NULL,auto_last_message='匿名優先更新：完整備份請確認登入模式後重新啟用' WHERE auto_full_archive=1 AND auto_enabled=1")
        stamp = now()
        if not targets_existed:
            db.execute('INSERT OR IGNORE INTO targets(username,group_name,auto_includes,auto_captions,created_at,updated_at) SELECT username,\'未分組\',\'["stories"]\',1,?,? FROM jobs WHERE username NOT LIKE \'id:%\' GROUP BY username', (stamp, stamp))
        migrate_capture_settings(db)
        db.execute("UPDATE jobs SET status='failed',progress=0,message='瀏覽器工作已中斷；不自動重新開啟或擷取',finished_at=? WHERE engine='playwright' AND status IN ('queued','running')", (now(),))
        db.execute("UPDATE jobs SET status='cancelled',message='已中止；保留已保存媒體',finished_at=? WHERE cancel_requested=1 AND status IN ('queued','running')", (now(),))
        if recover:
            db.execute("UPDATE jobs SET status='queued',progress=0,message='服務重新啟動，已重新排隊' WHERE status='running' AND cancel_requested=0")
            for row in db.execute("SELECT id,batch_id FROM jobs WHERE status='queued' ORDER BY id"):
                enqueue_job(row['id'], row['batch_id'])
        db.execute('PRAGMA optimize')

def migrate_capture_settings(db):
    for row in db.execute('SELECT * FROM targets').fetchall():
        if not row['auto_enabled'] and (not row['auto_last_run_at']) and (not row['auto_last_message']) and (json.loads(row['auto_includes']) == ['stories']):
            db.execute('UPDATE targets SET auto_includes=? WHERE id=?', (json.dumps(DEFAULT_AUTO_INCLUDES), row['id']))
        if row['auto_engine'] != 'gallery-dl' or not row['auto_account_id'] or row['auto_account_id'] <= 0:
            if row['auto_enabled'] or row['auto_engine'] != 'gallery-dl':
                db.execute("UPDATE targets SET auto_enabled=0,auto_next_run_at=NULL,auto_last_message='已改為 gallery-dl 登入模式；請選擇登入並保存排程' WHERE id=?", (row['id'],))
        db.execute("UPDATE targets SET auto_engine='gallery-dl',auto_auth_mode='authenticated',auto_full_archive=0 WHERE id=?", (row['id'],))
    db.execute("UPDATE jobs SET status='cancelled',message='舊擷取方式已停用；請使用登入重新建立工作',finished_at=? WHERE status IN ('queued','running') AND (engine='instaloader' OR auth_mode!='authenticated' OR account_id<=0)", (now(),))

def gallery_dl_command():
    if not importlib.util.find_spec('gallery_dl'):
        return ([], None)
    try:
        result = subprocess.run([sys.executable, '-m', 'gallery_dl', '--version'], capture_output=True, text=True, timeout=10)
        return ([sys.executable, '-m', 'gallery_dl'], (result.stdout or result.stderr).strip() or None)
    except Exception:
        return ([sys.executable, '-m', 'gallery_dl'], None)

def access_parts(engine, includes, mode='authenticated'):
    if engine != 'gallery-dl':
        raise ValueError('此擷取引擎已停用；請使用 gallery-dl')
    if mode != 'authenticated':
        raise ValueError('手動 Fetch 與 Auto Archive 只支援使用登入；請更新設定')
    return [('authenticated', list(includes))]

def global_cooldown(db):
    values = [row[0] for row in db.execute('SELECT rate_limit_until FROM safety_state UNION ALL SELECT rate_limit_until FROM accounts') if cooldown_active(row[0])]
    path = DATA_DIR / 'instaloader-rate.sqlite3'
    if path.is_file():
        rate_db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            row = rate_db.execute('SELECT blocked_until FROM state WHERE id=1').fetchone()
            if row and row[0] > datetime.now(timezone.utc).timestamp():
                values.append(datetime.fromtimestamp(row[0], timezone.utc).isoformat())
        finally:
            rate_db.close()
    return max(values) if values else None

def validate_access(db, payload, includes, engine, check_safety=True):
    parts = access_parts(engine, includes, payload.get('authMode', 'authenticated'))
    if engine == 'instaloader' and (payload.get('fullArchive') or payload.get('fullCheck')) and any((mode == 'anonymous' for mode, _ in parts)):
        raise ValueError('匿名 Instaloader 目前只支援有限篇數；完整分頁接續備份需明確選擇「使用登入」')
    until = global_cooldown(db) if check_safety else None
    if until:
        raise RateLimitError(until)
    account_id = int(payload.get('accountId', 0) or 0)
    if any((mode == 'authenticated' for mode, _ in parts)):
        account = db.execute('SELECT * FROM accounts WHERE id=? AND id>0', (account_id,)).fetchone()
        if not account:
            raise ValueError('此操作需要登入；請選擇登入工作階段')
        if check_safety and account['status'] == 'auth_required':
            raise ValueError('請先確認 Instagram 登入／驗證並更新本機 Cookie')
        if engine in ('instaloader', 'playwright') and (not account['cookie_file'] or not Path(account['cookie_file']).is_file()):
            raise ValueError('請先在登入管理保存本機 Cookie 快照')
    else:
        account_id = anonymous_account(db)
    return (account_id, parts)

def create_capture_jobs(db, payload, username, includes, target, batch_id):
    engine = payload.get('engine', 'gallery-dl')
    speed, max_rpm = speed_choice(payload) if engine == 'gallery-dl' else ('legacy', max(1, min(12, int(payload.get('maxRpm', 12)))))
    policy = validate_rate_policy(payload.get('ratePolicy')) if engine == 'instaloader' else {}
    account_id, parts = validate_access(db, payload, includes, engine)
    ids = []
    for mode, kinds in parts:
        identity = anonymous_account(db) if mode == 'anonymous' else account_id
        cursor = db.execute('INSERT INTO jobs(username,account_id,includes,captions,max_rpm,message,created_at,\n            target_id,avatar_requested,batch_id,engine,max_items,full_archive,refresh_discovery,auth_mode)\n            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (username, identity, json.dumps(kinds), int(bool(payload.get('captions', True))), max_rpm, f"等待執行 · {('匿名；不會自動登入重試' if mode == 'anonymous' else '使用登入')}", now(), target['id'] if target else None, int('avatar' in kinds), batch_id, engine, int(payload.get('maxItems', 3)), int(bool(payload.get('fullArchive'))), int(bool(payload.get('refreshDiscovery'))), mode))
        ids.append(cursor.lastrowid)
        db.execute('UPDATE jobs SET gallery_speed=? WHERE id=?', (speed, cursor.lastrowid))
        db.execute('UPDATE jobs SET rate_policy=?,added_count=0 WHERE id=?', (json.dumps(policy), cursor.lastrowid))
        db.execute('UPDATE jobs SET full_check=? WHERE id=?', (int(bool(payload.get('fullCheck'))), cursor.lastrowid))
    return ids

def auto_success_times(db):
    """Only wholly completed automatic batches count, including historical jobs."""
    rows = db.execute("SELECT target_id, MAX(completed_at) AS completed_at FROM (\n        SELECT target_id,batch_id,MAX(finished_at) AS completed_at FROM jobs\n        WHERE target_id IS NOT NULL AND batch_id LIKE 'auto-%'\n        GROUP BY target_id,batch_id\n        HAVING SUM(CASE WHEN status='completed' AND finished_at IS NOT NULL THEN 0 ELSE 1 END)=0\n    ) successful_batches GROUP BY target_id")
    return {row['target_id']: row['completed_at'] for row in rows}

def new_batch_id(prefix='batch'):
    return f'{prefix}-{uuid.uuid4().hex}'

def console_event(level, message, job_id=None, account_id=None):
    with connect() as db:
        db.execute('INSERT INTO console_events(level,message,job_id,account_id,created_at) VALUES(?,?,?,?,?)', (level, redact(message)[-1200:], job_id, account_id, now()))
        db.execute('DELETE FROM console_events WHERE id NOT IN (SELECT id FROM console_events ORDER BY id DESC LIMIT 1000)')

def cooldown_active(value):
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False

def selected_media(values):
    result = []
    for value in values if isinstance(values, list) else ():
        if value in MEDIA_INCLUDES and value not in result:
            result.append(value)
    return result

def saved_media_paths(lines):
    root = LIBRARY_DIR.resolve()
    result = []
    for line in lines:
        try:
            candidate = Path(line.strip()).resolve()
            candidate.relative_to(root)
            if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTENSIONS and (candidate not in result):
                result.append(candidate)
        except (OSError, ValueError):
            continue
    return result

def gallery_dl_failure(lines, code, engine='gallery-dl'):
    return next((line for line in reversed(lines) if '[error]' in line.lower()), f'{engine} 結束碼 {code}')

def includes_for_target(values, target, avatar=False):
    includes = selected_media(values)
    wants_avatar = bool(avatar or (isinstance(values, list) and 'avatar' in values) or (target is not None and (not target['avatar_initialized'])))
    if wants_avatar:
        includes.append('avatar')
    return (includes, wants_avatar)

class RateLimitError(ValueError):

    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f'Instagram 429 冷卻中，請等待至 {retry_at} 後再建立工作')

def build_config(job, account):
    anonymous = dict(job).get('auth_mode') == 'anonymous'
    cookie_file = Path(account['cookie_file']) if not anonymous and account['cookie_file'] else None
    if not cookie_file or not cookie_file.is_file():
        raise ValueError('An explicit existing Cookie file is required')
    cookie_source = str(cookie_file)
    config = {**gallery_options(job), 'extractor': {'base-directory': str(LIBRARY_DIR), 'archive': str(DOWNLOAD_ARCHIVE), 'directory': ['instagram', '{username}', '{subcategory}'], 'postprocessors': [{'name': 'metadata', 'event': 'file', 'extension-format': '{extension}.json'}] if job['captions'] else [], 'instagram': {'api': 'rest', 'cookies': cookie_source, 'include': json.loads(job['includes']), 'user-cache': 'memory', 'videos': True, 'previews': False, 'metadata': True, 'user-agent': '+chrome', 'highlights': {'directory': ['instagram', '{username}', 'highlights', '{highlight_title}']}, 'avatar': {'directory': ['instagram', '{username}', 'avatar'], 'filename': '{media_id}.{extension}'}}}, 'output': {'progress': False, 'shorten': 'eaw'}}
    if anonymous:
        config['extractor']['instagram'].update({'username': None, 'password': None, 'netrc': False, 'cookies-update': False})
    config['extractor']['archive-event'] = ['file', 'skip']
    config['extractor']['instagram']['archive-full-check'] = bool(job['full_check'])
    config['extractor']['instagram']['archive-target'] = job['username']
    config['extractor']['directory'] = ['instagram', '{archive_target}', '{subcategory}']
    config['extractor']['instagram']['highlights']['directory'] = ['instagram', '{archive_target}', 'highlights', '{highlight_title}']
    config['extractor']['instagram']['avatar']['directory'] = ['instagram', '{archive_target}', 'avatar']
    config['cache'] = {'file': str(DATA_DIR/'gallery-cache.sqlite3')}
    config['extractor']['instagram']['cookies-update'] = False
    path = RUNTIME_DIR / f"job-{job['id']}.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    return path

def gallery_dl_environment():
    env = os.environ.copy()
    blocked_proxy_names = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'}
    for key in list(env):
        if key.upper() in blocked_proxy_names:
            env.pop(key, None)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    return env

def cancellation_requested(job_id):
    with connect() as db:
        row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
    return bool(row and row['cancel_requested'])

@serialized
def cancel_job(job_id):
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('工作不存在')
        if row['status'] == 'queued':
            db.execute("UPDATE jobs SET cancel_requested=1,status='cancelled',message='已取消排隊；保留既有資料',finished_at=? WHERE id=?", (now(), job_id))
        elif row['status'] == 'running':
            db.execute("UPDATE jobs SET cancel_requested=1,message='正在中止下載；保留已保存媒體' WHERE id=?", (job_id,))
        elif auto_recovery.recovery(row).get('pending'):
            state = auto_recovery.recovery(row)
            state.update(pending=False, stopped_reason='cancelled_by_user')
            auto_recovery.write(db, row, state)
            db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
        else:
            return {'ok': True, 'status': row['status']}
    console_event('info', f'工作 #{job_id} 已要求中止；既有資料保留', job_id)
    return {'ok': True, 'status': 'cancelled' if row['status'] == 'queued' else 'cancelling'}

def finish_cancellation(job_id):
    with connect() as db:
        db.execute("UPDATE jobs SET status='cancelled',message='已中止；已保存媒體及 DB 紀錄保留',finished_at=? WHERE id=?", (now(), job_id))
    console_event('info', f'工作 #{job_id} 已中止；媒體、去重紀錄保留', job_id)

def account_result_ready(db, account, finished):
    """An old worker must not reset a newer Cookie or safety decision.

    Call while holding MUTATION_LOCK, in the job-result transaction. The saved
    Cookie timestamp is the revision used by the existing import API; include
    its path as well so replacing the credential source also invalidates it.
    """
    return db.execute("UPDATE accounts SET last_used_at=?,status='ready',rate_limit_until=NULL WHERE id=? AND COALESCE(cookie_file,'')=COALESCE(?,'') AND COALESCE(cookie_updated_at,'')=COALESCE(?,'') AND status=? AND COALESCE(rate_limit_until,'')=COALESCE(?,'')", (finished, account['id'], account['cookie_file'], account['cookie_updated_at'], account['status'], account['rate_limit_until'])).rowcount == 1

def account_result_auth_required(db, account, message, finished):
    """Pause only the credential revision that the failed child used."""
    changed = db.execute("UPDATE accounts SET status='auth_required' WHERE id=? AND COALESCE(cookie_file,'')=COALESCE(?,'') AND COALESCE(cookie_updated_at,'')=COALESCE(?,'')", (account['id'], account['cookie_file'], account['cookie_updated_at'])).rowcount == 1
    if changed:
        db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE account_id=? AND status='queued'", (message, finished, account['id']))
    return changed

def run_job(job_id):
    with connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['status'] != 'queued':
            return
        if job['engine'] != 'gallery-dl' or job['auth_mode'] != 'authenticated' or job['account_id'] <= 0:
            db.execute("UPDATE jobs SET status='cancelled',message='登入模式更新：舊擷取方式已停用，請重新建立工作',finished_at=? WHERE id=?", (now(), job_id))
            return
        account = db.execute('SELECT * FROM accounts WHERE id=?', (job['account_id'],)).fetchone()
        if not account:
            db.execute("UPDATE jobs SET status='failed',message='登入工作階段不存在',finished_at=? WHERE id=?", (now(), job_id))
            return
        until = global_cooldown(db)
        if until:
            message = f'429 保護中；匿名與登入工作均停止，請等待至 {until}'
            db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (message, now(), job_id))
            auto_recovery.defer(db, job, until)
            db.commit()
            console_event('warning', message, job_id, account['id'])
            return
        if account['status'] == 'auth_required':
            message = '登入已暫停；請先確認 Instagram 登入／驗證並更新本機 Cookie'
            db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (message, now(), job_id))
            db.commit()
            console_event('warning', message, job_id, account['id'])
            return
        job = auto_recovery.prepare(db, job, now())
        claimed = db.execute("UPDATE jobs SET status='running',progress=8,message=?,started_at=? WHERE id=? AND status='queued' AND cancel_requested=0", (f"準備 {job['engine']}…", now(), job_id))
        if not claimed.rowcount:
            return
    engine = job['engine']
    anonymous = job['auth_mode'] == 'anonymous'
    console_event('info', f"工作 #{job_id} 開始 [{engine}] [{('匿名' if anonymous else '使用登入')}]：{job['username']} · {', '.join(json.loads(job['includes']))}", job_id, account['id'])
    try:
        guard_disk()
        if engine == 'gallery-dl':
            command, _ = gallery_dl_command()
            if not command:
                raise ValueError('找不到 gallery-dl，請安裝 requirements.txt 依賴')
            config_path = build_config(job, account)
            pacing = gallery_options(job)
            console_event('info', f"gallery-dl 速度：{('保守（預設）' if job['gallery_speed'] == 'conservative' else '自訂')} · 下載等待 {pacing['sleep']} 秒 · 請求間隔 {pacing['sleep-request']} 秒 · 重試 exp=30 · 429 exp=60（全服務至少 15 分鐘冷卻優先）", job_id, account['id'])
            args = [sys.executable, '-m', 'instagram_archive_core.gallery_worker'] + ['--config-ignore', '--config', str(config_path), '--download-archive', str(DOWNLOAD_ARCHIVE), f"https://www.instagram.com/{job['username']}/"]
        else:
            raise ValueError('不支援的擷取引擎')
    except (ValueError, RuntimeError) as error:
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (str(error), now(), job_id))
        console_event('error', str(error), job_id, account['id'])
        return
    log_path = RUNTIME_DIR / f'job-{job_id}.log'
    auth_message = '載入本機 Cookie 快照並連線…' if account['cookie_file'] and Path(account['cookie_file']).is_file() else '讀取瀏覽器 Cookie 並連線…'
    if anonymous:
        auth_message = '匿名連線；不讀取 Cookie 快照或瀏覽器登入資料，不自動切換登入'
    with connect() as db:
        db.execute('UPDATE jobs SET progress=20,message=?,log_path=?,added_count=0 WHERE id=?', (auth_message, str(log_path), job_id))
    process = None
    cancellation_monitor = None
    try:
        with MUTATION_LOCK:
            if cancellation_requested(job_id):
                finish_cancellation(job_id)
                return
            process = subprocess.Popen(args, cwd=DATA_DIR, env=auto_recovery.environment(job, gallery_dl_environment()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', **job_control.process_options())
        storage_failed = threading.Event()
        last_storage_check = [0.0]
        def should_stop():
            if time.monotonic() - last_storage_check[0] >= 5:
                last_storage_check[0] = time.monotonic()
                try: guard_disk()
                except Exception: storage_failed.set()
            return storage_failed.is_set() or cancellation_requested(job_id)
        cancellation_monitor = job_control.monitor(process, should_stop)
        lines = []
        rate_limited = False
        saved_paths = set()
        auth_required = False
        extraction_errors = []
        with log_path.open('w', encoding='utf-8') as log:
            for line in process.stdout:
                clean = redact(line.rstrip())
                log.write(f'{now()} {clean}\n')
                log.flush()
                lines = (lines + [clean])[-30:]
                if engine == 'gallery-dl' and '[error]' in clean.lower():
                    extraction_errors = (extraction_errors + [clean])[-10:]
                saved_paths.update(saved_media_paths([clean]))
                if '[error] AuthRequired:' in clean or (engine != 'instaloader' and 'feedback_required' in clean.lower()):
                    auth_required = True
                if clean:
                    console_event('error' if '[error]' in clean.lower() else 'warning' if '[warning]' in clean.lower() else 'info', clean, job_id, account['id'])
                    with connect() as db:
                        db.execute('UPDATE jobs SET progress=55,message=?,added_count=? WHERE id=?', (clean[-220:], len(saved_paths), job_id))
                if re.search('(?:HttpError[^\\n]*(?:429|Too Many Requests)|\\b429\\s+Too Many Requests\\b|status(?: code)?[^\\n]*\\b429\\b)', clean, re.IGNORECASE):
                    rate_limited = True
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    break
                if auth_required:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    break
        if process.stdout:
            process.stdout.close()
        if not auth_required and (not cancellation_requested(job_id)):
            with connect() as db:
                rate_limited = rate_limited or bool(global_cooldown(db))
        if rate_limited:
            if cancellation_requested(job_id):
                finish_cancellation(job_id)
                return
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=RATE_LIMIT_COOLDOWN_MINUTES)).isoformat()
            with connect() as db:
                retry_at = auto_recovery.retry_deadline(job, max(retry_at, global_cooldown(db) or retry_at))
                message = f'偵測到 Instagram 429，匿名與登入工作均已強制停止；至少 {RATE_LIMIT_COOLDOWN_MINUTES} 分鐘內禁止重試（至 {retry_at}）'
                db.execute('INSERT INTO safety_state(id,rate_limit_until) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET rate_limit_until=MAX(rate_limit_until,excluded.rate_limit_until)', (retry_at,))
                db.execute("UPDATE accounts SET rate_limit_until=?,status='rate_limited' WHERE id=?", (retry_at, account['id']))
                db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (message, now(), job_id))
                for interrupted in db.execute("SELECT * FROM jobs WHERE id=? OR status='queued'", (job_id,)).fetchall():
                    auto_recovery.defer(db, interrupted, retry_at)
                db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE status='queued'", (message, now()))
            console_event('critical', message, job_id, account['id'])
            return
        try:
            code = process.wait(timeout=5 if auth_required else None)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        if storage_failed.is_set():
            with connect() as db:
                db.execute("UPDATE jobs SET status='queued',message='Media storage unavailable; partial files preserved' WHERE id=?",(job_id,))
            return
        if extraction_errors:
            code = code or 1
            lines.extend(extraction_errors)
        backup = {}
        if engine == 'instaloader':
            try:
                backup = json.loads((RUNTIME_DIR / f'job-{job_id}-result.json').read_text(encoding='utf-8'))
                if backup.get('job_id') != job_id:
                    backup = {}
            except (OSError, ValueError):
                pass
            if code == 0 and job['full_archive'] and (not (backup.get('requested_backup_complete') or backup.get('incremental_complete'))):
                code = 3
                lines.append('[instaloader][error] 尚無完整媒體核對結果，不能標記完整備份')
        with MUTATION_LOCK, connect() as db:
            if cancellation_requested(job_id):
                finish_cancellation(job_id)
                return
            if backup:
                db.execute('UPDATE jobs SET result_json=? WHERE id=?', (json.dumps(backup, ensure_ascii=False), job_id))
            if code == 3 and engine == 'playwright':
                db.execute("UPDATE jobs SET status='cancelled',progress=0,message='瀏覽器已關閉／等待逾時；未執行保存',finished_at=? WHERE id=?", (now(), job_id))
                db.commit()
                console_event('info', f'工作 #{job_id} 已取消，未執行保存', job_id, account['id'])
            elif code == 0:
                finished = now()
                completed_message = f"Instaloader 本次範圍完成（每類最多 {job['max_items']} 篇）；新增 {len(saved_paths)} 個媒體，並非完整封存" if engine == 'instaloader' else '擷取完成；重複項目已由 archive 跳過'
                if engine == 'instaloader' and backup:
                    complete = '所選範圍備份完成' if backup.get('requested_backup_complete') else '批次完成，並非完整封存'
                    completed_message = f"{complete}；清單 {('已完整' if backup.get('requested_discovery_complete') else '待接續')}，合併 {backup['unique_posts']} 篇／{backup['expected_media']} 個媒體，已核對 {backup['verified_unique_posts']} 篇；新增 {len(saved_paths)} 個媒體"
                if engine == 'instaloader' and anonymous:
                    completed_message = f"匿名有限篇數完成；每類最多 {job['max_items']} 篇，新增 {len(saved_paths)} 個媒體；非完整封存，不保存分頁游標"
                if backup.get('incremental_complete') and (not job['full_check']):
                    completed_message = f'增量檢查完成；遇到已知貼文或清單結尾；新增 {len(saved_paths)} 個媒體'
                if engine == 'playwright':
                    completed_message = f'瀏覽器連續保存完成；新增 {len(saved_paths)} 個媒體，已存在項目跳過；非完整帳號封存'
                db.execute("UPDATE jobs SET status='completed',progress=100,message=?,finished_at=? WHERE id=?", (completed_message, finished, job_id))
                account_result_ready(db, account, finished)
                if job['target_id'] and job['avatar_requested']:
                    db.execute("UPDATE targets SET avatar_initialized=1,avatar_last_saved_at=?,avatar_last_message='頭像已檢查；新版本已保存，重複版本由 archive 跳過',updated_at=? WHERE id=?", (finished, finished, job['target_id']))
                db.commit()
                console_event('success', f'工作 #{job_id} 擷取完成', job_id, account['id'])
            else:
                failure = gallery_dl_failure(lines, code, engine)
                finished = now()
                if anonymous:
                    failure = '匿名存取失敗（未使用登入資料，未切換登入）；' + failure
                if saved_paths or (engine == 'instaloader' and backup.get('verified_unique_posts', 0)):
                    message = f'部分完成：已保存 {len(saved_paths)} 個媒體；{failure}'
                    db.execute("UPDATE jobs SET status='partial',progress=100,message=?,finished_at=? WHERE id=?", (message[-500:], finished, job_id))
                    db.execute("UPDATE accounts SET last_used_at=?,status='ready' WHERE id=?", (finished, account['id']))
                    if job['target_id'] and job['avatar_requested'] and any(('avatar' in path.relative_to(LIBRARY_DIR.resolve()).parts for path in saved_paths)):
                        db.execute("UPDATE targets SET avatar_initialized=1,avatar_last_saved_at=?,avatar_last_message='頭像已保存；其他內容有部分錯誤',updated_at=? WHERE id=?", (finished, finished, job['target_id']))
                    db.commit()
                    console_event('warning', f'工作 #{job_id} {message}', job_id, account['id'])
                else:
                    db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (failure[-500:], finished, job_id))
                    db.commit()
                    console_event('error', f'工作 #{job_id} 失敗：{failure}', job_id, account['id'])
        if auth_required and anonymous:
            console_event('warning', '匿名存取被拒絕；未載入登入資料、未切換引擎。請勿連續重試；需要登入時由你手動選擇。', job_id, account['id'])
        if auth_required and (not anonymous):
            message = 'Instagram 限制（feedback_required）或登入需要驗證；已暫停此登入所有引擎的排隊工作，請先在 Instagram 處理，再更新本機 Cookie'
            with MUTATION_LOCK, connect() as db:
                changed = account_result_auth_required(db, account, message, now())
            console_event('warning', message if changed else '舊 Cookie 工作失敗；登入已更新，保留目前登入狀態與排隊工作', job_id, account['id'])
    except Exception as error:
        if cancellation_requested(job_id):
            finish_cancellation(job_id)
            return
        with connect() as db:
            db.execute("UPDATE jobs SET status='failed',progress=0,message=?,finished_at=? WHERE id=?", (type(error).__name__, now(), job_id))
        console_event('error', f'工作 #{job_id} 發生例外：{type(error).__name__}', job_id, account['id'])
    finally:
        if cancellation_monitor:
            done, thread = cancellation_monitor
            done.set()
            thread.join(timeout=27)
        if process is not None and process.poll() is None:
            job_control.stop_process(process)

def worker():
    while True:
        if not wait_for_queued_job(timeout=30):
            continue
        with WORK_LOCK:
            try:
                job_id = JOB_QUEUE.get_nowait()
            except queue.Empty:
                continue
            try:
                run_job(job_id)
            except Exception as error:
                console_event('error', f'VM 背景工作失敗：{type(error).__name__}')
            finally:
                JOB_QUEUE.task_done()

@serialized
def schedule_due_targets():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        retry_ids = auto_recovery.enqueue_due(db, sys.modules[__name__])
    for job_id in retry_ids:
        enqueue_job(job_id, 'auto-retry-')
    with connect() as db:
        due = [row['id'] for row in db.execute('SELECT id FROM targets WHERE auto_enabled=1 AND auto_next_run_at<=? ORDER BY auto_next_run_at', (now(),))]
    for target_id in due:
        ids = []
        event = None
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM targets WHERE id=?', (target_id,)).fetchone()
            if not current or not current['auto_enabled'] or (not current['auto_next_run_at']) or (current['auto_next_run_at'] > now()):
                continue
            username = f"id:{current['numeric_id']}" if current['numeric_id'] else current['username']
            payload = {'engine': current['auto_engine'], 'authMode': current['auto_auth_mode'], 'accountId': current['auto_account_id'], 'maxItems': current['auto_max_items'], 'fullArchive': bool(current['auto_full_archive']), 'fullCheck': bool(current['auto_full_check']), 'refreshDiscovery': current['auto_engine'] == 'instaloader', 'captions': bool(current['auto_captions']), 'maxRpm': current['auto_max_rpm'], 'gallerySpeed': current['auto_gallery_speed']}
            payload['ratePolicy'] = validate_rate_policy(json.loads(current['auto_rate_policy']) or None)
            interval = max(1, min(168, int(current['auto_interval_hours'])))
            try:
                until = global_cooldown(db)
                if until:
                    raise RateLimitError(until)
                if db.execute("SELECT 1 FROM jobs WHERE username=? AND status IN ('queued','running')", (username,)).fetchone():
                    raise ValueError('相同工作仍在佇列或執行中')
                includes, _ = includes_for_target(json.loads(current['auto_includes']), current)
                if current['auto_engine'] == 'instaloader':
                    includes = selected_media(json.loads(current['auto_includes']))
                ids = create_capture_jobs(db, payload, username, includes, current, new_batch_id('auto'))
                next_run = (datetime.now(timezone.utc) + timedelta(hours=interval)).isoformat()
                message = f"Auto Archive 已建立工作 {', '.join(('#' + str(i) for i in ids))}；下次 {hk_timestamp(next_run)}"
                db.execute('UPDATE targets SET auto_last_run_at=? WHERE id=?', (now(), target_id))
                event = ('info', f"@{current['username']} {message}", ids[0], current['auto_account_id'])
            except (RateLimitError, ValueError) as error:
                next_run = error.retry_at if isinstance(error, RateLimitError) else (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                message = f'Auto Archive 暫停：{error}'
                event = ('warning', f"@{current['username']} {message}", None, current['auto_account_id'])
            db.execute('UPDATE targets SET auto_next_run_at=?,auto_last_message=? WHERE id=?', (next_run, message, target_id))
        if event:
            console_event(*event)
        for job_id in ids:
            enqueue_job(job_id, 'auto-')

def scheduler():
    stop_event = threading.Event()
    while True:
        try:
            schedule_due_targets()
        except Exception as error:
            console_event('error', f'Auto Archive 排程器錯誤：{type(error).__name__}')
        stop_event.wait(30)

JOB_QUEUE = AutomaticFirstQueue()


def configure():
    """Bind explicit storage and make temporary media share its filesystem."""
    global DATA_DIR, LIBRARY_DIR, RUNTIME_DIR, COOKIE_DIR, DB_PATH, DOWNLOAD_ARCHIVE
    DATA_DIR = guard_disk()
    LIBRARY_DIR, RUNTIME_DIR = DATA_DIR/'library', DATA_DIR/'runtime'
    DB_PATH, DOWNLOAD_ARCHIVE = DATA_DIR/'archive.sqlite3', DATA_DIR/'media.sqlite3'
    COOKIE_DIR = None  # Credentials remain in an operator-managed external file.
    temp = DATA_DIR/'tmp'
    temp.mkdir(mode=0o700,exist_ok=True)
    os.environ['ARCHIVE_DATA_DIR'] = str(DATA_DIR)
    for key in ('TMPDIR','TEMP','TMP'): os.environ[key] = str(temp)


def register_account(label,cookie_file):
    """Register an external Netscape cookie file, never copy or print its contents."""
    cookie=Path(cookie_file)
    if not cookie.is_absolute() or cookie.is_symlink() or not cookie.is_file():
        raise ValueError('Cookie file must be an existing absolute regular file')
    if cookie.stat().st_mode & 0o077:
        raise ValueError('Cookie file must be owner-only (0600)')
    with connect() as db:
        cursor=db.execute("INSERT INTO accounts(label,browser,cookie_file,cookie_source,cookie_status,status,cookie_updated_at,created_at) VALUES(?,'none',?,'external','saved','ready',?,?)",
                          (label,str(cookie),now(),now()))
        return cursor.lastrowid


def validate_username(username):
    username=str(username).strip().lower()
    if not re.fullmatch(r'[a-z0-9_.]{1,30}',username) or username in ('.','..'):
        raise ValueError('Invalid Instagram username')
    return username


def set_target(username,account_id,includes,interval_hours=24,enabled=True,full_check=False):
    username=validate_username(username)
    if not includes or any(x not in MEDIA_INCLUDES|{'avatar'} for x in includes):
        raise ValueError('Choose posts,reels,stories,highlights,avatar')
    if not 1<=interval_hours<=168:raise ValueError('Interval must be 1–168 hours')
    with connect() as db:
        validate_access(db,{'accountId':account_id},includes,'gallery-dl',check_safety=False)
        db.execute('''INSERT INTO targets(username,auto_enabled,auto_account_id,auto_interval_hours,auto_includes,auto_full_check,auto_next_run_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET auto_enabled=excluded.auto_enabled,
            auto_account_id=excluded.auto_account_id,auto_interval_hours=excluded.auto_interval_hours,
            auto_includes=excluded.auto_includes,auto_full_check=excluded.auto_full_check,
            auto_next_run_at=excluded.auto_next_run_at,updated_at=excluded.updated_at''',
            (username,int(enabled),account_id,interval_hours,json.dumps(includes),int(full_check),now() if enabled else None,now(),now()))


def fetch(username,account_id,includes,full_check=False):
    username=validate_username(username)
    if not includes or any(x not in MEDIA_INCLUDES|{'avatar'} for x in includes):
        raise ValueError('Choose posts,reels,stories,highlights,avatar')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM jobs WHERE username=? AND status IN ('queued','running')",(username,)).fetchone():
            raise ValueError('Target already queued or running')
        target=db.execute('SELECT * FROM targets WHERE username=?',(username,)).fetchone()
        ids=create_capture_jobs(db,{'accountId':account_id,'fullCheck':full_check},username,includes,target,new_batch_id('manual'))
    for job_id in ids:enqueue_job(job_id)
    return ids
