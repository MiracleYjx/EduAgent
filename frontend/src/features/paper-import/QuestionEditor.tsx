import { useState } from "react";
import { Alert, Button, Card, Input, Space } from "antd";
import type { ContentBlock } from "../../api/learningClient";
import type { PaperEdit } from "../../api/papersClient";
import { QuestionBlocks } from "../practice/QuestionContent";

type Asset = { id: string; label: string; source?: PaperEdit["source_locations"][number] };
function BlockEditor({ blocks, onChange, assets, mediaUrl, label }: {
  blocks: ContentBlock[]; onChange: (value: ContentBlock[]) => void;
  assets: Asset[]; mediaUrl: (id: string) => string; label: string;
}) {
  function replace(index: number, block: ContentBlock) {
    onChange(blocks.map((v,i) => i === index ? block : v));
  }
  return <fieldset><legend>{label}</legend>
    {blocks.map((block,index) => <Card key={index} size="small">
      {block.type === "text" ? <Input.TextArea aria-label={label+"文字 "+(index+1)} value={block.text}
        onChange={e => replace(index,{type:"text",text:e.target.value})} /> : <>
        <QuestionBlocks blocks={[block]} mediaUrl={mediaUrl} />
        {block.type === "image" || block.type === "original" ? <label>图像归属
          <select aria-label={label+"图像 "+(index+1)} value={block.asset_id} onChange={e =>
            replace(index,block.type==="original" ? {...block,asset_id:e.target.value,source:assets.find(a=>a.id===e.target.value)?.source??block.source} : {...block,asset_id:e.target.value})}>
            {assets.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select></label> : <Button onClick={() => replace(index,{type:"text",text:""})}>改用校正文字</Button>}
      </>}
      <Space>
        <Button disabled={index === 0} onClick={() => {
          const next=[...blocks]; [next[index-1],next[index]]=[next[index],next[index-1]];onChange(next);
        }}>上移内容</Button>
        <Button disabled={index === blocks.length-1} onClick={() => {
          const next=[...blocks]; [next[index],next[index+1]]=[next[index+1],next[index]];onChange(next);
        }}>下移内容</Button>
        <Button onClick={() => onChange(blocks.filter((_,i) => i !== index))}>移除此内容</Button>
      </Space>
    </Card>)}
    <Space><Button onClick={() => onChange([...blocks,{type:"text",text:""}])}>添加文字</Button>
      <Button disabled={!assets.length} onClick={() => onChange([...blocks,{type:"image",asset_id:assets[0].id,alt:"题目图片"}])}>添加原图</Button>
    </Space>
  </fieldset>;
}
export function QuestionEditor({ initial, assets, knowledge, sourceChoices, mediaUrl, busy, onSave, onCancel, allowNoSource = false }: {
  allowNoSource?: boolean; initial: PaperEdit; assets: Asset[]; knowledge: {id:string;name:string}[];
  sourceChoices?: PaperEdit["source_locations"];
  mediaUrl: (id:string)=>string; busy:boolean; onSave:(body:PaperEdit)=>void; onCancel:()=>void;
}) {
  const [value,setValue]=useState(initial);
  const patch=(change:Partial<PaperEdit>)=>setValue(old=>({...old,...change}));
  const blocks = value.stem_blocks as ContentBlock[];
  const valid=(allowNoSource || value.source_locations.length > 0) && blocks.length > 0 && blocks.every(b=>b.type !== "text" || b.text.trim())
    && (value.question_type === "short_answer" || (value.options?.length ?? 0) >= 2);
  return <form onSubmit={e=>{e.preventDefault();if(valid) onSave(value);}}>
    <fieldset disabled={busy}><Space orientation="vertical" style={{width:"100%"}} size={12}>
      <label>题型<select aria-label="题型" value={value.question_type} disabled={busy} onChange={e=>{
        const type=e.target.value as PaperEdit["question_type"];
        patch({question_type:type,options:type==="short_answer"?[]:value.options});
      }}><option value="single_choice">单选</option><option value="multiple_choice">多选</option>
        <option value="short_answer">文字主观题</option></select></label>
      <BlockEditor label="题干" blocks={blocks} assets={assets} mediaUrl={mediaUrl}
        onChange={stem_blocks=>patch({stem_blocks})} />
      {value.question_type !== "short_answer" ? <fieldset><legend>按原顺序校正选项</legend>
        {(value.options ?? []).map((option,index)=><div key={index}>
          <label>选项标识<Input aria-label={"选项标识 "+(index+1)} value={option.id} onChange={e=>patch({
            options:value.options?.map((o,i)=>i===index?{...o,id:e.target.value}:o),
          })} /></label>
          <BlockEditor label={"选项 "+(index+1)} blocks={option.blocks as ContentBlock[]} assets={assets} mediaUrl={mediaUrl}
            onChange={next=>patch({options:value.options?.map((o,i)=>i===index?{...o,blocks:next}:o)})} />
          <Button onClick={()=>patch({options:value.options?.filter((_,i)=>i!==index)})}>删除此选项</Button>
        </div>)}
        <Button onClick={()=>patch({options:[...(value.options ?? []),{id:"",blocks:[{type:"text",text:""}]}]})}>添加选项</Button>
      </fieldset>:null}
      <label>分值（原卷未提供时默认10分）<Input aria-label="题目分值" type="number" min={0.01} step={0.01}
        value={value.score ?? ""} onChange={e=>patch({score:e.target.value || null})} /></label>
      <label>难度（可选）<Input aria-label="题目难度" value={value.difficulty ?? ""}
        onChange={e=>patch({difficulty:e.target.value || null})} /></label>
      <label>关联知识点<select multiple aria-label="关联知识点" value={value.knowledge_point_ids ?? []}
        onChange={e=>patch({knowledge_point_ids:Array.from(e.target.selectedOptions,o=>o.value)})}>
        {knowledge.map(k=><option key={k.id} value={k.id}>{k.name}</option>)}
      </select></label>
      {sourceChoices ? <fieldset><legend>实际来源位置</legend>{sourceChoices.map((loc,index)=>{
        const key=JSON.stringify(loc);
        return <label key={index}><input type="checkbox" checked={value.source_locations.some(s=>JSON.stringify(s)===key)}
          onChange={e=>patch({source_locations:e.target.checked?[...value.source_locations,loc]:
            value.source_locations.filter(s=>JSON.stringify(s)!==key)})} />
          {loc.page?"第 "+loc.page+" 页／":""}{loc.anchor}</label>;
      })}</fieldset>:null}
      <label>待核对疑点（每行一项；对照原文后可修改）<Input.TextArea aria-label="待核对疑点"
        value={value.uncertainties?.join("\n") ?? ""} onChange={e=>patch({uncertainties:e.target.value.split("\n").filter(s=>s.trim())})} /></label>
      <details><summary>展开参考答案与解析（可选）</summary>
        <label>参考答案<Input.TextArea aria-label="参考答案" value={value.reference_answer ?? ""}
          onChange={e=>patch({reference_answer:e.target.value || null})} /></label>
        <label>评分要点<Input.TextArea aria-label="评分要点" value={value.scoring_rubric ?? ""}
          onChange={e=>patch({scoring_rubric:e.target.value || null})} /></label>
        <BlockEditor label="解析" blocks={(value.explanation_blocks ?? []) as ContentBlock[]}
          assets={assets} mediaUrl={mediaUrl} onChange={explanation_blocks=>patch({explanation_blocks})} />
      </details>
      <Alert type="info" title="保存校正会生成新的内容修订；保存和确认入库不等于通过练习准入。" />
      <Space><Button htmlType="submit" type="primary" disabled={!valid || busy} loading={busy}>保存本题校正</Button>
        <Button disabled={busy} onClick={onCancel}>取消校正</Button></Space>
    </Space></fieldset>
  </form>;
}
