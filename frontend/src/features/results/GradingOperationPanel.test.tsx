import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LearningOperation } from "../../api/learningClient";
import { GradingOperationPanel } from "./GradingOperationPanel";

const operation:LearningOperation={id:"server-operation",purpose:"practice_grading:practice",
  runtime_snapshot_id:"snapshot",preview_digest:"a".repeat(64),state:"prepared",
  actual_requests:0,request_budget:3,steps:[{step_id:"item",task_kind:"subjective_grading",
    task_models:{subjective_grading:"frozen-model"},scope_summary:"仅选定题，包含检索预算"}],
  estimate_state:"unknown",estimated_cost:null};

it("shows the server budget and cancellation sends nothing",()=>{
  const confirm=vi.fn(),execute=vi.fn(),cancel=vi.fn(),refresh=vi.fn();
  render(<GradingOperationPanel value={operation} busy={false} onConfirm={confirm}
    onExecute={execute} onCancel={cancel} onRefresh={refresh} />);
  expect(screen.getByText(/本次最多 3 次远程请求/)).toHaveTextContent("费用无法可靠估算");
  expect(screen.getByText(/估算不是供应商账单硬上限/)).toBeVisible();
  expect(confirm).not.toHaveBeenCalled();expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"取消费用确认"}));
  expect(cancel).toHaveBeenCalledOnce();expect(confirm).not.toHaveBeenCalled();expect(execute).not.toHaveBeenCalled();
});

it("requires an explicit execute action after confirmation and refresh is read only",()=>{
  const confirm=vi.fn(),execute=vi.fn(),refresh=vi.fn(),reset=vi.fn();
  render(<GradingOperationPanel value={{...operation,state:"confirmed"}} busy={false}
    onConfirm={confirm} onExecute={execute} onCancel={reset} onRefresh={refresh} />);
  expect(screen.getByText("已确认，待执行")).toBeVisible();expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"读取操作状态"}));
  expect(refresh).toHaveBeenCalledOnce();expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"重新选择并预览"}));
  expect(reset).toHaveBeenCalledOnce();expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"执行已确认评分"}));
  expect(execute).toHaveBeenCalledOnce();
});

it("preserves unknown outcome and never offers automatic paid retry",()=>{
  const execute=vi.fn();
  render(<GradingOperationPanel value={{...operation,state:"outcome_unknown",actual_requests:1}}
    busy={false} onConfirm={vi.fn()} onExecute={execute} onCancel={vi.fn()} onRefresh={vi.fn()} />);
  expect(screen.getByText("结果未知，需要人工核对")).toBeVisible();
  expect(screen.queryByRole("button",{name:"执行已确认评分"})).toBeNull();
  expect(execute).not.toHaveBeenCalled();
});
