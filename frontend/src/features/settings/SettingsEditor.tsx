import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Skeleton, Space, Tabs, Typography } from "antd";
import * as api from "../../api/client";
import type { ConfigurationValues, DraftView, ProbeView, ProviderCatalogEntry, SettingsView, ValidationReport } from "../../api/contracts";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";
import { EnvironmentSettings } from "./EnvironmentSettings";
import { MaintenanceSettings } from "./MaintenanceSettings";
import { IndexIdentitySettings } from "./IndexIdentitySettings";
import { CredentialSettings } from "./CredentialSettings";
import { ProbePanel } from "../diagnostics/ProbePanel";
import { ModelSettings } from "./ModelSettings";

export function SettingsEditor({ settings, onFactsChanged }: { settings: SettingsView; onFactsChanged?: () => void }) {
  const catalog = useSessionResource(api.getProviderCatalog);
  if (catalog.loading) return <Skeleton active />;
  if (catalog.error || !catalog.data) return <Alert type="error" showIcon title="无法读取模型 Registry" action={<Button onClick={catalog.reload}>重试读取</Button>} />;
  return <CandidateEditor initial={settings} catalog={catalog.data} onFactsChanged={onFactsChanged} />;
}
export function CandidateEditor({ initial, catalog, onFactsChanged }: { initial: SettingsView; catalog: ProviderCatalogEntry[]; onFactsChanged?: () => void }) {
  const session = useSettingsSession();
  const [settings, setSettings] = useState(initial);
  const [values, setValues] = useState<ConfigurationValues>(initial.pending_values ?? initial.values ?? {});
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [probes, setProbes] = useState<ProbeView[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<api.SettingsApiError | null>(null);
  const [message, setMessage] = useState("");
  const [acknowledge, setAcknowledge] = useState(false);
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const conflict = error?.status === 409;
  const overridden = Object.values(settings.sources ?? {}).some(source => source.source === "process_env");
  const running = probes.some(item => item.status === "queued" || item.status === "running");
  async function run(action: (token: string) => Promise<void>) {
    if (lock.current || !session.token) return;
    lock.current = true; setBusy(true); setError(null); setMessage("");
    try { await action(session.token); }
    catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof api.SettingsApiError) {
        if (cause.status === 401 || cause.status === 403) session.expire(cause);
        else setError(cause);
      } else setError(new api.SettingsApiError("操作未完成，请先重新读取服务端事实。", { status: 0, code: "OPERATION_FAILED" }));
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  function edit(next: ConfigurationValues) { setValues(next); setDirty(true); setValidation(null); }
  function accept(saved: DraftView) {
    setDraft(saved); setValues(saved.values); setDirty(false); setValidation(null); setProbes([]);
  }
  return <Card className="settings-editor"><Space orientation="vertical" size={20} style={{ width: "100%" }}>
    <Typography.Text>当前生效：{settings.active_revision ?? "未配置"}；待重启：{settings.pending_revision ?? "无"}</Typography.Text>
    <Typography.Text>候选草稿：{draft?.revision_id ?? "未保存"}；{dirty ? "有未保存修改" : "没有未保存修改"}</Typography.Text>
    {message ? <Alert type="info" showIcon title={message} /> : null}
    {error ? <Alert type="error" showIcon title={`${error.code}：${error.message}`} description={conflict ? "其他窗口或进程已更新配置。请重载当前事实后重新编辑，不会自动覆盖。" : "草稿和原生效配置保持；失败不触发自动重试或模型替换。"} /> : null}
    <Checkbox checked={acknowledge} disabled={busy} onChange={event => setAcknowledge(event.target.checked)}>我知晓外部环境可能覆盖保存值；保存不修改环境变量</Checkbox>
    <Space wrap>
      <Button type="primary" disabled={busy || running || conflict || (overridden && !acknowledge)} onClick={() => { void run(async token => {
        const saved = await api.createDraft(token, { base_revision: settings.pending_revision ?? settings.active_revision, changes: values, acknowledge_override: acknowledge });
        if (!mounted.current) return; accept(saved); setMessage("草稿已保存；校验、能力检测与激活尚未执行。");
      }); }}>保存候选草稿</Button>
      <Button disabled={busy || !draft || dirty || conflict || running} onClick={() => { void run(async token => {
        const result = await api.validateDraft(token, draft!.revision_id, { expected_revision: draft!.parent_revision });
        if (mounted.current) setValidation(result);
      }); }}>校验已保存草稿</Button>
      <Button disabled={busy || !draft || dirty || !validation?.valid || conflict || running} onClick={() => { void run(async token => {
        const result = await api.activateDraft(token, draft!.revision_id, { expected_active_revision: draft!.parent_revision, probe_ids: probes.filter(item => item.status === "succeeded").map(item => item.probe_id), acknowledge_override: acknowledge });
        if (!mounted.current) return;
        setMessage(result.status === "pending_restart" ? "已保存为待重启修订；当前进程仍使用原配置。" : "服务端已激活此修订。");
        const fresh = await api.getSettingsView(token); if (mounted.current) { setSettings(fresh); setValidation(null); onFactsChanged?.(); }
      }); }}>显式激活草稿</Button>
      <Button disabled={busy || running} onClick={() => { void run(async token => {
        const fresh = await api.getSettingsView(token); if (!mounted.current) return;
        setSettings(fresh); setValues(fresh.pending_values ?? fresh.values ?? {}); setDraft(null); setDirty(false); setValidation(null); setProbes([]);
        setMessage("已重新加载当前配置；未保存编辑已丢弃，请重新核对。");
      }); }}>丢弃编辑并重载当前配置</Button>
    </Space>
    {validation ? <Alert type={validation.valid ? "success" : "error"} showIcon title={validation.valid ? "草稿校验通过（尚未激活）" : "草稿校验未通过"}
      description={validation.errors.map(item => `${item.field}：${item.code} · ${item.message}`).join("；")} /> : null}
    <Tabs destroyOnHidden defaultActiveKey="models" items={[
      { key: "models", label: "模型与任务", children: (<ModelSettings values={values} catalog={catalog} credentials={settings.credential_statuses} busy={busy || running || conflict} onChange={edit} />) },
      { key: "environment", label: "环境与来源", children: (<EnvironmentSettings values={values} settings={settings} credentials={settings.credential_statuses} busy={busy || running || conflict} onChange={edit} />) },
      { key: "credentials", label: "凭据", children: (<CredentialSettings credentials={settings.credential_statuses} externalOnly={settings.warnings.includes("EXTERNALLY_MANAGED")} onChanged={credential_statuses => setSettings(previous => ({ ...previous, credential_statuses }))} />) },
      { key: "maintenance", label: "维护与恢复", children: (<MaintenanceSettings settings={settings} onImported={accept} onRestored={fresh => { setSettings(fresh); setValues(fresh.pending_values ?? fresh.values ?? {}); setDraft(null); setDirty(false); setValidation(null); setProbes([]); onFactsChanged?.(); }} />) },
      { key: "indexes", label: "索引兼容性", children: <IndexIdentitySettings /> },
      { key: "probe", label: "能力检测", children: (<ProbePanel draft={draft} probes={probes} busy={busy} dirty={dirty} disabled={Boolean(conflict)}
      onProbe={(profile_id, capability) => { void run(async token => {
        const result = await api.createProbe(token, { draft_revision: draft!.revision_id, profile_id, capability, allow_billable: true, timeout_seconds: 15 });
        if (mounted.current) setProbes(previous => [...previous, result]);
      }); }}
      onRead={id => { void run(async token => { const next = await api.getProbe(token, id); if (mounted.current) setProbes(previous => previous.map(item => item.probe_id === next.probe_id ? next : item)); }); }}
      onCancel={id => { void run(async token => { const next = await api.cancelProbe(token, id); if (mounted.current) setProbes(previous => previous.map(item => item.probe_id === next.probe_id ? next : item)); }); }} />) },
    ]} />
  </Space></Card>;
}
