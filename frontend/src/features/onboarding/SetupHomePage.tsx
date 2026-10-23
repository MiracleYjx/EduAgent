import { useState } from "react";
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
import { useNavigate } from "react-router-dom";

import * as api from "../../api/client";
import type {
  OnboardingReport,
  SetupDataState,
  SetupRecommendation,
} from "../../api/contracts";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";

interface SetupHomeData {
  recommendation: SetupRecommendation;
  dataState: SetupDataState;
  onboarding: OnboardingReport;
}

async function readSetupHome(token: string, signal?: AbortSignal): Promise<SetupHomeData> {
  const [recommendation, dataState, onboarding] = await Promise.all([
    api.getSetupRecommendation(token, signal),
    api.getSetupDataState(token, signal),
    api.getOnboarding(token, signal),
  ]);
  return { recommendation, dataState, onboarding };
}

function sourceLabel(source: string | undefined): string {
  switch (source) {
    case "process_env":
      return "来自运行环境";
    case "environment_profile":
      return "来自本机环境";
    case "active_configuration":
      return "来自已保存设置";
    default:
      return "未检测到";
  }
}

function friendlyError(error: unknown): string {
  if (error instanceof api.SettingsApiError) {
    if (error.code === "NETWORK_ERROR") {
      return "无法连接设置服务，请确认 EduAgent 仍在运行。";
    }
    if (error.code === "DEPENDENCY_UNAVAILABLE") {
      return "设置服务暂时不可用，请稍后再试。";
    }
    return error.message;
  }
  return "读取设置状态未完成，请重试。";
}

function recommendationReason(reason: string | null | undefined): string {
  switch (reason) {
    case "DATABASE_NOT_CONFIGURED":
      return "没有检测到数据库连接";
    case "REDIS_NOT_CONFIGURED":
      return "没有检测到缓存服务";
    case "STORAGE_NOT_CONFIGURED":
      return "没有检测到资料目录";
    case "CREDENTIAL_OR_SOURCE_REQUIRED":
      return "检测到的连接需要额外凭据";
    default:
      return "需要手动确认连接设置";
  }
}

function dataReason(reason: string | null | undefined): string {
  switch (reason) {
    case "DATABASE_UNREACHABLE":
      return "数据库暂时连接不上";
    case "STORAGE_UNREADABLE":
      return "资料目录暂时读不到";
    case "PARTIAL_DATA_DETECTED":
      return "只检测到部分已有数据";
    case "UNRECOGNIZED_SCHEMA":
      return "数据库里已有其他结构";
    case "SCHEMA_WITHOUT_ROWS":
      return "检测到学习数据表，但暂时没有读到关键内容";
    default:
      return "暂时无法确认目标是否为空";
  }
}

function dataStateMessage(state: SetupDataState): {
  type: "success" | "info" | "warning";
  title: string;
  description: string;
} {
  switch (state.status) {
    case "new":
      return {
        type: "success",
        title: "这是一个全新的学习空间",
        description: "确认推荐配置后，会创建新的学习资料库；不会影响其他项目或已有数据。",
      };
    case "existing":
      return {
        type: "info",
        title: "检测到已有学习数据",
        description: "为避免覆盖，向导不会自动初始化。可以进入已有学习空间，或到高级设置核对连接。",
      };
    case "needs_confirmation":
      return {
        type: "warning",
        title: "检测到部分已有数据，需要确认",
        description: "数据库或资料目录已有内容，但无法确认是否属于当前学习空间；请到高级设置核对后再操作。",
      };
    default:
      return {
        type: "warning",
        title: "暂时无法完成只读检测",
        description: `${dataReason(state.reason)}。请检查数据库和资料目录，或到高级设置手动核对。`,
      };
  }
}

