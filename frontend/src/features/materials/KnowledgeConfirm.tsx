import { useEffect, useState } from "react";
import { Alert, Button, Card, Input, Space, Spin, Tag, Typography } from "antd";

import {
  confirmKnowledge,
  getKnowledge,
  LearningApiError,
  suggestKnowledge,
  type KnowledgeView,
} from "../../api/materialsClient";

type Drafts = Record<string, string>;

function key(kind: "p" | "c", id: string): string {
  return `${kind}:${id}`;
}

/**
 * 建议永远只是建议：用户改完名字再确认，确认后仍显示后端返回的真实状态。
 */
export function KnowledgeConfirm({ subjectId, onConfirmed }: {
  subjectId: string;
  onConfirmed?: () => void;
}) {
  const [view, setView] = useState<KnowledgeView | null>(null);
  const [drafts, setDrafts] = useState<Drafts>({});
  const [chapterPoints, setChapterPoints] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setView(null);
    setError(null);
    getKnowledge(subjectId, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        setView(value);
        setDrafts({});
        setChapterPoints({});
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(cause);
      });
    return () => controller.abort();
  }, [subjectId]);

  async function run(action: "suggest" | "confirm") {
    setBusy(true);
    setError(null);
    try {
      if (action === "suggest") {
        const value = await suggestKnowledge(subjectId);
        setView(value);
        setDrafts({});
        setChapterPoints({});
      } else if (view) {
        const value = await confirmKnowledge(subjectId, {
          knowledge_points: view.knowledge_points.map(point => ({
            id: point.id,
            name: (drafts[key("p", point.id)] ?? point.name).trim() || point.name,
          })),
          // 不改写章节的 source sections：只提交身份、标题和父级。
          chapters: view.chapters.map(chapter => ({
            id: chapter.id,
            title: (drafts[key("c", chapter.id)] ?? chapter.title).trim() || chapter.title,
            parent_id: chapter.parent_id ?? null,
            knowledge_point_ids: chapterPoints[chapter.id] ?? chapter.knowledge_point_ids,
          })),
        });
        setView(value);
        setDrafts({});
        setChapterPoints({});
        onConfirmed?.();
      }
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="章节与知识点"
      extra={
        <Space>
          <Button onClick={() => run("suggest")} loading={busy} disabled={busy}>
            生成建议
          </Button>
          <Button type="primary" onClick={() => run("confirm")} loading={busy} disabled={busy || !view}>
            确认修改
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" style={{ width: "100%" }} size={12}>
        <Typography.Paragraph type="secondary">
          建议来自资料里真实的标题与位置；确认前不会冒充正式关联，也不会自动生成练习。
        </Typography.Paragraph>
        {error ? (
          <Alert
            type="error"
            showIcon
            title="知识组织未完成"
            description={
              error instanceof LearningApiError
                ? `${error.code}：${error.message}`
                : "请求失败，请确认服务仍在运行。"
            }
          />
        ) : null}
        {!view ? <Spin description="正在读取章节与知识点"><div style={{ minHeight: 60 }} /></Spin> : null}
        {view ? (
          <>
            <Tag color={view.confirmed ? "green" : "gold"}>
              {view.confirmed ? "已确认" : "未确认"}
            </Tag>
            <div>
              <Typography.Title level={4}>章节</Typography.Title>
              {view.chapters.length === 0 ? <Typography.Text type="secondary">暂无章节</Typography.Text> : null}
              {view.chapters.map(chapter => (
                <div key={chapter.id} className="wizard-field">
                  <span>
                    {chapter.confirmation_state === "confirmed" ? "已确认" : "建议"}：
                  </span>
                  <Input
                    aria-label={`章节 ${chapter.title}`}
                    value={drafts[key("c", chapter.id)] ?? chapter.title}
                    maxLength={160}
                    disabled={busy}
                    onChange={event =>
                      setDrafts(previous => ({ ...previous, [key("c", chapter.id)]: event.target.value }))
                    }
                  />
                  <Typography.Text type="secondary">关联知识点（可多选）</Typography.Text>
                  <select
                    multiple
                    aria-label={`章节 ${chapter.title} 的知识点`}
                    value={chapterPoints[chapter.id] ?? chapter.knowledge_point_ids}
                    disabled={busy || view.knowledge_points.length === 0}
                    onChange={event => setChapterPoints(previous => ({
                      ...previous,
                      [chapter.id]: Array.from(event.target.selectedOptions, option => option.value),
                    }))}
                  >
                    {view.knowledge_points.map(point => (
                      <option key={point.id} value={point.id}>
                        {drafts[key("p", point.id)] ?? point.name}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <div>
              <Typography.Title level={4}>知识点</Typography.Title>
              {view.knowledge_points.length === 0 ? (
                <Typography.Text type="secondary">暂无知识点</Typography.Text>
              ) : null}
              {view.knowledge_points.map(point => (
                <label key={point.id} className="wizard-field">
                  <span>
                    {point.confirmation_state === "confirmed" ? "已确认" : "建议"}：
                  </span>
                  <Input
                    aria-label={`知识点 ${point.name}`}
                    value={drafts[key("p", point.id)] ?? point.name}
                    maxLength={160}
                    disabled={busy}
                    onChange={event =>
                      setDrafts(previous => ({ ...previous, [key("p", point.id)]: event.target.value }))
                    }
                  />
                </label>
              ))}
            </div>
          </>
        ) : null}
      </Space>
    </Card>
  );
}
