import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";

import { DiagnosticActions } from "./DiagnosticActions";

import { getReadiness, SettingsApiError } from "../../api/client";
import { aiReadiness } from "../../api/contracts";
import type {
  ReadinessCheck,
  ReadinessChecks,
  ReadinessReport,
} from "../../api/contracts";
import { CheckStatusTag, ReadyTag } from "../../components/StatusTag";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";

const checkLabels: Record<keyof ReadinessChecks, string> = {
  business_config: "业务配置",
  jwt_reference: "JWT 引用",
  postgres: "PostgreSQL",
  vector: "pgvector",
  migration: "迁移版本",
  schema: "表结构",
  redis: "Redis",
  storage: "持久目录",
};

interface CheckRow {
  key: string;
  label: string;
  status: ReadinessCheck["status"];
  code: string | null;
  detail: string | null;
}

function checkRows(report: ReadinessReport): CheckRow[] {
  return (
    Object.entries(report.business_prerequisites.checks) as Array<
      [keyof ReadinessChecks, ReadinessCheck]
    >
  ).map(([key, value]) => ({
    key,
    label: checkLabels[key] ?? key,
    status: value.status,
    code: value.code ?? null,
    detail: value.detail ?? null,
  }));
}

function errorDescription(error: unknown): {
  code: string;
  message: string;
  status: number | null;
} {
  if (error instanceof SettingsApiError) {
    return { code: error.code, message: error.message, status: error.status };
  }
  return {
    code: "REQUEST_FAILED",
    message: "无法读取分层就绪状态，请确认后端仍在运行。",
    status: null,
  };
}

function DiagnosticContent({ report: initial }: { report: ReadinessReport }) {
  const [report, setReport] = useState(initial);
  const rows = checkRows(report);
  const ai = aiReadiness(report);
  return (
    <Space orientation="vertical" size={20} style={{ width: "100%" }}>
      <div className="status-grid">
        <Card className="status-card" title="Setup">
          <ReadyTag ready={report.setup.ready} readyText="就绪" />
          <div className="status-value">
            <Typography.Text type="secondary">
              {report.setup.reason ?? "设置面可用"}
            </Typography.Text>
          </div>
        </Card>
        <Card className="status-card" title="Business Prerequisites">
          <ReadyTag
            ready={report.business_prerequisites.ready}
            readyText="前置条件通过"
            notReadyText="前置条件未通过"
          />
          <div className="status-value">
            <Typography.Text type="secondary">
              只表示依赖检查，不包含业务装配或模型可用性。
            </Typography.Text>
          </div>
        </Card>
        <Card className="status-card" title="Business">
          <ReadyTag ready={report.business.ready} readyText="业务就绪" />
          <div className="status-value">
            <Space orientation="vertical" size={2}>
              <Typography.Text>装配：{report.business.assembly}</Typography.Text>
              <Typography.Text>恢复：{report.business.recovery}</Typography.Text>
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
                : "没有真实计算结果；页面不会把未评估显示为可用。"}
            </Typography.Text>
          </div>
        </Card>
      </div>

      <Card title="业务前置检查">
        <Table<CheckRow>
          rowKey="key"
          size="small"
          pagination={false}
          dataSource={rows}
          columns={[
            { title: "检查项", dataIndex: "label", key: "label" },
            {
              title: "状态",
              dataIndex: "status",
              key: "status",
              render: (status: ReadinessCheck["status"]) => (
                <CheckStatusTag status={status} />
              ),
            },
            {
              title: "安全码",
              dataIndex: "code",
              key: "code",
              render: (value: string | null) => value ?? "—",
            },
            {
              title: "说明",
              dataIndex: "detail",
              key: "detail",
              render: (value: string | null) => value ?? "—",
            },
          ]}
        />
      </Card>

      <Card title="逐任务 AI 就绪">
        <Typography.Paragraph>以下逐项显示绑定、能力、凭据和接纳事实；unknown 表示证据不足，不推测为密码错误。接纳窗口用于候选激活，已激活事实不会因时间经过自动重测。</Typography.Paragraph>
        {Object.entries(ai.tasks ?? {}).length === 0 ? <Typography.Text>暂无逐任务检测事实；不代表所有模型可用。</Typography.Text> : Object.entries(ai.tasks ?? {}).map(([task, fact]) => <Card size="small" key={task} title={task} style={{ marginBottom: 12 }}>
          <Space orientation="vertical" size={4}>
            <Typography.Text>状态：{fact.ready ? "ready" : "not_ready"} · {fact.reason}</Typography.Text>
            <Typography.Text>配置：{fact.configured ? "已配置" : "未配置"}；能力：{fact.capability?.join("、") || "不适用 / 无声明"}</Typography.Text>
            <Typography.Text>凭据：{fact.credential}；接纳：{fact.activation_validation}</Typography.Text>
            <Typography.Text>最近检测：{fact.last_probe?.status ?? "unknown"} · {fact.last_probe?.tested_at ?? "无检测时间"}；候选接纳期限：{fact.last_probe?.activation_expires_at ?? "无"}</Typography.Text>
            <Typography.Text>最近运行：{fact.last_runtime_result ?? "unknown"}；依赖：{fact.dependencies?.join("、") || "无"}</Typography.Text>
          </Space>
        </Card>)}
      </Card>
      {report.candidate_diagnostics ? <Card title="候选依赖诊断（与生效配置分列）">{Object.entries(report.candidate_diagnostics).map(([name, check]) => <Typography.Paragraph key={name}>{name}：{check.status} · {check.code ?? "无错误码"} · {check.detail ?? ""}</Typography.Paragraph>)}</Card> : null}
      <DiagnosticActions onReport={setReport} />

      <Card title="环境与运行事实">
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "environment",
              label: "环境",
              children: report.environment ?? "unknown",
            },
            {
              key: "static",
              label: "静态资源",
              children: report.static_resources,
            },
            {
              key: "revision",
              label: "配置修订",
              children:
                report.business_prerequisites.configuration_revision ?? "未配置",
            },
            {
              key: "restart",
              label: "重启需要",
              children: report.restart_required ? "是" : "否",
            },
            {
              key: "pending",
              label: "待生效修订",
              children: report.pending_revision ?? "无",
            },
          ]}
        />
      </Card>
    </Space>
  );
}

