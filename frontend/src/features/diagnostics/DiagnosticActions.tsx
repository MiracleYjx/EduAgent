import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Space, Typography } from "antd";
import * as api from "../../api/client";
import type { DiagnosticsReport, DraftView, ProbeView, ReadinessReport } from "../../api/contracts";
import { useSettingsSession } from "../../auth/SettingsSession";
import { ProbePanel } from "./ProbePanel";

export function DiagnosticActions({ onReport }: { onReport: (report: ReadinessReport) => void }) {
  const session = useSettingsSession();
  const [busy, setBusy] = useState(false); const lock = useRef(false); const mounted = useRef(true);
  const [error, setError] = useState("");
  const [diagnostics, setDiagnostics] = useState<DiagnosticsReport | null>(null);
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [probes, setProbes] = useState<ProbeView[]>([]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const running = probes.some(item => item.status === "queued" || item.status === "running");
  async function run(action: (token: string) => Promise<void>) {
    if (lock.current || !session.token) return;
    lock.current = true; setBusy(true); setError("");
    try { await action(session.token); }
    catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof api.SettingsApiError) {
        if (cause.status === 401 || cause.status === 403) session.expire(cause);
        else setError(`${cause.code}：${cause.message}`);
      } else setError("DIAGNOSTICS_FAILED：未完成，请先核对已保存事实。");
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  return <Card title="显式诊断与脱敏导出"><Space orientation="vertical" size={16} style={{ width: "100%" }}>
    {error ? <Alert type="error" showIcon title={error} /> : null}
    <Space wrap>
      <Button disabled={busy} onClick={() => { void run(async token => { const result = await api.getReadiness(token); if (mounted.current) onReport(result); }); }}>刷新就绪事实</Button>
      <Button disabled={busy} onClick={() => { void run(async token => { const result = await api.runDependencyDiagnostics(token); if (mounted.current) setDiagnostics(result); }); }}>运行依赖诊断</Button>
      <Button disabled={busy} onClick={() => { void run(async token => {
        const blob = await api.getDiagnosticsExport(token); if (!mounted.current) return;
        const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
        try { anchor.href = url; anchor.download = "eduagent-settings-export.json"; anchor.click(); }
        finally { URL.revokeObjectURL(url); }
      }); }}>下载服务端脱敏诊断</Button>
    </Space>
    <Typography.Text type="secondary">导出只使用服务端允许的配置、就绪及版本字段，不拼接页面、输入或 SDK 原始响应。</Typography.Text>
    {diagnostics ? <Alert type={diagnostics.status === "ok" ? "success" : "warning"} showIcon title={`依赖诊断：${diagnostics.status} · ${diagnostics.evaluated_at}`}
      description={<Space orientation="vertical">{Object.entries(diagnostics.checks).map(([name, check]) => <Typography.Text key={name}>{name}：{check.status} · {check.code ?? "无错误码"} · {check.detail ?? "无补充信息"}</Typography.Text>)}</Space>} /> : null}
    <Typography.Paragraph>能力测试使用当前保存配置的独立候选副本。创建副本不调用模型、不激活配置；每次实际检测需另行确认费用。设置中心尚未保存的编辑不在此副本内。</Typography.Paragraph>
    <Button disabled={busy || running} onClick={() => { void run(async token => {
      const settings = await api.getSettingsView(token);
      if (!settings.values) throw new api.SettingsApiError("请先在设置中心保存有效配置。", { code: "CONFIGURATION_MISSING", status: 422 });
      const candidate = await api.createDraft(token, { base_revision: settings.pending_revision ?? settings.active_revision, changes: settings.values, acknowledge_override: false });
      if (mounted.current) { setDraft(candidate); setProbes([]); }
    }); }}>创建当前配置的检测副本</Button>
    <ProbePanel draft={draft} probes={probes} busy={busy}
      onProbe={(profile_id, capability) => { void run(async token => {
        const result = await api.createProbe(token, { draft_revision: draft!.revision_id, profile_id, capability, allow_billable: true, timeout_seconds: 15 });
        if (mounted.current) setProbes(previous => [...previous, result]);
      }); }}
      onRead={id => { void run(async token => { const next = await api.getProbe(token, id); if (mounted.current) setProbes(previous => previous.map(item => item.probe_id === next.probe_id ? next : item)); }); }}
      onCancel={id => { void run(async token => { const next = await api.cancelProbe(token, id); if (mounted.current) setProbes(previous => previous.map(item => item.probe_id === next.probe_id ? next : item)); }); }} />
  </Space></Card>;
}