export function SetupHomePage() {
  const session = useSettingsSession();
  const navigate = useNavigate();
  const resource = useSessionResource(readSetupHome);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  if (session.status === "unauthorized") {
    return <UnauthorizedNotice error={session.error} />;
  }
  if (session.status === "checking") {
    return (
      <Alert
        type="info"
        showIcon
        title="正在验证一次性授权码"
        description="授权码只在本次页面内存交换，不会写入地址栏或浏览器存储。"
      />
    );
  }
  if (resource.loading) {
    return <Card><Skeleton active paragraph={{ rows: 5 }} /></Card>;
  }
  if (resource.error || !resource.data) {
    return (
      <Alert
        type="error"
        showIcon
        title="设置状态读取失败"
        description={friendlyError(resource.error)}
        action={<Button onClick={resource.reload}>重新读取</Button>}
      />
    );
  }

  const { recommendation, dataState, onboarding } = resource.data;
  const business = recommendation.business;
  const database = business?.database_locator;
  const storage = business?.storage_root;
  const draft = onboarding.draft;
  const businessReady = onboarding.readiness.business.ready;
  const message = dataStateMessage(dataState);
  const canCreate = Boolean(
    recommendation.auto_apply &&
      dataState.can_initialize &&
      !draft,
  );

  async function useRecommended() {
    if (!session.token || !canCreate || !business || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const created = await api.createDraft(session.token, {
        base_revision: null,
        changes: { business },
        acknowledge_override: false,
      });
      const checked = await api.validateDraft(session.token, created.revision_id, {
        expected_revision: created.parent_revision,
      });
      if (!checked.valid) {
        setActionError("推荐配置未通过检查，请到高级设置查看具体字段。");
        return;
      }
      await api.saveOnboarding(session.token, {
        expected_revision: onboarding.progress.revision,
        current_step: 5,
        draft_revision: created.revision_id,
      });
      navigate("/setup/advanced");
    } catch (cause) {
      setActionError(friendlyError(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-stack">
      <Space orientation="vertical" size={6}>
        <Typography.Title level={2} className="page-heading">
          开始使用 EduAgent
        </Typography.Title>
        <Typography.Paragraph className="page-description">
          {recommendation.available
            ? "已经为这台电脑检测好推荐配置。普通使用不需要填写数据库、端口或路径。"
            : "还没有检测到完整的自动配置。请进入高级设置填写连接信息；普通使用仍不需要手动改代码。"}
        </Typography.Paragraph>
      </Space>

      {actionError ? (
        <Alert
          type="error"
          showIcon
          title="下一步未完成"
          description={actionError}
          action={<Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>}
        />
      ) : null}

      <Card
        title="推荐配置"
        extra={<Tag color={recommendation.auto_apply ? "success" : "warning"}>
          {recommendation.auto_apply ? "可以自动使用" : "需要确认"}
        </Tag>}
      >
        {recommendation.available ? (
          <Space orientation="vertical" size={16} style={{ width: "100%" }}>
            <Alert
              type={recommendation.auto_apply ? "success" : "warning"}
              showIcon
              title={recommendation.auto_apply ? "已检测到可用的学习环境" : "推荐配置还需要确认"}
              description={
                recommendation.auto_apply
                  ? "这些值来自当前运行环境，只用于预填；点击下一步后才会保存草稿。"
                  : "缺少部分推荐值或需要额外凭据；请到高级设置手动填写。"
              }
            />
            <Descriptions
              size="small"
              column={1}
              items={[
                {
                  key: "storage",
                  label: "学习资料保存位置",
                  children: storage ?? "未检测到",
                },
                {
                  key: "database",
                  label: "学习数据库",
                  children: database
                    ? `${database.host}:${database.port}/${database.database}（${database.user}）`
                    : "未检测到",
                },
                {
                  key: "redis",
                  label: "缓存服务",
                  children: business?.redis_locator
                    ? `${business.redis_locator.host}:${business.redis_locator.port}`
                    : "未检测到",
                },
                {
                  key: "source",
                  label: "检测来源",
                  children: [
                    sourceLabel(recommendation.sources?.["business.database_locator"]),
                    sourceLabel(recommendation.sources?.["business.storage_root"]),
                  ].filter((value, index, all) => all.indexOf(value) === index).join(" / "),
                },
              ]}
            />
            <Typography.Text type="secondary">
              AI 老师可以稍后配置；现在不填 API Key，也可以先管理资料、组卷和本地评分。
            </Typography.Text>
          </Space>
        ) : (
          <Alert
            type="warning"
            showIcon
            title="没有自动检测到完整配置"
            description={`${recommendationReason(recommendation.reason)}。请到高级设置填写数据库、Redis 和资料目录。`}
          />
        )}
      </Card>

      <Alert
        type={message.type}
        showIcon
        title={message.title}
        description={message.description}
        action={
          <Button onClick={resource.reload} disabled={busy}>
            重新检测
          </Button>
        }
      />

      {draft ? (
        <Alert
          type="info"
          showIcon
          title="检测到已暂存的设置"
          description="继续后会打开高级设置，恢复已保存的非敏感字段；不会自动重放写操作。"
          action={<Button onClick={() => navigate("/setup/advanced")}>继续配置</Button>}
        />
      ) : null}

      <Space wrap>
        {canCreate ? (
          <Button type="primary" loading={busy} disabled={busy} onClick={() => void useRecommended()}>
            使用推荐配置，下一步
          </Button>
        ) : null}
        {draft ? (
          <Button type="primary" onClick={() => navigate("/setup/advanced")}>
            继续配置
          </Button>
        ) : null}
        {dataState.status === "existing" && businessReady ? (
          <Button type="primary" onClick={() => navigate("/")}>
            进入学习
          </Button>
        ) : null}
        <Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>
      </Space>

      <Typography.Text type="secondary">
        高级设置里仍然可以展开编辑数据库、Redis、资料目录和 AI 凭据；这些字段没有被删除。
      </Typography.Text>
    </div>
  );
}
