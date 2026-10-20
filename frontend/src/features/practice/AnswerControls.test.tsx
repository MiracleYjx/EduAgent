import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useState } from "react";
import { ConfigProvider } from "antd";
import { AnswerControls, AnswerStatus, answerState, type AnswerDraft } from "./AnswerControls";
import { SubmitPracticeDialog } from "./SubmitPracticeDialog";

it("selects stable single IDs, multiple sets and supports clearing without answers/scores", () => {
  const changed = vi.fn();
  const options = [{ id: "not-a-label" }, { id: "stable-other" }];
  const { rerender } = render(<AnswerControls questionType="single_choice" options={options} value={{ kind: "choices", choice_ids: [] }} onChange={changed} />);
  fireEvent.click(screen.getByRole("radio", { name: "选项 2" }));
  expect(changed).toHaveBeenLastCalledWith({ kind: "choices", choice_ids: ["stable-other"] });
  rerender(<AnswerControls questionType="multiple_choice" options={options} value={{ kind: "choices", choice_ids: ["not-a-label"] }} onChange={changed} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "选项 2" }));
  expect(changed).toHaveBeenLastCalledWith({ kind: "choices", choice_ids: ["not-a-label", "stable-other"] });
  fireEvent.click(screen.getByRole("button", { name: "清空选择" }));
  expect(changed).toHaveBeenLastCalledWith({ kind: "choices", choice_ids: [] });
  expect(screen.queryByText(/答案|得分|解析/)).toBeNull();
});

it("preserves raw text while distinguishing whitespace from a valid zero answer", () => {
  function Form() {
    const [value, setValue] = useState<AnswerDraft>({ kind: "text", text_answer: "" });
    return <AnswerControls questionType="short_answer" options={[]} value={value} onChange={setValue} />;
  }
  const { container } = render(<Form />);
  const text = screen.getByRole("textbox", { name: "文字作答" });
  fireEvent.change(text, { target: { value: " \n " } });
  expect(text).toHaveValue(" \n ");
  expect(screen.getByText("未作答")).toBeVisible();
  fireEvent.change(text, { target: { value: "0" } });
  expect(screen.getByText("已作答")).toBeVisible();
  expect(container.querySelector('input[type="file"]')).toBeNull();
  expect(answerState({ kind: "text", text_answer: "0" })).toBe("answered");
});

it("uses the explicit answer state, independent of a later zero score", () => {
  const { rerender } = render(<AnswerStatus state="unanswered" />);
  expect(screen.getByText("未作答")).toBeVisible();
  rerender(<AnswerStatus state="answered" />);
  expect(screen.getByText("已作答")).toBeVisible();
  expect(screen.queryByText("未作答")).toBeNull();
});

it("identifies blank subjective questions and waits for explicit continue", async () => {
  const confirm = vi.fn(), cancel = vi.fn();
  // jsdom does not run CSS animations; keep the actual visibility assertion.
  render(<ConfigProvider theme={{ token: { motion: false } }}><SubmitPracticeDialog open items={[
    { id: "one", label: "第 1 题", questionType: "short_answer", draft: { kind: "text", text_answer: " \n " } },
    { id: "two", label: "第 2 题", questionType: "short_answer", draft: { kind: "text", text_answer: "0" } },
    { id: "three", label: "第 3 题", questionType: "single_choice", draft: { kind: "choices", choice_ids: [] } },
  ]} onCancel={cancel} onConfirm={confirm} /></ConfigProvider>);
  await waitFor(() => expect(screen.getByText(/第 1 题/)).toBeVisible());
  expect(screen.queryByText(/第 2 题|第 3 题/)).toBeNull();
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "返回填写" }));
  expect(cancel).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "仍然提交" }));
  expect(confirm).toHaveBeenCalledOnce();
});
