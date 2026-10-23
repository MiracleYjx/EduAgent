import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import { beforeEach, expect, it, vi } from "vitest";

import * as client from "../../api/practiceClient";
import { LearningApiError } from "../../api/learningClient";
import * as learning from "../../api/learningClient";
import * as papers from "../../api/papersClient";
import { PracticeResultsPage } from "./PracticeResultsPage";

vi.mock("../../api/practiceClient", async () => ({
  ...(await vi.importActual("../../api/practiceClient")),
  getPracticeReport: vi.fn(),
  getScoreDispute: vi.fn(),
  raiseScoreDispute: vi.fn(),
  regradeScoreDispute: vi.fn(),
  acceptScoreDispute: vi.fn(),
  previewRegrade: vi.fn(), executeRegrade: vi.fn(),
}));

vi.mock("../../api/learningClient", async () => ({
  ...(await vi.importActual("../../api/learningClient")),
  contentAssetUrl: (subject: string, practice: string, item: string, asset: string) =>
    `/asset/${subject}/${practice}/${item}/${asset}`,
  getLearningOperation: vi.fn(), confirmLearningOperation: vi.fn(),
}));

vi.mock("../../api/papersClient", async () => ({
  ...(await vi.importActual("../../api/papersClient")), getQuestion: vi.fn(),
}));

const content = {
  question_id: "question-1",
  revision_id: "revision-1",
  question_type: "short_answer",
  phase: "review",
  stem_blocks: [{ type: "text", text: "请说明变量作用域。" }],
  options: [],
  explanation_blocks: [{ type: "text", text: "PRIVATE explanation" }],
  automatic_grading_ready: true,
  issues: [],
  asset_sources: {},
};

