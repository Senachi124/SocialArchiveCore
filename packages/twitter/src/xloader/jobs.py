"""Serialize user controls and worker acknowledgements on the same job row."""
from . import db
from .events import event

ACTIVE = ('queued', 'running', 'paused')

def control(job_id, action):
    with db.connection() as c, c.transaction():
        row = c.execute('SELECT * FROM xl_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
        if not row: raise LookupError('工作不存在')
        if row['status'] == 'cancelled' or row['cancel_requested']:
            raise ValueError('工作已取消，不能復原')
        status = row['status']
        if action == 'pause' and status in ('queued', 'running'):
            message = '正在停止擷取並保存進度' if status == 'running' else '已暫停；可繼續或取消'
            c.execute("UPDATE xl_jobs SET pause_requested=true,status=%s,message=%s WHERE id=%s",
                      ('running' if status == 'running' else 'paused', message, job_id))
        elif action == 'cancel' and status in (*ACTIVE, 'failed', 'partial'):
            message = '正在取消；此工作不能復原' if status == 'running' else '已取消；不能復原，已保存內容保留'
            c.execute("UPDATE xl_jobs SET cancel_requested=true,pause_requested=false,status=%s,message=%s,finished_at=CASE WHEN status='running' THEN NULL ELSE now() END WHERE id=%s",
                      ('running' if status == 'running' else 'cancelled', message, job_id))
        elif action in ('resume', 'retry') and (status == 'paused' or (action == 'retry' and status == 'failed')):
            c.execute("UPDATE xl_jobs SET status='queued',pause_requested=false,finished_at=NULL,message='已繼續；等待執行' WHERE id=%s", (job_id,))
        else: raise ValueError('目前狀態不能執行此操作')
    event(action, {'pause': '要求暫停', 'cancel': '要求永久取消', 'resume': '繼續工作', 'retry': '重試失敗工作'}[action], job_id=job_id)

def claim(job_id):
    with db.connection() as c, c.transaction():
        c.execute('SELECT pg_advisory_xact_lock(89272104)')
        return c.execute("""UPDATE xl_jobs SET status='running',started_at=COALESCE(started_at,now()),
      active_since=now(),heartbeat_at=now(),message='準備擷取'
      WHERE id=%s AND status='queued' AND NOT cancel_requested AND NOT pause_requested RETURNING *""", (job_id,)).fetchone()

def interrupted(job_id):
    row = db.execute('SELECT status,cancel_requested,pause_requested FROM xl_jobs WHERE id=%s', (job_id,), one=True)
    return not row or row['status'] != 'running' or row['cancel_requested'] or row['pause_requested']

def finish(job_id, status, message):
    with db.connection() as c, c.transaction():
        row = c.execute('SELECT * FROM xl_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
        if row['cancel_requested'] or row['status'] == 'cancelled':
            status, message = 'cancelled', '已取消；不能復原，已保存內容保留'
        elif row['pause_requested']:
            status, message = 'paused', '已暫停；進度保留，可繼續或取消'
        c.execute("""UPDATE xl_jobs SET status=%s,message=%s,heartbeat_at=now(),
          elapsed_seconds=elapsed_seconds+COALESCE(EXTRACT(EPOCH FROM now()-active_since),0),active_since=NULL,
          finished_at=CASE WHEN %s IN ('queued','paused') THEN NULL ELSE now() END WHERE id=%s""", (status,message,status,job_id))
        c.execute("UPDATE xl_slices SET status='queued' WHERE job_id=%s AND status='running'", (job_id,))
    result = db.execute('SELECT elapsed_seconds FROM xl_jobs WHERE id=%s', (job_id,), one=True)
    event(status, message, job_id=job_id, elapsed_seconds=round(result['elapsed_seconds'],3))
    return status
