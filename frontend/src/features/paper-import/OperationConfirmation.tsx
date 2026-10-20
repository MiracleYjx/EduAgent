import { Alert, Button, Card, Typography } from "antd";
import type { LearningOperation } from "../../api/learningClient";

export function OperationConfirmation({ value, busy, onConfirm }: {
  value: LearningOperation; busy: boolean; onConfirm: () => void;
}) {
  return <Card title="确认本次模型操作">
    <Typography.Paragraph>本次最多 {value.request_budget} 次请求，已尝试 {value.actual_requests} 次。
      费用{value.estimate_state === "known" ? "估算 "+value.estimated_cost : "估算未知"}；模型输出可能需要校正。</Typography.Paragraph>
    <ul>{value.steps.map((step, index) => <li key={index}>
      {String(step.task_kind)}：{String(step.model ?? step.model_id ?? "使用预览绑定的模型")}，预算 {String(step.request_budget)} 次
      {typeof step.scope_summary==="string"?<Typography.Paragraph>{step.scope_summary}</Typography.Paragraph>:null}
    </li>)}</ul>
    <Alert type="info" title="仅确认这里保存的范围和模型；失败不会自动重发。" />
    <Button type="primary" loading={busy} disabled={busy || value.state !== "prepared"} onClick={onConfirm}>
      确认本次范围并执行
    </Button>
    {value.state !== "prepared" ? <Typography.Paragraph>操作状态：{value.state}</Typography.Paragraph> : null}
  </Card>;
}
