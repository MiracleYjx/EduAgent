import { useState } from "react";
import { Alert, Button, Checkbox, Input, Space, Tag, Typography } from "antd";

import type { PracticeItemResult, ScoreDispute } from "../../api/practiceClient";

const labels: Record<ScoreDispute["state"], string> = {
  pending: "争议待重评",
  awaiting_acceptance: "重评完成，待你接受",
  resolved: "争议已解决",
};

export function ScoreDisputePanel({
  item,
  dispute,
  busy,
  canAccept,
  latestAttemptId,
  error,
  onRaise,
  onRegrade,
  onAccept,
}: {
  item: PracticeItemResult;
  dispute: ScoreDispute | null;
  busy: boolean;
  canAccept: boolean;
  latestAttemptId?: string | null;
  error?: string;
  onRaise: (reason: string) => void;
  onRegrade: (useCurrentRevision?: boolean) => void;
  onAccept: (attemptId: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [useCurrentRevision, setUseCurrentRevision] = useState(false);

  if (dispute?.state === "resolved") {
    return (
      <Alert
        type="success"
        showIcon
        title={labels.resolved}
        description="当前有效重评已由你接受。无争议成绩不会要求逐题接受。"
      />
    );
  }

  if (dispute) {
    return (
      <section className="dispute-panel" aria-label="评分争议">
        <Space orientation="vertical" size={10} style={{ width: "100%" }}>
          <Space wrap>
            <Tag color={dispute.state === "pending" ? "orange" : "blue"}>
              {labels[dispute.state]}
            </Tag>
            <Typography.Text type="secondary">争议中，暂不进入有效诊断</Typography.Text>
          </Space>
          <Typography.Paragraph style={{ marginBottom: 0 }}>
            争议理由：{dispute.reason}
          </Typography.Paragraph>
          {dispute.state === "pending" ? <Checkbox checked={useCurrentRevision} disabled={busy}
            onChange={event => setUseCurrentRevision(event.target.checked)}>
            采用题库当前已校正并通过核验的修订（保留原轮次）
          </Checkbox> : null}
          {dispute.state === "pending" ? (
            confirming ? (
              <Alert
                type="warning"
                showIcon
                title="确认主动请求重评？"
                description="重评只针对本题当前评分轮次；是否调用模型取决于本题评分能力。提交后不会自动重复调用。"
                action={
                  <Space>
                    <Button size="small" onClick={() => setConfirming(false)} disabled={busy}>
                      取消
                    </Button>
                    <Button
                      size="small"
                      type="primary"
                      loading={busy}
                      onClick={() => {
                        setConfirming(false);
                        onRegrade(useCurrentRevision);
                      }}
                    >
                      确认重评
                    </Button>
                  </Space>
                }
              />
            ) : (
              <Button onClick={() => setConfirming(true)} disabled={busy}>
                主动请求重评
              </Button>
            )
          ) : (
            <Space wrap>
              <Typography.Text type="secondary">
                请先核对新旧轮次，再决定是否接受当前有效结果。
              </Typography.Text>
              <Button
                type="primary"
                disabled={busy || !canAccept || !latestAttemptId}
                onClick={() => latestAttemptId && onAccept(latestAttemptId)}
              >
                接受并解决争议
              </Button>
              {!canAccept ? (
                <Typography.Text type="secondary">
                  当前最新轮次不是可接受的有效重评，请先主动重评。
                </Typography.Text>
              ) : null}
            </Space>
          )}
          {error ? <Alert type="error" showIcon title={error} /> : null}
        </Space>
      </section>
    );
  }

  if (item.validation_state !== "valid" || item.answer_state === "unanswered") {
    return (
      <Typography.Text type="secondary">
        当前没有可提出争议的有效评分；失败、待核验和未作答不会伪造为可争议分数。
      </Typography.Text>
    );
  }

  return (
    <section className="dispute-panel" aria-label="发起评分争议">
      <Space orientation="vertical" size={10} style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          对本题评分依据有疑问时，可先填写理由。提出争议后本题暂不进入有效诊断。
        </Typography.Text>
        <Input.TextArea
          aria-label="争议理由"
          value={reason}
          maxLength={1000}
          rows={2}
          placeholder="说明你认为评分依据或答案存在的问题"
          onChange={event => setReason(event.target.value)}
          disabled={busy}
        />
        <Button
          disabled={busy || !reason.trim()}
          onClick={() => onRaise(reason.trim())}
        >
          提出争议
        </Button>
        {error ? <Alert type="error" showIcon title={error} /> : null}
      </Space>
    </section>
  );
}
