import { useId } from "react";
import { Button, Checkbox, Input, Radio, Space, Tag } from "antd";
import type { QuestionContentView } from "../../api/learningClient";
import "./content.css";

export type AnswerDraft = { kind: "choices"; choice_ids: string[] } | { kind: "text"; text_answer: string };
export type AnswerState = "answered" | "unanswered";

export function answerState(draft: AnswerDraft): AnswerState {
  return (draft.kind === "choices" ? draft.choice_ids.length > 0 : draft.text_answer.trim().length > 0) ? "answered" : "unanswered";
}

export function AnswerStatus({ state }: { state: AnswerState }) {
  return <Tag color={state === "answered" ? "blue" : "default"}>{state === "answered" ? "已作答" : "未作答"}</Tag>;
}

type Props = {
  questionType: QuestionContentView["question_type"];
  options: { id: string }[];
  value: AnswerDraft;
  onChange: (value: AnswerDraft) => void;
  disabled?: boolean;
};

/** Controlled input only; submission and grading remain the caller's protocol. */
export function AnswerControls({ questionType, options, value, onChange, disabled = false }: Props) {
  const inputId = useId();
  if ((questionType === "short_answer") !== (value.kind === "text")) throw new Error("作答类型与题型不一致");
  const choices = options.map((option, i) => ({ label: `选项 ${i + 1}`, value: option.id }));
  return <section className="question-answer-controls" aria-label="作答输入">
    <Space orientation="vertical" style={{ width: "100%" }}>
      {value.kind === "text" ? <>
        <label htmlFor={inputId}>文字作答</label>
        <Input.TextArea id={inputId} value={value.text_answer} disabled={disabled} autoSize={{ minRows: 4 }} onChange={event => onChange({ kind: "text", text_answer: event.target.value })} />
      </> : <>
        {questionType === "single_choice"
          ? <Radio.Group aria-label="单项选择" options={choices} disabled={disabled} value={value.choice_ids[0]} onChange={event => onChange({ kind: "choices", choice_ids: [String(event.target.value)] })} />
          : <Checkbox.Group aria-label="多项选择" options={choices} disabled={disabled} value={value.choice_ids} onChange={ids => onChange({ kind: "choices", choice_ids: ids.map(String) })} />}
        <Button disabled={disabled || value.choice_ids.length === 0} onClick={() => onChange({ kind: "choices", choice_ids: [] })}>清空选择</Button>
      </>}
      <AnswerStatus state={answerState(value)} />
    </Space>
  </section>;
}
