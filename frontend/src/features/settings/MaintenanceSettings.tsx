import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Space, Typography } from "antd";
import * as api from "../../api/client";
import type { DraftView, SettingsView } from "../../api/contracts";
import type { components } from "../../api/generated";
import { useSettingsSession } from "../../auth/SettingsSession";

type Schema = components["schemas"];
export function MaintenanceSettings({ settings, onImported, onRestored }: {
  settings: SettingsView; onImported: (draft: DraftView) => void; onRestored: (settings: SettingsView) => void;
}) {
  const session = useSettingsSession(); const mounted = useRef(true); const lock = useRef(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [sources, setSources] = useState<string[]>([]), [source, setSource] = useState("");
  const [legacy, setLegacy] = useState<Schema["LegacyImportPreview"] | null>(null), [importConfirmed, setImportConfirmed] = useState(false);
  const [recovery, setRecovery] = useState<Schema["RecoveryView"] | null>(null), [revision, setRevision] = useState("");
  const [restoreConfirmed, setRestoreConfirmed] = useState(false), [restartConfirmed, setRestartConfirmed] = useState(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function run(action: (token: string) => Promise<void>) {
    if (lock.current || !session.token) return;
    lock.current = true; setBusy(true); setError(""); setMessage("");
    try { await action(session.token); }
    catch (cause) {
      if (!mounted.current) return;
      setLegacy(null); setImportConfirmed(false); setRecovery(null); setRestoreConfirmed(false);
      if (cause instanceof api.SettingsApiError) {
        if (cause.status === 401 || cause.status === 403) session.expire(cause);
        else setError(`${cause.code}：${cause.message}；请重新读取并预览，不会自动重放。`);
      } else setError("MAINTENANCE_FAILED：操作未完成，请重新核对服务端事实。");
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  const selected = recovery?.revisions.find(item => item.revision_id === revision);
  const head = settings.pending_revision ?? settings.active_revision;
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Title level={3}>旧配置导入、恢复与重启</Typography.Title>
    {error ? <Alert type="error" showIcon title={error} /> : null}
    {message ? <Alert type="info" showIcon title={message} /> : null}
    <Button disabled={busy} onClick={() => { void run(async token => { const result = await api.getLegacySources(token); if (mounted.current) { setSources(result.sources); setLegacy(null); setImportConfirmed(false); setMessage(result.sources.length ? "请选择已授权的旧配置来源。" : "本机入口尚未授权旧配置来源。"); } }); }}>读取已授权的旧配置来源</Button>
    <div className="wizard-field"><label htmlFor="legacy-source">旧配置来源名称</label><select id="legacy-source" disabled={busy} value={source} onChange={event => { setSource(event.target.value); setLegacy(null); setImportConfirmed(false); }}><option value="">选择来源</option>{sources.map(item => <option key={item} value={item}>{item}</option>)}</select></div>
    <Button disabled={busy || !source} onClick={() => { setImportConfirmed(false); setLegacy(null); void run(async token => { const preview = await api.previewLegacyImport(token, source); if (mounted.current) setLegacy(preview); }); }}>预览旧配置导入</Button>
    {legacy ? <>
      <Alert type="warning" showIcon title="导入仅产生候选草稿，旧文件中的明文仍然存在" description={`来源：${legacy.source_id}；过期：${legacy.expires_at}；警告：${legacy.warnings.join("、")}；缺失：${legacy.missing_fields.join("、")}；凭据用途：${legacy.credential_purposes.join("、")}`} />
      <Typography.Text>数据库：{legacy.database_locator?.host ?? "未知"} / {legacy.database_locator?.database ?? "未知"}；目录：{legacy.storage_root ?? "未知"}；适配器：{legacy.provider_adapters.join("、")}</Typography.Text>
      <Checkbox disabled={busy} checked={importConfirmed} onChange={event => setImportConfirmed(event.target.checked)}>确认按此预览导入，不改删旧文件</Checkbox>
      <Button disabled={busy || !importConfirmed} onClick={() => { setImportConfirmed(false); void run(async token => { const imported = await api.applyLegacyImport(token, { preview_id: legacy.preview_id, confirm: true }); if (mounted.current) { onImported(imported); setLegacy(null); setMessage("旧配置已导入为候选草稿；仍需校验、检测和显式激活。"); } }); }}>确认导入候选</Button>
    </> : null}
    <Button disabled={busy} onClick={() => { setRestoreConfirmed(false); setRecovery(null); setRevision(""); void run(async token => { const result = await api.getRecovery(token); if (mounted.current) setRecovery(result); }); }}>读取可恢复修订</Button>
    {recovery ? <>
      <Alert type={recovery.damaged ? "warning" : "info"} showIcon title={recovery.damaged ? "当前配置指针损坏，未自动修复" : "请选择需恢复的已保存修订"} description="恢复继续执行原凭据、业务和未完成任务保护；只保存为待重启修订，不热切业务、不搬迁数据。" />
      <div className="wizard-field"><label htmlFor="recovery-revision">恢复目标修订</label><select id="recovery-revision" disabled={busy} value={revision} onChange={event => { setRevision(event.target.value); setRestoreConfirmed(false); }}><option value="">选择已核对的修订</option>{recovery.revisions.map(item => <option key={item.revision_id} value={item.revision_id} disabled={!item.available}>{item.revision_id} · {item.created_at ?? item.error_code} · {item.status ?? "不可用"}</option>)}</select></div>
      {selected?.values ? <Typography.Text>目标目录：{selected.values.business?.storage_root ?? "未知"}；数据库：{selected.values.business?.database_locator?.host ?? "未知"} / {selected.values.business?.database_locator?.database ?? "未知"}；模型：{Object.values(selected.values.providers ?? {}).map(item => item.model_id).join("、") || "未配置"}</Typography.Text> : null}
      <Checkbox disabled={busy || !selected?.available} checked={restoreConfirmed} onChange={event => setRestoreConfirmed(event.target.checked)}>已核对目标，确认恢复为待重启修订</Checkbox>
      <Button disabled={busy || !selected?.available || !restoreConfirmed} onClick={() => { setRestoreConfirmed(false); void run(async token => {
        await api.restoreRevision(token, { revision_id: revision, expected_revision: recovery.current_revision, expected_manifest_fingerprint: recovery.manifest_fingerprint, confirm: true });
        const fresh = await api.getSettingsView(token); if (mounted.current) { onRestored(fresh); setRecovery(null); setMessage("恢复已保存为待重启；当前业务未热切换。"); }
      }); }}>确认恢复所选修订</Button>
    </> : null}
    {head ? <>
      <Typography.Text>待重启修订：{settings.pending_revision ?? "无"}；首次装配或待重启变更需要重启应用。</Typography.Text>
      <Checkbox checked={restartConfirmed} disabled={busy} onChange={event => setRestartConfirmed(event.target.checked)}>确认按当前修订请求重启</Checkbox>
      <Button disabled={busy || !restartConfirmed} onClick={() => { setRestartConfirmed(false); void run(async token => { const result = await api.restartApplication(token, { expected_revision: head }); if (mounted.current) setMessage(`${result.status}：${result.reason}`); }); }}>请求应用重启</Button>
    </> : null}
  </Space>;
}
