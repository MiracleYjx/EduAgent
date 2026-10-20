import { Alert, Space, Typography } from "antd";

import type { SessionError } from "../auth/SettingsSession";

export function UnauthorizedNotice({
  error,
}: {
  error?: SessionError | null;
}) {
  return (
    <Alert
      type="warning"
      showIcon
      title={error?.code === "ORIGIN_NOT_ALLOWED" ? "来源校验失败" : "需要重新授权"}
      description={
        <Space orientation="vertical" size={6}>
          <Typography.Text>
            请返回仍在运行的 EduAgent Launcher，点击“打开设置”或“重新授权”。
          </Typography.Text>
          <Typography.Text type="secondary">
            管理令牌只保存在当前页面内存中；刷新、关闭页面或会话过期后都需要重新授权。
          </Typography.Text>
          {error ? (
            <Typography.Text code>
              {error.code}：{error.message}
            </Typography.Text>
          ) : null}
        </Space>
      }
    />
  );
}
