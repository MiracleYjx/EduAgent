import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Space, Table, Typography } from "antd";
import * as api from "../../api/client";
import type { CredentialStatusView } from "../../api/contracts";
import { useSettingsSession } from "../../auth/SettingsSession";
import type { CredentialPurpose } from "../onboarding/CredentialInput";

const states: Record<CredentialStatusView["status"], string> = {
  absent: "缺失", available: "可用", invalid: "无效", unavailable: "不可用（尚未核实或无法读取）",
  revoked: "已撤销", reentry_required: "需重新录入（凭据不可解密或已丢失）",
};
const purposes: Record<CredentialPurpose, string> = { provider: "模型", database: "数据库", redis: "Redis", jwt: "JWT" };
export function CredentialSettings({ credentials, externalOnly, onChanged }: {
  credentials: CredentialStatusView[]; externalOnly: boolean; onChanged: (values: CredentialStatusView[]) => void;
}) {
  const session = useSettingsSession();
  const [target, setTarget] = useState("");
  const [purpose, setPurpose] = useState<CredentialPurpose>("provider");
  const [secret, setSecret] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const lock = useRef(false); const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const selected = credentials.find(item => `${item.credential_ref}:${item.version}` === target);
  const external = externalOnly || credentials.some(item => item.backend === "mounted_secret");
  const readonly = external || Boolean(selected && selected.backend !== "dpapi");
  async function run(action: (token: string) => Promise<void>) {
    if (lock.current || !session.token) return;
    lock.current = true; setBusy(true); setError(""); setMessage("");
    try { await action(session.token); }
    catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof api.SettingsApiError) {
        if (cause.status === 401 || cause.status === 403) session.expire(cause);
        else setError(`${cause.code}：${cause.message}`);
      } else setError("CREDENTIAL_OPERATION_FAILED：操作未完成，请先核对状态。");
    } finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Title level={3}>安全凭据</Typography.Title>
    <Alert type="info" showIcon title="只显示引用与状态，不读取密钥原文" description="替换会创建候选版本；请在模型或环境配置中显式选择新引用并保存、校验、激活。原版本在引用解除前保留。业务备份不包含可恢复密钥，恢复时可能需要重新录入。" />
    {credentials.length === 0 ? <Typography.Text>尚无已登记凭据</Typography.Text> : <Table<CredentialStatusView> size="small" pagination={false} scroll={{ x: 600 }} rowKey={item => `${item.credential_ref}:${item.version}`} dataSource={credentials} columns={[
      { title: "用途", dataIndex: "purpose", render: value => purposes[value as CredentialPurpose] ?? value },
      { title: "状态", dataIndex: "status", render: (value: CredentialStatusView["status"]) => states[value] },
      { title: "来源", dataIndex: "backend", render: value => value ?? "unknown" },
      { title: "引用", dataIndex: "credential_ref" }, { title: "版本", dataIndex: "version" },
    ]} />}
    {error ? <Alert type="error" showIcon title={error} description="原凭据引用保持。IN_USE 表示仍被 active、pending 或未完成任务引用，不能强制删除。" /> : null}
    {message ? <Alert type="info" showIcon title={message} /> : null}
    {external ? <Alert type="info" showIcon title="外部 Secret 为只读" description="由宿主挂载或进程环境维护。请在部署端更新 Secret，并按部署流程重新启动；此页面不写入、替换或删除外部秘密。空目录需先在宿主配置可信挂载。" /> : <>
      <div className="wizard-field"><label htmlFor="credential-target">操作目标</label>
        <select id="credential-target" value={target} disabled={busy} onChange={event => { setTarget(event.target.value); setSecret(""); setConfirmDelete(false); setError(""); }}>
          <option value="">新增安全凭据</option>{credentials.map(item => <option key={`${item.credential_ref}:${item.version}`} value={`${item.credential_ref}:${item.version}`}>{item.purpose} · {item.credential_ref} / {item.version}</option>)}
        </select></div>
      {readonly ? <Typography.Text>所选引用由外部管理，需到宿主更新。</Typography.Text> : <>
        {!selected ? <div className="wizard-field"><label htmlFor="credential-purpose">凭据用途</label><select id="credential-purpose" value={purpose} disabled={busy} onChange={event => { setPurpose(event.target.value as CredentialPurpose); setSecret(""); }}>
          {Object.entries(purposes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></div> : null}
        <div className="wizard-field"><label htmlFor="credential-secret">新凭据原文</label><Input.Password id="credential-secret" autoComplete="new-password" visibilityToggle={false} value={secret} disabled={busy} onChange={event => setSecret(event.target.value)} /></div>
        <Button disabled={busy || !secret.trim() || session.status !== "authorized"} onClick={() => {
          const value = secret; setSecret("");
          void run(async token => {
            const saved = selected ? await api.createCredentialVersion(token, selected.credential_ref, { secret_value: value }) : await api.createCredential(token, { purpose, secret_value: value });
            if (!mounted.current) return;
            onChanged([...credentials.filter(item => item.credential_ref !== saved.credential_ref || item.version !== saved.version), saved]);
            setMessage("候选凭据已保存，原引用未改动；请显式绑定新版本。");
          });
        }}>{selected ? "保存新的候选版本" : "安全保存新凭据"}</Button>
        {selected ? <Space wrap><Checkbox disabled={busy} checked={confirmDelete} onChange={event => setConfirmDelete(event.target.checked)}>确认删除所选凭据版本</Checkbox>
          <Button danger disabled={busy || !confirmDelete || session.status !== "authorized"} onClick={() => { setSecret(""); setConfirmDelete(false); void run(async token => {
            await api.deleteCredentialVersion(token, selected.credential_ref, selected.version);
            if (!mounted.current) return;
            onChanged(credentials.filter(item => item !== selected)); setTarget(""); setMessage("所选未引用版本已删除。");
          }); }}>删除所选版本</Button></Space> : null}
      </>}
    </>}
  </Space>;
}
