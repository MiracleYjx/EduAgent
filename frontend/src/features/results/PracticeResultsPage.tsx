import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { Link, useParams } from "react-router-dom";

import {
  acceptScoreDispute,
  getPracticeReport,
  getScoreDispute,
  raiseScoreDispute,
  regradeScoreDispute,
  type PracticeAttempt,
  type PracticeItemResult,
  type PracticeReport,
  type ScoreDispute,
} from "../../api/practiceClient";
import { contentAssetUrl, LearningApiError } from "../../api/learningClient";
import { QuestionContent } from "../practice/QuestionContent";
import { getQuestion } from "../../api/papersClient";
import { ScoreDisputePanel } from "./ScoreDisputePanel";

function percent(value?: string | null): string {
  if (value === null || value === undefined || value === "") return "无有效样本";
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  return `${(number * 100).toFixed(2).replace(/\.?0+$/, "")}%`;
}

function scoreText(score?: string | null): string {
  return score === null || score === undefined ? "无有效分数" : score;
}

function statusOf(item: PracticeItemResult): { text: string; color: string } {
  if (item.answer_state === "unanswered") return { text: "未作答（本地 0 分）", color: "default" };
  if (item.disputed) return { text: "争议中，暂不进入诊断", color: "orange" };
  if (item.validation_state === "failed") return { text: "评分失败", color: "red" };
  if (item.validation_state === "needs_review") return { text: "待核验", color: "gold" };
  if (item.validation_state === "outcome_unknown") return { text: "结果未知", color: "volcano" };
  if (item.score === "0.00" || item.score === "0") return { text: "已作答，0 分", color: "blue" };
  return { text: "有效评分", color: "green" };
}

function sourceText(source: PracticeAttempt["execution_source"]): string {
  return source === "model"
    ? "模型评分"
    : source === "local_unanswered"
      ? "本地未作答零分"
      : "本地确定性评分";
}

function errorText(error: unknown): string {
  if (!(error instanceof LearningApiError)) return "请求失败，请稍后重试。";
  if (error.code === "NETWORK_ERROR") return "网络连接失败，请检查服务是否仍在运行。";
  const details = error.details;
  const message =
    typeof details === "object" && details !== null && "message" in details
      ? String((details as { message?: unknown }).message ?? "")
      : "";
  return `${error.status ? `${error.status} ` : ""}${error.code}${message ? `：${message}` : ""}`;
}

