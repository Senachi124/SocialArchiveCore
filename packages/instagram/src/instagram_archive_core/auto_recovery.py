"""Durable automatic retry chains; historical attempts remain immutable failures."""
import json
from datetime import datetime, timedelta, timezone


def automatic(job):
    return (str(job['batch_id']).startswith('auto-') and job['engine']=='gallery-dl'
            and job['auth_mode']=='authenticated' and job['target_id']
            and not job['cancel_requested'])


def result(job):
    value=json.loads(job['result_json'] or '{}')
    return value if isinstance(value,dict) else {}


def recovery(job):
    return result(job).get('auto_recovery',{})


def write(db,job,state):
    value=result(job);value['auto_recovery']=state
    raw=json.dumps(value,ensure_ascii=False)
    db.execute('UPDATE jobs SET result_json=? WHERE id=?',(raw,job['id']))
    return {**dict(job),'result_json':raw}


def initial(job):
    return {'root_job':job['id'],'attempt':0,'boundary_before':None,'pending':False}


def prepare(db,job,stamp):
    if not automatic(job):return job
    state=recovery(job) or initial(job)
    if not state.get('boundary_before'):state['boundary_before']=stamp
    return write(db,job,state)


def defer(db,job,retry_at):
    if not automatic(job):return
    state=recovery(job) or initial(job)
    state.update(pending=True,retry_at=retry_at)
    write(db,job,state)


def retry_deadline(job,upstream):
    attempt=min(5,max(0,int(recovery(job).get('attempt',0))))
    seconds=min(21600,900*(2**attempt))
    return max(upstream or '',(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat())


def environment(job,env):
    env=dict(env);env.pop('ARCHIVE_BOUNDARY_BEFORE',None)
    if automatic(job) and recovery(job).get('boundary_before'):
        env['ARCHIVE_BOUNDARY_BEFORE']=recovery(job)['boundary_before']
    return env


def enqueue_due(db,app):
    if app.global_cooldown(db):return []
    queued=[]
    rows=db.execute("SELECT * FROM jobs WHERE status='failed' AND batch_id LIKE 'auto-%' "
                    "AND result_json LIKE '%auto_recovery%' ORDER BY id").fetchall()
    for job in rows:
        state=recovery(job)
        if not automatic(job) or not state.get('pending') or state['retry_at']>app.now():continue
        target=db.execute('SELECT * FROM targets WHERE id=?',(job['target_id'],)).fetchone()
        if (not target or not target['auto_enabled'] or target['auto_engine']!=job['engine']
                or target['auto_account_id']!=job['account_id']):
            state.update(pending=False,stopped_reason='schedule_disabled_or_changed')
            write(db,job,state);continue
        account=db.execute('SELECT * FROM accounts WHERE id=?',(job['account_id'],)).fetchone()
        if not account or account['status']=='auth_required':continue
        if db.execute("SELECT 1 FROM jobs WHERE target_id=? AND status IN ('queued','running')",
                      (job['target_id'],)).fetchone():continue
        payload={'engine':job['engine'],'authMode':job['auth_mode'],'accountId':job['account_id'],
                 'captions':bool(job['captions']),'maxRpm':job['max_rpm'],
                 'gallerySpeed':job['gallery_speed'],'maxItems':job['max_items'],
                 'fullArchive':bool(job['full_archive']),'fullCheck':bool(job['full_check'])}
        ids=app.create_capture_jobs(db,payload,job['username'],json.loads(job['includes']),target,
                                    app.new_batch_id('auto-retry'))
        assert len(ids)==1
        child=db.execute('SELECT * FROM jobs WHERE id=?',(ids[0],)).fetchone()
        write(db,child,{'root_job':state['root_job'],'attempt':state['attempt']+1,
                       'retry_of':job['id'],'boundary_before':state['boundary_before'],'pending':False})
        state.update(pending=False,retry_job=ids[0]);write(db,job,state)
        db.execute('UPDATE targets SET auto_last_message=? WHERE id=?',
                   (f"冷卻結束，接續 #{job['id']} → #{ids[0]}；已保存媒體會跳過",target['id']))
        queued.extend(ids)
    return queued
