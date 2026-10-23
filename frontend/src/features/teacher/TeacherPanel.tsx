import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Space, Typography } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import * as client from "../../api/teacherClient";
import { getLearningOperation, type LearningOperation } from "../../api/learningClient";
import { getMaterial, listSeries, type MaterialSeriesView, type MaterialView } from "../../api/materialsClient";
import { ConversationList } from "./ConversationList";
import { SourceViewer } from "../materials/SourceViewer";

const labels:Record<string,string>={prepared:"已生成预览",cancelled:"已取消，未执行",confirmed:"已确认待执行",running:"正在执行；不会自动补发",
  succeeded:"执行成功",failed:"执行失败",outcome_unknown:"结果未知；可能已产生费用，需要人工核对",partial:"部分失败"};

export function TeacherPanel({subjectId}:{subjectId:string}) {
  const [params,setParams]=useSearchParams();
  const cid=params.get("conversation");const item=params.get("teacher_item");
  const [values,setValues]=useState<client.Conversation[]>([]);
  const [value,setValue]=useState<client.ConversationView|null>(null);
  const [operation,setOperation]=useState<LearningOperation|null>(null);
  const [needsConfirmation,setNeedsConfirmation]=useState(true);
  const [text,setText]=useState("");
  const [mode,setMode]=useState<"uploaded_material"|"general_knowledge">("uploaded_material");
  const [series,setSeries]=useState<MaterialSeriesView[]>([]);
  const [versions,setVersions]=useState<string[]>([]);
  const [material,setMaterial]=useState<MaterialView|null>(null);
  const [location,setLocation]=useState("");
  const [busy,setBusy]=useState(false);
  const [uncertain,setUncertain]=useState(false);
  const [error,setError]=useState<unknown>(null);
  const key=useRef<string|null>(null);
  useEffect(()=>{let active=true;setValues([]);setValue(null);setOperation(null);setError(null);setUncertain(false);
    client.listConversations(subjectId).then(v=>{if(active)setValues(v);}).catch(e=>{if(active)setError(e);});
    listSeries(subjectId).then(v=>{if(active)setSeries(v);}).catch(e=>{if(active)setError(e);});
    return ()=>{active=false;};},[subjectId]);
  async function load(identity:string) {
    const next=await client.getConversation(subjectId,identity);
    const opId=next.pending_operation_id??next.messages.at(-1)?.operation_id;
    const saved=opId?await getLearningOperation(opId):null;
    setValue(next);setOperation(saved);setNeedsConfirmation(saved?.state==="prepared");setUncertain(false);key.current=null;
  }
  useEffect(()=>{let active=true;setValue(null);setOperation(null);
    if(cid) client.getConversation(subjectId,cid).then(async next=>{
      const opId=next.pending_operation_id??next.messages.at(-1)?.operation_id;
      const saved=opId?await getLearningOperation(opId):null;
      if(active){setValue(next);setOperation(saved);setNeedsConfirmation(saved?.state==="prepared");setUncertain(false);key.current=null;}
    }).catch(e=>{if(active)setError(e);});
    return ()=>{active=false;};},[subjectId,cid]);
  async function run(action:()=>Promise<void>) {
    setBusy(true);setError(null);
    try{await action();}catch(e){setError(e);setUncertain(true);}finally{setBusy(false);}
  }
  const select=(identity:string)=>{const next=new URLSearchParams(params);next.set("conversation",identity);setParams(next);};
  function apply(turn:client.TeacherTurn){setValue(turn.conversation);setOperation(turn.operation);setNeedsConfirmation(turn.requires_confirmation);}
  async function send() {
    if(!value)return;
    key.current??=crypto.randomUUID();
    const turn=await client.previewMessage(subjectId,value.id,{text,idempotency_key:key.current,grounding:mode,
      retrieval_mode:mode==="general_knowledge"?"none":"keyword_only",material_version_ids:mode==="general_knowledge"?[]:versions,
      ...(item?{practice_item_id:item}:{})});
    apply(turn);
    if(!turn.requires_confirmation&&turn.operation.state==="prepared"){
      const done=await client.executeMessage(subjectId,value.id,turn.operation.id);apply(done);
    }
    key.current=null;
  }
  const pending=Boolean(value?.pending_operation_id);
  const interruptedIntent=pending&&operation?.state==="confirmed"&&value?.messages.some(m=>m.operation_id===operation.id&&m.state==="running");
  const canExecute=pending&&operation?.state==="confirmed"&&!uncertain&&!interruptedIntent;
  const operationLabel=operation?.state==="prepared"
    ? value?.messages.some(m=>m.operation_id===operation.id&&m.state==="cancelled")?"已取消预览，未执行"
      :needsConfirmation?"已生成预览；尚未确认本会话费用条件":"已生成预览；沿用已确认费用条件"
    :labels[operation?.state??""]??operation?.state;
  async function open(documentId:string,anchor:string){const m=await getMaterial(subjectId,documentId);setMaterial(m);setLocation(anchor);}
  return <section className="page-stack" aria-label="科目AI教师">
    <Typography.Title level={3}>科目 AI 教师</Typography.Title>
    <Alert type="info" title="主动发送才会请求模型，聊天不修改正式成绩" description="新会话首次发送须确认费用条件；后续仍为收费操作，每次发送单独登记有限预算。模型或费用条件变化需再次确认。无法可靠估算的费用保持未知，不是账单硬上限。刷新、追问按钮和打开对话不会自动发送。" />
    <ConversationList values={values} busy={busy} onSelect={select} onCreate={()=>run(async()=>{
      const created=await client.createConversation(subjectId);setValues(old=>[created,...old]);select(created.id);
    })} onDelete={identity=>run(async()=>{await client.deleteConversation(subjectId,identity);setValues(old=>old.filter(v=>v.id!==identity));
      if(cid===identity){const next=new URLSearchParams(params);next.delete("conversation");setParams(next);setValue(null);setOperation(null);}
    })} />
    {error?<Alert type="error" title="教师操作未完成" description={error instanceof Error?error.message:"请求失败"} />:null}
    {value?<><h4>{value.title}</h4><Button disabled={busy} onClick={()=>run(()=>load(value.id))}>只读刷新对话与操作</Button>
      {item?<Alert type="info" title="当前带入已提交题目" description={`${item}；仅使用服务端允许读取的作答和评分事实。`} />:null}
      {value.messages.map(message=><Card key={message.id} title={message.role==="user"?"你的消息":"AI 教师"}>
        <Typography.Paragraph style={{whiteSpace:"pre-wrap"}}>{message.text||labels[message.state]||message.state}</Typography.Paragraph>
        <p>{labels[message.state]??message.state} · 操作 {message.operation_id} · 快照 {message.runtime_snapshot_id??"未运行"}</p>
        {message.error_code?<Alert type="error" title={message.error_code} />:null}
        {message.reply?<><p>回答依据：{message.reply.basis==="model_knowledge"?"模型知识补充；没有上传资料依据":message.reply.basis==="mixed"?"上传资料与模型知识混合":"以下实际提供的上传资料"}</p>
          {(message.reply.uncertainties??[]).map((s,i)=><p key={i}>不确定项：{s}</p>)}
          {(message.citations??[]).map(source=><div key={source.id}><p>{source.title} · {JSON.stringify(source.location)}</p>
            {source.document_id?<Button onClick={()=>run(()=>open(source.document_id!,JSON.stringify(source.location)))}>打开教师引用原文</Button>:null}
            {source.practice_id?<Link to={`/subjects/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(source.practice_id)}`}>打开本条携带的题目与评分历史</Link>:null}</div>)}
          {(message.reply.practice_knowledge_point_ids?.length??0)>0?<Link to={`/subjects/${encodeURIComponent(subjectId)}/practice?${new URLSearchParams([
            ...message.reply.practice_knowledge_point_ids!.map(point=>["knowledge_point",point]),["mode","balanced"]])}`}>调整并预览教师建议练习</Link>:null}
        </>:null}
      </Card>)}
      <label>答疑依据<select aria-label="教师答疑依据" value={mode} disabled={busy||pending||uncertain} onChange={e=>{setMode(e.target.value as typeof mode);setVersions([]);key.current=null;}}>
        <option value="uploaded_material">当前科目资料（明确采用关键词检索）</option><option value="general_knowledge">通用知识（不检索教材）</option></select></label>
      {mode==="uploaded_material"?<label>资料版本（未选时使用当前默认资料）<select multiple aria-label="教师资料版本" disabled={busy||pending||uncertain} value={versions} onChange={e=>setVersions(Array.from(e.target.selectedOptions,o=>o.value))}>
        {series.flatMap(s=>s.versions.map(v=><option key={v.id} value={v.id}>{s.name} · {v.label}</option>))}</select></label>:null}
      <Input.TextArea aria-label="教师消息" value={text} maxLength={4000} disabled={busy||pending||uncertain} onChange={e=>{setText(e.target.value);key.current=null;}} />
      <Space>{["请继续解释原因","请换一个例子","请用更简单的方式解释"].map(prompt=><Button key={prompt} disabled={busy||pending||uncertain} onClick={()=>{setText(prompt);key.current=null;}}>{prompt}</Button>)}</Space>
      <Button disabled={busy||pending||uncertain||!text.trim()} onClick={()=>run(send)}>发送（收费）</Button>
    </>:null}
    {operation?<Card title="本条消息的费用与执行状态"><p>{operationLabel}</p>
      {interruptedIntent?<Alert type="warning" title="执行登记中断，需要人工核对；不会自动补发。" />:null}
      <p>操作 {operation.id}；请求预算 {operation.request_budget} 次，已尝试 {operation.actual_requests} 次；费用估算未知。</p>
      {operation.steps.map(step=><p key={String(step.step_id)}>{String(step.model)} · {String(step.scope_summary??"")}</p>)}
      {pending&&operation.state==="prepared"&&needsConfirmation?<Button disabled={busy||uncertain} onClick={()=>run(async()=>{
        const confirmed=await client.confirmMessage(subjectId,value!.id,operation.id,operation.preview_digest);setOperation(confirmed);setNeedsConfirmation(false);
      })}>确认本会话当前费用条件</Button>:null}
      {canExecute?<Button disabled={busy} onClick={()=>run(async()=>apply(await client.executeMessage(subjectId,value!.id,operation.id)))}>执行本条已确认消息</Button>:null}
      {pending&&["prepared","confirmed"].includes(operation.state)&&operation.actual_requests===0?<Button disabled={busy||uncertain} onClick={()=>run(async()=>{
        setValue(await client.cancelMessage(subjectId,value!.id,operation.id));setOperation(null);key.current=null;
      })}>取消本条预览</Button>:null}
      {pending&&["succeeded","failed","partial"].includes(operation.state)?<Button disabled={busy||uncertain} onClick={()=>run(async()=>apply(await client.executeMessage(subjectId,value!.id,operation.id)))}>同步已保存结果（不会补发）</Button>:null}
      {uncertain?<Alert type="warning" title="网络结果尚未核实，请只读刷新；不会自动重试或认定未收费。" />:null}
    </Card>:null}
    {material?<><p>教师引用位置：{location}</p><SourceViewer subjectId={subjectId} material={material} /></>:null}
  </section>;
}
