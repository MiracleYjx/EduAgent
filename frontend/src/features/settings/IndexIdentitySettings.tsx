import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Space, Typography } from "antd";
import * as api from "../../api/client";
import type { components } from "../../api/generated";
import { useSettingsSession } from "../../auth/SettingsSession";

type Schema = components["schemas"];
type Identity = Schema["IndexIdentityView"];
function objectInput(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("object required");
  return value as Record<string, unknown>;
}
export function IndexIdentitySettings() {
  const session = useSettingsSession(); const lock = useRef(false); const mounted = useRef(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [identities, setIdentities] = useState<Identity[]>([]), [view, setView] = useState<Identity | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function run(action: (token: string) => Promise<void>) {
    if (lock.current || !session.token) return;
    lock.current = true; setBusy(true); setError("");
    try { await action(session.token); }
    catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof api.SettingsApiError) {
        if (cause.status === 401 || cause.status === 403) session.expire(cause);
        else setError(`${cause.code}：${cause.message}；请重新读取身份，再重新预览。`);
      } else setError("INDEX_OPERATION_FAILED：操作未完成，请读取服务端事实后再决定。");
      throw cause;
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  function read(id: string) { void run(async token => { const next = await api.getIndexIdentity(token, id); if (mounted.current) setView(next); }).catch(() => {}); }
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Title level={3}>历史索引身份与兼容性</Typography.Title>
    <Alert type="warning" showIcon title="历史身份未知时不得套用当前模型" description="按旧部署、摄取或基准记录填写可核验证据。接纳只登记身份与审计，不修改 chunk、向量或维度，不自动下载、重建或删除索引。" />
    {error ? <Alert type="error" showIcon title={error} /> : null}
    <Button disabled={busy} onClick={() => { void run(async token => { const result = await api.getIndexIdentities(token); if (mounted.current) { setIdentities(result.identities); setView(null); } }).catch(() => {}); }}>读取知识库索引身份</Button>
    <div className="wizard-field"><label htmlFor="index-kb">知识库身份</label><select id="index-kb" disabled={busy} value={view?.kb_id ?? ""} onChange={event => { setView(null); if (event.target.value) read(event.target.value); }}><option value="">选择知识库</option>{identities.map(item => <option key={item.kb_id} value={item.kb_id}>{item.kb_id} · {item.status}</option>)}</select></div>
    {view ? <>
      <Typography.Text>现有身份：{view.status}；模型：{view.model_id ?? "unknown"}；实际维度：{view.stored_dimension ?? "unknown"}；身份修订：{view.identity_revision ?? "unknown"}</Typography.Text>
      <Button disabled={busy} onClick={() => read(view.kb_id)}>重新读取所选身份</Button>
      {view.status === "legacy_unknown" ? <AdoptionForm key={`${view.kb_id}:${view.identity_revision}`} view={view} busy={busy}
        onPreview={async payload => { let result: Schema["AdoptionPreview"] | null = null; await run(async token => { result = await api.previewIndexAdoption(token, view.kb_id, payload); }); return result!; }}
        onConfirm={async preview => { await run(async token => { await api.confirmIndexAdoption(token, view.kb_id, { preview_id: preview.preview_id, expected_identity_revision: preview.expected_identity_revision, confirm: true }); const next = await api.getIndexIdentity(token, view.kb_id); if (mounted.current) setView(next); }); }} /> : null}
      {view.adoption ? <Alert type="info" showIcon title="已读回接纳记录" description={<pre className="safe-json">{JSON.stringify(view.adoption, null, 2)}</pre>} /> : null}
      {view.audits.length ? <details><summary>接纳审计（服务端事实）</summary><pre className="safe-json">{JSON.stringify(view.audits, null, 2)}</pre></details> : null}
    </> : null}
  </Space>;
}

function AdoptionForm({ view, busy, onPreview, onConfirm }: {
  view: Identity; busy: boolean;
  onPreview: (payload: Schema["AdoptionPreviewRequest"]) => Promise<Schema["AdoptionPreview"]>;
  onConfirm: (preview: Schema["AdoptionPreview"]) => Promise<void>;
}) {
  const [fields, setFields] = useState({ provider: "", adapter: "", model_id: "", model_version: "", endpoint_identity: "", dimension: "", semantic_parameters: "" });
  const [evidence, setEvidence] = useState([{ kind: "deployment_record", source_reference: "", source_date: "", safe_excerpt: "", supported_fields: "" }]);
  const [covers, setCovers] = useState(""), [verified, setVerified] = useState(false);
  const [preview, setPreview] = useState<Schema["AdoptionPreview"] | null>(null), [confirmed, setConfirmed] = useState(false), [error, setError] = useState("");
  const labels: Record<keyof typeof fields, string> = { provider: "历史 Provider", adapter: "历史适配器", model_id: "历史模型名称", model_version: "历史模型版本（未知则留空）", endpoint_identity: "历史端点标识（脱敏）", dimension: "证据中的向量维度", semantic_parameters: "历史语义参数（JSON 对象）" };
  function invalidate() { setPreview(null); setConfirmed(false); setError(""); }
  return <Space orientation="vertical" size={12} style={{ width: "100%" }}>
    <div className="wizard-form-grid">{(Object.keys(fields) as Array<keyof typeof fields>).map(key => <div className="wizard-field" key={key}><label htmlFor={`identity-${key}`}>{labels[key]}</label><Input id={`identity-${key}`} disabled={busy} autoComplete="off" type={key === "dimension" ? "number" : "text"} value={fields[key]} onChange={event => { invalidate(); setFields(previous => ({ ...previous, [key]: event.target.value })); }} /></div>)}</div>
    {evidence.map((record, index) => <fieldset key={index} className="credential-fieldset" disabled={busy}><legend>脱敏证据 {index + 1}</legend>
      <div className="wizard-field"><label htmlFor={`evidence-kind-${index}`}>证据类别 {index + 1}</label><select id={`evidence-kind-${index}`} value={record.kind} onChange={event => { invalidate(); setEvidence(previous => previous.map((item, i) => i === index ? { ...item, kind: event.target.value } : item)); }}><option value="deployment_record">部署记录</option><option value="ingestion_record">摄取记录</option><option value="benchmark_record">基准记录</option></select></div>
      {([["source_reference", "记录定位"], ["source_date", "记录日期（YYYY-MM-DD）"], ["safe_excerpt", "脱敏摘要（无密钥、无业务正文）"], ["supported_fields", "证据支持的字段（JSON 对象，手工核对）"]] as const).map(([key, label]) => <div className="wizard-field" key={key}><label htmlFor={`evidence-${key}-${index}`}>{label} {index + 1}</label><Input.TextArea id={`evidence-${key}-${index}`} autoComplete="off" value={record[key]} onChange={event => { invalidate(); setEvidence(previous => previous.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item)); }} /></div>)}
      {evidence.length > 1 ? <Button disabled={busy} onClick={() => { invalidate(); setEvidence(previous => previous.filter((_, i) => i !== index)); }}>移除此条证据</Button> : null}
    </fieldset>)}
    <Button disabled={busy} onClick={() => { invalidate(); setEvidence(previous => [...previous, { kind: "deployment_record", source_reference: "", source_date: "", safe_excerpt: "", supported_fields: "" }]); }}>添加另一条证据</Button>
    <div className="wizard-field"><label htmlFor="attestation-covers">已核实的历史范围</label><Input id="attestation-covers" disabled={busy} value={covers} onChange={event => { invalidate(); setCovers(event.target.value); }} /></div>
    <Checkbox checked={verified} disabled={busy} onChange={event => { invalidate(); setVerified(event.target.checked); }}>我已依据上述记录核实历史身份与知识库范围</Checkbox>
    {error ? <Alert type="error" showIcon title={error} /> : null}
    <Button disabled={busy || !verified || !covers} onClick={() => {
      invalidate();
      let payload: Schema["AdoptionPreviewRequest"];
      try { payload = {
        expected_identity_revision: view.identity_revision,
        proposed_identity: { ...fields, model_version: fields.model_version || null, endpoint_identity: fields.endpoint_identity || null, dimension: Number(fields.dimension), semantic_parameters: objectInput(fields.semantic_parameters) },
        evidence: evidence.map(record => ({ ...record, kb_scope: view.kb_id, supported_fields: objectInput(record.supported_fields) })),
        operator_attestation: { verified: true, covers },
      }; } catch { setError("语义参数和证据字段必须是有效 JSON 对象；未发送预览请求。"); return; }
      void onPreview(payload).then(setPreview).catch(() => { setPreview(null); });
    }}>预览历史身份接纳</Button>
    {preview ? <>
      <Alert type="warning" showIcon title="预览尚未接纳" description={`限制：${preview.limitations.join("、") || "服务端未列出额外限制"}；过期：${preview.expires_at}；修改向量：${preview.modifies_vectors ? "是" : "否"}`} />
      <pre className="safe-json">{JSON.stringify({ proposed: preview.proposed_identity, verified_facts: preview.verified_facts }, null, 2)}</pre>
      <Checkbox checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)}>已核对预览限制，确认仅登记身份与审计</Checkbox>
      <Button disabled={busy || !confirmed || preview.modifies_vectors} onClick={() => { const current = preview; setPreview(null); setConfirmed(false); void onConfirm(current).catch(() => {}); }}>确认接纳历史身份</Button>
    </> : null}
  </Space>;
}
