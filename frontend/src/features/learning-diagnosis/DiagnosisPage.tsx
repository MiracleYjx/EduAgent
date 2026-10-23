import { useEffect, useState } from "react";
import { Alert, Button, Card, Space, Spin, Typography } from "antd";
import { Link } from "react-router-dom";
import { getDiagnosis, type DiagnosisFilters, type DiagnosisStatistics, type PersonalDiagnosis } from "../../api/diagnosisClient";
import { getMaterial, type MaterialView } from "../../api/materialsClient";
import { SourceViewer } from "../materials/SourceViewer";
import { TeacherPanel } from "../teacher/TeacherPanel";

const rate = (value: string | number | null | undefined) => value == null ? "无有效样本" : `${(Number(value)*100).toFixed(2)}%`;
const status = {insufficient_evidence:"证据不足", reinforce:"建议强化", observed:"已有表现记录"};

function Statistics({value}:{value:DiagnosisStatistics}) {
  return <><p>练习 {value.practice_count} 次；有效已答 {value.valid_count}；问题排除 {value.excluded_count}；未答 {value.unanswered_count}</p>
    <table aria-label="关联题表现"><thead><tr><th>样本范围</th><th>样本数</th><th>平均得分率</th><th>已答选择题正确率</th></tr></thead>
      <tbody>{([["累计首次",value.first],["重练",value.repractice],["最近20道首次",value.recent],["此前最多20道首次",value.previous]] as const).map(([name,v])=><tr key={name}>
        <td>{name}</td><td>{v.count}</td><td>{rate(v.score_rate)}</td><td>{rate(v.choice_correct_rate)}（{v.choice_count}题）</td></tr>)}</tbody></table>
    <p>{status[value.status]}；反复出错 {value.repeated_wrong_question_ids.length} 道不同题。</p>
    {Object.entries(value.exclusion_counts).map(([reason,count])=><span key={reason}>{reason}：{count}；</span>)}</>;
}

export function DiagnosisPage({subjectId}:{subjectId:string}) {
  const [filters,setFilters]=useState<DiagnosisFilters>({source_kind:"",answer_origin:""});
  const [value,setValue]=useState<PersonalDiagnosis|null>(null);
  const [error,setError]=useState<unknown>(null);
  const [refresh,setRefresh]=useState(0);
  const [material,setMaterial]=useState<MaterialView|null>(null);
  const [location,setLocation]=useState("");
  const evidence=value?.evidence??[];
  useEffect(()=>{
    const controller=new AbortController();setValue(null);setError(null);setMaterial(null);
    getDiagnosis(subjectId,filters,controller.signal).then(setValue).catch(e=>{if(!controller.signal.aborted)setError(e);});
    return ()=>controller.abort();
  },[subjectId,filters,refresh]);
  async function open(documentId:string, anchor:string) {
    try { const result=await getMaterial(subjectId,documentId);setMaterial(result);setLocation(anchor); }
    catch(e){setError(e);}
  }
  return <section className="page-stack" aria-label="学习诊断">
    <Typography.Title level={3}>学习诊断</Typography.Title>
    <Alert type="info" title="关联题表现，不是精确掌握度" description="仅统计已作答且评分有效、无未解决争议的记录。每题等权；多知识点题不是对各知识点的独立测量。未答和评分失败不当作薄弱证据；重评不新增作答，也不刷新原提交时间。至少5道不同题且近期均值低于60%才建议强化。" />
    <Space><label>题目来源<select aria-label="诊断题目来源" value={filters.source_kind} onChange={e=>setFilters(f=>({...f,source_kind:e.target.value as DiagnosisFilters["source_kind"]}))}>
      <option value="">全部来源</option><option value="uploaded">上传原题</option><option value="ai">AI生成</option></select></label>
      <label>答案来源<select aria-label="诊断答案来源" value={filters.answer_origin} onChange={e=>setFilters(f=>({...f,answer_origin:e.target.value}))}>
        <option value="">全部答案</option><option value="paper">原卷</option><option value="ai_completion">AI补充</option><option value="user_correction">用户校正</option><option value="ai_generated">AI生成</option></select></label>
      <Button onClick={()=>setRefresh(n=>n+1)}>刷新诊断</Button></Space>
    {error?<Alert type="error" title="诊断或资料读取失败" description={error instanceof Error?error.message:"请求失败"} />:null}
    {!value&&!error?<Spin />:null}
    {value?<><Card title="科目学习与练习历史"><Statistics value={value.statistics} /></Card>
      {value.knowledge_points.map(point=><Card key={point.id} title={`${point.name} · 关联题表现`}>
        <Statistics value={point.statistics} /><p>{point.suggestion.message}</p>
        {point.suggestion.sources.map(source=><div key={source.chunk_id}><p>{source.title}：{JSON.stringify(source.location)}</p><blockquote>{source.excerpt}</blockquote>
          <Button onClick={()=>open(source.document_id,JSON.stringify(source.location))}>打开真实资料位置</Button></div>)}
        <Link to={`/subjects/${encodeURIComponent(subjectId)}/practice?knowledge_point=${encodeURIComponent(point.id)}&mode=${point.suggestion.practice_conditions.mode}`}>调整并预览针对性练习</Link>
      </Card>)}
      <Card title="可追溯作答记录">{evidence.length===0?<p>暂无已提交作答记录。</p>:evidence.slice().reverse().map(item=><p key={item.answer_id}>
        <Link to={`/subjects/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(item.practice_id)}`}>查看练习与评分依据</Link> · {item.source_kind==="ai"?"AI生成":"上传原题"}／{item.answer_origin??"来源未记载"} · {new Date(item.submitted_at).toLocaleString()} · {item.exclusion??`${item.score} / ${item.max_score}`} · 修订 {item.revision_id}
      </p>)}</Card></>:null}
    {material?<><p>定位：{location}</p><SourceViewer subjectId={subjectId} material={material} /></>:null}
    <TeacherPanel subjectId={subjectId} />
  </section>;
}
