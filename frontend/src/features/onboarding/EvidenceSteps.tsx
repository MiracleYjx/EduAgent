import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Space, Table, Tag, Typography } from "antd";
import type { ActivationView, DraftView, InitializationPreview, OperationView, ProbeView, ReadinessReport, RestartView, SettingsView } from "../../api/contracts";
import { getSubjects, LearningApiError } from "../../api/learningClient";

export function ProbeStep({ draft, probes, busy, onProbe, onCancel, onRead }: {
  draft: DraftView | null; probes: ProbeView[]; busy: boolean;
  onProbe: (profileId: string, capability: string) => void;
  onCancel: (id: string) => void; onRead: () => void;
}) {
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const binding = draft?.values.bindings?.default_llm;
  const profile = binding?.profile_id ? draft?.values.providers?.[binding.profile_id] : null;
  const running = probes.some(item => item.status === "queued" || item.status === "running");
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="warning" showIcon title="每次只检测一个能力，可能产生费用"
      description="默认文本结构化模型需 text 与 json_object 两个独立检测；各执行一次共至多两次外部请求，请分别确认。没有自动重试、降级或定时重测，取消也可能已计费。成功只证明这次候选能力，不代表业务或其他 AI 能力已就绪。" />
    {profile ? ["text", "json_object"].map(capability => <Space key={capability} wrap>
      <Checkbox disabled={busy || running} checked={Boolean(confirmed[capability])}
        onChange={event => setConfirmed(previous => ({ ...previous, [capability]: event.target.checked }))}>
        我确认本次 {capability} 检测可能收费（至多一次外部请求）
      </Checkbox>
      <Button disabled={busy || running || !confirmed[capability]} onClick={() => {
        setConfirmed(previous => ({ ...previous, [capability]: false }));
        onProbe(profile.profile_id, capability);
      }}>检测 {capability}</Button>
    </Space>) : <Typography.Paragraph>尚未配置默认 AI。可返回配置，或明确跳过 AI 后继续基础初始化。</Typography.Paragraph>}
    {probes.map(probe => <Alert key={probe.probe_id} type={probe.status === "succeeded" ? "success" : probe.status === "failed" ? "error" : "info"}
      showIcon title={`${probe.capability} · ${probe.status} · ${probe.result_code}`}
      description={<Space orientation="vertical" size={4}>
        <Typography.Text>{probe.message}</Typography.Text>
        <Typography.Text>检测时间：{probe.tested_at}；候选接纳期限：{probe.expires_at ?? "未接纳"}</Typography.Text>
        <Typography.Text>外部请求：{probe.external_requests ?? 0}；{probe.may_have_been_billed ? "可能已计费；取消不保证退款" : "无已计费事实"}</Typography.Text>
        {(probe.status === "running" || probe.status === "queued") ? <Button disabled={busy} onClick={() => onCancel(probe.probe_id)}>取消本次检测</Button> : null}
      </Space>} />)}
    <Button disabled={busy} onClick={onRead}>刷新检测与操作状态</Button>
    <Typography.Text type="secondary">已激活的接纳事实不因超过十分钟自动失效。刷新和重新进入只读取既有结果。</Typography.Text>
  </Space>;
}

