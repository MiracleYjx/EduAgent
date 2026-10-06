from pathlib import Path
import json, os, subprocess, sys, time
root=Path.cwd(); private=root/'.cache/t189-t168-retest-20261006/final-gate/m0'; private.mkdir(parents=True, exist_ok=True)
env=os.environ.copy(); config=private/'synthetic.env'
config.write_text('LLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=synthetic-m0-only-key\nDEEPSEEK_BASE_URL=https://api.deepseek.com\nDEEPSEEK_MODEL=deepseek-chat\nEMBEDDING_PROVIDER=openai_compatible\nRERANK_PROVIDER=none\nCONFIDENCE_THRESHOLD=0.7\nJWT_SECRET_KEY=synthetic-m0-only-0123456789abcdef0123456789\nDEV_MODE=True\nOCR_ENABLED=False\n',encoding='utf-8')
env.update(COMPOSE_PROJECT_NAME='eduagent-test',POSTGRES_PORT='15432',REDIS_PORT='16379',BACKEND_PORT='18000',EDUAGENT_ENV_FILE=str(config),DEMO_EMBEDDING_MODEL='synthetic-m0-model',DEMO_EMBEDDING_BASE_URL='https://example.invalid/v1',DEMO_EMBEDDING_API_KEY='synthetic-m0-key',PYTHONIOENCODING='utf-8')
receipt={'scope':'current-source isolated existing test volumes; synthetic providers; no cloud inference','source_commit':'f46c2f9','commands':[]}
def run(args, name, timeout=600):
 start=time.monotonic(); r=subprocess.run(args,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout); (private/(name+'.log')).write_text(r.stdout+'\n'+r.stderr,encoding='utf-8'); receipt['commands'].append({'name':name,'returncode':r.returncode,'elapsed_seconds':time.monotonic()-start}); return r
try:
 test=run([sys.executable,'-m','pytest','tests/integration/test_m0_smoke.py','-q','--junitxml='+str(private/'pytest.xml')],'pytest',900)
 print(test.stdout[-2000:])
 if test.returncode==0:
  for args,name in [(['docker','compose','ps','--all','--format','json'],'containers'),(['docker','compose','exec','-T','backend','alembic','current'],'migration'),(['docker','compose','exec','-T','redis','redis-cli','ping'],'redis')]: run(args,name)
finally:
 run(['docker','compose','down'],'owned-project-down')
 (private/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2))
sys.exit(test.returncode)
