import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Space, Typography } from "antd";
import type { DraftView, ProbeView } from "../../api/contracts";

export function ProbePanel({ draft, probes, busy, dirty = false, disabled = false, onProbe, onRead, onCancel }: {
  draft: DraftView | null; probes: ProbeView[]; busy: boolean; dirty?: boolean; disabled?: boolean;
  onProbe: (profile: string, capability: string) => void; onRead: (id: string) => void; onCancel: (id: string) => void;
}) {
  const [profileId, setProfileId] = useState("");
  const [capability, setCapability] = useState("");
  const [billable, setBillable] = useState(false);
  useEffect(() => { setBillable(false); }, [profileId, capability, draft?.revision_id]);
  const selected = draft?.values.providers?.[profileId];
  const running = probes.some(item => item.status === "queued" || item.status === "running");
  return (
    <fieldset className="credential-fieldset"><legend>已保存候选的独立能力检测</legend>
      <Space orientation="vertical" size={12} style={{ width: "100%" }}>
        <Alert type="warning" showIcon title="每次能力检测可能收费" description="至多一次外部请求，不自动重试或重测。取消可能已计费；成功只证明本次候选能力，保存和激活不会在此按钮之外自动测试。" />
        <div className="wizard-form-grid"><div className="wizard-field"><label htmlFor="probe-profile">检测模型</label>
          <select id="probe-profile" value={profileId} disabled={busy || running || dirty} onChange={event => { setProfileId(event.target.value); setCapability(""); }}>
            <option value="">选择已保存模型</option>{Object.values(draft?.values.providers ?? {}).filter(item => item.enabled).map(item => <option key={item.profile_id} value={item.profile_id}>{item.display_name}</option>)}
          </select></div><div className="wizard-field"><label htmlFor="probe-capability">检测能力</label>
          <select id="probe-capability" disabled={busy || running || dirty} value={capability} onChange={event => setCapability(event.target.value)}>
            <option value="">选择单项能力</option>{selected?.declared_capabilities.map(item => <option key={item} value={item}>{item}</option>)}
          </select></div></div>
        <Checkbox checked={billable} disabled={busy || running} onChange={event => setBillable(event.target.checked)}>确认本次能力检测可能产生费用</Checkbox>
        <Button disabled={busy || running || dirty || !draft || !selected || !capability || !billable || disabled} onClick={() => {
          setBillable(false); onProbe(profileId, capability);
        }}>单独检测所选能力</Button>
        {probes.map(probe => <Alert key={probe.probe_id} type={probe.status === "failed" ? "error" : probe.status === "succeeded" ? "success" : "info"} showIcon
          title={`${probe.capability} · ${probe.status} · ${probe.result_code}`} description={<Space orientation="vertical">
            <Typography.Text>{probe.message}</Typography.Text>
            <Typography.Text>关联编号：{probe.correlation_id}；外部请求：{probe.external_requests ?? 0}；{probe.retryable ? "允许手动重试，仍需重新确认费用" : "不自动重试"}{probe.retry_after ? `；建议等待 ${probe.retry_after} 秒` : ""}</Typography.Text>
            {probe.result_code === "UNKNOWN_PROVIDER_ERROR" ? <Typography.Text>原因未确定，不推测为密码错误。</Typography.Text> : null}<Typography.Text>检测：{probe.tested_at}；候选有效期：{probe.expires_at ?? "无"}；{probe.may_have_been_billed ? "可能已计费" : "无已计费事实"}</Typography.Text>
            {probe.status === "running" || probe.status === "queued" ? <Space><Button disabled={busy} onClick={() => { onRead(probe.probe_id); }}>刷新本次检测</Button>
              <Button disabled={busy} onClick={() => { onCancel(probe.probe_id); }}>取消本次检测</Button></Space> : null}
          </Space>} />)}
      </Space>
    </fieldset>
  );
}
