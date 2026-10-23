import { useState } from "react";
import { Alert, Button, Input, Space, Typography } from "antd";
import type { ConfigurationValues, CredentialReference, CredentialStatusView } from "../../api/contracts";
import { CredentialInput, type CredentialPurpose } from "./CredentialInput";

export function BusinessStep({ values, catalog, busy, externalOnly, onSave, onCredential }: {
  values: ConfigurationValues | null;
  catalog: CredentialStatusView[];
  busy: boolean;
  externalOnly: boolean;
  onSave: (changes: Record<string, unknown>) => void;
  onCredential: (purpose: CredentialPurpose, secret: string, accept: (ref: CredentialReference) => void) => void;
}) {
  const business = values?.business;
  const db = business?.database_locator;
  const redis = business?.redis_locator;
  const [fields, setFields] = useState({
    storage: business?.storage_root ?? "", dbHost: db?.host ?? "", dbPort: db?.port ?? 5432,
    dbName: db?.database ?? "", dbUser: db?.user ?? "", dbSchema: db?.schema ?? "public",
    redisHost: redis?.host ?? "", redisPort: redis?.port ?? 6379, redisDatabase: redis?.database ?? 0,
  });
  const [refs, setRefs] = useState<Record<string, CredentialReference | null>>({
    database: business?.database_password_ref ?? null, redis: business?.redis_password_ref ?? null,
  });
  function field<K extends keyof typeof fields>(key: K, value: (typeof fields)[K]) {
    setFields(previous => ({ ...previous, [key]: value }));
  }
  function input(key: keyof typeof fields, label: string, numeric = false) {
    return <div className="wizard-field" key={key}>
      <label htmlFor={`business-${key}`}>{label}</label>
      <Input id={`business-${key}`} type={numeric ? "number" : "text"} value={fields[key]} disabled={busy}
        autoComplete="off" onChange={event => field(key, numeric ? Number(event.target.value) : event.target.value)} />
    </div>;
  }
  return <Space orientation="vertical" size={20} style={{ width: "100%" }}>
    <Alert type="info" showIcon title="目录与依赖配置仅保存为草稿"
      description="不会安装数据库/Redis，不会搬迁或清空已有数据。数据库、存储和凭据可用性由后端分别校验；保存不代表就绪。" />
    <form autoComplete="off" onSubmit={event => {
      event.preventDefault();
      onSave({ business: {
        ...business,
        database_locator: { host: fields.dbHost, port: fields.dbPort, database: fields.dbName, user: fields.dbUser, schema: fields.dbSchema },
        redis_locator: { host: fields.redisHost, port: fields.redisPort, database: fields.redisDatabase },
        storage_root: fields.storage || null,
        database_password_ref: refs.database, redis_password_ref: refs.redis,
      } });
    }}>
      <div className="wizard-form-grid">
        {input("storage", "持久目录（绝对路径）")}
        {input("dbHost", "数据库主机")}{input("dbPort", "数据库端口", true)}
        {input("dbName", "数据库名称")}{input("dbUser", "数据库用户")}{input("dbSchema", "数据库 Schema")}
        {input("redisHost", "Redis 主机")}{input("redisPort", "Redis 端口", true)}{input("redisDatabase", "Redis 数据库编号", true)}
      </div>
      <Typography.Paragraph type="secondary">
        连接定位字段不得嵌入密码或 URL；密码通过下方凭据库提交。个人学习不需要旧业务 JWT。
      </Typography.Paragraph>
      {(["database", "redis"] as const).map(purpose => <CredentialInput
        key={purpose} purpose={purpose} reference={refs[purpose]} catalog={catalog} busy={busy} externalOnly={externalOnly}
        onSelect={ref => setRefs(previous => ({ ...previous, [purpose]: ref }))}
        onSave={(kind, secret) => onCredential(kind, secret, ref => setRefs(previous => ({ ...previous, [purpose]: ref })))} />)}
      <Button type="primary" htmlType="submit" disabled={busy} style={{ marginTop: 16 }}>保存目录与依赖草稿</Button>
    </form>
  </Space>;
}
