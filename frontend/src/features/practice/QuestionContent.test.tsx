import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { QuestionContent } from "./QuestionContent";
import type { QuestionContentView } from "../../api/learningClient";

export const mixedQuestion: QuestionContentView = {
  question_id: "q", revision_id: "r", question_type: "single_choice", phase: "answering",
  automatic_grading_ready: false,
  stem_blocks: [
    { type: "text", text: "文字之前" },
    { type: "image", asset_id: "image-1", alt: "第一图" },
    { type: "text", text: "两图之间" },
    { type: "image", asset_id: "image-2", alt: "第二图" },
    { type: "formula", expression: { kind: "sup", children: [{ kind: "identifier", text: "x", children: [] }, { kind: "number", text: "2", children: [] }] } },
    { type: "table", headers: ["时间(s)", "位移(m)"], rows: [["1", "2"], ["2", "4"]] },
  ],
  options: [{ id: "b-id", blocks: [{ type: "image", asset_id: "option-b", alt: "B选项图" }] },
    { id: "a-id", blocks: [{ type: "text", text: "第二选项" }] }],
  issues: [],
};
const mediaUrl = (id: string) => "/api/learning/test/assets/" + encodeURIComponent(id);

it("renders the original mixed order, native formula, table and option ownership", () => {
  const { container } = render(<QuestionContent content={mixedQuestion} mediaUrl={mediaUrl} />);
  const stem = screen.getByRole("region", { name: "题干" });
  expect([...stem.children].map(e => e.getAttribute("data-content-type"))).toEqual(["text", "image", "text", "image", "formula", "table"]);
  expect(screen.getByAltText("第一图")).toHaveAttribute("src", mediaUrl("image-1"));
  expect(screen.getByRole("region", { name: "选项 1" })).toContainElement(screen.getByAltText("B选项图"));
  expect(container.querySelector("math msup mi")).toHaveTextContent("x");
  expect(screen.getByRole("table")).toHaveTextContent("时间(s)");
  expect(screen.queryByText(/参考答案|评分|解析/)).toBeNull();
});

it("reports a missing image and retains uncertain original with a real location", () => {
  const question: QuestionContentView = { ...mixedQuestion, stem_blocks: [{ type: "original", asset_id: "original", reason: "复杂公式待确认", source: { document_id: "doc", anchor: "word/document.xml#p[3]" } }] };
  render(<QuestionContent content={question} mediaUrl={mediaUrl} />);
  expect(screen.getByText(/复杂公式待确认/)).toBeVisible();
  expect(screen.getByText(/word\/document.xml#p\[3\]/)).toBeVisible();
  fireEvent.error(screen.getByAltText("待核对原内容"));
  expect(screen.getByText(/资源不可用/)).toBeVisible();
  expect(screen.queryByAltText("待核对原内容")).toBeNull();
});

it("does not render explanations in answering phase even if a caller supplies them", () => {
  const content: QuestionContentView = { ...mixedQuestion, explanation_blocks: [{ type: "text", text: "隐藏解析" }, { type: "image", asset_id: "secret", alt: "解析图片" }] };
  const { rerender } = render(<QuestionContent content={content} mediaUrl={mediaUrl} />);
  expect(screen.queryByText("隐藏解析")).toBeNull();
  expect(screen.queryByAltText("解析图片")).toBeNull();
  rerender(<QuestionContent content={{ ...content, phase: "review" }} mediaUrl={mediaUrl} />);
  expect(screen.getByText("隐藏解析")).toBeVisible();
});
