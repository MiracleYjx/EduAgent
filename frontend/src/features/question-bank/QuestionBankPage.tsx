import { useEffect, useState } from "react";
import { Alert, Button, Card, Input, Space, Tag, Typography } from "antd";
import { confirmLearningOperation, type LearningOperation } from "../../api/learningClient";
import { getKnowledge, type KnowledgeView } from "../../api/materialsClient";
import { completeAnswer, getQuestion, listDuplicates, listQuestions, previewAnswer, questionAssetUrl,
  resolveDuplicate, reviseQuestion, type Duplicate, type PaperEdit, type Question } from "../../api/papersClient";
import { OperationConfirmation } from "../paper-import/OperationConfirmation";
import { QuestionEditor } from "../paper-import/QuestionEditor";
import { QuestionBlocks } from "../practice/QuestionContent";

const admission:Record<string,string>={incomplete:"待补全",needs_validation:"待核验",eligible:"可用于练习",needs_correction:"待校正"};
const answerSource:Record<string,string>={paper:"原卷",user_correction:"用户校正",ai_completion:"AI 补充"};
function Contents({ value }: {value:Question}) {
  return <><QuestionBlocks blocks={value.stem_blocks} mediaUrl={a=>questionAssetUrl(value.id,a)} />
    <ol>{value.options?.map(o=><li key={o.id}>{o.id}<QuestionBlocks blocks={o.blocks} mediaUrl={a=>questionAssetUrl(value.id,a)} /></li>)}</ol></>;
}
function DuplicatePanel({ value,onDone }: {value:Duplicate;onDone:()=>void}) {
  const [questions,setQuestions]=useState<Question[]|null>(null);
  const [error,setError]=useState<unknown>(null);
  const [busy,setBusy]=useState(false);
  async function open(){setError(null);try{setQuestions(await Promise.all([getQuestion(value.left_id),getQuestion(value.right_id)]));}
    catch(cause){setError(cause);}}
  async function resolve(decision:"same"|"different"){
    setBusy(true);setError(null);
    try{await resolveDuplicate(value,decision);onDone();}catch(cause){setError(cause);}finally{setBusy(false);}
  }
  return <Card title="疑似重复题（尚未合并）">
    <Typography.Paragraph>差异：{value.differences.join("、")||"当前题面一致"}。文字相同不证明是同题；请核对图片、选项和关键条件。</Typography.Paragraph>
    <Button disabled={busy} onClick={open}>展开双方内容</Button>
    {questions?<><div className="status-grid">{questions.map(q=><Card key={q.id} title={q.id}>
      <Contents value={q} /><Typography.Paragraph>来源 {q.source_kind==="uploaded"?"上传试卷":"AI 生成"}，修订 {q.revision}</Typography.Paragraph>
      <Typography.Paragraph>来源位置：{q.source_locations.map(s=>s.document_id+"／"+(s.page??s.anchor)).join("；")}</Typography.Paragraph>
    </Card>)}</div>
    <Space><Button disabled={busy} onClick={()=>resolve("same")}>确认同题并保留双方历史</Button>
      <Button disabled={busy} onClick={()=>resolve("different")}>确认不同题</Button></Space>
    </>:null}
    {error?<Alert type="error" title="同题裁决未完成" description={error instanceof Error?error.message:"请求失败"} />:null}
  </Card>;
}
export function QuestionBankPage({ subjectId }: {subjectId:string}) {
  const [rows,setRows]=useState<Question[]|null>(null);
  const [duplicates,setDuplicates]=useState<Duplicate[]>([]);
  const [knowledge,setKnowledge]=useState<KnowledgeView|null>(null);
  const [filters,setFilters]=useState({source_kind:"",admission_state:"",difficulty:"",knowledge_point_ids:""});
  const [error,setError]=useState<unknown>(null);
  const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState<string|null>(null);
  const [operation,setOperation]=useState<{question:string;value:LearningOperation}|null>(null);
  const [refresh,setRefresh]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();
    setError(null);setRows(null);setEditing(null);setOperation(null);
    Promise.all([listQuestions(subjectId,filters,controller.signal),listDuplicates(subjectId,controller.signal),getKnowledge(subjectId,controller.signal)])
      .then(([questions,pairs,points])=>{if(!controller.signal.aborted){setRows(questions);setDuplicates(pairs);setKnowledge(points);}})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause);});
    return()=>controller.abort();
  },[subjectId,filters,refresh]);
  async function run(action:()=>Promise<void>){setBusy(true);setError(null);try{await action();}
    catch(cause){setError(cause);}finally{setBusy(false);}}
  function edit(value:Question):PaperEdit {return {
    question_type:value.question_type,stem_blocks:value.stem_blocks,options:value.options??[],
    explanation_blocks:value.explanation_blocks??[],reference_answer:value.reference_answer,
    scoring_rubric:value.scoring_rubric,score:value.default_score,difficulty:value.difficulty,
    expected_revision:value.revision,source_locations:value.source_locations,
    knowledge_point_ids:value.knowledge_point_ids,uncertainties:value.uncertainties??[],
  };}
  async function confirm(){
    if(!operation)return;
    await run(async()=>{
      await confirmLearningOperation(operation.value.id,{preview_digest:operation.value.preview_digest});
      const result=await completeAnswer(operation.question,operation.value.id);
      if("revision_id" in result){setOperation(null);setRefresh(n=>n+1);}
      else{setOperation({question:operation.question,value:result});throw new Error("操作未完成："+result.state+"；不会自动重发。");}
    });
  }
  return <section className="page-stack">
    <Typography.Title level={3}>题库</Typography.Title>
    <Typography.Paragraph>未准入题仍可管理。补答案和复核需要主动确认费用；难度由你校正，不随正确率自动改变。</Typography.Paragraph>
    <Space wrap>
      <label>题目来源<select aria-label="题目来源" disabled={busy} value={filters.source_kind} onChange={e=>setFilters(f=>({...f,source_kind:e.target.value}))}>
        <option value="">全部来源</option><option value="uploaded">上传试卷</option><option value="ai">AI 生成</option></select></label>
      <label>练习准入<select aria-label="练习准入" disabled={busy} value={filters.admission_state} onChange={e=>setFilters(f=>({...f,admission_state:e.target.value}))}>
        <option value="">全部状态</option>{Object.entries(admission).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
      <label>难度筛选<Input aria-label="难度筛选" disabled={busy} value={filters.difficulty} onChange={e=>setFilters(f=>({...f,difficulty:e.target.value}))} /></label>
      <label>知识点筛选<select aria-label="知识点筛选" disabled={busy} value={filters.knowledge_point_ids}
        onChange={e=>setFilters(f=>({...f,knowledge_point_ids:e.target.value}))}>
        <option value="">全部知识点</option>{knowledge?.knowledge_points.filter(k=>k.confirmation_state==="confirmed").map(k=><option key={k.id} value={k.id}>{k.name}</option>)}
      </select></label>
      <Button disabled={busy} onClick={()=>setRefresh(n=>n+1)}>刷新题库</Button>
    </Space>
    {error?<Alert type="error" title="题库操作未完成" description={error instanceof Error?error.message:"请求失败"} />:null}
    {operation?<OperationConfirmation value={operation.value} busy={busy} onConfirm={confirm} />:null}
    {rows?.length===0?<Alert type="info" title="当前条件下没有题目" />:null}
    {rows?.map(value=><Card key={value.id}>
      <Space wrap><Tag>{value.source_kind==="uploaded"?"来源：上传试卷":"来源：AI 生成"}</Tag>
        <Tag>参考答案：{value.answer_origin?answerSource[value.answer_origin]??value.answer_origin:"未提供"}</Tag>
        <Tag color={value.admission_state==="eligible"?"green":"warning"}>{admission[value.admission_state]??value.admission_state}</Tag>
        <Tag>难度：{value.difficulty??"未标注"}</Tag><Tag>分值：{value.default_score}</Tag></Space>
      <Contents value={value} />
      {value.canonical_id!==value.id?<Typography.Paragraph>共享练习身份：{value.canonical_id}（原来源和历史仍保留）</Typography.Paragraph>:null}
      {(value.issues?.length??0)>0?<Alert type="warning" title="待处理问题" description={value.issues?.join("；")} />:null}
      <Typography.Paragraph type="secondary">真实来源：{value.source_locations.map(loc=>loc.document_id+"／"+(loc.page?"第 "+loc.page+" 页":loc.anchor)).join("；")||"AI 生成；未冒充上传原卷"}</Typography.Paragraph>
      <details><summary>查看参考答案与解析</summary><p>{value.reference_answer??"原卷未提供"}</p><p>{value.scoring_rubric}</p>
        <QuestionBlocks blocks={value.explanation_blocks??[]} mediaUrl={a=>questionAssetUrl(value.id,a)} /></details>
      <Space><Button disabled={busy} onClick={()=>setEditing(value.id)}>校正题目与难度</Button>
        <Button disabled={busy} onClick={()=>run(async()=>setOperation({question:value.id,value:await previewAnswer(value,value.reference_answer?"review":"complete")}))}>
          {value.reference_answer?"主动复核准入（费用预览）":"主动补充答案（费用预览）"}
        </Button></Space>
      {editing===value.id?<QuestionEditor allowNoSource={value.source_kind==="ai"} key={value.id+":"+value.revision} initial={edit(value)}
        assets={value.stem_blocks.concat(...(value.options??[]).map(o=>o.blocks),value.explanation_blocks??[])
          .flatMap(b=>b.type==="image"||b.type==="original"?[{id:b.asset_id,label:b.asset_id,source:value.asset_sources?.[b.asset_id]}]:[])}
        knowledge={knowledge?.knowledge_points.filter(k=>k.confirmation_state==="confirmed")??[]}
        mediaUrl={a=>questionAssetUrl(value.id,a)} busy={busy} onCancel={()=>setEditing(null)}
        onSave={body=>run(async()=>{await reviseQuestion(value.id,body);setRefresh(n=>n+1);})} />:null}
    </Card>)}
    {duplicates.map(pair=><DuplicatePanel key={pair.id} value={pair} onDone={()=>setRefresh(n=>n+1)} />)}
  </section>;
}