const report = {
  practice_id: "practice-1",
  subject_id: "subject-1",
  state: "partial_failed",
  round: 2,
  total_count: 4,
  answered_count: 3,
  unanswered_count: 1,
  valid_count: 2,
  invalid_count: 1,
  answered_zero_count: 1,
  completion_rate: "0.7500",
  group_score_rate: null,
  choice_correct_rate: null,
  choice_sample_count: 0,
  unresolved_dispute_count: 1,
  items: [
    {
      item_id: "item-unanswered",
      position: 1,
      question_type: "short_answer",
      answer_state: "unanswered",
      score: "0.00",
      max_score: "10.00",
      validation_state: "valid",
      execution_source: "local_unanswered",
      reason: "未作答：按本地规则记 0 分，不调用模型。",
      disputed: false,
      content,
      reference_answer: "局部与全局作用域。",
      scoring_rubric: "说明两点。",
      feedback: null,
      history: [],
    },
    {
      item_id: "item-failed",
      position: 2,
      question_type: "short_answer",
      answer_state: "answered",
      score: null,
      max_score: "10.00",
      validation_state: "failed",
      execution_source: "model",
      reason: "主观题评分未完成：GRADING_PROVIDER_FAILED。",
      error_code: "GRADING_PROVIDER_FAILED",
      disputed: false,
      content,
      reference_answer: "局部与全局作用域。",
      scoring_rubric: "说明两点。",
      answer_text: "变量只在函数里。",
      feedback: null,
      history: [],
    },
    {
      item_id: "item-zero",
      position: 3,
      question_type: "single_choice",
      answer_state: "answered",
      score: "0.00",
      max_score: "10.00",
      validation_state: "valid",
      execution_source: "local_objective",
      reason: "答案错误",
      disputed: true,
      dispute_id: "dispute-1",
      latest_attempt_id: "attempt-2",
      content: { ...content, question_type: "single_choice", options: [] },
      reference_answer: "A",
      scoring_rubric: null,
      choice_ids: ["B"],
      feedback: {
        correct_points: ["写对了一点"],
        missing_knowledge_points: ["缺少：作用域链"],
        suggestions: ["补全说明。"],
        retrieved_context_ids: ["chunk-1"],
      },
      history: [
        {
          id: "attempt-1",
          round: 1,
          score: "0.00",
          max_score: "10.00",
          validation_state: "valid",
          execution_source: "local_objective",
          reason: "第一轮错误",
          created_at: "2026-10-20T00:00:00Z",
        },
        {
          id: "attempt-2",
          round: 2,
          score: "0.00",
          max_score: "10.00",
          validation_state: "valid",
          execution_source: "local_objective",
          reason: "第二轮仍错误",
          created_at: "2026-10-20T00:05:00Z",
        },
      ],
    },
    {
      item_id: "item-zero-plain",
      position: 4,
      question_type: "single_choice",
      answer_state: "answered",
      score: "0.00",
      max_score: "10.00",
      validation_state: "valid",
      execution_source: "local_objective",
      reason: "选择错误",
      disputed: false,
      content: { ...content, question_type: "single_choice", options: [] },
      reference_answer: "A",
      scoring_rubric: null,
      choice_ids: ["B"],
      feedback: null,
      history: [],
    },
    {
      item_id: "item-valid",
      position: 4,
      question_type: "single_choice",
      answer_state: "answered",
      score: "8.00",
      max_score: "10.00",
      validation_state: "valid",
      execution_source: "local_objective",
      reason: "选择正确",
      disputed: false,
      content: { ...content, question_type: "single_choice", options: [] },
      reference_answer: "A",
      scoring_rubric: null,
      choice_ids: ["A"],
      feedback: null,
      history: [],
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(learning.getLearningOperation).mockReset();
  vi.mocked(learning.confirmLearningOperation).mockReset();
  vi.mocked(client.previewRegrade).mockReset();
  vi.mocked(client.executeRegrade).mockReset();
  vi.mocked(papers.getQuestion).mockReset();
  vi.mocked(client.getPracticeReport).mockResolvedValue(report as client.PracticeReport);
  vi.mocked(client.getScoreDispute).mockResolvedValue({
    id: "dispute-1",
    item_id: "item-zero",
    answer_id: "answer-1",
    state: "pending",
    reason: "评分依据需要复核",
    disputed_attempt_id: "attempt-1",
  });
});

function mount() {
  render(<ConfigProvider theme={{token:{motion:false}}}><MemoryRouter><PracticeResultsPage subjectId="subject-1" practiceId="practice-1" /></MemoryRouter></ConfigProvider>);
}

it("distinguishes unanswered, answered-zero, failed and disputed rows", async () => {
  mount();
  expect(await screen.findByText("练习结果")).toBeVisible();
  expect(screen.getByText("未作答（本地 0 分）")).toBeVisible();
  expect(screen.getByText("评分失败")).toBeVisible();
  expect(screen.getByText("已作答，0 分")).toBeVisible();
  expect(screen.getByText("争议中，暂不进入诊断")).toBeVisible();
  expect(screen.getAllByText("有效评分").length).toBeGreaterThan(0);
  expect(screen.queryByText("0%")).toBeNull();
});

it("does not present a partial group as a final score and shows explicit missing samples", async () => {
  mount();
  expect(await screen.findByText("本组尚有未确定成绩")).toBeVisible();
  expect(screen.getByText("未形成最终值")).toBeVisible();
  expect(screen.getAllByText("无有效样本").length).toBeGreaterThan(0);
  expect(screen.getAllByText("PRIVATE explanation").length).toBeGreaterThan(0);
  expect(screen.getByText("第 1 轮")).toBeVisible();
  expect(screen.getByText("第 2 轮")).toBeVisible();
  expect(screen.getByText("写对了一点")).toBeVisible();
  expect(screen.getByText("缺少：作用域链")).toBeVisible();
  expect(screen.getByText("争议待重评")).toBeVisible();
  expect(screen.queryByRole("button", { name: "接受并解决争议" })).toBeNull();
  expect(client.regradeScoreDispute).not.toHaveBeenCalled();
  expect(client.acceptScoreDispute).not.toHaveBeenCalled();
});

it("shows a real 403 instead of replacing it with an empty report", async () => {
  vi.mocked(client.getPracticeReport).mockRejectedValue(
    new LearningApiError("ORIGIN_NOT_ALLOWED", 403, { message: "来源不被允许" }),
  );
  mount();
  expect(await screen.findByText("无法读取练习结果")).toBeVisible();
  expect(screen.getByText(/403 ORIGIN_NOT_ALLOWED：来源不被允许/)).toBeVisible();
  expect(screen.queryByText("练习结果")).toBeNull();
});

const fee:learning.LearningOperation={id:"server-regrade",purpose:"dispute_regrade:dispute-1",
  runtime_snapshot_id:"snapshot",preview_digest:"a".repeat(64),state:"prepared",request_budget:1,
  actual_requests:0,steps:[{task_kind:"subjective_grading",model:"frozen-model"}],estimate_state:"unknown"};

it("restores regrade fee state without executing and uses the server operation ID",async()=>{
  vi.mocked(client.getScoreDispute).mockResolvedValue({id:"dispute-1",item_id:"item-zero",answer_id:"answer-1",
    state:"pending",reason:"review",disputed_attempt_id:"attempt-1",regrade_operation_id:fee.id});
  vi.mocked(learning.getLearningOperation).mockResolvedValue(fee);
  vi.mocked(learning.confirmLearningOperation).mockImplementation(async()=>{
    const value={...fee,state:"confirmed"};
    vi.mocked(learning.getLearningOperation).mockResolvedValue(value);
    return value;
  });
  vi.mocked(client.executeRegrade).mockImplementation(async()=>{
    const value={...fee,state:"outcome_unknown",actual_requests:1};
    vi.mocked(learning.getLearningOperation).mockResolvedValue(value);
    return value;
  });
  mount();
  expect(await screen.findByText("已生成预览，尚未确认费用")).toBeVisible();
  expect(screen.queryByRole("checkbox",{name:/采用题库当前已校正/})).toBeNull();
  expect(client.executeRegrade).not.toHaveBeenCalled();expect(learning.confirmLearningOperation).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"确认本次费用与范围"}));
  await waitFor(()=>expect(learning.confirmLearningOperation).toHaveBeenCalledWith(fee.id,{preview_digest:fee.preview_digest}));
  // The next read is authoritative; it must not be replaced by an optimistic state.
  expect(client.executeRegrade).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button",{name:"执行已确认评分"}));
  await waitFor(()=>expect(client.executeRegrade).toHaveBeenCalledWith("dispute-1",fee.id));
  expect(await screen.findByText("结果未知，需要人工核对")).toBeVisible();
});

