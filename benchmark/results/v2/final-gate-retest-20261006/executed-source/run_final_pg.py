from pathlib import Path
import json, os, subprocess, sys, time
from uuid import uuid4
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from backend.app.core.config import get_settings
root=Path.cwd(); out=root/'.cache/t189-t168-retest-20261006/final-gate/pg-supplement';out.mkdir(parents=True,exist_ok=True)
base=create_engine(str(get_settings().database_url),poolclass=NullPool).url
control=create_engine(base.set(database='postgres'),isolation_level='AUTOCOMMIT',poolclass=NullPool)
name='eduagent_finalpg_20261006_'+uuid4().hex[:8]; created=False; receipt={'scope':'real container pg_dump/restore and assets; owned database only','database':name,'original_database':base.database}
try:
 with control.connect() as c:c.execute(text('CREATE DATABASE "'+name+'"'))
 created=True
 target=create_engine(base.set(database=name),poolclass=NullPool)
 with target.begin() as c:c.execute(text('CREATE EXTENSION vector'))
 target.dispose()
 env=os.environ.copy();env.update(DATABASE_URL=base.set(database=name).render_as_string(hide_password=False),EDUAGENT_TEST_PG_CONTAINER='eduagent-postgres-1',PYTHONIOENCODING='utf-8')
 start=time.monotonic();r=subprocess.run([sys.executable,'-m','pytest','tests/integration/test_backup_restore.py','tests/integration/test_question_asset_persistence.py','-q','--junitxml='+str(out/'pytest.xml')],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=900)
 (out/'pytest.log').write_text(r.stdout+'\n'+r.stderr,encoding='utf-8');receipt.update(returncode=r.returncode,elapsed_seconds=time.monotonic()-start);print(r.stdout[-2000:])
finally:
 if created:
  with control.connect() as c:
   active=c.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE datname=:name'),{'name':name});receipt['active_connections_at_cleanup']=active
   if active==0:c.execute(text('DROP DATABASE "'+name+'"'));receipt['owned_database_removed']=True
   else:receipt['owned_database_removed']=False
 control.dispose();(out/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2));sys.exit(r.returncode)
