import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { PracticeItemResult, ScoreDispute } from "../../api/practiceClient";
import { ScoreDisputePanel } from "./ScoreDisputePanel";

const item = {
  item_id: "item-1",
  position: 1,
  question_type: "single_choice",
  answer_state: "answered",
  score: "0.00",
  max_score: "10.00",
  validation_state: "valid",
  execution_source: "local_objective",
  reason: "答案错误",
  disputed: false,
  content: {
    question_id: "q",
    revision_id: "r",
    question_type: "single_choice",
    phase: "review",
    stem_blocks: [{ type: "text", text: "题目" }],
    options: [],
    explanation_blocks: null,
    automatic_grading_ready: true,
    issues: [],
    asset_sources: {},
  },
  reference_answer: "A",
  scoring_rubric: null,
  answer_text: null,
  choice_ids: ["B"],
  feedback: null,
  history: [
    {
      id: "attempt-1",
      round: 1,
      score: "0.00",
      max_score: "10.00",
      validation_state: "valid",
      execution_source: "local_objective",
      reason: "答案错误",
      created_at: "2026-10-20T00:00:00Z",
    },
  ],
} as PracticeItemResult;

const pending = {
  id: "dispute-1",
  item_id: "item-1",
  answer_id: "answer-1",
  state: "pending",
  reason: "评分依据需要复核",
  disputed_attempt_id: "attempt-1",
} as ScoreDispute;

it("requires a reason before raising a dispute", () => {
  const raise = vi.fn();
  render(<ScoreDisputePanel item={item} dispute={null} busy={false} canAccept={false} latestAttemptId="attempt-1"
    onRaise={raise} onRegrade={vi.fn()} onAccept={vi.fn()} />);
  const button = screen.getByRole("button", { name: "提出争议" });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "争议理由" }), { target: { value: "  参考答案有误  " } });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(raise).toHaveBeenCalledWith("参考答案有误");
});

it("does not regrade until the explicit confirmation", () => {
  const regrade = vi.fn();
  render(<ScoreDisputePanel item={{ ...item, disputed: true }} dispute={pending} busy={false}
    canAccept={false} latestAttemptId="attempt-1" onRaise={vi.fn()} onRegrade={regrade} onAccept={vi.fn()} />);
  expect(screen.getByText("争议待重评")).toBeVisible();
  expect(regrade).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "主动请求重评" }));
  expect(screen.getByText("确认主动请求重评？")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "确认重评" }));
  expect(regrade).toHaveBeenCalledOnce();
});

it("accepts only the explicitly supplied newer valid attempt", () => {
  const accept = vi.fn();
  const awaiting = { ...pending, state: "awaiting_acceptance" as const, accepted_attempt_id: null };
  render(<ScoreDisputePanel item={{ ...item, disputed: true }} dispute={awaiting} busy={false}
    canAccept latestAttemptId="attempt-2" onRaise={vi.fn()} onRegrade={vi.fn()} onAccept={accept} />);
  fireEvent.click(screen.getByRole("button", { name: "接受并解决争议" }));
  expect(accept).toHaveBeenCalledWith("attempt-2");
});

it("does not offer acceptance for a non-current or failed latest round", () => {
  const awaiting = { ...pending, state: "awaiting_acceptance" as const, accepted_attempt_id: null };
  render(<ScoreDisputePanel item={{ ...item, disputed: true }} dispute={awaiting} busy={false}
    canAccept={false} latestAttemptId="attempt-1" onRaise={vi.fn()} onRegrade={vi.fn()} onAccept={vi.fn()} />);
  expect(screen.getByRole("button", { name: "接受并解决争议" })).toBeDisabled();
  expect(screen.getByText(/当前最新轮次不是可接受的有效重评/)).toBeVisible();
});

it("shows an already resolved dispute without a per-item acceptance step", () => {
  const resolved = { ...pending, state: "resolved" as const, accepted_attempt_id: "attempt-2" };
  render(<ScoreDisputePanel item={{ ...item, disputed: false }} dispute={resolved} busy={false}
    canAccept={false} latestAttemptId="attempt-2" onRaise={vi.fn()} onRegrade={vi.fn()} onAccept={vi.fn()} />);
  expect(screen.getByText("争议已解决")).toBeVisible();
  expect(screen.queryByRole("button", { name: "接受并解决争议" })).toBeNull();
});


it("uses a corrected revision only after the user's explicit selection", () => {
  const regrade = vi.fn();
  render(<ScoreDisputePanel item={{ ...item, disputed: true }} dispute={pending} busy={false}
    canAccept={false} latestAttemptId="attempt-1" onRaise={vi.fn()} onRegrade={regrade} onAccept={vi.fn()} />);
  fireEvent.click(screen.getByRole("checkbox", { name: /采用题库当前已校正/ }));
  expect(regrade).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "主动请求重评" }));
  fireEvent.click(screen.getByRole("button", { name: "确认重评" }));
  expect(regrade).toHaveBeenCalledWith(true);
});
