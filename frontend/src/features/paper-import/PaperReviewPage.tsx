import { useEffect, useState } from "react";
import { Alert, Button, Card, Checkbox, Space, Spin, Tag, Typography } from "antd";
import { Link, useParams } from "react-router-dom";
import { confirmLearningOperation, getLearningOperation, LearningApiError, type LearningOperation } from "../../api/learningClient";
import { getKnowledge, materialAssetUrl, type KnowledgeView, type MaterialView } from "../../api/materialsClient";
import { confirmPaper, editPaper, extractPaper, processLocalOCR, getPaper, getPaperMaterial, previewExtraction,
  type Paper, type PaperEdit, type PaperQuestion } from "../../api/papersClient";
import { QuestionBlocks } from "../practice/QuestionContent";
import { SourceViewer } from "../materials/SourceViewer";
import { OperationConfirmation } from "./OperationConfirmation";
import { QuestionEditor } from "./QuestionEditor";

export function paperEdit(question: PaperQuestion): PaperEdit {
  return {
    question_type:question.question_type, stem_blocks:question.stem_blocks,
    options:question.options ?? [], explanation_blocks:question.explanation_blocks ?? [],
    expected_revision:question.revision, source_locations:question.source_locations,
    question_number:question.question_number, reference_answer:question.reference_answer,
    scoring_rubric:question.scoring_rubric, score:question.score, difficulty:question.difficulty,
    knowledge_point_ids:question.knowledge_point_ids ?? [], uncertainties:question.uncertainties ?? [],
    confirmed_asset_ids:question.confirmed_asset_ids ?? [],
  };
}
export function PaperReviewPage() {
  const { subjectId="", importId="" }=useParams();
  const [value,setValue]=useState<Paper|null>(null);
  const [material,setMaterial]=useState<MaterialView|null>(null);
  const [knowledge,setKnowledge]=useState<KnowledgeView|null>(null);
  const [error,setError]=useState<unknown>(null);
  const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState<string|null>(null);
  const [selected,setSelected]=useState<string[]>([]);
  const [acknowledge,setAcknowledge]=useState(false);
  const [operation,setOperation]=useState<LearningOperation|null>(null);
  const [refresh,setRefresh]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();
    setValue(null);setMaterial(null);setError(null);setEditing(null);setSelected([]);setOperation(null);
    getPaper(importId,controller.signal).then(async paper=>{
      if(paper.subject_id!==subjectId) throw new LearningApiError("PAPER_NOT_FOUND",404);
      const [source,points]=await Promise.all([
        getPaperMaterial(paper.document_id,controller.signal),getKnowledge(subjectId,controller.signal),
      ]);
      const saved=paper.extraction_operation_id&&!paper.questions.length?await getLearningOperation(paper.extraction_operation_id,controller.signal):null;
      if(!controller.signal.aborted){setValue(paper);setMaterial(source);setKnowledge(points);setOperation(saved);}
    }).catch(cause=>{if(!controller.signal.aborted)setError(cause);});
    return()=>controller.abort();
  },[subjectId,importId,refresh]);
  async function run(action:()=>Promise<void>){setBusy(true);setError(null);try{await action();}
    catch(cause){setError(cause);}finally{setBusy(false);}}
  function media(assetId:string){
    const asset=value?.assets.find(a=>a.id===assetId);
    return asset&&value ? materialAssetUrl(subjectId,value.document_id,asset.sha256) : "";
  }
  async function extract(){
    if(!value||!operation)return;
    await run(async()=>{
      await confirmLearningOperation(operation.id,{preview_digest:operation.preview_digest});
      const result=await extractPaper(value.id,operation.id);
      if("questions" in result){setValue(result);setOperation(null);}
      else{setOperation(result);throw new Error("抽取未完成："+result.state+"。已有原文和状态被保留，不会自动重试。");}
    });
  }
  return <section className="page-stack">
    <Link to={"/subjects/"+encodeURIComponent(subjectId)+"/materials"}>返回科目资料</Link>
    <Typography.Title level={2}>试卷预览与校正</Typography.Title>
    <Alert type="info" title="缺答案原样保存；选定题确认入库后，仍须满足练习准入。无需逐题提前查看答案。" />
    {error?<Alert type="error" title="试卷操作未完成" description={error instanceof Error?error.message:"请求失败"} />:null}
    {!value&&!error?<Spin />:null}
    <Button disabled={busy} onClick={()=>setRefresh(n=>n+1)}>刷新试卷状态</Button>
    {value?<><Typography.Paragraph>导入状态：{value.status}；共 {value.questions.length} 道暂存题。</Typography.Paragraph>
      {material?.requires_ocr ? <Button disabled={busy || value.status === "Parsing"} onClick={()=>run(async()=>{
        setValue(await processLocalOCR(value.id));
        setMaterial(await getPaperMaterial(value.document_id));
      })}>本地 OCR（不调用远程模型）</Button>:null}
      {!value.questions.length?<Button disabled={busy} onClick={()=>run(async()=>{
        setOperation(await previewExtraction(value.id));
      })}>预览抽题范围与费用</Button>:null}
      {operation?<OperationConfirmation value={operation} busy={busy} onConfirm={extract} />:null}
      {operation && ["failed","partial","outcome_unknown"].includes(operation.state)?<Card title="主动重新尝试">
        {operation.state==="outcome_unknown"?<Checkbox checked={acknowledge} disabled={busy} onChange={e=>setAcknowledge(e.target.checked)}>
          我知道上次结果未知，可能已经执行或收费；本次将是新的尝试
        </Checkbox>:null}
        <Button disabled={busy||(operation.state==="outcome_unknown"&&!acknowledge)} onClick={()=>run(async()=>{
          setOperation(await previewExtraction(value.id,operation.id,acknowledge));setAcknowledge(false);
        })}>创建新的费用预览</Button>
      </Card>:null}
      <Button type="primary" disabled={busy||!selected.length} loading={busy} onClick={()=>run(async()=>{
        setValue(await confirmPaper(value,selected));setSelected([]);
      })}>确认选定 {selected.length} 道题入库</Button>
      {value.questions.map((question,index)=><Card key={question.id} title={"原题 "+(question.question_number??String(index+1))}>
        <Space orientation="vertical" style={{width:"100%"}}>
          <Space><Tag>{question.question_type}</Tag><Tag>{question.status}</Tag>
            {question.reference_answer?null:<Tag color="warning">缺参考答案</Tag>}
            <Tag>答案来源：{question.answer_origin==="paper"?"原卷":question.answer_origin==="user_correction"?"用户校正":"未提供"}</Tag></Space>
          {(question.uncertainties?.length??0)>0?<Alert type="warning" title="识别疑点" description={question.uncertainties?.join("；")} />:null}
          <QuestionBlocks blocks={question.stem_blocks} mediaUrl={media} />
          <ol>{question.options?.map(option=><li key={option.id}>{option.id}<QuestionBlocks blocks={option.blocks} mediaUrl={media} /></li>)}</ol>
          <Typography.Paragraph type="secondary">真实来源：{question.source_locations.map(loc=>
            loc.page?"第 "+loc.page+" 页（"+(loc.anchor??"")+"）":loc.anchor).join("；")}</Typography.Paragraph>
          {question.question_id?<Link to={"/subjects/"+encodeURIComponent(subjectId)+"/question-bank"}>已入库，打开题库管理</Link>:<>
            <Checkbox disabled={busy} checked={selected.includes(question.id)} onChange={e=>setSelected(old=>
              e.target.checked?[...old,question.id]:old.filter(q=>q!==question.id))}>选择此题入库</Checkbox>
            <Button disabled={busy} onClick={()=>setEditing(question.id)}>校正本题</Button>
          </>}
          {editing===question.id?<QuestionEditor key={question.id+":"+question.revision}
            initial={paperEdit(question)} assets={value.assets.map(a=>({id:a.id,source:a.source,label:
              (a.source.page?"第 "+a.source.page+" 页／":"")+a.source.anchor}))}
            knowledge={knowledge?.knowledge_points.filter(k=>k.confirmation_state==="confirmed")??[]}
            sourceChoices={material?.blocks?.map(b=>({document_id:value.document_id,page:b.page,anchor:b.anchor}))}
            mediaUrl={media} busy={busy} onCancel={()=>setEditing(null)} onSave={body=>run(async()=>{
              setValue(await editPaper(value.id,question.id,body));setEditing(null);
            })} />:null}
        </Space>
      </Card>)}
    </>:null}
    {material?<SourceViewer subjectId={subjectId} material={material} />:null}
  </section>;
}