function History({ rows }: { rows: PracticeAttempt[] }) {
  if (rows.length === 0) return <Typography.Text type="secondary">暂无评分轮次。</Typography.Text>;
  return (
    <div className="result-history-scroll">
      <table className="result-history" aria-label="评分轮次历史">
        <thead>
          <tr>
            <th scope="col">轮次</th>
            <th scope="col">分数</th>
            <th scope="col">状态</th>
            <th scope="col">来源</th>
            <th scope="col">本轮依据</th>
            <th scope="col">说明</th>
            <th scope="col">时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.id} data-attempt-id={row.id}>
              <td>第 {row.round} 轮</td>
              <td>{scoreText(row.score)} / {row.max_score ?? "—"}</td>
              <td>{row.validation_state}</td>
              <td>{sourceText(row.execution_source)}</td>
              <td>{row.question_revision_id ?? "原始冻结修订"}
                <div>参考答案：{row.reference_answer ?? "未记录"}</div>
                <div>评分要点：{row.scoring_rubric ?? "无"}</div></td>
              <td>{row.reason}{row.error_code ? `（${row.error_code}）` : ""}</td>
              <td>{new Date(row.created_at).toLocaleString("zh-CN")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Feedback({ item }: { item: PracticeItemResult }) {
  const feedback = item.feedback;
  if (!feedback) return <Typography.Text type="secondary">本题没有结构化评分要点。</Typography.Text>;
  const sections: Array<[string, string[] | undefined]> = [
    ["得分点", feedback.correct_points],
    ["缺失知识点", feedback.missing_knowledge_points],
    ["学习建议", feedback.suggestions],
  ];
  return (
    <Space orientation="vertical" size={6} style={{ width: "100%" }}>
      {sections.map(([label, values]) => (
        <div key={label}>
          <Typography.Text strong>{label}：</Typography.Text>
          {values?.length ? <ul className="result-points">{values.map((value, index) => <li key={`${label}-${index}`}>{value}</li>)}</ul>
            : <Typography.Text type="secondary">无</Typography.Text>}
        </div>
      ))}
      {feedback.retrieved_context_ids?.length ? (
        <Typography.Text type="secondary">
          评分实际使用的资料片段：{feedback.retrieved_context_ids.join("、")}
        </Typography.Text>
      ) : null}
    </Space>
  );
}

function itemCanAccept(item: PracticeItemResult, dispute: ScoreDispute | null): boolean {
  if (!dispute || dispute.state !== "awaiting_acceptance") return false;
  const rows = item.history ?? [];
  const latest = rows[rows.length - 1];
  const disputed = rows.find(row => row.id === dispute.disputed_attempt_id);
  return Boolean(
    latest &&
      latest.validation_state === "valid" &&
      latest.id !== dispute.disputed_attempt_id &&
      (!disputed || latest.round > disputed.round),
  );
}

export function PracticeResultsPage({ subjectId, practiceId }: { subjectId: string; practiceId: string }) {
  const regradeKeys = useRef<Record<string, { scope: string; key: string }>>({});
  const [report, setReport] = useState<PracticeReport | null>(null);
  const [disputes, setDisputes] = useState<Record<string, ScoreDispute>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [busyItem, setBusyItem] = useState<string | null>(null);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getPracticeReport(practiceId, controller.signal)
      .then(async value => {
        if (controller.signal.aborted) return;
        setReport(value);
        const found: Record<string, ScoreDispute> = {};
        for (const item of value.items) {
          if (!item.dispute_id) continue;
          try {
            found[item.item_id] = await getScoreDispute(item.dispute_id, controller.signal);
          } catch (cause) {
            if (!controller.signal.aborted && cause instanceof LearningApiError && cause.status === 409) {
              // A stale dispute reference is a visible state, not a fabricated one.
              continue;
            }
          }
        }
        if (!controller.signal.aborted) setDisputes(found);
      })
      .catch(cause => {
        if (controller.signal.aborted) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [practiceId]);

  async function reload() {
    const value = await getPracticeReport(practiceId);
    setReport(value);
    const found: Record<string, ScoreDispute> = {};
    for (const item of value.items) {
      if (!item.dispute_id) continue;
      try {
        found[item.item_id] = await getScoreDispute(item.dispute_id);
      } catch {
        // Keep the report visible; the panel will show that state is unavailable.
      }
    }
    setDisputes(found);
  }

  async function act(itemId: string, action: () => Promise<unknown>) {
    setBusyItem(itemId);
    setActionErrors(previous => {
      const next = { ...previous };
      delete next[itemId];
      return next;
    });
    try {
      await action();
      await reload();
    } catch (cause) {
      setActionErrors(previous => ({ ...previous, [itemId]: errorText(cause) }));
    } finally {
      setBusyItem(null);
    }
  }

  if (loading) {
    return <Spin description="正在读取练习结果"><div style={{ minHeight: 120 }} /></Spin>;
  }
  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        title="无法读取练习结果"
        description={<Space orientation="vertical">
          <span>{errorText(error)}</span>
          <Link to={`/subjects/${encodeURIComponent(subjectId)}/practice`}>返回练习</Link>
        </Space>}
      />
    );
  }
  if (!report) {
    return <Empty description="暂无评分结果；整组提交后才能查看解析。" />;
  }

  const partial = report.state !== "completed";
  return (
    <section className="page-stack" aria-labelledby="results-title">
      <Link to={`/subjects/${encodeURIComponent(subjectId)}/practice`}>返回练习</Link>
      <div>
        <Typography.Title level={2} id="results-title">练习结果</Typography.Title>
        <Typography.Paragraph type="secondary">
          整组已提交。未作答、已作答零分、评分失败和待核验分别展示，不会仅凭 0 分推断答错。
        </Typography.Paragraph>
      </div>
      {partial ? (
        <Alert
          type="warning"
          showIcon
          title="本组尚有未确定成绩"
          description={`存在 ${report.invalid_count} 项失败或待核验；当前不展示全组最终得分率，已确定题目仍可独立查看。`}
        />
      ) : null}
      {report.unresolved_dispute_count > 0 ? (
        <Alert
          type="warning"
          showIcon
          title={`${report.unresolved_dispute_count} 项争议尚未解决`}
          description="争议记录暂不进入有效诊断。请查看新旧轮次后，由你主动接受当前有效重评。"
        />
      ) : null}

      <div className="status-grid">
        <Card title="作答完成率"><Typography.Title level={3}>{percent(report.completion_rate)}</Typography.Title>
          <Typography.Text type="secondary">已答 {report.answered_count} / {report.total_count} 题</Typography.Text></Card>
        <Card title="本组得分率"><Typography.Title level={3}>{report.group_score_rate ? percent(report.group_score_rate) : "未形成最终值"}</Typography.Title>
          <Typography.Text type="secondary">{partial ? "存在未确定成绩" : "逐题得分率简单平均，含未答零分"}</Typography.Text></Card>
        <Card title="选择题正确率"><Typography.Title level={3}>{percent(report.choice_correct_rate)}</Typography.Title>
          <Typography.Text type="secondary">有效样本 {report.choice_sample_count}；未答单列 {report.unanswered_count}</Typography.Text></Card>
        <Card title="未答 / 已答零分"><Typography.Title level={3}>{report.unanswered_count} / {report.answered_zero_count}</Typography.Title>
          <Typography.Text type="secondary">两者在状态和评分来源上分别保留</Typography.Text></Card>
      </div>

      <Space orientation="vertical" size={16} style={{ width: "100%" }}>
        {report.items.map(item => {
          const status = statusOf(item);
          const dispute = disputes[item.item_id] ?? null;
          return (
            <Card
              key={item.item_id}
              title={`第 ${item.position} 题 · ${item.question_type}`}
              extra={<Space wrap><Tag color={status.color}>{status.text}</Tag><Tag>{item.score === null ? "无有效分数" : `${item.score} / ${item.max_score}`}</Tag></Space>}
            >
              <QuestionContent
                content={item.content}
                mediaUrl={asset => contentAssetUrl(subjectId, practiceId, item.item_id, asset)}
              />
              <Descriptions column={1} size="small" bordered style={{ marginTop: 12 }}>
                <Descriptions.Item label="你的作答">
                  {item.answer_state === "unanswered"
                    ? "未作答"
                    : item.answer_text
                      ? item.answer_text
                      : item.choice_ids?.length
                        ? item.choice_ids.join("、")
                        : "已作答"}
                </Descriptions.Item>
                <Descriptions.Item label="参考答案">{item.reference_answer || "未提供参考答案"}</Descriptions.Item>
                <Descriptions.Item label="评分要点">{item.scoring_rubric || "未提供评分要点"}</Descriptions.Item>
                <Descriptions.Item label="判定依据">{item.reason}</Descriptions.Item>
                <Descriptions.Item label="执行来源">{sourceText(item.execution_source)}</Descriptions.Item>
              </Descriptions>
              <Divider titlePlacement="start">评分依据与缺失点</Divider>
              <Feedback item={item} />
              <Divider titlePlacement="start">评分轮次</Divider>
              <History rows={item.history ?? []} />
              <Divider />
              {item.disputed && !dispute ? (
                <Alert
                  type="warning"
                  showIcon
                  title="争议状态暂时无法读取"
                  description="请刷新结果页重试；不会自动重评或自动接受。"
                />
              ) : (
                <ScoreDisputePanel
                  item={item}
                  dispute={dispute}
                  busy={busyItem === item.item_id}
                  canAccept={itemCanAccept(item, dispute)}
                  latestAttemptId={item.latest_attempt_id}
                  error={actionErrors[item.item_id]}
                  onRaise={reason => act(item.item_id, () => raiseScoreDispute(practiceId, item.item_id, reason))}
                  onRegrade={useCurrent => dispute && act(item.item_id, async () => {
                    const current = useCurrent ? await getQuestion(item.content.question_id) : null;
                    if (current && current.subject_id !== subjectId) throw new LearningApiError("QUESTION_SCOPE_INVALID",409);
                    const scope = `${dispute.id}:${item.latest_attempt_id}:${current?.revision_id ?? "frozen"}`;
                    const previous = regradeKeys.current[item.item_id];
                    const key = previous?.scope === scope ? previous.key : crypto.randomUUID();
                    regradeKeys.current[item.item_id] = { scope, key };
                    return regradeScoreDispute(dispute.id, { idempotency_key: key,
                      expected_attempt_id: item.latest_attempt_id, revision_id: current?.revision_id });
                  })}
                  onAccept={attemptId => dispute && act(item.item_id, () => acceptScoreDispute(dispute.id, attemptId))}
                />
              )}
            </Card>
          );
        })}
      </Space>
      <Button onClick={() => void reload()}>刷新结果</Button>
    </section>
  );
}

export function PracticeResultsRoute() {
  const { subjectId = "", practiceId = "" } = useParams();
  return <PracticeResultsPage subjectId={subjectId} practiceId={practiceId} />;
}
