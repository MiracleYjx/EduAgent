import { useNavigate } from "react-router-dom";
import { createPaper } from "../../api/papersClient";
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Space, Spin, Tag, Typography } from "antd";

import {
  APPROVED_UPLOAD_BYTES,
  createVersion,
  LearningApiError,
  listMaterials,
  listSeries,
  setDefaultVersion,
  uploadMaterial,
  type MaterialPurpose,
  type MaterialSeriesView,
  type MaterialView,
} from "../../api/materialsClient";
import { KnowledgeConfirm } from "./KnowledgeConfirm";
import { SourceViewer } from "./SourceViewer";
import { MaterialIndexPanel } from "./MaterialIndexPanel";
import { MaterialOCRPanel } from "./MaterialOCRPanel";

const stateLabels: Record<string, string> = {
  uploaded: "已上传",
  parsed: "已解析",
  indexed: "已索引",
  failed: "失败",
};

const parseLabels: Record<string, string> = {
  parsed: "解析完成",
  partial: "部分完成",
  failed: "解析失败",
};

function purposeLabel(value: MaterialPurpose): string {
  return value === "study_material" ? "学习资料" : "试卷";
}

function describe(error: unknown): string {
  if (error instanceof LearningApiError) return `${error.code}：${error.message}`;
  if (error instanceof Error) return error.message;
  return "请求失败，请确认服务仍在运行。";
}

