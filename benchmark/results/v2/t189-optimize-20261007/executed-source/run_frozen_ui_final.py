"""Run unchanged UI/deployment regressions with the frozen Gradio module copy."""
from pathlib import Path
import importlib.abc,importlib.util,json,os,sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from backend.app.core.config import get_settings
from scripts.freeze_gradio import freeze_api_component_lookup,freeze_type_hint_writer
OUT=ROOT/'benchmark/results/v2/t189-optimize-20261007';private=Path(__file__).parent
url=make_url(str(get_settings().database_url));name='eduagent_t189_'+uuid4().hex[:12]
admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
os.environ['DATABASE_URL']=url.set(database=name).render_as_string(hide_password=False)
os.environ['STORAGE_ROOT']=str(private/'ui-regression-storage')
os.environ['DEEPSEEK_BASE_URL']='https://example.invalid/v1'
os.environ['DEEPSEEK_API_KEY']='synthetic-ui-regression-key'
os.environ['GRADIO_ANALYTICS_ENABLED']='False'
get_settings.cache_clear()
source=Path(importlib.util.find_spec('gradio').origin).with_name('component_meta.py')
frozen=freeze_type_hint_writer(source,private/'ui-regression-final/component_meta.py')
api_frozen=freeze_api_component_lookup(source.with_name('blocks.py'),private/'ui-regression-final/blocks.py')
variants={'gradio.component_meta':frozen,'gradio.blocks':api_frozen}
class Loader(importlib.abc.Loader):
 def __init__(self,path):self.path=path
 def create_module(self,spec):return None
 def exec_module(self,module):exec(compile(self.path.read_text(encoding='utf-8'),str(self.path),'exec'),module.__dict__)
class Finder(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in variants:return importlib.util.spec_from_file_location(fullname,variants[fullname],loader=Loader(variants[fullname]))
sys.meta_path.insert(0,Finder())
code=None
try:
 from alembic import command
 from alembic.config import Config
 command.upgrade(Config(str(ROOT/'alembic.ini')),'head')
 import pytest
 code=pytest.main(['tests/unit/ui','tests/unit/deployment/test_windows_launcher.py','tests/unit/deployment/test_windows_tls.py','tests/unit/deployment/test_windows_fault_observer.py','tests/unit/test_retry_policy.py','tests/unit/test_retry_policy_sdk.py','tests/unit/test_deepseek.py','tests/contract/test_embedding_provider.py','tests/unit/ingestion/test_paper_provider_lifecycle.py','-q','--junitxml='+str(OUT/'final-frozen-ui-regression.xml')])
finally:
 from backend.app.core.database import get_session_factory
 get_session_factory.cache_clear()
 with admin.connect() as c:
  active=c.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE datname=:n'),{'n':name})
  removed=False
  if active==0:c.execute(text(f'DROP DATABASE "{name}"'));removed=True
 admin.dispose()
 (OUT/'final-frozen-ui-regression-receipt.json').write_text(json.dumps({'exit_code':code,'owned_database':name,'active_connections':active,'removed':removed,'scope':'Unchanged original UI/deployment/SDK tests using both exact build-only Gradio transforms; not frozen EXE performance','model_calls':0},indent=2),encoding='utf-8')
raise SystemExit(code)
