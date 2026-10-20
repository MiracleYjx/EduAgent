/** Browser-only integration fixture. Not routed or shipped by the product UI. */
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Alert, Button, Card, ConfigProvider, Typography } from "antd";
import { contentAssetUrl, getQuestionContent, type QuestionContentView } from "../src/api/learningClient";
import { QuestionContent } from "../src/features/practice/QuestionContent";
import { AnswerControls, type AnswerDraft } from "../src/features/practice/AnswerControls";
import { SubmitPracticeDialog } from "../src/features/practice/SubmitPracticeDialog";
import { DataChart } from "../src/features/practice/DataChart";

function Harness() {
  const parameters = new URLSearchParams(location.search);
  const subject = parameters.get("subject") ?? "";
  const practice = parameters.get("practice") ?? "";
  const item = parameters.get("item") ?? "";
  const [content, setContent] = useState<QuestionContentView>();
  const [error, setError] = useState<string>();
  const [single, setSingle] = useState<AnswerDraft>({ kind: "choices", choice_ids: [] });
  const [multiple, setMultiple] = useState<AnswerDraft>({ kind: "choices", choice_ids: [] });
  const [subjective, setSubjective] = useState<AnswerDraft>({ kind: "text", text_answer: "" });
  const [confirming, setConfirming] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const load = () => getQuestionContent(subject, practice, item).then(setContent).catch(e => setError(String(e)));
  useEffect(() => { void load(); }, []);
  return <ConfigProvider><main style={{ maxWidth: 980, margin: "24px auto", padding: 20 }}>
    <Typography.Title level={2}>图文阅读共享组件</Typography.Title>
    <Alert type="info" title="独立 Test 合成记录；仅验收共享组件，未接入练习提交或评分。" />
    <Button onClick={() => void load()} style={{ margin: "12px 0" }}>重新读取内容</Button>
    {error && <Alert type="error" title={error} />}
    {content && <Card title={content.phase === "answering" ? "作答阶段" : "提交后查看"}>
      <QuestionContent content={content} mediaUrl={id => contentAssetUrl(subject, practice, item, id)} />
      <AnswerControls questionType="single_choice" options={content.options} value={single} onChange={setSingle} />
      <Typography.Title level={3}>多选输入组件</Typography.Title>
      <AnswerControls questionType="multiple_choice" options={content.options} value={multiple} onChange={setMultiple} />
      <Typography.Title level={3}>文字主观输入组件</Typography.Title>
      <AnswerControls questionType="short_answer" options={[]} value={subjective} onChange={setSubjective} />
      <Button onClick={() => setConfirming(true)}>检查整组提交提示</Button>
      {confirmed && <Alert type="success" title="已触发组件确认回调，未发送提交或评分请求" />}
      <SubmitPracticeDialog open={confirming} items={[{ id: "subjective", label: "文字主观题", questionType: "short_answer", draft: subjective }]}
        onCancel={() => setConfirming(false)} onConfirm={() => { setConfirming(false); setConfirmed(true); }} />
      <Typography.Title level={3}>有限数据图表组件</Typography.Title>
      <DataChart chart={{ kind: "line", title: "位移随时间变化", x_axis: { label: "时间", unit: "s" }, y_axis: { label: "位移", unit: "m" }, points: [{ x: 0, y: 0 }, { x: 1, y: 2 }, { x: 2, y: 4 }], basis: "题设给定：速度2m/s，初始位移0m" }} review="consistent" />
      <DataChart chart={{ kind: "bar", title: "待核对柱状数据", x_axis: { label: "序号", unit: "无量纲" }, y_axis: { label: "测量值", unit: "m" }, points: [{ x: 1, y: 3 }, { x: 2, y: -2 }], basis: "合成冲突样例，保留原数据" }} review="conflicting" />
    </Card>}
  </main></ConfigProvider>;
}

createRoot(document.getElementById("root")!).render(<Harness />);
