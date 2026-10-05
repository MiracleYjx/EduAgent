// Actual Edge/Gradio interaction. No internal component or service injection.
const fs = require('fs');
const { chromium } = require(process.env.T179_PLAYWRIGHT);
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
 const browser = await chromium.launch({channel:'msedge', headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 let result={case_id:spec.case_id,run_id:spec.run_id,passed:false,timeout_limit_ms:60000};
 try {
  await page.goto(spec.url+'/gradio/',{waitUntil:'domcontentloaded'});
  await page.getByRole('button',{name:'刷新考试',exact:true}).click();
  const row=page.locator('#t179-exams .virtual-body .body-cell').filter({hasText:spec.title}).first();
  await page.locator('#t179-exams .virtual-body .body-cell').first().waitFor({state:'visible',timeout:60000});
  for(let i=0;i<80 && !(await row.count());i++){await page.locator('#t179-exams .virtual-table-viewport').hover();await page.mouse.wheel(0,380);await page.waitForTimeout(80);}
  await row.waitFor({state:'attached',timeout:10000}); await row.click();
  console.log(JSON.stringify({event:'setup',step:'exam selected'}));
  await page.getByRole('tab',{name:'选择题目',exact:true}).click();
  await page.getByText('按条件组卷',{exact:true}).click();
  for (const [suffix,value] of [['single',spec.counts.SINGLE_CHOICE||0],['true',spec.counts.TRUE_FALSE||0],['short',spec.counts.SHORT_ANSWER||0],['total',spec.total]]) {
   await page.locator('#edu-exam-assembly-'+suffix+' input, #edu-exam-assembly-'+suffix+' textarea').fill(String(value));
  }
  const coverage=page.locator('#edu-exam-assembly-coverage');
  await coverage.getByRole('button',{name:'Add row',exact:true}).click();
  for (const [column,value] of [[0,spec.label],[1,'1']]) {
   await coverage.locator('.virtual-body [data-row="0"][data-col="'+column+'"]').dblclick();
   await coverage.getByRole('textbox',{name:'Edit cell',exact:true}).fill(value);
   await coverage.getByRole('textbox',{name:'Edit cell',exact:true}).press('Enter');
  }
  await page.getByText('教师完整试卷预览',{exact:true}).click();
  await page.locator('#edu-exam-assembly-preview article').first().waitFor({state:'visible'});
  const button=page.locator('#edu-exam-assembly-assemble');
  console.log(JSON.stringify({event:'setup',step:'conditions entered'})); await button.scrollIntoViewIfNeeded();
  await page.exposeFunction('t179Start',v=>console.log(JSON.stringify({event:'start',...v})));
  await button.evaluate(el=>el.addEventListener('click',()=>{
   window.t179Timing={start:performance.now(),started_at:new Date().toISOString()};
   window.t179Start(window.t179Timing);
  },{once:true,capture:true}));
  await button.click();
  await page.waitForFunction(({sat,count,total})=>{
   const panel=document.querySelector('#edu-exam-assembly-preview');
   const status=document.querySelector('#edu-exam-assembly-status')?.textContent||'';
   const articles=panel?.querySelectorAll('article');
   const completeImages=Array.from(panel?.querySelectorAll('img')||[]).every(i=>i.complete&&i.naturalWidth>0);
   const successful=sat ? (articles?.length===count && panel.textContent.includes('总分 '+Number(total).toFixed(2)) && document.body.textContent.includes('组卷成功，已重新读取')) : (status.includes('本次要求已保存')&&status.includes('未收录知识点')&&articles?.length===2);
   return successful&&completeImages;
  },{sat:spec.sat,count:spec.count,total:spec.total},{timeout:60000,polling:'raf'});
  result={...result,...await page.evaluate(async()=>{
   await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
   return {...window.t179Timing,completed_at:new Date().toISOString(),elapsed_ms:performance.now()-window.t179Timing.start};
  }),passed:true,status:await page.locator('#edu-exam-assembly-status').innerText(),article_count:await page.locator('#edu-exam-assembly-preview article').count(),preview:await page.locator('#edu-exam-assembly-preview').innerText(),images:await page.locator('#edu-exam-assembly-preview img').evaluateAll(a=>a.map(i=>({complete:i.complete,width:i.naturalWidth,height:i.naturalHeight})))};
 } catch(e) {
  result.error={type:e.name,message:e.message};
  result.observed_at=new Date().toISOString();
  result.elapsed_ms=await page.evaluate(()=>window.t179Timing ? performance.now()-window.t179Timing.start:null).catch(()=>null);
 }
 result.browser_errors=errors; result.browser_version=browser.version();
 result.start_event='actual teacher assembly button click';
 result.end_event='full actual preview/unsatisfied diagnostic, images decoded, two animation frames';
 result.measurement_source='same browser monotonic performance.now; startup/login/form entry outside timing';
 fs.writeFileSync(spec.output,JSON.stringify(result,null,2));
 await page.screenshot({path:spec.screenshot,fullPage:true});
 console.log(JSON.stringify({event:'result',...result}));
 await browser.close(); process.exitCode=result.passed?0:1;
})().catch(e=>{console.error(e.message);process.exitCode=2});