it("does not show a 409 confirmation conflict as a successful regrade",async()=>{
  vi.mocked(client.getScoreDispute).mockResolvedValue({id:"dispute-1",item_id:"item-zero",answer_id:"answer-1",
    state:"pending",reason:"review",disputed_attempt_id:"attempt-1",regrade_operation_id:fee.id});
  vi.mocked(learning.getLearningOperation).mockResolvedValue(fee);
  vi.mocked(learning.confirmLearningOperation).mockRejectedValue(new LearningApiError("PREVIEW_CHANGED",409));
  mount();
  fireEvent.click(await screen.findByRole("button",{name:"确认本次费用与范围"}));
  expect(await screen.findByText(/409 PREVIEW_CHANGED/)).toBeVisible();
  expect(client.executeRegrade).not.toHaveBeenCalled();
  expect(screen.queryByText("执行成功，结果已保存")).toBeNull();
});

it.each(["failed", "succeeded"])("allows an explicit fresh regrade preview after an applied %s operation", async state => {
  const previous = { ...fee, state, actual_requests: 1 };
  const next = { ...fee, id: "next-regrade", preview_digest: "b".repeat(64) };
  const item = { ...report.items[2], question_type: "short_answer", content,
    answer_text: "变量只在函数里。", latest_attempt_id: "attempt-2",
    history: report.items[2].history?.map(row => row.id === "attempt-2"
      ? { ...row, operation_id: previous.id } : row) };
  vi.mocked(client.getPracticeReport).mockResolvedValue({ ...report, items: [item] } as client.PracticeReport);
  const dispute = { id: "dispute-1", item_id: item.item_id, answer_id: "answer-1",
    state: state === "failed" ? "pending" : "awaiting_acceptance", reason: "review",
    disputed_attempt_id: "attempt-1", regrade_operation_id: previous.id } as client.ScoreDispute;
  vi.mocked(client.getScoreDispute).mockResolvedValue(dispute);
  vi.mocked(learning.getLearningOperation).mockResolvedValue(previous);
  vi.mocked(papers.getQuestion).mockResolvedValue({ subject_id: "subject-1", revision_id: "corrected-revision" } as Awaited<ReturnType<typeof papers.getQuestion>>);
  vi.mocked(client.previewRegrade).mockImplementation(async () => {
    vi.mocked(client.getScoreDispute).mockResolvedValue({ ...dispute, regrade_operation_id: next.id });
    vi.mocked(learning.getLearningOperation).mockResolvedValue(next);
    return next;
  });
  mount();
  expect(await screen.findByText(state === "failed" ? "执行失败" : "执行成功，结果已保存")).toBeVisible();
  expect(client.previewRegrade).not.toHaveBeenCalled();
  if (state === "succeeded") fireEvent.click(screen.getByRole("checkbox", { name: /采用题库当前已校正/ }));
  fireEvent.click(screen.getByRole("button", { name: "主动请求重评" }));
  fireEvent.click(screen.getByRole("button", { name: "确认重评" }));
  await waitFor(() => expect(client.previewRegrade).toHaveBeenCalledOnce());
  expect(client.previewRegrade).toHaveBeenCalledWith("dispute-1", {
    idempotency_key: expect.any(String), expected_attempt_id: "attempt-2",
    revision_id: state === "succeeded" ? "corrected-revision" : undefined,
  });
  expect(await screen.findByRole("button", { name: "确认本次费用与范围" })).toBeVisible();
  expect(learning.confirmLearningOperation).not.toHaveBeenCalled();
  expect(client.executeRegrade).not.toHaveBeenCalled();
});

