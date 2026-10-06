from pathlib import Path
import hashlib,json,subprocess
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from backend.app.core.config import get_settings
root=Path.cwd();out=root/'benchmark/results/v2/final-gate-retest-20261006'
expected={'README.md':'93277070d870ff09eb524acd8cae57df80aaed2eb382c8f58c0b995b8045fa2e','README.zh-CN.md':'d621fd9a82a4e4b06751fa50562a34140bf05c22997a4331e0343c4448382b8d','benchmark/corpus/grading_samples.json':'9f8c56328973fb56f88564560f1166bc0415a54fb5022f98c07fc6c168f94fca','.env':'0834523cc6a2ce5742012f62b903397f1b404d678d9e066a5a4fe19875a02566'}
actual={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in expected}
assert actual==expected, {p:actual[p] for p in expected if actual[p]!=expected[p]}
engine=create_engine(str(get_settings().database_url),poolclass=NullPool)
with engine.connect() as c:
 revision=c.scalar(text('SELECT version_num FROM public.alembic_version'))
 tables=c.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"))
 counts={p:c.scalar(text('SELECT count(*) FROM public.'+p)) for p in ['exams','exam_questions','submissions','answers','grading_results','review_records']}
print(json.dumps({'database':engine.url.database,'revision':revision,'tables':tables,'counts':counts},indent=2)); assert revision=='0012_audit_logs' and tables==24
assert counts=={'exams':1,'exam_questions':2,'submissions':1,'answers':2,'grading_results':0,'review_records':0}
engine.dispose()
r=subprocess.run(['docker','ps','--format','{{.Names}}|{{.Status}}'],capture_output=True,text=True,check=True)
receipt={'protected_file_sha256':actual,'all_protected_files_unchanged':True,'original_database_revision':revision,'original_database_table_count_including_alembic':tables,'original_database_application_table_count':tables-1,'original_database_counts':counts,'original_containers':r.stdout.strip().splitlines(),'post_hooks':'skipped; .specify/extensions.yml absent'}
(out/'workspace-preservation.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2))