export function InitializationStep({ draft, preview, operation, busy, onPreview, onApply, onRead }: {
  draft: DraftView | null; preview: InitializationPreview | null; operation: OperationView | null; busy: boolean;
  onPreview: () => void; onApply: () => void; onRead: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const running = operation?.status === "queued" || operation?.status === "running";
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" showIcon title="初始化不等于安装服务、激活或创建账号"
      description="仅在核对环境、目标和迁移预览后显式执行允许的非破坏性迁移；不会清库、创建业务账号或自动装配旧业务。初始化中断后先重新检查真实版本，不自动重放。" />
    <Button disabled={busy || !draft || running} onClick={() => { setConfirmed(false); onPreview(); }}>预览初始化目标</Button>
    {preview ? <>
      <Descriptions column={1} size="small" items={[
        { key: "environment", label: "环境", children: preview.environment },
        { key: "target", label: "目标身份", children: preview.target_identity },
        { key: "current", label: "当前迁移头", children: preview.current_head ?? "未知／尚未初始化（不代表连接成功）" },
        { key: "head", label: "目标迁移头", children: preview.target_head ?? "未知，禁止执行" },
        { key: "steps", label: "允许步骤", children: preview.steps?.join(" → ") ?? "无" },
        { key: "expires", label: "预览期限", children: preview.expires_at },
      ]} />
      {!preview.allowed || preview.destructive ? <Alert type="error" title="预览不允许执行，请先修复目标依赖或完成独立审查。" /> : null}
      <Checkbox checked={confirmed} disabled={busy || running} onChange={event => setConfirmed(event.target.checked)}>我已核对上述环境、数据库和迁移目标，确认仅执行本次允许变更</Checkbox>
      <Button type="primary" disabled={busy || running || !confirmed || !preview.allowed || preview.destructive} onClick={() => { setConfirmed(false); onApply(); }}>确认执行本次初始化</Button>
    </> : null}
    {operation ? <Alert type={operation.status === "succeeded" ? "success" : operation.status === "failed" ? "error" : "warning"}
      showIcon title={operation.status === "interrupted" ? "初始化已中断：请重新预览并核实实际版本" : `初始化操作：${operation.status}`}
      description={`${operation.error_code ?? "无安全错误码"}；操作 ID：${operation.operation_id}；步骤：${operation.steps?.join(" → ") ?? "未知"}`} /> : null}
    <Button disabled={busy} onClick={onRead}>刷新检测与操作状态</Button>
  </Space>;
}

export function CompletionStep({ draft, valid, settings, report, activation, restart, busy, onActivate, onRestart, onFinish }: {
  draft: DraftView | null; valid: boolean; settings: SettingsView; report: ReadinessReport;
  activation: ActivationView | null; restart: RestartView | null; busy: boolean;
  onActivate: () => void; onRestart: () => void; onFinish: () => void;
}) {
  const [activateConfirmed, setActivateConfirmed] = useState(false);
  const [restartConfirmed, setRestartConfirmed] = useState(false);
  const [personalStatus, setPersonalStatus] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setPersonalStatus(null);
    getSubjects(controller.signal).then(() => setPersonalStatus("ready")).catch(error => {
      if (!controller.signal.aborted) setPersonalStatus(error instanceof LearningApiError ? error.code : "NETWORK_ERROR");
    });
    return () => controller.abort();
  }, [settings.active_revision, settings.pending_revision, activation?.status]);
  const head = settings.pending_revision ?? settings.active_revision;
  const stale = Boolean(draft && draft.parent_revision !== (head ?? null));
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Alert type={report.business.ready ? "success" : "warning"} showIcon
      title={report.business.ready ? "Business Ready：业务已就绪" : "Business 尚未就绪，业务入口保持门禁"}
      description={`旧业务兼容状态：前置条件${report.business_prerequisites.ready ? "通过" : "未通过"}；装配：${report.business.assembly}；恢复：${report.business.recovery}。个人科目服务独立核验，不要求业务 JWT。`} />
    <Alert type={personalStatus === "ready" ? "success" : "info"}
      title={personalStatus === "ready" ? "个人科目服务可用" : `个人科目服务：${personalStatus ?? "正在核验"}`} />
    {settings.pending_revision || activation?.status === "pending_restart" ? <Alert type="warning" showIcon title="pending_restart：配置已接纳，等待重启"
      description="当前进程不切换数据库、目录或凭据；重启后仍须重新检查前置依赖、装配和原恢复阶段。不会自动重复付费检测。" /> : null}
    {activation?.status === "active" ? <Alert type="info" title="配置已激活；当前业务和 AI 状态仍以 readiness 为准" /> : null}
    <Descriptions size="small" column={1} items={[
      { key: "active", label: "当前 active 修订", children: settings.active_revision ?? "尚无" },
      { key: "pending", label: "待重启修订", children: settings.pending_revision ?? "无" },
      { key: "draft", label: "草稿修订", children: draft?.revision_id ?? "尚无" },
    ]} />
    <Checkbox checked={activateConfirmed} disabled={busy} onChange={event => setActivateConfirmed(event.target.checked)}>
      我确认激活此草稿；新增或变更的 AI 绑定须有匹配检测证据，核心配置变更可能等待重启
    </Checkbox>
    <Button type="primary" disabled={busy || !draft || !valid || stale || !activateConfirmed || activation !== null} onClick={() => { setActivateConfirmed(false); onActivate(); }}>确认激活配置</Button>
    {stale && !activation ? <Typography.Text type="warning">草稿基准已不是当前修订。请重新读取并显式另存候选，不能覆盖新的 active/pending。</Typography.Text> : null}
    <Checkbox checked={restartConfirmed} disabled={busy || !head} onChange={event => setRestartConfirmed(event.target.checked)}>我确认请求重启；只允许受控排空自有进程，不强制中止在途任务</Checkbox>
    <Button disabled={busy || !head || !restartConfirmed} onClick={() => { setRestartConfirmed(false); onRestart(); }}>请求重启或查看手工指引</Button>
    {restart ? <Alert type="info" title={restart.status} description={restart.reason} /> : null}
    <Typography.Title level={4}>逐任务 AI 能力（来自服务端，不保证长期在线）</Typography.Title>
    <Table size="small" pagination={false} rowKey="task" dataSource={Object.entries(report.ai?.tasks ?? {}).map(([task, fact]) => ({ task, ...fact }))}
      columns={[
        { title: "任务", dataIndex: "task" },
        { title: "就绪", dataIndex: "ready", render: value => <Tag>{value ? "可用" : "未就绪"}</Tag> },
        { title: "接纳", dataIndex: "activation_validation" },
        { title: "原因", dataIndex: "reason" },
        { title: "最近检测", dataIndex: "last_probe", render: value => value?.tested_at ?? "未检测／不适用" },
      ]} />
    <Button href="/">进入个人学习</Button>
    <Typography.Text type="secondary">Setup 令牌不授予课程、成绩或账号权限。无 AI 时，仅不依赖 AI 的原授权操作可以使用。</Typography.Text>
    <Button disabled={busy || personalStatus !== "ready"} onClick={onFinish}>记录向导完成</Button>
  </Space>;
}
