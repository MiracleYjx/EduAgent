import { Button, Popconfirm, Space } from "antd";
import type { Conversation } from "../../api/teacherClient";

export function ConversationList({values,busy,onSelect,onCreate,onDelete}:{values:Conversation[];busy:boolean;
  onSelect:(id:string)=>void;onCreate:()=>void;onDelete:(id:string)=>void}) {
  return <Space orientation="vertical"><Button disabled={busy} onClick={onCreate}>新建科目对话</Button>
    {values.length===0?<p>暂无科目对话。</p>:values.map(row=><Space key={row.id}>
      <Button disabled={busy} onClick={()=>onSelect(row.id)}>{row.title} · {new Date(row.created_at).toLocaleString()}</Button>
      <Popconfirm title="删除此对话？" description="只删除对话消息，正式成绩和共享资料保留。" onConfirm={()=>onDelete(row.id)} okText="删除对话" cancelText="保留">
        <Button disabled={busy} danger>删除此对话</Button></Popconfirm>
    </Space>)}
  </Space>;
}
