import { Alert, Button, Card, Space, Typography } from "antd";
import { getReadiness, SettingsApiError } from "../../api/client";
import { useSettingsSession } from "../../auth/SettingsSession";
import { UnauthorizedNotice } from "../../components/UnauthorizedNotice";
import { useSessionResource } from "../../hooks/useApiResource";

export function BusinessEntryPage() {
  const session = useSettingsSession();
  const readiness = useSessionResource(getReadiness);
  const report = readiness.data;
  return <section className="page-stack" aria-labelledby="business-entry-title">
    <Typography.Title level={2} id="business-entry-title">进入教学评测平台</Typography.Title>
    <Typography.Paragraph>题库、组卷、考试、阅卷与成绩等业务继续使用原业务页面及独立账号登录。</Typography.Paragraph>
    {session.status === "unauthorized" ? <UnauthorizedNotice error={session.error} /> : <Card>
      <Space orientation="vertical" size={16}>
        {readiness.loading || session.status === "checking" ? <Alert type="info" showIcon title="正在读取业务就绪状态" /> :
          readiness.error ? <Alert type="error" showIcon title="无法确认业务状态" description={readiness.error instanceof SettingsApiError ? `${readiness.error.code}：${readiness.error.message}` : "读取失败，请确认后端仍在运行后重试。"} /> :
          report ? <>
            <Typography.Text>Setup：{report.setup.ready ? "就绪" : `未就绪 · ${report.setup.reason ?? "unknown"}`}</Typography.Text>
            <Alert type={report.business.ready ? "success" : "warning"} showIcon
              title={report.business.ready ? "Business 已就绪" : "Business 尚未就绪"}
              description={report.business.ready ? "可进入原业务登录页；AI 能力以各任务就绪状态为准。" : `${report.business.reason ?? "unknown"}；请在设置或诊断中处理后刷新。`} />
            {report.business.ready ? <Button type="primary" href="/gradio/">进入原业务登录页</Button> : null}
          </> : null}
        <Button disabled={readiness.loading || session.status !== "authorized"} onClick={readiness.reload}>刷新业务状态</Button>
        <Typography.Text type="secondary">切换到业务页会离开设置管理会话。业务页可返回设置；需要管理授权时请从 Launcher 重新打开。</Typography.Text>
      </Space>
    </Card>}
  </section>;
}
