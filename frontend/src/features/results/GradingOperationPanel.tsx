import { Alert, Button, Card, Space, Typography } from "antd";
import type { LearningOperation } from "../../api/learningClient";

const states: Record<string,string> = {
  prepared:"已生成预览，尚未确认费用", confirmed:"已确认，待执行",
  running:"正在执行；中断后请人工核对", succeeded:"执行成功，结果已保存",
  failed:"执行失败", partial:"部分执行完成", outcome_unknown:"结果未知，需要人工核对",
};

export function GradingOperationPanel({value,busy,onConfirm,onExecute,onRefresh,onCancel,uncertain=false}: {
  value:LearningOperation; busy:boolean; onConfirm:()=>void; onExecute:()=>void;
  onRefresh:()=>void; onCancel:()=>void;
  uncertain?:boolean;
}) {
  const terminal=["succeeded","failed","partial","outcome_unknown"].includes(value.state);
  return <Card title="评分费用与调用确认" aria-label="评分费用操作">
    <Space orientation="vertical" style={{width:"100%"}}>
      <Typography.Text strong>{uncertain?"上次读取的状态：":""}{states[value.state]??value.state}</Typography.Text>
      {uncertain?<Alert type="warning" title="请求连接中断，服务端结果尚未核实。请先读取操作状态，不要另建操作补发。" />:null}
      <Typography.Text>操作 ID：{value.id}</Typography.Text>
      <Typography.Paragraph>本次最多 {value.request_budget} 次远程请求，已登记发送尝试 {value.actual_requests} 次。
        {value.estimate_state==="known"?`费用估算 ${value.estimated_cost}。`:"费用无法可靠估算。"}
        估算不是供应商账单硬上限；未提供的 Token 或金额预算保持未知。</Typography.Paragraph>
      <ul>{value.steps.map((step,index)=><li key={String(step.step_id??index)}>
        {String(step.scope_summary??step.task_kind)}
        <div>模型：{JSON.stringify(step.task_models??{[String(step.task_kind)]:step.model})}</div>
      </li>)}</ul>
      <Alert type="info" title="不会自动重试或补发；结果未知时请先核对供应商记录。页面刷新只读取状态。" />
      <Space wrap>
        {value.state==="prepared"?<>
          <Button disabled={busy||uncertain} onClick={onCancel}>取消费用确认</Button>
          <Button type="primary" loading={busy} disabled={uncertain} onClick={onConfirm}>确认本次费用与范围</Button>
        </>:null}
        {value.state==="confirmed"?<Button type="primary" loading={busy} disabled={uncertain} onClick={onExecute}>执行已确认评分</Button>:null}
        {value.state==="confirmed"&&value.actual_requests===0?<Button disabled={busy||uncertain} onClick={onCancel}>重新选择并预览</Button>:null}
        {terminal?<Button disabled={busy||uncertain} onClick={onExecute}>同步已保存结果（不补发）</Button>:null}
        <Button disabled={busy} onClick={onRefresh}>读取操作状态</Button>
      </Space>
    </Space>
  </Card>;
}
