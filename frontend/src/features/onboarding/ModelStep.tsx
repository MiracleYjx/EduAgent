import { useState } from "react";
import { Alert, Button, Input, Radio, Select, Space, Typography } from "antd";
import type { CredentialReference, CredentialStatusView, DraftView, ProviderCatalogEntry } from "../../api/contracts";
import { CredentialInput, type CredentialPurpose } from "./CredentialInput";

export function ModelStep({ draft, catalog, providers, busy, externalOnly, onSave, onBind, onCredential }: {
  draft: DraftView;
  catalog: CredentialStatusView[];
  providers: ProviderCatalogEntry[];
  busy: boolean;
  externalOnly: boolean;
  onSave: (changes: Record<string, unknown>) => void;
  onBind: (profileId: string, reference: CredentialReference) => void;
  onCredential: (purpose: CredentialPurpose, secret: string, accept: (ref: CredentialReference) => void) => void;
}) {
  const binding = draft.values.bindings?.default_llm;
  const current = binding?.profile_id ? draft.values.providers?.[binding.profile_id] : undefined;
  const [profileId] = useState(() => current?.profile_id ?? crypto.randomUUID());
  const [adapter, setAdapter] = useState(current?.adapter_id ?? "");
  const [model, setModel] = useState(current?.model_id ?? "");
  const [url, setUrl] = useState(current?.base_url ?? "");
  const [auth, setAuth] = useState<"api_key" | "none">(current?.auth_mode ?? "api_key");
  const [reference, setReference] = useState<CredentialReference | null>(current?.credential_ref ?? null);
  const choices = providers.filter(entry => entry.kind === "llm" && entry.capabilities.includes("text") && entry.capabilities.includes("json_object"));
  const selected = choices.find(entry => entry.adapter_id === adapter);

  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" showIcon title="可明确跳过 AI，稍后配置"
      description="本向导只配置首次使用的默认文本结构化模型。独立 Embedding、Vision、OCR 与任务覆盖不在本页扩展；业务就绪和逐任务 AI 状态仍分别报告。Reviewer 保持规则复核。" />
    <Button disabled={busy} onClick={() => onSave({ bindings: { default_llm: {
      task_kind: "default_llm", profile_id: null, profile_revision: null, inherit_default: false, required_capabilities: [],
    } } })}>跳过 AI 并保存草稿</Button>
    <div className="wizard-field">
      <label htmlFor="model-adapter">默认模型适配器（来自 Registry）</label>
      <Select id="model-adapter" value={adapter || undefined} disabled={busy} placeholder="选择真实注册的文本适配器"
        onChange={setAdapter} options={choices.map(entry => ({ value: entry.adapter_id, label: entry.adapter_id }))} />
    </div>
    {selected ? <Typography.Text type="secondary">声明能力：{selected.capabilities.join("、")}；声明不等于检测通过。</Typography.Text> : null}
    <div className="wizard-form-grid">
      <div className="wizard-field"><label htmlFor="model-id">模型名称</label><Input id="model-id" value={model} disabled={busy} autoComplete="off" onChange={event => setModel(event.target.value)} /></div>
      <div className="wizard-field"><label htmlFor="model-url">模型服务地址（不得包含密钥）</label><Input id="model-url" value={url} disabled={busy} autoComplete="off" onChange={event => setUrl(event.target.value)} /></div>
    </div>
    <Radio.Group value={auth} disabled={busy} onChange={event => setAuth(event.target.value as "api_key" | "none")}>
      <Radio value="api_key">使用 API Key 引用</Radio><Radio value="none">无鉴权本地服务（后端校验适用性）</Radio>
    </Radio.Group>
    <Button type="primary" disabled={busy || !selected} onClick={() => {
      const revision = crypto.randomUUID();
      onSave({ providers: { [profileId]: {
        profile_id: profileId, profile_revision: revision, display_name: "默认文本模型", adapter_id: adapter,
        model_id: model, base_url: url || null, auth_mode: auth,
        credential_ref: auth === "api_key" ? current?.credential_ref ?? null : null,
        declared_capabilities: ["text", "json_object"],
        parameters: current?.adapter_id === adapter ? current.parameters ?? {} : {}, enabled: true,
      } }, bindings: { default_llm: {
        task_kind: "default_llm", profile_id: profileId, profile_revision: revision, inherit_default: false, required_capabilities: ["text", "json_object"],
      } } });
    }}>保存默认模型草稿</Button>
    {current && current.auth_mode !== "none" ? <>
      <CredentialInput purpose="provider" reference={reference} catalog={catalog} busy={busy} externalOnly={externalOnly}
        onSelect={setReference} onSave={(purpose, secret) => onCredential(purpose, secret, setReference)} />
      <Button disabled={busy || reference === null} onClick={() => reference && onBind(current.profile_id, reference)}>绑定模型凭据引用</Button>
    </> : <Typography.Text type="secondary">先保存模型草稿，再按需保存并绑定安全凭据。无鉴权服务不要求虚假 Key。</Typography.Text>}
  </Space>;
}
