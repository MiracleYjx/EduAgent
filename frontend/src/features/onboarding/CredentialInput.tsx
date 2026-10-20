import { useState } from "react";
import { Button, Input, Select, Space, Typography } from "antd";
import type { CredentialReference, CredentialStatusView } from "../../api/contracts";

export type CredentialPurpose = "jwt" | "database" | "redis" | "provider";
const names: Record<CredentialPurpose, string> = { jwt: "JWT", database: "数据库", redis: "Redis", provider: "模型" };

export function referenceFromStatus(value: CredentialStatusView): CredentialReference {
  if (!value.environment_id || !value.backend) {
    throw new Error("凭据响应缺少引用身份。");
  }
  return {
    credential_id: value.credential_ref, version: value.version,
    environment_id: value.environment_id, backend: value.backend, purpose: value.purpose,
    external_secret_id: value.external_secret_id ?? null,
  };
}

export function CredentialInput({ purpose, reference, catalog, busy, externalOnly = false, onSave, onSelect }: {
  purpose: CredentialPurpose;
  reference: CredentialReference | null;
  catalog: CredentialStatusView[];
  busy: boolean;
  externalOnly?: boolean;
  onSave: (purpose: CredentialPurpose, secret: string) => void;
  onSelect: (reference: CredentialReference | null) => void;
}) {
  const [secret, setSecret] = useState("");
  const name = names[purpose];
  const candidates = catalog.filter(item => item.purpose === purpose);
  const externallyManaged = externalOnly || catalog.some(item => item.backend === "mounted_secret");
  function submit() {
    const value = secret;
    setSecret(""); // Clear even on failed/expired requests; never refill a secret.
    onSave(purpose, value);
  }
  return (
    <fieldset className="credential-fieldset">
      <legend>{name} 安全凭据</legend>
      <Space orientation="vertical" style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          只保存后端引用，不读回原文。外部 Secret 部署只允许选择已登记引用，写入会被拒绝。
        </Typography.Text>
        {candidates.length > 0 ? <>
          <label htmlFor={`${purpose}-existing`}>已登记的{name}凭据引用</label>
          <Select id={`${purpose}-existing`} style={{ width: "100%" }} allowClear disabled={busy}
            placeholder="选择引用（不读取凭据）"
            value={reference ? `${reference.credential_id}:${reference.version}` : undefined}
            onChange={value => {
              const selected = candidates.find(item => `${item.credential_ref}:${item.version}` === value);
              onSelect(selected ? referenceFromStatus(selected) : null);
            }}
            options={candidates.map(item => ({
              value: `${item.credential_ref}:${item.version}`,
              label: `${item.backend ?? "unknown"} · ${item.status} · ${item.credential_ref}`,
              disabled: (item.status !== "available" && !(item.backend === "mounted_secret" && item.status === "unavailable")) || !item.backend || !item.environment_id,
            }))} />
        </> : null}
        {externallyManaged ? <Typography.Text>外部 Secret 仅由宿主维护；本页不写入、替换或生成凭据。未核实的引用仍需后端在显式操作时验证。</Typography.Text> : <>
        <label htmlFor={`${purpose}-secret`}>{name} 密钥输入</label>
        <Input.Password id={`${purpose}-secret`} value={secret} disabled={busy}
          autoComplete="new-password" visibilityToggle={false}
          onChange={event => setSecret(event.target.value)} />
        <Space wrap>
          <Button disabled={busy || !secret} onClick={submit}>安全保存 {name} 凭据</Button>
          {purpose === "jwt" ? <Button disabled={busy} onClick={() => {
            const bytes = crypto.getRandomValues(new Uint8Array(48));
            const generated = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
            bytes.fill(0);
            setSecret("");
            onSave("jwt", generated);
          }}>生成并安全保存 JWT</Button> : null}
        </Space>
        </>}
        <Typography.Text className="mono">
          {reference ? `已选择引用 ${reference.credential_id} / ${reference.version}（尚以草稿保存结果为准）` : "尚未选择凭据引用"}
        </Typography.Text>
      </Space>
    </fieldset>
  );
}
