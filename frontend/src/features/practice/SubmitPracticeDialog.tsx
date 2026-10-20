import { Alert, Modal } from "antd";
import type { QuestionContentView } from "../../api/learningClient";
import { answerState, type AnswerDraft } from "./AnswerControls";

export type SubmissionItem = {
  id: string;
  label: string;
  questionType: QuestionContentView["question_type"];
  draft: AnswerDraft;
};

export function SubmitPracticeDialog({ open, items, submitting = false, onCancel, onConfirm }: {
  open: boolean;
  items: SubmissionItem[];
  submitting?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const blank = items.filter(item => item.questionType === "short_answer" && answerState(item.draft) === "unanswered");
  return <Modal open={open} title="确认整组提交" okText={blank.length ? "仍然提交" : "确认提交"} cancelText="返回填写"
    onOk={onConfirm} onCancel={onCancel} confirmLoading={submitting} cancelButtonProps={{ disabled: submitting }} closable={!submitting} mask={{ closable: !submitting }}>
    {blank.length > 0 ? <Alert type="warning" title="以下文字主观题尚未填写" description={<>
      <ul>{blank.map(item => <li key={item.id}>{item.label}</li>)}</ul>
      <p>可以返回填写，也可以继续提交并保留未作答状态。</p>
    </>} /> : <p>确认后按整组提交；作答期间不展示解析。</p>}
  </Modal>;
}
