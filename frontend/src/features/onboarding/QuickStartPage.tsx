import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Skeleton,
  Space,
  Steps,
  Tag,
  Typography,
} from "antd";
import { useNavigate } from "react-router-dom";

import * as api from "../../api/client";
import type {
  OnboardingReport,
  OperationView,
  SettingsView,
  SetupDataState,
  SetupRecommendation,
} from "../../api/contracts";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";

type Screen = "welcome" | "prepare" | "enter";
type Stage = "draft" | "initialize" | "activate" | "ready";
type FlowStatus = "idle" | "running" | "failed" | "success";

interface QuickStartData {
  recommendation: SetupRecommendation;
  dataState: SetupDataState;
  onboarding: OnboardingReport;
  settings: SettingsView;
}

interface FlowState {
  status: FlowStatus;
  stage: Stage;
  error: string | null;
}

interface DraftReceipt {
  id: string;
  parentRevision: string | null;
}

interface InitializationReceipt {
  operationId: string | null;
  succeeded: boolean;
}

const stageOrder: Stage[] = ["draft", "initialize", "activate", "ready"];
const stageLabels = [
  "创建配置草稿",
  "初始化学习空间",
  "启用设置",
  "等待学习空间就绪",
];

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function readQuickStart(
  token: string,
  signal?: AbortSignal,
): Promise<QuickStartData> {
  const [recommendation, dataState, onboarding, settings] = await Promise.all([
    api.getSetupRecommendation(token, signal),
    api.getSetupDataState(token, signal),
    api.getOnboarding(token, signal),
    api.getSettingsView(token, signal),
  ]);
  return { recommendation, dataState, onboarding, settings };
}

function dataReason(reason: string | null | undefined): string {
  switch (reason) {
    case "DATABASE_UNREACHABLE":
      return "数据库暂时连接不上";
    case "STORAGE_UNREADABLE":
      return "资料目录暂时读不到";
    case "STORAGE_NOT_DIRECTORY":
      return "资料位置不是文件夹";
    case "PARTIAL_DATA_DETECTED":
      return "只检测到部分已有数据";
    case "UNRECOGNIZED_SCHEMA":
      return "数据库里已有其他结构";
    case "SCHEMA_WITHOUT_ROWS":
      return "检测到学习数据表，但暂时没有读到关键内容";
    case "ROW_EVIDENCE_UNAVAILABLE":
      return "无法只读检查关键表内容";
    default:
      return "暂时无法确认目标是否为空";
  }
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

function humanError(error: unknown): string {
  if (error instanceof api.SettingsApiError) {
    switch (error.code) {
      case "NETWORK_ERROR":
        return "无法连接设置服务，请确认 EduAgent 仍在运行。";
      case "OVERRIDE_ACKNOWLEDGEMENT_REQUIRED":
        return "当前运行设置与将要保存的值不同，系统没有替你绕过确认。请到高级设置核对后再启用。";
      case "PROBE_REQUIRED":
      case "PROBE_EXPIRED":
        return "AI 配置需要一次检测才能启用；请到高级设置完成检测。";
      case "REVISION_CONFLICT":
        return "设置已被其他窗口更新，请重新打开首次设置后再试。";
      case "DEPENDENCY_UNAVAILABLE":
        return "数据库或缓存服务暂时不可用，学习空间还没有准备好。";
      case "STORAGE_NOT_WRITABLE":
        return "资料目录不可写，请检查目录权限后重试。";
      case "DATABASE_UNREACHABLE":
        return "数据库连接失败，请检查数据库服务是否启动。";
      case "INITIALIZATION_RUNNER_UNAVAILABLE":
        return "当前运行方式不能执行受控初始化，请打开高级设置查看迁移指引。";
      default:
        return error.message;
    }
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "准备学习空间时遇到未预期的问题，请重试或打开高级设置。";
}

function operationError(operation: OperationView): string {
  switch (operation.error_code) {
    case "DATABASE_UNREACHABLE":
      return "数据库连接失败，未能创建学习空间。请检查数据库服务后重试。";
    case "STORAGE_NOT_WRITABLE":
      return "资料目录不可写，未能创建学习空间。请检查目录权限后重试。";
    case "MIGRATION_FAILED":
      return "数据库结构创建失败，学习空间还没有准备好。请检查数据库权限后重试。";
    default:
      return operation.error_code
        ? `学习空间初始化未完成（${operation.error_code}）。请重试或打开高级设置。`
        : "学习空间初始化未完成，请重试或打开高级设置。";
  }
}

function sourceLabel(source: string | undefined): string {
  switch (source) {
    case "process_env":
      return "当前运行环境";
    case "environment_profile":
      return "本机环境";
    case "active_configuration":
      return "已保存设置";
    default:
      return "未检测到";
  }
}

function statusOf(
  state: FlowState,
  index: number,
): "wait" | "process" | "finish" | "error" {
  if (state.status === "success") return "finish";
  const current = stageOrder.indexOf(state.stage);
  if (index < current) return "finish";
  if (index > current) return "wait";
  if (state.status === "failed") return "error";
  return "process";
}

async function waitForOperationById(
  token: string,
  operationId: string,
): Promise<OperationView> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const operation = await api.getOperation(token, operationId);
    if (operation.status === "succeeded") return operation;
    if (operation.status === "failed" || operation.status === "interrupted") {
      throw new Error(operationError(operation));
    }
    await sleep(700);
  }
  throw new Error("创建学习空间用时过长，请到高级设置查看操作状态。");
}

