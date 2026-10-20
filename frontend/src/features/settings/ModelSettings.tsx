import { useState } from "react";
import { Alert, Button, Checkbox, Input, Space, Typography } from "antd";
import type { ConfigurationValues, CredentialStatusView, ProviderCatalogEntry } from "../../api/contracts";
import type { components } from "../../api/generated";
import { referenceFromStatus } from "../onboarding/CredentialInput";

type Profile = components["schemas"]["ProviderProfile"];
const taskFields = [
  ["default_llm", "默认 LLM", "llm", false], ["question_generation", "出题", "llm", true],
  ["subjective_grading", "主观阅卷", "llm", true], ["embedding", "Embedding", "embedding", false],
  ["rerank", "Rerank", "rerank", true], ["vision", "Vision", "vision", false], ["ocr", "OCR", "ocr", false],
] as const;
export function profileKind(profile: Profile): string {
  for (const [capability, kind] of [["embedding", "embedding"], ["ocr", "ocr"], ["local_rerank", "rerank"], ["llm_rerank", "rerank"], ["vision", "vision"]]) {
    if (profile.declared_capabilities.includes(capability)) return kind;
  }
  return "llm";
}
function parameterFields(entry?: ProviderCatalogEntry): [string, Record<string, unknown>][] {
  const properties = entry?.parameters.properties;
  if (!properties || typeof properties !== "object") return [];
  return Object.entries(properties).flatMap(([name, raw]) => {
    if (!raw || typeof raw !== "object") return [];
    const schema = raw as Record<string, unknown>;
    const alternatives = Array.isArray(schema.anyOf) ? schema.anyOf : [schema];
    const numeric = alternatives.find(item => item?.type === "number" || item?.type === "integer");
    return numeric ? [[name, numeric as Record<string, unknown>]] : [];
  });
}