export function DiagnosticsPage() {
  const session = useSettingsSession();
  const readiness = useSessionResource(getReadiness);
  const error = errorDescription(readiness.error);
  const needsAuthorization =
    session.status === "unauthorized" ||
    (readiness.error instanceof SettingsApiError &&
      readiness.error.status === 401);

  return (
    <div className="page-stack">
      <Space orientation="vertical" size={6}>
        <Typography.Title level={2} className="page-heading">
          基础诊断
        </Typography.Title>
        <Typography.Paragraph className="page-description">
          查看分层就绪与逐任务能力事实。读取和刷新不调用模型；依赖检测、付费能力测试与导出均需显式操作。
        </Typography.Paragraph>
      </Space>

      {needsAuthorization ? (
        <UnauthorizedNotice error={session.error} />
      ) : session.status === "checking" ? (
        <Alert type="info" showIcon title="正在验证管理会话" />
      ) : readiness.loading ? (
        <Card>
          <Skeleton active paragraph={{ rows: 5 }} />
        </Card>
      ) : readiness.error ? (
        <Alert
          type={error.status === 503 ? "warning" : "error"}
          showIcon
          title={
            error.status === 403
              ? "来源校验失败"
              : error.status === 503
                ? "设置依赖暂不可用"
                : "诊断读取失败"
          }
          description={`${error.code}：${error.message}`}
          action={<Button onClick={readiness.reload}>重试</Button>}
        />
      ) : readiness.data ? (
        <DiagnosticContent report={readiness.data} />
      ) : (
        <Card>
          <Typography.Text type="secondary">
            尚无可展示的诊断状态。
          </Typography.Text>
        </Card>
      )}
    </div>
  );
}
