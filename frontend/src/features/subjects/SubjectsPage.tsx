import { useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Input, Space, Spin, Typography } from "antd";
import { Link, useParams } from "react-router-dom";
import { PracticeBuilder } from "../practice/PracticeBuilder";
import { QuestionBankPage } from "../question-bank/QuestionBankPage";
import { MaterialsPage } from "../materials/MaterialsPage";
import { DiagnosisPage } from "../learning-diagnosis/DiagnosisPage";
import { TeacherPanel } from "../teacher/TeacherPanel";
import { createSubject, getSubject, getSubjects, LearningApiError, patchSubject, type Subject, type SubjectCreate } from "../../api/learningClient";

function Failure({ error }: { error: unknown }) {
  return <Alert type="error" showIcon title="无法完成科目操作" description={<Space orientation="vertical">
    <span>{error instanceof LearningApiError ? `${error.code}：${error.message}` : "请求失败，请确认服务仍在运行。"}</span>
    <span>尚未配置或依赖不可用时，请先完成设置；已有数据不会被替换为空科目。</span>
    <Link to="/settings">打开设置中心</Link>
  </Space>} />;
}

function SubjectEditor({ initial, busy, onSave, onCancel }: {
  initial?: Subject; busy: boolean; onSave: (body: SubjectCreate) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  return <Card title={initial ? "修改科目" : "创建科目"}>
    <form onSubmit={event => { event.preventDefault(); if (name.trim()) onSave({ name: name.trim(), description: description.trim() || null }); }}>
      <Space orientation="vertical" style={{ width: "100%" }} size={16}>
        <div className="wizard-field"><label htmlFor="subject-name">科目名称</label>
          <Input id="subject-name" value={name} maxLength={160} required disabled={busy} onChange={event => setName(event.target.value)} /></div>
        <div className="wizard-field"><label htmlFor="subject-description">科目说明（可选）</label>
          <Input.TextArea id="subject-description" value={description} maxLength={4000} disabled={busy} onChange={event => setDescription(event.target.value)} /></div>
        <Space><Button type="primary" htmlType="submit" loading={busy} disabled={!name.trim()}>保存科目</Button>
          <Button disabled={busy} onClick={onCancel}>取消</Button></Space>
      </Space>
    </form>
  </Card>;
}

export function SubjectsPage() {
  const [subjects, setSubjects] = useState<Subject[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    getSubjects(controller.signal).then(setSubjects).catch(cause => {
      if (!controller.signal.aborted) { setError(cause); setSubjects(null); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);
  async function save(body: SubjectCreate) {
    setSaving(true); setError(null);
    try {
      const result = await createSubject(body);
      setSubjects(previous => [...(previous ?? []), result]); setCreating(false);
    } catch (cause) { setError(cause); }
    finally { setSaving(false); }
  }
  return <section className="page-stack" aria-labelledby="subjects-title">
    <div><Typography.Title level={2} id="subjects-title">我的科目</Typography.Title>
      <Typography.Paragraph type="secondary">从一个科目开始，整理你的个人学习空间。同一应用实例中的浏览器共享这些记录。</Typography.Paragraph></div>
    <Space><Button type="primary" disabled={loading || subjects === null || creating} onClick={() => setCreating(true)}>创建科目</Button>
      <Button disabled={loading || saving} onClick={() => setRefresh(value => value + 1)}>刷新科目</Button></Space>
    {error ? <Failure error={error} /> : null}
    {loading ? <Spin description="正在读取科目"><div style={{ minHeight: 100 }} /></Spin> : null}
    {creating ? <SubjectEditor busy={saving} onSave={save} onCancel={() => setCreating(false)} /> : null}
    {!loading && subjects?.length === 0 ? <Card><Empty description="暂无科目" /></Card> : null}
    {!loading && subjects ? <div className="status-grid">{subjects.map(item => <Card key={item.id}>
      <Typography.Title level={3}><Link to={`/subjects/${encodeURIComponent(item.id)}`}>{item.name}</Link></Typography.Title>
      <Typography.Paragraph type="secondary">{item.description || "尚未填写科目说明"}</Typography.Paragraph>
    </Card>)}</div> : null}
  </section>;
}

const areas = [
  ["materials", "资料"], ["question-bank", "题库"], ["practice", "练习"],
  ["diagnosis", "学习诊断"], ["teacher", "AI 教师"],
] as const;

export function SubjectPage() {
  const { subjectId = "", area } = useParams();
  const [value, setValue] = useState<Subject | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setValue(null); setError(null); setEditing(false);
    getSubject(subjectId, controller.signal).then(setValue).catch(cause => {
      if (!controller.signal.aborted) setError(cause);
    });
    return () => controller.abort();
  }, [subjectId]);
  async function save(body: SubjectCreate) {
    setSaving(true); setError(null);
    try { setValue(await patchSubject(subjectId, body)); setEditing(false); }
    catch (cause) { setError(cause); }
    finally { setSaving(false); }
  }
  const selected = areas.find(([key]) => key === area);
  return <section className="page-stack">
    <Link to="/">返回我的科目</Link>
    {error ? <Failure error={error} /> : null}
    {!value && !error ? <Spin /> : null}
    {value ? <>
      <div><Typography.Title level={2}>{value.name}</Typography.Title>
        <Typography.Paragraph type="secondary">{value.description || "尚未填写科目说明"}</Typography.Paragraph>
        <Button disabled={editing} onClick={() => setEditing(true)}>修改科目</Button></div>
      {editing ? <SubjectEditor initial={value} busy={saving} onSave={save} onCancel={() => setEditing(false)} /> : null}
      <div className="status-grid">{areas.map(([key, label]) => <Card key={key}>
        <Typography.Title level={4}><Link to={`/subjects/${encodeURIComponent(value.id)}/${key}`}>{label}</Link></Typography.Title>
        <Typography.Text type="secondary">管理你的科目内容</Typography.Text>
      </Card>)}</div>
      {area === "materials" ? <MaterialsPage subjectId={value.id} /> : null}
      {area === "question-bank" ? <QuestionBankPage subjectId={value.id} /> : null}
      {area === "practice" ? <PracticeBuilder key={value.id} subjectId={value.id} /> : null}
      {area === "diagnosis" ? <DiagnosisPage key={value.id} subjectId={value.id} /> : null}
      {area === "teacher" ? <TeacherPanel key={value.id} subjectId={value.id} /> : null}
      {area && !["materials", "question-bank", "practice", "diagnosis", "teacher"].includes(area) ? <Alert type="info" showIcon title={`${selected?.[1] ?? "此页面"}尚未开放`} description="请使用当前科目入口。" /> : null}
    </> : null}
  </section>;
}