export function ModelSettings({ values, catalog, credentials, busy, onChange }: {
  values: ConfigurationValues; catalog: ProviderCatalogEntry[]; credentials: CredentialStatusView[];
  busy: boolean; onChange: (values: ConfigurationValues) => void;
}) {
  const profiles = values.providers ?? {};
  const [selected, setSelected] = useState(Object.keys(profiles)[0] ?? "");
  const [adapter, setAdapter] = useState("");
  const profile = profiles[selected];
  const entry = profile ? catalog.find(item => item.kind === profileKind(profile) && item.adapter_id === profile.adapter_id) : undefined;
  function updateProfile(changes: Partial<Profile>) {
    if (!profile) return;
    const next = { ...profile, ...changes, profile_revision: crypto.randomUUID() };
    const bindings = Object.fromEntries(Object.entries(values.bindings ?? {}).map(([name, binding]) => [name,
      binding.profile_id === profile.profile_id ? { ...binding, profile_revision: next.profile_revision } : binding,
    ]));
    onChange({ ...values, providers: { ...profiles, [selected]: next }, bindings });
  }
  function bind(task: string, value: string) {
    const chosen = profiles[value];
    if (value === "") { const bindings = { ...values.bindings }; delete bindings[task]; onChange({ ...values, bindings }); return; }
    onChange({ ...values, bindings: { ...values.bindings, [task]: {
      ...values.bindings?.[task], task_kind: task, profile_id: chosen?.profile_id ?? null,
      profile_revision: chosen?.profile_revision ?? null, inherit_default: value === "inherit",
      required_capabilities: [],
    } } });
  }
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Title level={3}>模型与任务绑定</Typography.Title>
    <Alert type="info" showIcon title="声明能力不等于检测通过" description="默认模型用于文字任务；出题、主观阅卷可覆盖。Embedding、Vision、OCR 独立配置。每项能力须单独检测，保存不触发模型调用。" />
    <div className="wizard-form-grid">
      {taskFields.map(([task, label, kind, inherit]) => <div className="wizard-field" key={task}>
        <label htmlFor={`binding-${task}`}>{label}模型</label>
        <select id={`binding-${task}`} disabled={busy} value={values.bindings?.[task]?.profile_id ?? (inherit ? "inherit" : "")}
          onChange={event => bind(task, event.target.value)}>
          {inherit ? <option value="inherit">继承默认 LLM：{profiles[values.bindings?.default_llm?.profile_id ?? ""]?.display_name ?? "未配置"}</option> : <option value="">未配置（不影响无关任务）</option>}
          {Object.values(profiles).filter(item => item.enabled && (profileKind(item) === kind || (kind === "rerank" && profileKind(item) === "llm")))
            .filter(item => kind !== "llm" || ["text", "json_object"].every(cap => item.declared_capabilities.includes(cap)))
            .map(item => <option key={item.profile_id} value={item.profile_id}>{item.display_name} · {item.model_id}</option>)}
        </select>
      </div>)}
    </div>
    <Typography.Text>Reviewer：规则复核，模型选择不适用。</Typography.Text>
    <Typography.Text>诊断、语义核验、拆题：继承默认 LLM，不单独覆盖。</Typography.Text>
    <Typography.Text type="secondary">未配置或能力不适用时保持原状态；不会自动切换模型。Rerank 的 llm / openai_compatible 别名使用同一适配语义。</Typography.Text>
    <div className="wizard-form-grid">
      <div className="wizard-field"><label htmlFor="profile-select">编辑模型配置</label>
        <select id="profile-select" value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}>
          <option value="">选择模型配置</option>{Object.values(profiles).map(item => <option key={item.profile_id} value={item.profile_id}>{item.display_name}</option>)}
        </select>
      </div>
      <div className="wizard-field"><label htmlFor="adapter-add">新增适配器</label>
        <select id="adapter-add" disabled={busy} value={adapter} onChange={event => setAdapter(event.target.value)}>
          <option value="">从实际 Registry 选择</option>{catalog.map(item => <option key={`${item.kind}:${item.adapter_id}`} value={`${item.kind}:${item.adapter_id}`}>
            {item.kind} / {item.adapter_id}{item.canonical_id !== item.adapter_id ? `（别名：${item.canonical_id}）` : ""}
          </option>)}
        </select>
      </div>
    </div>
    <Button disabled={busy || !adapter} onClick={() => {
      const choice = catalog.find(item => `${item.kind}:${item.adapter_id}` === adapter); if (!choice) return;
      const id = crypto.randomUUID();
      const capabilities = choice.kind === "llm" ? choice.capabilities.filter(cap => cap !== "vision") : choice.capabilities;
      onChange({ ...values, providers: { ...profiles, [id]: {
        profile_id: id, profile_revision: crypto.randomUUID(), display_name: `新 ${choice.kind} 配置`,
        adapter_id: choice.adapter_id, model_id: "", auth_mode: choice.lifecycle === "local_until_restart" ? "none" : "api_key",
        declared_capabilities: capabilities, parameters: {}, enabled: true,
      } } }); setSelected(id);
    }}>添加模型配置</Button>
    {profile ? <fieldset disabled={busy} className="credential-fieldset"><legend>编辑候选模型（尚未保存）</legend>
      <Space orientation="vertical" size={12} style={{ width: "100%" }}>
        <Typography.Text>适配器：{profile.adapter_id}；能力：{profile.declared_capabilities.join("、")}；生命周期：{entry?.lifecycle ?? "Registry 未登记"}</Typography.Text>
        {!entry ? <Alert type="warning" title="此适配器已不在 Registry，不能宣称可用" /> : null}
        <div className="wizard-form-grid">
          <div className="wizard-field"><label htmlFor="profile-name">显示名称</label><Input id="profile-name" value={profile.display_name} onChange={event => updateProfile({ display_name: event.target.value })} /></div>
          <div className="wizard-field"><label htmlFor="profile-model">模型名称</label><Input id="profile-model" autoComplete="off" value={profile.model_id} onChange={event => updateProfile({ model_id: event.target.value })} /></div>
          <div className="wizard-field"><label htmlFor="profile-url">服务 URL（无密钥）</label><Input id="profile-url" autoComplete="off" value={profile.base_url ?? ""} onChange={event => updateProfile({ base_url: event.target.value || null })} /></div>
          <div className="wizard-field"><label htmlFor="profile-directory">本地资源绝对路径</label><Input id="profile-directory" value={profile.resource_directory ?? ""} onChange={event => updateProfile({ resource_directory: event.target.value || null })} /></div>
          <div className="wizard-field"><label htmlFor="profile-auth">鉴权方式</label><select id="profile-auth" value={profile.auth_mode} onChange={event => updateProfile({ auth_mode: event.target.value as "api_key" | "none", credential_ref: null })}>
            <option value="api_key">安全凭据引用</option>{profile.adapter_id !== "deepseek" ? <option value="none">无鉴权（后端校验适用性）</option> : null}
          </select></div>
          {profile.auth_mode === "api_key" ? <div className="wizard-field"><label htmlFor="profile-credential">模型凭据引用</label><select id="profile-credential"
            value={profile.credential_ref ? `${profile.credential_ref.credential_id}:${profile.credential_ref.version}` : ""}
            onChange={event => { const selectedRef = credentials.find(item => `${item.credential_ref}:${item.version}` === event.target.value); updateProfile({ credential_ref: selectedRef ? referenceFromStatus(selectedRef) : null }); }}>
            <option value="">未绑定</option>{credentials.filter(item => item.purpose === "provider").map(item => <option disabled={item.status !== "available" || !item.backend || !item.environment_id} key={`${item.credential_ref}:${item.version}`} value={`${item.credential_ref}:${item.version}`}>{item.credential_ref} / {item.version} · {item.status}</option>)}
          </select></div> : null}
        </div>
        <Checkbox checked={profile.enabled} onChange={event => updateProfile({ enabled: event.target.checked })}>启用此模型配置</Checkbox>
        <details><summary>高级参数（来自 Registry）</summary><div className="wizard-form-grid">
          {parameterFields(entry).map(([name, schema]) => <div className="wizard-field" key={name}><label htmlFor={`param-${name}`}>{name}</label>
            <input id={`param-${name}`} type="number" min={typeof schema.minimum === "number" ? schema.minimum : undefined}
              max={typeof schema.maximum === "number" ? schema.maximum : undefined} step={schema.type === "integer" ? 1 : "any"}
              value={typeof profile.parameters?.[name] === "number" ? profile.parameters[name] as number : ""}
              onChange={event => { const parameters = { ...profile.parameters }; if (event.target.value === "") delete parameters[name]; else parameters[name] = Number(event.target.value); updateProfile({ parameters }); }} />
          </div>)}
        </div><Typography.Text type="secondary">留空使用适配器默认值；服务端负责最终范围校验。</Typography.Text></details>
      </Space>
    </fieldset> : null}
  </Space>;
}
