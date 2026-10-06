from pathlib import Path
import subprocess,zipfile,io,os,json,shutil,hashlib,time
from datetime import UTC,datetime
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from backend.app.core.config import get_settings
from alembic import command
from alembic.config import Config
root=Path.cwd(); private=root/'.cache/t189-t168-retest-20261006/final-gate'; private.mkdir(exist_ok=False)
snapshot=private/'source'; snapshot.mkdir()
commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
data=subprocess.check_output(['git','archive','--format=zip',commit])
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    for entry in archive.infolist():
        assert (snapshot/entry.filename).resolve().is_relative_to(snapshot)
    archive.extractall(snapshot)
# Isolated committed fixture, not modification of user's current corpus.
shutil.copy2(root/'.env',snapshot/'.env')
url=make_url(str(get_settings().database_url)); name='eduagent_finalgate_20261006_'+os.urandom(4).hex()
admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
created=False; active=None; removed=False; code=None; started=time.perf_counter()
try:
    with admin.connect() as conn: conn.execute(text(f'CREATE DATABASE "{name}"'))
    created=True
    env=os.environ | {'DATABASE_URL':url.set(database=name).render_as_string(hide_password=False),'PYTHONIOENCODING':'utf-8'}
    os.environ['DATABASE_URL']=env['DATABASE_URL']; get_settings.cache_clear()
    command.upgrade(Config(str(root/'alembic.ini')),'head')
    with (private/'pytest.log').open('w',encoding='utf-8') as log:
        result=subprocess.run([str(root/'.cache/e2-t157-159-20261002/runtime/Scripts/python.exe'),'-m','pytest','-q','--junitxml='+str(private/'pytest.xml')],cwd=snapshot,env=env,stdout=log,stderr=log,check=False)
    code=result.returncode
finally:
    if created:
        with admin.connect() as conn:
            active=conn.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE datname=:name'),{'name':name})
            if active==0 and name!=url.database:
                conn.execute(text(f'DROP DATABASE "{name}"')); removed=True
    admin.dispose()
    receipt={'source_commit':commit,'source_kind':'git archive of committed code and corpus; user workspace files unmodified','corpus_sha256':hashlib.sha256((snapshot/'benchmark/corpus/grading_samples.json').read_bytes()).hexdigest(),'database':name,'active_connections':active,'database_removed':removed,'exit_code':code,'seconds':time.perf_counter()-started,'completed_at':datetime.now(UTC).isoformat()}
    (private/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt),flush=True)