it.each(["outcome_unknown", "running", "succeeded"])("does not bypass an %s operation without a saved known result", async state => {
  vi.mocked(client.getScoreDispute).mockResolvedValue({ id: "dispute-1", item_id: "item-zero",
    answer_id: "answer-1", state: "pending", reason: "review", disputed_attempt_id: "attempt-1",
    regrade_operation_id: fee.id });
  vi.mocked(learning.getLearningOperation).mockResolvedValue({ ...fee, state, actual_requests: 1 });
  mount();
  expect(await screen.findByText(`操作 ID：${fee.id}`)).toBeVisible();
  expect(screen.queryByRole("button", { name: "主动请求重评" })).toBeNull();
  expect(client.previewRegrade).not.toHaveBeenCalled();
  expect(client.executeRegrade).not.toHaveBeenCalled();
});

it("keeps an applied unknown regrade closed to another paid preview", async () => {
  const item = { ...report.items[2], validation_state: "outcome_unknown", score: null,
    history: report.items[2].history?.map(row => row.id === "attempt-2"
      ? { ...row, validation_state: "outcome_unknown", operation_id: fee.id } : row) };
  vi.mocked(client.getPracticeReport).mockResolvedValue({ ...report, items: [item] } as client.PracticeReport);
  vi.mocked(client.getScoreDispute).mockResolvedValue({ id: "dispute-1", item_id: item.item_id,
    answer_id: "answer-1", state: "pending", reason: "review", disputed_attempt_id: "attempt-1",
    regrade_operation_id: fee.id });
  vi.mocked(learning.getLearningOperation).mockResolvedValue({ ...fee, state: "outcome_unknown", actual_requests: 1 });
  mount();
  expect(await screen.findByText("结果未知，需要人工核对")).toBeVisible();
  expect(screen.queryByRole("button", { name: "主动请求重评" })).toBeNull();
  expect(client.previewRegrade).not.toHaveBeenCalled();
});

it("can cancel an unsent fee preview and explicitly choose a new regrade scope", async () => {
  vi.mocked(client.getScoreDispute).mockResolvedValue({ id: "dispute-1", item_id: "item-zero",
    answer_id: "answer-1", state: "pending", reason: "review", disputed_attempt_id: "attempt-1",
    regrade_operation_id: fee.id });
  vi.mocked(learning.getLearningOperation).mockResolvedValue(fee);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "取消费用确认" }));
  expect(await screen.findByRole("button", { name: "主动请求重评" })).toBeVisible();
  expect(client.previewRegrade).not.toHaveBeenCalled();
  expect(learning.confirmLearningOperation).not.toHaveBeenCalled();
  expect(client.executeRegrade).not.toHaveBeenCalled();
});