function VersionPanel({ subjectId, material, series, onChanged }: {
  subjectId: string;
  material: MaterialView;
  series: MaterialSeriesView[];
  onChanged: () => void;
}) {
  const [label, setLabel] = useState("");
  const [seriesId, setSeriesId] = useState("");
  const [seriesName, setSeriesName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function add() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const value = await createVersion(subjectId, {
        document_id: material.id,
        series_id: seriesId || null,
        series_name: seriesId ? null : seriesName.trim() || null,
        label: label.trim(),
      });
      setMessage(`已加入版本系列「${value.name}」`);
      setLabel("");
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Space orientation="vertical" style={{ width: "100%" }} size={8}>
      <Typography.Text type="secondary">
        {material.series_id
          ? `属于系列「${series.find(item => item.id === material.series_id)?.name ?? material.series_id}」，版本「${material.version_label ?? ""}」`
          : "尚未加入版本系列；系统不会自动把不同出版社的资料归为同一本教材。"}
      </Typography.Text>
      <div className="wizard-field">
        <label htmlFor={`version-label-${material.id}`}>版本标签</label>
        <Input
          id={`version-label-${material.id}`}
          value={label}
          maxLength={160}
          placeholder="例如：2024 版·上册"
          disabled={busy}
          onChange={event => setLabel(event.target.value)}
        />
      </div>
      <div className="wizard-field">
        <label htmlFor={`version-series-${material.id}`}>加入已有系列（可选）</label>
        <select
          id={`version-series-${material.id}`}
          value={seriesId}
          disabled={busy}
          onChange={event => setSeriesId(event.target.value)}
        >
          <option value="">新建系列</option>
          {series.map(item => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      {seriesId === "" ? (
        <div className="wizard-field">
          <label htmlFor={`version-series-name-${material.id}`}>新系列名称</label>
          <Input
            id={`version-series-name-${material.id}`}
            value={seriesName}
            maxLength={160}
            disabled={busy}
            onChange={event => setSeriesName(event.target.value)}
          />
        </div>
      ) : null}
      <Button onClick={add} loading={busy} disabled={busy || !label.trim()}>
        确认加入版本
      </Button>
      {message ? <Alert type="success" showIcon title={message} /> : null}
      {error ? <Alert type="error" showIcon title="版本操作未完成" description={describe(error)} /> : null}
    </Space>
  );
}

export function MaterialsPage({ subjectId }: { subjectId: string }) {
  const navigate = useNavigate();
  const [materials, setMaterials] = useState<MaterialView[] | null>(null);
  const [series, setSeries] = useState<MaterialSeriesView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [purpose, setPurpose] = useState<MaterialPurpose>("study_material");
  const [file, setFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState({ year: "", publisher: "", isbn: "", exam_name: "" });
  const formRef = useRef<HTMLFormElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([listMaterials(subjectId, controller.signal), listSeries(subjectId, controller.signal)])
      .then(([rows, groups]) => {
        if (controller.signal.aborted) return;
        setMaterials(rows);
        setSeries(groups);
      })
      .catch(cause => {
        if (controller.signal.aborted) return;
        setError(cause);
        setMaterials(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [subjectId, refresh]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setError(null);
    if (!file) {
      setError(new Error("请选择要上传的文件。"));
      return;
    }
    if (file.size > APPROVED_UPLOAD_BYTES) {
      setError(new LearningApiError("LIMIT_EXCEEDED", 413));
      return;
    }
    setBusy(true);
    try {
      const value = await uploadMaterial(subjectId, file, {
        purpose,
        year: metadata.year ? Number(metadata.year) : null,
        publisher: metadata.publisher || null,
        isbn: metadata.isbn || null,
        exam_name: metadata.exam_name || null,
      });
      setMessage(
        `${value.original_filename} 已上传：${parseLabels[value.parse_state] ?? value.parse_state}` +
          `（${value.chunk_count} 个片段${value.requires_ocr ? "；需要 OCR" : ""}）`,
      );
      formRef.current?.reset();
      setFile(null);
      setMetadata({ year: "", publisher: "", isbn: "", exam_name: "" });
      setRefresh(value => value + 1);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page-stack" aria-labelledby="materials-title">
      <div>
        <Typography.Title level={3} id="materials-title">资料</Typography.Title>
        <Typography.Paragraph type="secondary">
          只处理你主动上传的文件：学习资料进入个人知识库，试卷只保留原件与解析事实。
          单次上传上限 20 MiB；DOCX 解包上限 20 MiB、最多 8 张图，PDF 每批 10 页。
          超限或失败会明确拒绝，不静默截断，也不会自动续跑批次。
        </Typography.Paragraph>
      </div>

      <Card title="上传资料">
        <form ref={formRef} onSubmit={submit}>
          <Space orientation="vertical" style={{ width: "100%" }} size={12}>
            <div className="wizard-field">
              <label htmlFor="material-purpose">用途</label>
              <select
                id="material-purpose"
                value={purpose}
                disabled={busy}
                onChange={event => setPurpose(event.target.value as MaterialPurpose)}
              >
                <option value="study_material">学习资料（教材/讲义）</option>
                <option value="exam_paper">试卷（真题原件）</option>
              </select>
            </div>
            <div className="wizard-field">
              <label htmlFor="material-file">文件（PDF / TXT / Markdown / DOCX / PNG / JPEG）</label>
              <input
                id="material-file"
                type="file"
                accept=".pdf,.txt,.md,.markdown,.docx,.png,.jpg,.jpeg"
                disabled={busy}
                onChange={event => setFile(event.target.files?.[0] ?? null)}
              />
            </div>
            {purpose === "study_material" ? (
              <Space wrap>
                <label className="wizard-field">
                  <span>出版年份（可选）</span>
                  <Input
                    aria-label="出版年份"
                    value={metadata.year}
                    disabled={busy}
                    onChange={event => setMetadata(previous => ({ ...previous, year: event.target.value }))}
                  />
                </label>
                <label className="wizard-field">
                  <span>出版社（可选）</span>
                  <Input
                    aria-label="出版社"
                    value={metadata.publisher}
                    disabled={busy}
                    onChange={event => setMetadata(previous => ({ ...previous, publisher: event.target.value }))}
                  />
                </label>
                <label className="wizard-field">
                  <span>ISBN（可选）</span>
                  <Input
                    aria-label="ISBN"
                    value={metadata.isbn}
                    disabled={busy}
                    onChange={event => setMetadata(previous => ({ ...previous, isbn: event.target.value }))}
                  />
                </label>
              </Space>
            ) : (
              <label className="wizard-field">
                <span>试卷名称（可选）</span>
                <Input
                  aria-label="试卷名称"
                  value={metadata.exam_name}
                  disabled={busy}
                  onChange={event => setMetadata(previous => ({ ...previous, exam_name: event.target.value }))}
                />
              </label>
            )}
            <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
              上传并解析
            </Button>
          </Space>
        </form>
      </Card>

      {message ? <Alert type="success" showIcon title={message} /> : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          title="资料操作未完成"
          description={
            <Space orientation="vertical">
              <span>{describe(error)}</span>
              <span>失败或部分完成的资料会保留真实状态；是否重新上传由你决定，系统不会自动续跑。</span>
            </Space>
          }
        />
      ) : null}
      {loading ? <Spin description="正在读取资料"><div style={{ minHeight: 80 }} /></Spin> : null}

      {!loading && materials?.length === 0 ? (
        <Card>
          <Typography.Text type="secondary">还没有资料。上传一份讲义或试卷开始。</Typography.Text>
        </Card>
      ) : null}

      {materials?.map(material => (
        <Card
          key={material.id}
          title={material.original_filename}
          extra={<Tag>{purposeLabel(material.purpose)}</Tag>}
        >
          <Space orientation="vertical" style={{ width: "100%" }} size={10}>
            <Typography.Text>
              状态：{stateLabels[material.state] ?? material.state}／解析：
              {parseLabels[material.parse_state] ?? material.parse_state}／片段 {material.chunk_count} 个
              {material.requires_ocr ? "／需 OCR" : ""}
              {material.issues?.length ? `／疑点：${(material.issues ?? []).join("、")}` : ""}
            </Typography.Text>
            {material.error_code ? (
              <Alert
                type="error"
                showIcon
                title={`${material.error_code}${material.retryable ? "（可重试）" : "（不可重试）"}`}
                description={material.error_message ?? undefined}
              />
            ) : null}
            <Space>
              {material.purpose === "exam_paper" ? <Button disabled={busy || material.state === "failed"} onClick={async()=>{
                setBusy(true);setError(null);
                try {const paper=await createPaper(subjectId,material.id);
                  navigate("/subjects/"+encodeURIComponent(subjectId)+"/imports/"+encodeURIComponent(paper.id));}
                catch(cause){setError(cause);}finally{setBusy(false);}
              }}>整理为题库</Button>:null}
              <Button onClick={() => setOpenId(openId === material.id ? null : material.id)}>
                {openId === material.id ? "收起原文" : "打开原文"}
              </Button>
              <Button onClick={() => setRefresh(value => value + 1)}>刷新状态</Button>
            </Space>
            {openId === material.id ? <SourceViewer subjectId={subjectId} material={material} /> : null}
            {material.purpose === "study_material" ? <MaterialOCRPanel
              key={`ocr:${subjectId}:${material.id}`}
              subjectId={subjectId}
              material={material}
              onChanged={value => setMaterials(previous => previous?.map(row => row.id === value.id ? value : row) ?? null)}
            /> : null}
            {material.purpose === "study_material" ? <MaterialIndexPanel
              key={`${subjectId}:${material.id}`}
              subjectId={subjectId}
              material={material}
              onIndexed={value => setMaterials(previous => previous?.map(row => row.id === value.id ? value : row) ?? null)}
            /> : null}
            <VersionPanel
              subjectId={subjectId}
              material={material}
              series={series}
              onChanged={() => setRefresh(value => value + 1)}
            />
          </Space>
        </Card>
      ))}

      {series.length > 0 ? (
        <Card title="版本系列与默认版本">
          <Space orientation="vertical" style={{ width: "100%" }} size={12}>
            <Typography.Paragraph type="secondary">
              默认版本只影响之后的新请求；已有引用仍指向它们创建时的真实版本。
            </Typography.Paragraph>
            {series.map(group => (
              <div key={group.id} data-testid="material-series">
                <Typography.Title level={5}>{group.name}</Typography.Title>
                <Space wrap>
                  {group.versions.map(version => (
                    <Space key={version.id}>
                      <Tag color={version.is_default ? "green" : undefined}>
                        {version.label}
                        {version.is_default ? "（默认）" : ""}
                      </Tag>
                      {version.is_default ? null : (
                        <Button
                          size="small"
                          onClick={async () => {
                            setError(null);
                            try {
                              await setDefaultVersion(group.id, version.id);
                              setMessage(`默认版本已切换为「${version.label}」`);
                              setRefresh(value => value + 1);
                            } catch (cause) {
                              setError(cause);
                            }
                          }}
                        >
                          设为默认
                        </Button>
                      )}
                    </Space>
                  ))}
                </Space>
              </div>
            ))}
          </Space>
        </Card>
      ) : null}

      <KnowledgeConfirm subjectId={subjectId} onConfirmed={() => setRefresh(value => value + 1)} />
    </section>
  );
}
