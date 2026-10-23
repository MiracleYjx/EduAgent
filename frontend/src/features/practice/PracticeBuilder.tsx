import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Space, Tag, Typography } from "antd";
import { useNavigate, useSearchParams } from "react-router-dom";
import { contentAssetUrl, confirmLearningOperation, getLearningOperation, isLearningOperation, LearningApiError, type LearningOperation } from "../../api/learningClient";
import { getKnowledge, listMaterials, listSeries, type KnowledgeView, type MaterialSeriesView, type MaterialView } from "../../api/materialsClient";
import {
  changedPreview, defaultPracticeConditions, executeGeneration, getPractice, getPracticePreview,
  previewGeneration, previewPractice, savePracticeAnswer, startPractice, submitPractice,
  previewGrading, executeGrading,
  type PersonalPractice, type PracticeConditions, type PracticePreview,
} from "../../api/practiceClient";
import { OperationConfirmation } from "../paper-import/OperationConfirmation";
import { AnswerControls, answerState, type AnswerDraft } from "./AnswerControls";
import { SubmitPracticeDialog } from "./SubmitPracticeDialog";
import { QuestionContent } from "./QuestionContent";
import { GradingOperationPanel } from "../results/GradingOperationPanel";

export function PracticeBuilder({subjectId}:{subjectId:string}) {
  const [params,setParams]=useSearchParams();
  const navigate=useNavigate();
  const internalRoute=useRef<string|null>(null);
  const [conditions,setConditions]=useState<PracticeConditions>(()=>{
    const points=params.getAll("knowledge_point");
    return {...defaultPracticeConditions,...(points.length?{knowledge_point_ids:points,mode:params.get("mode")==="weak"?"weak" as const:"balanced" as const}:{})};
  });
  const [knowledge,setKnowledge]=useState<KnowledgeView|null>(null);
  const [materials,setMaterials]=useState<MaterialView[]>([]);
  const [series,setSeries]=useState<MaterialSeriesView[]>([]);
  const [preview,setPreview]=useState<PracticePreview|null>(null);
  const [practice,setPractice]=useState<PersonalPractice|null>(null);
  const [operation,setOperation]=useState<LearningOperation|null>(null);
  const [gradingOperation,setGradingOperation]=useState<LearningOperation|null>(null);
  const [gradingUncertain,setGradingUncertain]=useState(false);
  const [grounding,setGrounding]=useState<"uploaded_material"|"general_knowledge">("uploaded_material");
  const [versions,setVersions]=useState<string[]>([]);
  const [scores,setScores]=useState<Record<string,string>>({});
  const [drafts,setDrafts]=useState<Record<string,AnswerDraft>>({});
  const [busy,setBusy]=useState(Boolean(params.get("preview")||params.get("practice")));
  const [error,setError]=useState<unknown>(null);
  const [notice,setNotice]=useState("");
  const [submitOpen,setSubmitOpen]=useState(false);
  const [submitting,setSubmitting]=useState(false);
  const previewId=params.get("preview"),practiceId=params.get("practice");
  const patch=(change:Partial<PracticeConditions>)=>{
    setConditions(old=>({...old,...change}));setPreview(null);setOperation(null);setScores({});
  };
  useEffect(()=>{
    let active=true;
    setError(null);setPractice(null);setPreview(null);setOperation(null);setGradingOperation(null);setGradingUncertain(false);
    Promise.all([getKnowledge(subjectId),listMaterials(subjectId),listSeries(subjectId)]).then(([k,m,s])=>{
      if(active){setKnowledge(k);setMaterials(m);setSeries(s);}
    }).catch(e=>{if(active)setError(e);});
    return ()=>{active=false;};
  },[subjectId]);
  useEffect(()=>{
    const route=practiceId?"practice:"+practiceId:previewId?"preview:"+previewId:null;
    if(internalRoute.current===route && route!==null){internalRoute.current=null;return;}
    internalRoute.current=null;
    let active=true;
    if(practiceId){
      if(practice?.id===practiceId)return;
      setBusy(true);
      getPractice(practiceId).then(async p=>{
        if(p.subject_id!==subjectId)throw new LearningApiError("PRACTICE_SCOPE_INVALID",409);
        if(active){setPractice(p);setDrafts(Object.fromEntries(p.items.map(item=>[item.id,
          item.content.question_type==="short_answer"?{kind:"text",text_answer:item.answer.text_answer??""}:
             {kind:"choices",choice_ids:item.answer.choice_ids??[]}])));}
        if(p.grading_operation_id){
          const op=await getLearningOperation(p.grading_operation_id);
          if(active)setGradingOperation(op);
        }
      }).catch(e=>{if(active)setError(e);}).finally(()=>{if(active)setBusy(false);});
    } else if(previewId) {
      if(preview?.id===previewId)return;
      setBusy(true);
      getPracticePreview(previewId).then(async p=>{
        if(p.subject_id!==subjectId)throw new LearningApiError("PRACTICE_SCOPE_INVALID",409);
        if(active){setPreview(p);setConditions(p.conditions);setScores({});
          if(p.generation_grounding){setGrounding(p.generation_grounding);setVersions(p.generation_material_version_ids??[]);}}
        if(p.generation_operation_id){
          const op=await getLearningOperation(p.generation_operation_id);
          if(active)setOperation(op);
        }
      }).catch(e=>{if(active)setError(e);}).finally(()=>{if(active)setBusy(false);});
    }
    return ()=>{active=false;};
  },[subjectId,previewId,practiceId]);

  async function run(action:()=>Promise<void>){
    setBusy(true);setError(null);setNotice("");
    try{await action();}
    catch(e){
      const changed=changedPreview(e);
      if(changed){setPreview(changed.current_preview);setScores({});setOperation(null);
        setNotice("候选已变化：新增 "+changed.added_question_ids.length+"、移除 "+changed.removed_question_ids.length+
          "、修订变化 "+changed.changed_question_ids.length+"。请核对新预览后重新确认。");}
      else setError(e);
    } finally{setBusy(false);}
  }
  async function prepare(next=conditions){
    const p=await previewPractice(subjectId,next);
    if(p.subject_id!==subjectId)throw new LearningApiError("PRACTICE_SCOPE_INVALID",409);
    setPreview(p);setConditions(p.conditions);setOperation(null);setScores({});
    internalRoute.current="preview:"+p.id;
    setParams({preview:p.id},{replace:true});
    return p;
  }
  async function requestAI(){
    const next={...conditions,source:conditions.source==="uploaded"?"mixed" as const:conditions.source};
    const p=preview??await prepare(next);
    const chosen=p.conditions.source==="uploaded"?await prepare(next):p;
    setOperation(await previewGeneration(chosen.id,{
      grounding,material_version_ids:grounding==="uploaded_material"?versions:[],idempotency_key:crypto.randomUUID(),
    }));
  }
  function draftFor(item:PersonalPractice["items"][number]):AnswerDraft {
    return drafts[item.id]??(item.content.question_type==="short_answer"?{kind:"text",text_answer:""}:{kind:"choices",choice_ids:[]});
  }
  async function submitAll(blankConfirmed:boolean) {
    if(!practice) return;
    setSubmitting(true);setError(null);setNotice("");
    try{
      for(const item of practice.items){
        const draft=draftFor(item);
        await savePracticeAnswer(practice.id,item.id,draft.kind==="text"?{text_answer:draft.text_answer}:
          {choice_ids:draft.choice_ids});
      }
      const result=await submitPractice(practice.id,blankConfirmed);
      setSubmitOpen(false);
      navigate(`/subjects/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(result.practice_id)}`);
    }catch(e){
      if(e instanceof LearningApiError && e.code==="PRACTICE_BLANK_ANSWERS"){
        setSubmitOpen(true);setNotice("检测到未填写的文字主观题，请确认继续或返回填写。");
      }else if(e instanceof LearningApiError && e.code==="GRADING_OPERATION_REQUIRED"){
        try{
          setGradingOperation(await previewGrading(practice.id,blankConfirmed,crypto.randomUUID()));
          setGradingUncertain(false);
          setSubmitOpen(false);
        }catch(cause){setError(cause);}
      }else setError(e);
    }finally{setSubmitting(false);}
  }
  if(practice){
    const gradingLocked=gradingUncertain||Boolean(gradingOperation&&gradingOperation.state!=="prepared");
    const submissionItems=practice.items.map(item=>({id:item.id,label:`第 ${item.position} 题`,questionType:item.content.question_type,draft:draftFor(item)}));
    return <section className="page-stack" aria-label="当前练习">
      <Typography.Title level={3}>当前练习</Typography.Title>
      <Typography.Paragraph>题目修订、顺序、知识点和分值已固定。答案按题保存为草稿。</Typography.Paragraph>
      {error?<Alert type="error" title={error instanceof Error?error.message:"保存失败"} />:null}
      {gradingOperation?<GradingOperationPanel value={gradingOperation} busy={busy||submitting} uncertain={gradingUncertain}
        onCancel={()=>setGradingOperation(null)}
        onRefresh={()=>void run(async()=>{
          setGradingOperation(await getLearningOperation(gradingOperation.id));setGradingUncertain(false);
        })}
        onConfirm={()=>void run(async()=>{
          try{setGradingOperation(await confirmLearningOperation(gradingOperation.id,{preview_digest:gradingOperation.preview_digest}));}
          catch(e){if(e instanceof LearningApiError&&e.code==="NETWORK_ERROR")setGradingUncertain(true);throw e;}
        })}
        onExecute={()=>void run(async()=>{
          let result;
          try{result=await executeGrading(practice.id,gradingOperation.id);}
          catch(e){if(e instanceof LearningApiError&&e.code==="NETWORK_ERROR")setGradingUncertain(true);throw e;}
          if(isLearningOperation(result)){
            setGradingOperation(result);setPractice(await getPractice(practice.id));
          }else navigate(`/subjects/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(result.practice_id)}`);
        })} />:null}
      <Space wrap><Button disabled={busy||submitting} onClick={()=>{setPractice(null);setDrafts({});setParams({},{replace:true});setPreview(null);setOperation(null);}}>准备另一组练习</Button>
        {practice.state!=="in_progress"?<Button onClick={()=>navigate(`/subjects/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(practice.id)}`)}>查看评分结果</Button>:null}</Space>
      {practice.items.map(item=><Card key={item.id} title={"第 "+item.position+" 题 · "+item.score+" 分"}>
        <Space><Tag>{item.source_kind==="uploaded"?"上传原题":"AI 题"}</Tag>
          {item.source_kind==="ai"?<Tag>{item.grounding==="general_knowledge"?"通用知识":"上传资料依据"}</Tag>:null}</Space>
        <QuestionContent content={item.content} mediaUrl={asset=>contentAssetUrl(subjectId,practice.id,item.id,asset)} />
        <AnswerControls questionType={item.content.question_type} options={item.content.options}
          value={drafts[item.id]??(item.content.question_type==="short_answer"?{kind:"text",text_answer:""}:{kind:"choices",choice_ids:[]})}
          onChange={draft=>{setGradingOperation(null);setDrafts(old=>({...old,[item.id]:draft}));}} disabled={busy||submitting||gradingLocked||practice.state!=="in_progress"} />
        <Button disabled={busy||submitting||gradingLocked||practice.state!=="in_progress"} onClick={()=>run(async()=>{
          const draft=drafts[item.id];
          const saved=await savePracticeAnswer(practice.id,item.id,draft?.kind==="text"?{text_answer:draft.text_answer}:
            {choice_ids:draft?.choice_ids??[]});
          setPractice(old=>old?{...old,items:old.items.map(i=>i.id===item.id?{...i,answer:saved}:i)}:old);
          setNotice("第 "+item.position+" 题草稿已保存："+(saved.answer_state==="answered"?"已作答":"未作答"));
        })}>保存第 {item.position} 题草稿</Button>
      </Card>)}
      {notice?<Alert type="success" title={notice} />:null}
      <Space wrap>
        <Button type="primary" loading={submitting} disabled={busy||gradingLocked||practice.state!=="in_progress"}
          onClick={()=>{
            const blank=submissionItems.some(item=>item.questionType==="short_answer" && answerState(item.draft)==="unanswered");
            if(blank)setSubmitOpen(true); else void submitAll(false);
          }}>整组提交并评分</Button>
        <Typography.Text type="secondary">整组提交后才展示解析；未填写的文字主观题会先由你确认。</Typography.Text>
      </Space>
      <SubmitPracticeDialog open={submitOpen} items={submissionItems} submitting={submitting}
        onCancel={()=>setSubmitOpen(false)} onConfirm={()=>{setSubmitOpen(false);void submitAll(true);}} />
    </section>;
  }
  return <section className="page-stack" aria-label="准备练习">
    <Typography.Title level={3}>准备练习</Typography.Title>
    <Card><form onSubmit={e=>{e.preventDefault();run(async()=>{await prepare();});}}>
      <fieldset disabled={busy}><Space orientation="vertical" style={{width:"100%"}} size={12}>
        <label>出题来源<select aria-label="出题来源" value={conditions.source} onChange={e=>patch({source:e.target.value as PracticeConditions["source"]})}>
          <option value="mixed">真题优先</option><option value="uploaded">全真题</option><option value="ai">全 AI</option>
        </select></label>
        <label>练习模式<select aria-label="练习模式" value={conditions.mode} onChange={e=>patch({mode:e.target.value as PracticeConditions["mode"]})}>
          <option value="balanced">均衡覆盖</option><option value="random">随机练习</option><option value="weak">薄弱强化</option>
        </select></label>
        <label>题量<Input aria-label="练习题量" type="number" min={1} step={1} value={conditions.count}
          onChange={e=>patch({count:Number(e.target.value)})} /></label>
        <label>难度（可选）<Input aria-label="练习难度" value={conditions.difficulty??""} onChange={e=>patch({difficulty:e.target.value||null})} /></label>
        <label>知识点范围<select multiple aria-label="练习知识点" value={conditions.knowledge_point_ids}
          onChange={e=>patch({knowledge_point_ids:Array.from(e.target.selectedOptions,o=>o.value)})}>
          {knowledge?.knowledge_points.filter(k=>k.confirmation_state==="confirmed").map(k=><option key={k.id} value={k.id}>{k.name}</option>)}
        </select></label>
        <label>题型<select multiple aria-label="练习题型" value={conditions.question_types} onChange={e=>patch({
          question_types:Array.from(e.target.selectedOptions,o=>o.value) as PracticeConditions["question_types"]})}>
          <option value="single_choice">单选</option><option value="multiple_choice">多选</option><option value="short_answer">文字主观题</option>
        </select></label>
        <label>原题来源（可选）<select multiple aria-label="原题文件范围" value={conditions.source_document_ids}
          onChange={e=>patch({source_document_ids:Array.from(e.target.selectedOptions,o=>o.value)})}>
          {materials.filter(m=>m.purpose==="exam_paper").map(m=><option key={m.id} value={m.id}>{m.original_filename}</option>)}
        </select></label>
        <label><input type="checkbox" checked={conditions.prefer_unseen} onChange={e=>patch({prefer_unseen:e.target.checked})} />优先未做题</label>
        <label><input type="checkbox" checked={conditions.allow_practiced} onChange={e=>patch({allow_practiced:e.target.checked})} />允许使用已做题</label>
        <Button htmlType="submit" type="primary" loading={busy} disabled={!Number.isInteger(conditions.count)||conditions.count<1||!conditions.question_types?.length}>预览候选与覆盖</Button>
      </Space></fieldset>
    </form></Card>
    {error?<Alert type="error" title="练习操作未完成" description={error instanceof Error?error.message:"请求失败"} />:null}
    {notice?<Alert type="warning" title={notice} />:null}
    {preview?<Card title="实际候选与覆盖">
      <Typography.Paragraph>符合条件 {preview.eligible_count} 题：上传原题 {preview.uploaded_count}、AI {preview.ai_count}；
        未做 {preview.unseen_count}、已做 {preview.practiced_count}；待核验排除 {preview.excluded_count}。</Typography.Paragraph>
      <Typography.Paragraph>请求 {preview.conditions.count} 题，本次可选 {preview.selected_count} 题。
        候选题不足 {preview.candidate_shortage} 题；未做题不足 {preview.unseen_shortage} 题。</Typography.Paragraph>
      <Typography.Paragraph>其他题 {preview.other_count}、原错题 {preview.original_wrong_count}。
        薄弱知识点 {preview.weak_knowledge_point_ids.length} 个；无充分有效样本时不推断薄弱。</Typography.Paragraph>
      <ul>{Object.entries(preview.coverage).map(([id,count])=><li key={id}>
        {knowledge?.knowledge_points.find(k=>k.id===id)?.name??id}：实际覆盖 {count} 题
      </li>)}</ul>
      {!preview.can_start?<Alert type="warning" title="数量不足，请明确选择处理方式；不会自动重练、放宽条件或补 AI。" />:null}
      <Space wrap>
        <Button disabled={busy||preview.selected_count===0||preview.can_start} onClick={()=>run(async()=>{
          await prepare({...conditions,count:preview.selected_count});
        })}>减少到 {preview.selected_count} 题</Button>
        <Button disabled={busy||Boolean(conditions.allow_practiced)} onClick={()=>run(async()=>{
          await prepare({...conditions,allow_practiced:true});
        })}>允许重练并重新预览</Button>
        <Button disabled={busy} onClick={()=>{setPreview(null);setOperation(null);setParams({},{replace:true});}}>调整筛选条件</Button>
      </Space>
      {preview.items.map((item,i)=><label key={item.question_id}>第 {i+1} 题分值
        <Input aria-label={"第 "+(i+1)+" 题分值"} type="number" min={0.01} step={0.01} disabled={busy}
          value={scores[item.question_id]??item.default_score} onChange={e=>setScores(old=>({...old,[item.question_id]:e.target.value}))} />
      </label>)}
      <Button type="primary" disabled={busy||!preview.can_start} onClick={()=>run(async()=>{
        const value=await startPractice(preview.id,{preview_digest:preview.preview_digest,scores});
        setPractice(value);setDrafts({});internalRoute.current="practice:"+value.id;setParams({practice:value.id},{replace:true});
      })}>确认题量、分值并开始练习</Button>
    </Card>:null}
    <Card title="主动请求 AI 出题">
      <Typography.Paragraph>全真题不会自动补 AI。点击请求后如需补充，将明确改为真题优先；
        先预览实际模型、发送范围和费用，再由你确认。生成结果仍须通过准入。</Typography.Paragraph>
      <label>AI 出题依据<select aria-label="AI 出题依据" disabled={busy} value={grounding} onChange={e=>{setGrounding(e.target.value as typeof grounding);setOperation(null);}}>
        <option value="uploaded_material">所选上传资料</option><option value="general_knowledge">通用知识（无教材引用）</option>
      </select></label>
      {grounding==="uploaded_material"?<label>资料版本<select multiple aria-label="出题资料版本" value={versions}
        disabled={busy} onChange={e=>{setVersions(Array.from(e.target.selectedOptions,o=>o.value));setOperation(null);}}>
        {series.flatMap(s=>s.versions.map(v=><option key={v.id} value={v.id}>{s.name} · {v.label}{v.is_default?"（默认）":""}</option>))}
      </select></label>:null}
      <Button disabled={busy||Boolean(preview?.can_start)||grounding==="uploaded_material"&&!versions.length} onClick={()=>run(requestAI)}>请求 AI 补充并预览费用</Button>
      {operation?<OperationConfirmation value={operation} busy={busy} onConfirm={()=>run(async()=>{
        if(!preview)throw new LearningApiError("PREVIEW_REQUIRED",409);
        const confirmed=await confirmLearningOperation(operation.id,{preview_digest:operation.preview_digest});
        setOperation(confirmed);
        const result=await executeGeneration(preview.id,operation.id);
        if("selected_count" in result){setPreview(result);setOperation(null);}
        else {setOperation(result);setNotice("生成未完成："+result.state+"。不会自动重发。");}
      })} />:null}
    </Card>
  </section>;
}
