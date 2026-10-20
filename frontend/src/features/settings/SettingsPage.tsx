import {
  Alert,
  Button,
  Card,
  Descriptions,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";

import { SettingsEditor } from "./SettingsEditor";

import { getSettingsView, SettingsApiError } from "../../api/client";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";

function errorDetails(error: unknown): { code: string; message: string } {
  if (error instanceof SettingsApiError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: "SETTINGS_UNAVAILABLE",
    message: "无法读取设置概览，请确认后端仍在运行。",
  };
}

export function SettingsPage() {
  const session = useSettingsSession();
  const settings = useSessionResource(getSettingsView);
  const error = errorDetails(settings.error);
  const needsAuthorization =
    session.status === "unauthorized" ||
    (settings.error instanceof SettingsApiError &&
      settings.error.status === 401);

  return (
    <div className="page-stack">
      <Space orientation="vertical" size={6}>
        <Typography.Title level={2} className="page-heading">
          设置概览
        </Typography.Title>
        <Typography.Paragraph className="page-description">
          查看生效与待重启配置，编辑模型和任务绑定；保存、检测与激活分别确认。
        </Typography.Paragraph>
      </Space>

      {needsAuthorization ? (
        <UnauthorizedNotice error={session.error} />
      ) : session.status === "checking" ? (
        <Alert type="info" showIcon title="正在验证管理会话" />
      ) : settings.loading ? (
        <Card>
          <Skeleton active paragraph={{ rows: 4 }} />
        </Card>
      ) : settings.error ? (
        <Alert
          type={settings.error instanceof SettingsApiError && settings.error.status === 503 ? "warning" : "error"}
          showIcon
          title="设置概览读取失败"
          description={`${error.code}：${error.message}`}
          action={<Button onClick={settings.reload}>重试</Button>}
        />
      ) : settings.data ? (
        <Space orientation="vertical" size={20} style={{ width: "100%" }}>
          <Card
            title="当前事实"
            extra={
              <Tag color={settings.data.configuration === "available" ? "success" : "default"}>
                {settings.data.configuration}
              </Tag>
            }
          >
            <Descriptions
              size="small"
              column={1}
              items={[
                {
                  key: "environment",
                  label: "环境",
                  children: settings.data.environment ?? "unknown",
                },
                {
                  key: "environment_id",
                  label: "环境身份",
                  children: (
                    <span className="mono">
                      {settings.data.environment_id ?? "unknown"}
                    </span>
                  ),
                },
                {
                  key: "active",
                  label: "生效修订",
                  children: settings.data.active_revision ?? "未配置",
                },
                {
                  key: "pending",
                  label: "待生效修订",
                  children: settings.data.pending_revision ?? "无",
                },
                {
                  key: "restart",
                  label: "需要重启",
                  children: settings.data.restart_required ? "是" : "否",
                },
                {
                  key: "assembly",
                  label: "业务装配",
                  children: settings.data.business_assembly,
                },
                {
                  key: "static",
                  label: "静态资源",
                  children: settings.data.static_resources,
                },
              ]}
            />
          </Card>

          <SettingsEditor settings={settings.data} onFactsChanged={settings.reload} />

          {settings.data.warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              title="设置事实存在警告"
              description={settings.data.warnings.join("、")}
            />
          ) : null}

        </Space>
      ) : (
        <Card>
          <Typography.Text type="secondary">
            尚无设置概览；请从 Launcher 重新打开设置页。
          </Typography.Text>
        </Card>
      )}
    </div>
  );
}
