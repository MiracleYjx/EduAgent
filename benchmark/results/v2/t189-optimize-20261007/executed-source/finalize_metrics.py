from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,re,statistics,subprocess,sys
import psutil
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
sys.path.insert(0,str(Path.cwd()))
from backend.app.core.config import get_settings
root=Path.cwd();out=root/'benchmark/results/v2/t189-optimize-20261007'
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def save(name,data):(out/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
before=load(out/'preservation-before.json')
u=make_url(str(get_settings().database_url));assert u.database=='eduagent'
e=create_engine(u)
with e.connect() as c:
 facts={'revision':c.scalar(text('SELECT version_num FROM alembic_version')),'tables':c.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")),'counts':{n:c.scalar(text('SELECT count(*) FROM '+n)) for n in ['exams','exam_questions','submissions','answers','grading_results','review_records']}}
e.dispose()
processes=[]
for p in psutil.process_iter(['pid','name','exe']):
 try:
  if p.info['exe'] and 't189-optimize-20261007' in p.info['exe'].lower() and p.info['name'].lower()=='eduagent.exe':processes.append(p.info['pid'])
 except (psutil.AccessDenied,psutil.NoSuchProcess):pass
services={}
for n in ['eduagent-postgres-1','eduagent-redis-1']:
 a=subprocess.run(['docker','inspect','--format','{{json .State}}',n],capture_output=True,text=True,encoding='utf-8',check=True,timeout=30)
 state=json.loads(a.stdout);services[n]={'running':state['Running'],'health':state.get('Health',{}).get('Status')}
after={'hashes':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in before['hashes']},'original_database':facts,'remaining_owned_frozen_pids':processes,'shared_services':services}
after['protected_files_unchanged']=after['hashes']==before['hashes'];after['original_database_unchanged']=facts==before['original_database']
save('preservation-after.json',after)
assert after['protected_files_unchanged'] and after['original_database_unchanged'] and not processes
assert all(v['running'] and v['health']=='healthy' for v in services.values())
s=load(out/'startup-final/summary.json');fault=load(out/'faults-final/summary.json');runtime=load(out/'runtime-local/receipt.json');build=load(out/'build-api-receipt.json')
modes={}
for mode,target,count in [('first',30,3),('subsequent',10,5)]:
 runs=[r for r in s['runs'] if r['mode']==mode];seconds=[r['elapsed_seconds'] for r in runs];assert len(runs)==count
 modes[mode]={'target_seconds_exclusive':target,'planned':count,'passed':sum(r['timing_target_met'] for r in runs),'seconds':seconds,'min_seconds':min(seconds),'median_seconds':statistics.median(seconds),'max_seconds':max(seconds)}
resources={'sampled_windows':len(s['runs']),'complete':sum(r['memory_budget']['resource_complete'] for r in s['runs']),'budget_passed':sum(r['memory_budget']['budget_pass'] for r in s['runs']),'peaks_mib':{k:max(r['resources']['peaks'][k] for r in s['runs'])/1048576 for k in s['runs'][0]['resources']['peaks']},'limits_bytes':s['runs'][0]['memory_budget']['limits_bytes'],'scope':s['resource_scope'],'note':'Startup windows only; one-second sampled peaks, not continuous physical-memory high-water or long model workload; shared PG pages may repeat. Per-run legacy scope excludes parent redirector wording; actual observer includes owned launcher and descendants as top-level summary states.'}
cleanups={}
for n in ['startup-sdk','startup-gradio','startup-final']:
 rows=load(out/n/'cleanup.json');cleanups[n]=all(x.get('removed') and x['active_connections']==0 for x in rows)
for n in ['fault-baseline-receipt.json','frozen-ui-regression-receipt.json','final-frozen-ui-regression-receipt.json']:
 r=load(out/n);cleanups[n]=r['removed'] and r['active_connections']==0
cleanups['fault-final-owned-migration-db']=fault['database_removed'] and fault['active_connections']==0
cleanups['runtime-local']=runtime['database_removed']
old=load(out/'faults/summary.json');audit=[]
for c in old['cases']:
 raw=(out/'faults'/ (c['case']+'.log')).read_text(encoding='utf-8')
 match=re.search(r'启动失败：\[([^\]]+)\]',raw);step=match.group(1) if match else None
 expected='外置模型' if c['case']=='missing_local_model' else c['expected_step']
 audit.append({'case':c['case'],'reported_passed':c['passed'],'expected_actual_step':expected,'actual_failure_step':step,'audited_valid':step==expected and c['no_business_port_observed'] and c['owned_preload_exited']})
save('faults-observer-audit.json',{'original_receipt_unchanged':True,'original_reported_passed':sum(c['passed'] for c in old['cases']),'audited_valid':sum(c['audited_valid'] for c in audit),'planned':8,'reason':'Reused deleted isolated DB caused early PostgreSQL failures; substring matching falsely accepted Redis preflight heading. Strict explicit failure-step audit; final fresh valid fixture passed all 8.','cases':audit})
source_equal=all(hashlib.sha256((root/n).read_bytes()).hexdigest()==h for n,h in build['source_files_sha256'].items())
exe=root/'.cache/t189-optimize-20261007/build-api/dist/EduAgent/EduAgent.exe';exe_equal=hashlib.sha256(exe.read_bytes()).hexdigest()==build['executable_sha256']
passed=(all(m['planned']==m['passed'] for m in modes.values()) and resources['complete']==8 and resources['budget_passed']==8 and all(r['startup_success'] and r['normal_exit_code']==0 and r['owned_worker_exited'] for r in s['runs']) and all(c['passed'] and c['no_business_port_observed'] and c['owned_preload_exited'] and c['actual_failure_step']==c['expected_step'] for c in fault['cases']) and len(fault['cases'])==8 and len(runtime['cases'])==4 and all(c['passed'] for c in runtime['cases']) and runtime['exits']==[0,0] and all(cleanups.values()) and source_equal and exe_equal)
summary={'task':'T189','created_utc':datetime.now(timezone.utc).isoformat(),'accepted':passed,'protocol':'v2-evaluation-1','scope':'Final actual Windows frozen package performance and related technical gates; no browser rendering, cloud inference or clean-host installation claim','startup':modes,'resources':resources,'normal_exit_passed':sum(r['normal_exit_code']==0 and r['owned_worker_exited'] for r in s['runs']),'faults':{'passed':sum(c['passed'] for c in fault['cases']),'planned':8,'source':'faults-final/summary.json'},'runtime':{'passed':sum(c['passed'] for c in runtime['cases']),'planned':4,'cloud_calls':0,'local_controlled_503_requests':len(runtime['local_calls']),'source':'runtime-local/receipt.json'},'cleanup':cleanups,'build':{'source_commit':build['source_commit'],'source_dirty':build['source_dirty'],'source_hashes_match_current':source_equal,'actual_executable_matches_receipt':exe_equal,'executable_sha256':build['executable_sha256'],'file_count':build['file_count'],'size_bytes':build['size_bytes']},'prior_attempts_retained':['startup-sdk (subsequent 1/5)','startup-gradio (subsequent 4/5)','faults (raw 7/8; strict audited 6/8)','profile-frozen-before and v2 (native py-spy capture failed)','runtime-local.log (observer import error before creating DB/server)'],'annotation':'AI-assisted + developer review; independent teacher 0; no label changes'}
save('summary.json',summary);assert passed
print(json.dumps({'accepted':passed,'peaks_mib':resources['peaks_mib'],'preserved':after['protected_files_unchanged'] and after['original_database_unchanged'],'old_fault_audit':sum(c['audited_valid'] for c in audit),'owned_cleanup':cleanups,'source_equal':source_equal},ensure_ascii=False))