async function waitForReady(): Promise<void> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    try {
      if (await api.getBusinessReady()) return;
    } catch {
      // The backend may be restarting; keep the page and poll the public
      // readiness endpoint. No private token is retried here.
    }
    await sleep(1_000);
  }
  throw new Error("学习空间还没有在预期时间内就绪。请重试或打开高级设置。");
}

export function QuickStartPage() {
  const session = useSettingsSession();
  const navigate = useNavigate();
  const resource = useSessionResource(readQuickStart);
  const [screen, setScreen] = useState<Screen>("welcome");
  const [flow, setFlow] = useState<FlowState>({
    status: "idle",
    stage: "draft",
    error: null,
  });
  const mounted = useRef(true);
  const draftReceipt = useRef<DraftReceipt | null>(null);
  const validated = useRef(false);
  const initializationReceipt = useRef<InitializationReceipt | null>(null);
  const activationRevision = useRef<string | null>(null);
  const restartRequested = useRef(false);
  const startedAt = useRef<number | null>(null);
  const stageRef = useRef<Stage>("draft");
  const busyRef = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (flow.status !== "success") return;
    const timer = window.setTimeout(() => {
      if (mounted.current) navigate("/", { replace: true });
    }, 1_800);
    return () => window.clearTimeout(timer);
  }, [flow.status, navigate]);

  const data = resource.data;
  const draft = data?.onboarding.draft ?? null;
  const businessReady = data?.onboarding.readiness.business.ready ?? false;
  const hasSavedConfiguration = Boolean(
    data?.settings.active_revision || data?.settings.pending_revision,
  );
  const canAutoPrepare = Boolean(
    data &&
      data.recommendation.available &&
      data.recommendation.auto_apply &&
      data.dataState.status === "new" &&
      data.dataState.can_initialize &&
      !draft &&
      !hasSavedConfiguration,
  );

  async function runFlow() {
    if (!session.token || !data || busyRef.current) return;
    const token = session.token;
    busyRef.current = true;
    setScreen("enter");
    stageRef.current = "draft";
    setFlow({ status: "running", stage: "draft", error: null });
    if (startedAt.current === null) startedAt.current = Date.now();
    try {
      if (!draftReceipt.current) {
        setFlow({ status: "running", stage: "draft", error: null });
        const created = await api.createDraft(token, {
          base_revision: null,
          changes: { business: data.recommendation.business ?? {} },
          acknowledge_override: false,
        });
        draftReceipt.current = {
          id: created.revision_id,
          parentRevision: created.parent_revision ?? null,
        };
      }
      const receipt = draftReceipt.current;
      if (!receipt) {
        throw new Error("配置草稿没有保存成功，请重试或打开高级设置。");
      }
      if (!validated.current) {
        const checked = await api.validateDraft(token, receipt.id, {
          expected_revision: receipt.parentRevision,
        });
        if (!checked.valid) {
          const detail = checked.errors?.[0];
          throw new Error(
            detail
              ? `推荐配置未通过检查：${detail.message} 请到高级设置核对具体字段。`
              : "推荐配置未通过检查，请到高级设置核对具体字段。",
          );
        }
        validated.current = true;
      }

      if (!initializationReceipt.current?.succeeded) {
        stageRef.current = "initialize";
        setFlow({ status: "running", stage: "initialize", error: null });
        const resumedOperationId = initializationReceipt.current?.operationId;
        if (resumedOperationId) {
          // A previous attempt may still be running. Resume its receipt rather
          // than creating a second initialization.
          try {
            await waitForOperationById(token, resumedOperationId);
          } catch (error) {
            initializationReceipt.current = null;
            throw error;
          }
          initializationReceipt.current = {
            operationId: resumedOperationId,
            succeeded: true,
          };
        } else {
          const preview = await api.previewInitialization(token, {
            draft_revision: receipt.id,
          });
          if (!preview.allowed || preview.destructive) {
            throw new Error(
              "当前目标不允许无损创建学习空间，请到高级设置检查迁移目标。",
            );
          }
          const operation = await api.applyInitialization(token, {
            confirmation_id: preview.preview_id,
            expected_revision: receipt.id,
          });
          if (operation.status === "failed" || operation.status === "interrupted") {
            initializationReceipt.current = null;
            throw new Error(operationError(operation));
          }
          if (operation.status === "succeeded") {
            initializationReceipt.current = {
              operationId: operation.operation_id,
              succeeded: true,
            };
          } else {
            initializationReceipt.current = {
              operationId: operation.operation_id,
              succeeded: false,
            };
            try {
              await waitForOperationById(token, operation.operation_id);
            } catch (error) {
              initializationReceipt.current = null;
              throw error;
            }
            initializationReceipt.current = {
              operationId: operation.operation_id,
              succeeded: true,
            };
          }
        }
      }

      if (!activationRevision.current) {
        stageRef.current = "activate";
        setFlow({ status: "running", stage: "activate", error: null });
        const activation = await api.activateDraft(token, receipt.id, {
          expected_active_revision: data.settings.active_revision,
          probe_ids: [],
          acknowledge_override: false,
        });
        activationRevision.current = activation.revision_id;
      }

      if (!restartRequested.current) {
        stageRef.current = "ready";
        setFlow({ status: "running", stage: "ready", error: null });
        const restart = await api.restartApplication(token, {
          expected_revision: activationRevision.current,
        });
        if (restart.status !== "restart_requested") {
          throw new Error(
            "学习空间已经创建，但当前启动方式不能自动完成重启；请打开高级设置完成最后一步。",
          );
        }
        restartRequested.current = true;
      }

      stageRef.current = "ready";
      setFlow({ status: "running", stage: "ready", error: null });
      await waitForReady();
      if (!mounted.current) return;
      setFlow({ status: "success", stage: "ready", error: null });
    } catch (error) {
      if (
        error instanceof api.SettingsApiError &&
        (error.status === 401 || error.status === 403)
      ) {
        session.expire(error);
      }
      if (mounted.current) {
        setFlow({
          status: "failed",
          stage: stageRef.current,
          error: humanError(error),
        });
      }
    } finally {
      busyRef.current = false;
    }
  }

  function enterExisting() {
    startedAt.current = Date.now();
    setScreen("enter");
    setFlow({ status: "success", stage: "ready", error: null });
  }

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

  if (screen === "welcome") {
    return (
      <div className="page-stack">
        <Space orientation="vertical" size={10}>
          <Typography.Title level={2} className="page-heading">
            开始使用 EduAgent
          </Typography.Title>
          <Typography.Paragraph className="page-description">
            为你准备好学习空间，大约需要 20 秒。
          </Typography.Paragraph>
        </Space>

        {resource.error ? (
          <Alert
            type="warning"
            showIcon
            title="暂时无法读取推荐配置"
            description={humanError(resource.error)}
            action={<Button onClick={resource.reload}>重新检测</Button>}
          />
        ) : null}

        <Space wrap>
          <Button
            type="primary"
            size="large"
            loading={resource.loading}
            disabled={resource.loading || !data}
            onClick={() => setScreen("prepare")}
          >
            开始使用
          </Button>
          <Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>
        </Space>
        <Typography.Text type="secondary">
          高级设置仍可调整数据库、Redis、资料目录和 AI 凭据。
        </Typography.Text>
      </div>
    );
  }

  if (screen === "prepare") {
    if (resource.loading) {
      return <Card><Skeleton active paragraph={{ rows: 6 }} /></Card>;
    }
    if (resource.error || !data) {
      return (
        <Alert
          type="error"
          showIcon
          title="准备学习空间前无法读取状态"
          description={humanError(resource.error)}
          action={
            <Space>
              <Button onClick={resource.reload}>重新检测</Button>
              <Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>
            </Space>
          }
        />
      );
    }

    const recommendedBusiness = data.recommendation.business;
    const database = recommendedBusiness?.database_locator;
    const redis = recommendedBusiness?.redis_locator;
    const storage = recommendedBusiness?.storage_root;
    const blockedByRecommendation = !data.recommendation.available || !data.recommendation.auto_apply;
    const existing = data.dataState.status === "existing";
    // An existing learning space normally has an active revision; that must not
    // hide the direct-entry exit. A saved configuration only blocks auto-create.
    const blockedBySavedSettings = Boolean(draft) || (hasSavedConfiguration && !existing);
    const blockedByData =
      data.dataState.status === "needs_confirmation" ||
      data.dataState.status === "unknown";

    return (
      <div className="page-stack">
        <Space orientation="vertical" size={6}>
          <Typography.Title level={2} className="page-heading">
            准备学习空间
          </Typography.Title>
          <Typography.Paragraph className="page-description">
            这些推荐值只用于这次准备，不会暴露密码；创建前不会写入任何配置。
          </Typography.Paragraph>
        </Space>

        <Card
          title="推荐配置"
          extra={
            <Tag color={data.recommendation.auto_apply ? "success" : "warning"}>
              {data.recommendation.auto_apply ? "可以自动使用" : "需要确认"}
            </Tag>
          }
        >
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
                children: redis ? `${redis.host}:${redis.port}/${redis.database}` : "未检测到",
              },
              {
                key: "source",
                label: "检测来源",
                children: [
                  sourceLabel(data.recommendation.sources?.["business.database_locator"]),
                  sourceLabel(data.recommendation.sources?.["business.storage_root"]),
                ]
                  .filter((value, index, all) => all.indexOf(value) === index)
                  .join(" / "),
              },
            ]}
          />
          {data.recommendation.warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              title="推荐配置需要额外确认"
              description="检测到的连接需要额外凭据或覆盖确认；请到高级设置完成。"
              style={{ marginTop: 12 }}
            />
          ) : null}
        </Card>

        <Alert
          type={
            data.dataState.status === "new"
              ? "success"
              : data.dataState.status === "existing"
                ? "info"
                : "warning"
          }
          showIcon
          title={
            data.dataState.status === "new"
              ? "将创建全新学习空间"
              : data.dataState.status === "existing"
                ? "检测到已有学习空间，将直接进入"
                : data.dataState.status === "needs_confirmation"
                  ? "检测到部分已有数据，需要确认"
                  : "暂时无法确认数据状态"
          }
          description={
            data.dataState.status === "new"
              ? "点击创建后会建立新的学习资料库；不会影响其他项目或已有数据。"
              : data.dataState.status === "existing"
                ? "已有数据不会被替换或初始化；业务就绪时可以直接进入。"
                : `${dataReason(data.dataState.reason)}。为避免覆盖，三屏不会自动初始化，请到高级设置核对。`
          }
        />

        {blockedByRecommendation ? (
          <Alert
            type="warning"
            showIcon
            title="没有检测到可自动应用的完整推荐配置"
            description={`${recommendationReason(data.recommendation.reason)}。请到高级设置填写数据库、Redis 和资料目录。`}
          />
        ) : null}

        {blockedBySavedSettings ? (
          <Alert
            type={draft ? "info" : "warning"}
            showIcon
            title={draft ? "检测到已暂存的设置" : "检测到已保存的设置"}
            description="三屏不会覆盖已有草稿或已保存设置；请到高级设置继续或核对。"
          />
        ) : null}

        {existing && !businessReady ? (
          <Alert
            type="info"
            showIcon
            title="已有学习空间，但业务还没有就绪"
            description="三屏不会自动初始化已有数据；请到高级设置核对连接和迁移状态。"
          />
        ) : null}

        <Space wrap>
          {canAutoPrepare ? (
            <Button type="primary" onClick={() => void runFlow()}>
              创建并进入
            </Button>
          ) : null}
          {existing && businessReady && !blockedBySavedSettings ? (
            <Button type="primary" onClick={enterExisting}>
              进入学习
            </Button>
          ) : null}
          {!canAutoPrepare && !(existing && businessReady && !blockedBySavedSettings) ? (
            <Button type="primary" onClick={() => navigate("/setup/advanced")}>
              去高级设置
            </Button>
          ) : null}
          {blockedByData && !blockedBySavedSettings ? (
            <Typography.Text type="secondary">
              需要确认已有数据时，三屏不会自动创建或覆盖。
            </Typography.Text>
          ) : null}
        </Space>
        <Space wrap>
          <Button onClick={resource.reload} disabled={flow.status === "running"}>
            重新检测
          </Button>
          <Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>
        </Space>
      </div>
    );
  }

  const steps = stageLabels.map((label, index) => ({
    title: label,
    status: statusOf(flow, index),
  }));
  const elapsedSeconds =
    flow.status === "success" && startedAt.current !== null
      ? Math.max(1, Math.round((Date.now() - startedAt.current) / 1000))
      : null;

  return (
    <div className="page-stack">
      <Space orientation="vertical" size={6}>
        <Typography.Title level={2} className="page-heading">
          {flow.status === "success"
            ? "学习空间已准备好"
            : flow.status === "failed"
              ? "学习空间还没有准备好"
              : "正在进入学习"}
        </Typography.Title>
        <Typography.Paragraph className="page-description">
          {flow.status === "success"
            ? "正在打开你的学习空间；也可以立即点击进入。"
            : flow.status === "failed"
              ? "任一步失败都会停在这里，不会自动重试。"
              : "正在创建草稿、初始化、启用并等待就绪；请保持页面打开。"}
        </Typography.Paragraph>
      </Space>

      <Card>
        <Steps
          size="small"
          orientation="vertical"
          current={
            flow.status === "success"
              ? stageLabels.length
              : stageOrder.indexOf(flow.stage)
          }
          status={
            flow.status === "failed"
              ? "error"
              : flow.status === "success"
                ? "finish"
                : "process"
          }
          items={steps}
        />
      </Card>

      {flow.status === "failed" ? (
        <Alert
          type="error"
          showIcon
          title="本次准备没有完成"
          description={flow.error}
          action={
            <Space>
              <Button type="primary" onClick={() => void runFlow()}>
                重试
              </Button>
              <Button onClick={() => navigate("/setup/advanced")}>高级设置</Button>
            </Space>
          }
        />
      ) : null}

      {flow.status === "success" ? (
        <Alert
          type="success"
          showIcon
          title="学习空间已准备好"
          description={
            elapsedSeconds
              ? `准备完成，用时约 ${elapsedSeconds} 秒；页面会自动进入学习。`
              : "页面会自动进入学习。"
          }
          action={
            <Button type="primary" onClick={() => navigate("/", { replace: true })}>
              立即进入学习
            </Button>
          }
        />
      ) : null}

      {flow.status === "running" ? (
        <Typography.Text type="secondary">
          正在完成准备。若需要重启应用，本页会自动等待它恢复，不需要手动重启。
        </Typography.Text>
      ) : null}
    </div>
  );
}
