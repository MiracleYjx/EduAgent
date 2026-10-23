import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
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

import { getReadiness, getSetupReady, SettingsApiError } from "../../api/client";
import { aiReadiness } from "../../api/contracts";
import type { ReadinessReport, SetupReadyResponse } from "../../api/contracts";
import { ReadyTag } from "../../components/StatusTag";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useApiResource, useSessionResource } from "../../hooks/useApiResource";
import { OnboardingWizard } from "./OnboardingWizard";

function setupStatus(data: SetupReadyResponse | null) {
  if (data === null) {
    return null;
  }
  if (data.status === "ok") {
    return {
      ready: true,
      label: "Setup 服务可用",
      detail: "设置授权与静态入口已启动，不代表业务或 AI 已就绪。",
    };
  }
  return {
    ready: false,
    label: "Setup 服务未就绪",
    detail: data.detail ?? `失败检查：${data.failed_check ?? "unknown"}`,
  };
}

function privateErrorText(error: unknown): { code: string; message: string } {
  if (error instanceof SettingsApiError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: "READINESS_UNAVAILABLE",
    message: "无法读取分层就绪状态，请确认后端仍在运行。",
  };
}

function ReadinessSummary({ report }: { report: ReadinessReport }) {
  const ai = aiReadiness(report);
  return (
    <div className="status-grid">
      <Card className="status-card" title="Setup">
        <ReadyTag ready={report.setup.ready} readyText="就绪" />
        <div className="status-value">
          <Typography.Text type="secondary">
            {report.setup.reason ?? "设置面可用"}
          </Typography.Text>
        </div>
      </Card>
      <Card className="status-card" title="Business">
        <ReadyTag ready={report.business.ready} readyText="业务就绪" />
        <div className="status-value">
          <Space orientation="vertical" size={2}>
            <Typography.Text>
              装配：{report.business.assembly}
            </Typography.Text>
            <Typography.Text type="secondary">
              恢复：{report.business.recovery}
            </Typography.Text>
            {report.business.reason ? (
              <Typography.Text type="secondary">
                {report.business.reason}
              </Typography.Text>
            ) : null}
          </Space>
        </div>
      </Card>
      <Card className="status-card" title="AI">
        <Tag color={ai.evaluated ? "processing" : "default"}>
          {ai.evaluated ? ai.status : "未评估"}
        </Tag>
        <div className="status-value">
          <Typography.Text type="secondary">
            {ai.evaluated
              ? ai.reason
              : "查询不触发模型或付费 Probe；AI 状态只来自后端真实事实。"}
          </Typography.Text>
        </div>
      </Card>
    </div>
  );
}

export function OnboardingPage() {
  const navigate = useNavigate();
  const session = useSettingsSession();
  const publicSetup = useApiResource(getSetupReady, "setup-ready");
  const readiness = useSessionResource(getReadiness);
  useEffect(() => {
    if (readiness.error instanceof SettingsApiError && readiness.error.status === 403) {
      session.expire(readiness.error);
    }
  }, [readiness.error, session]);
  const state = setupStatus(publicSetup.data);
  const privateError = privateErrorText(readiness.error);
  const needsAuthorization =
    session.status === "unauthorized" ||
    (readiness.error instanceof SettingsApiError &&
      readiness.error.status === 401);

  return (
    <div className="page-stack">
      <Space orientation="vertical" size={6}>
        <Typography.Title level={2} className="page-heading">
          高级设置
        </Typography.Title>
        <Typography.Paragraph className="page-description">
          这里可以展开调整数据库、Redis、资料目录和 AI。普通使用请返回“开始使用”；所有持久变更与可能收费的操作仍需明确触发。
        </Typography.Paragraph>
      </Space>

      {needsAuthorization ? (
        <UnauthorizedNotice error={session.error} />
      ) : session.status === "checking" ? (
        <Alert
          type="info"
          showIcon
          title="正在验证一次性授权码"
          description="授权码只在本次页面内存交换，成功后不会写入地址栏或浏览器存储。"
        />
      ) : (
        <Alert
          type="success"
          showIcon
          title="管理会话已授权"
          description="当前页面可以读取分层就绪与已保存的非敏感设置概览。"
        />
      )}

      <Button onClick={() => navigate("/setup/quickstart")}>返回开始使用</Button>

      <Card
        title="Setup 状态"
        extra={
          <Button onClick={publicSetup.reload} loading={publicSetup.loading}>
            刷新
          </Button>
        }
      >
        {publicSetup.loading ? <Skeleton active paragraph={{ rows: 2 }} /> : null}
        {publicSetup.error ? (
          <Alert
            type="error"
            showIcon
            title="Setup 状态读取失败"
            description={
              publicSetup.error instanceof Error
                ? publicSetup.error.message
                : "无法连接设置服务。"
            }
          />
        ) : null}
        {state ? (
          <Space orientation="vertical" size={16} style={{ width: "100%" }}>
            <Space wrap>
              <ReadyTag ready={state.ready} readyText={state.label} />
              <Typography.Text type="secondary">{state.detail}</Typography.Text>
            </Space>
            <Descriptions
              size="small"
              column={1}
              items={[
                {
                  key: "configuration",
                  label: "配置读取",
                  children: publicSetup.data?.setup.configuration ?? "unknown",
                },
                {
                  key: "authorization",
                  label: "设置授权",
                  children:
                    publicSetup.data?.setup.settings_authorization ?? "unknown",
                },
                {
                  key: "static",
                  label: "静态资源",
                  children: publicSetup.data?.setup.static_resources ?? "unknown",
                },
              ]}
            />
          </Space>
        ) : null}
      </Card>

      {session.status === "authorized" ? (
        <Card title="分层状态" extra={<Tag color="blue">来自 readiness API</Tag>}>
          {readiness.loading ? (
            <Skeleton active paragraph={{ rows: 3 }} />
          ) : readiness.data && !readiness.error ? (
            <ReadinessSummary report={readiness.data} />
          ) : (
            <Alert
              type="warning"
              showIcon
              title="分层状态暂不可用"
              description={`${privateError.code}：${privateError.message}`}
              action={<Button onClick={readiness.reload}>重试</Button>}
            />
          )}
        </Card>
      ) : null}

      {session.status === "authorized" ? (
        <OnboardingWizard onFactsChanged={readiness.reload} />
      ) : (
        <Card title="首次启动向导">
          <Typography.Text>授权后可恢复已保存草稿与向导进度；不会恢复未保存的秘密输入。</Typography.Text>
        </Card>
      )}
    </div>
  );
}
