"""Create a disposable local PostgreSQL cluster and run offline capture integration.

Requires initdb/pg_ctl/psql on PATH (or PG_BIN), XLOADER_TEST_TMP for synthetic
media and XLOADER_TEST_CONTROL_TMP for the disposable database control files.
No existing database, service, credentials or archive is accessed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
bin_dir = Path(os.environ.get('PG_BIN', '/usr/bin'))
media_parent = Path(os.environ['XLOADER_TEST_TMP']).resolve(strict=True)
control_parent = Path(os.environ['XLOADER_TEST_CONTROL_TMP']).resolve()
control_parent.mkdir(exist_ok=True)


def run(name, *args, **kwargs):
    result = subprocess.run([str(bin_dir/name), *map(str,args)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{name} failed: {result.stdout}')
    return result


with tempfile.TemporaryDirectory(dir=control_parent) as control_tmp, \
     tempfile.TemporaryDirectory(dir=media_parent) as media_tmp:
    control = Path(control_tmp)
    data = Path(media_tmp)
    socket = control/'socket'
    socket.mkdir(mode=0o700)
    if len(str(socket).encode()) > 80:
        raise ValueError('XLOADER_TEST_CONTROL_TMP is too long for a PostgreSQL Unix socket; choose a shorter project path')
    cluster = control/'cluster'
    run('initdb', '-D', cluster, '--auth=trust', '--no-locale', '--encoding=UTF8')
    # The unique Unix socket is accessible only to this user's private directory.
    run('pg_ctl', '-D', cluster, '-l', control/'postgres.log', '-o',
        f"-k {socket} -h '' -p 55439", '-w', 'start')
    try:
        run('psql', '-h', socket, '-p','55439','-d','postgres','-c','CREATE SCHEMA xloader_test')
        config = control/'db.json'
        config.write_text(json.dumps({'database':{'host':str(socket),'port':55439,'dbname':'postgres'}}))
        config.chmod(0o600)
        for name in ('control','tmp'):
            (data/name).mkdir(mode=0o700)
        (data/'control/cookies.txt').write_text('synthetic-presence-only')
        env = {**os.environ, 'PYTHONPATH':str(root/'src'),
               'XLOADER_DATA':str(data), 'XLOADER_CONTROL':str(data/'control'),
               'TMPDIR':str(data/'tmp'), 'XLOADER_DB_CONFIG':str(config),
               'XLOADER_SCHEMA':'xloader_test','XLOADER_TEST_DATABASE':'isolated'}
        result = subprocess.run([sys.executable,'-m','unittest','discover','-s',str(root/'tests'),'-p','test_postgres.py','-v'],env=env)
    finally:
        run('pg_ctl','-D',cluster,'-m','fast','-w','stop')
    raise SystemExit(result.returncode)
