import { Input, Space, Table, Typography } from "antd";
import type { ConfigurationValues, CredentialStatusView, SettingsView } from "../../api/contracts";
import { referenceFromStatus } from "../onboarding/CredentialInput";

function safeValue(values: ConfigurationValues | null | undefined, path: string): string {
  if (!values) return "不可用";
  const value = path.replace(".schema_name", ".schema").split(".").reduce<unknown>((node, part) => node && typeof node === "object" ? (node as Record<string, unknown>)[part] : undefined, values);
  if (value === undefined || value === null) return "未设置";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
export function EnvironmentSettings({ values, settings, credentials, busy, onChange }: {
  values: ConfigurationValues; settings: SettingsView; credentials: CredentialStatusView[]; busy: boolean; onChange: (next: ConfigurationValues) => void;
}) {
  const business = values.business;
  const db = business?.database_locator ?? { host: "", port: 5432, database: "", user: "", schema: "public" };
  const redis = business?.redis_locator ?? { host: "", port: 6379, database: 0 };
  const sourceRows = Object.entries(settings.sources ?? {}).map(([field, source]) => ({
    field, source: source.source, saved: safeValue(settings.values, field), effective: safeValue(settings.effective_values, field), pending: settings.pending_values ? safeValue(settings.pending_values, field) : "无待重启值",
  }));
  return <Space orientation="vertical" size={16} style={{ width: "100%" }}>
    <Typography.Title level={3}>环境、依赖与配置来源</Typography.Title>
    <Typography.Paragraph>这里只编辑候选定位与凭据引用，不移动已有存储、不修改旧 .env。来源表分别显示保存值、当前进程解析值和待重启值；解析值不代表业务已热加载。</Typography.Paragraph>
    {settings.effective_errors?.map((issue, index) => <Typography.Text role="alert" type="danger" key={index}>{issue.segment}：{issue.code} · {issue.fields.join("、")}；当前解析不可用，未回落保存值。</Typography.Text>)}
    <Table tableLayout="fixed" size="small" rowKey="field" pagination={{ pageSize: 8 }} scroll={{ x: 720 }} dataSource={sourceRows} columns={[
      { title: "字段", dataIndex: "field" }, { title: "当前来源", dataIndex: "source" },
      { title: "已保存值", dataIndex: "saved" }, { title: "当前解析值", dataIndex: "effective" }, { title: "待重启值", dataIndex: "pending" },
    ]} />
    {business ? <div className="wizard-form-grid">
      <div className="wizard-field"><label htmlFor="env-storage">存储目录（绝对路径）</label><Input id="env-storage" disabled={busy} value={business.storage_root ?? ""} onChange={event => onChange({ ...values, business: { ...business, storage_root: event.target.value || null } })} /></div>
      {([["host", "数据库主机"], ["port", "数据库端口"], ["database", "数据库名称"], ["user", "数据库用户"], ["schema", "数据库 Schema"]] as const).map(([key, label]) => <div className="wizard-field" key={key}><label htmlFor={`env-db-${key}`}>{label}</label><Input id={`env-db-${key}`} disabled={busy} type={key === "port" ? "number" : "text"} value={db[key]} onChange={event => onChange({ ...values, business: { ...business, database_locator: { ...db, [key]: key === "port" ? Number(event.target.value) : event.target.value } } })} /></div>)}
      {([["host", "Redis 主机"], ["port", "Redis 端口"], ["database", "Redis 数据库编号"]] as const).map(([key, label]) => <div className="wizard-field" key={key}><label htmlFor={`env-redis-${key}`}>{label}</label><Input id={`env-redis-${key}`} disabled={busy} type={key === "host" ? "text" : "number"} value={redis[key]} onChange={event => onChange({ ...values, business: { ...business, redis_locator: { ...redis, [key]: key === "host" ? event.target.value : Number(event.target.value) } } })} /></div>)}
      {([["jwt", "jwt_secret_ref", "JWT"], ["database", "database_password_ref", "数据库"], ["redis", "redis_password_ref", "Redis"]] as const).map(([purpose, field, label]) => {
        const reference = business[field];
        return <div className="wizard-field" key={field}><label htmlFor={`env-ref-${purpose}`}>{label}凭据引用</label><select id={`env-ref-${purpose}`} disabled={busy} value={reference ? `${reference.credential_id}:${reference.version}` : ""} onChange={event => {
          const choice = credentials.find(item => `${item.credential_ref}:${item.version}` === event.target.value);
          onChange({ ...values, business: { ...business, [field]: choice ? referenceFromStatus(choice) : null } });
        }}><option value="">未绑定</option>{credentials.filter(item => item.purpose === purpose).map(item => <option key={`${item.credential_ref}:${item.version}`} value={`${item.credential_ref}:${item.version}`} disabled={!item.backend || !item.environment_id || (item.status !== "available" && item.backend !== "mounted_secret")}>{item.credential_ref} / {item.version} · {item.status}</option>)}</select></div>;
      })}
    </div> : <Typography.Text>基础配置尚未提供，请先完成首次向导或恢复已有修订。</Typography.Text>}
  </Space>;
}
