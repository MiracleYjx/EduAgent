"""Valid isolated preconditions for exact-step frozen fault acceptance."""
from pathlib import Path
from uuid import uuid4
import json,os,re,subprocess,sys
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from backend.app.core.config import get_settings
OUT=ROOT/'benchmark/results/v2/t189-optimize-20261007';private=ROOT/'.cache/t189-optimize-20261007'
original=make_url(str(get_settings().database_url));name='eduagent_t189_'+uuid4().hex[:12]
admin=create_engine(original.set(database='postgres'),isolation_level='AUTOCOMMIT')
values=dotenv_values(private/'startup-gradio-configs/first-1.env')
values['DATABASE_URL']=original.set(database=name).render_as_string(hide_password=False)
values['STORAGE_ROOT']=str(private/'fault-baseline-storage')
config=private/'fault-baseline.env';config.write_text('\n'.join(k+'='+json.dumps(v) for k,v in values.items() if v is not None),encoding='utf-8')
with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
created=True;code=None;revision=None
try:
 env=os.environ | {k:v for k,v in values.items() if v is not None} | {'PYTHONIOENCODING':'utf-8'}
 with (OUT/'fault-baseline-migration.log').open('w',encoding='utf-8') as log:
  subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT,env=env,stdout=log,stderr=log,check=True,timeout=90)
 engine=create_engine(original.set(database=name))
 with engine.connect() as c:revision=c.scalar(text('SELECT version_num FROM alembic_version'))
 engine.dispose();assert revision=='0023_grading_exam_question'
 with (OUT/'faults-final.log').open('w',encoding='utf-8') as log:
  run=subprocess.run([sys.executable,str(ROOT/'benchmark/t189/fault_acceptance.py'),'--exe',str(private/'build-api/dist/EduAgent/EduAgent.exe'),'--source-config',str(config),'--output',str(OUT/'faults-final'),'--private-root',str(private/'faults-final-configs'),'--port','19503'],cwd=ROOT,stdout=log,stderr=log,timeout=240)
 code=run.returncode
finally:
 with admin.connect() as c:
  active=c.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE datname=:n'),{'n':name})
  removed=active==0 and name!=original.database and bool(re.fullmatch(r'eduagent_t189_[a-f0-9]{12}',name))
  if removed:c.execute(text(f'DROP DATABASE "{name}"'))
 admin.dispose()
 (OUT/'fault-baseline-receipt.json').write_text(json.dumps({'database':name,'baseline_revision':revision,'exit_code':code,'active_connections':active,'removed':removed,'meaning':'Source migration prepares only the isolated fault fixture; all faults execute actual frozen EXE'},indent=2),encoding='utf-8')
assert code==0
print('isolated baseline cleaned; inspect every exact-step fault result')
