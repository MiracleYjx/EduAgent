"""Execute the authorized fixed protocol once, sequentially, without hidden retries."""
import json,os,subprocess,sys,time
from datetime import UTC,datetime
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
run=Path(__file__).resolve().parent
source=run/'source-final'
out=repo/'benchmark/results/v2/t160-retest-20261003'
commit=json.loads((run/'source-final-receipt.json').read_text())['commit']
base=['--repo-root',str(repo),'--source-root',str(source),'--run-root',str(run)]
commands=[('quality',[str(source/'benchmark/t160/quality_runner.py'),'--execute-confirmed',*base,'--annotations',str(repo/'benchmark/corpus/t160-assisted-20261003/annotations.draft.json'),'--input-root',str(repo/'benchmark/corpus/v2-draft-20261001'),'--ocr-model-dir',str(repo/'.cache/t155-ocr-20261002/rapid-models')]),('performance',[str(source/'benchmark/t160/performance_runner.py'),'--execute',*base,'--output-root',str(out/'import-performance'),'--model-dir',str(repo/'.cache/t155-ocr-20261002/rapid-models'),'--isolation-file',str(run/'isolation.json'),'--source-commit',commit])]
for label in ['final_pytest','final_mypy','final_ruff','final_ruff_tools','final_alembic_check']:
 p=run/(label+'.json')
 if not p.exists():raise SystemExit('Required check receipt missing: '+label)
 if json.loads(p.read_text())['exit_code']!=0:raise SystemExit('Required check failed: '+label)
env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
for label,args in commands:
 record={'label':label,'source_commit':commit,'started_at':datetime.now(UTC).isoformat(),'command':[sys.executable,*args]}
 start=time.perf_counter()
 with (run/('formal-'+label+'.log')).open('xb') as log:
  result=subprocess.run([sys.executable,*args],cwd=repo,env=env,stdout=log,stderr=subprocess.STDOUT,check=False)
 record.update(exit_code=result.returncode,completed_at=datetime.now(UTC).isoformat(),elapsed_seconds=time.perf_counter()-start)
 (run/('formal-'+label+'.json')).write_text(json.dumps(record,indent=2),encoding='utf-8')
 print(json.dumps(record),flush=True)
 if result.returncode:raise SystemExit(result.returncode)
