import { useEffect, useState } from "react";
import { Alert, Button, Space, Typography } from "antd";
import { confirmMaterialOCR, executeMaterialOCR, getMaterial, previewMaterialOCR, recoverMaterialOCR, reviewMaterialOCR,
  type MaterialOCR, type MaterialView } from "../../api/materialsClient";
import { SourceViewer } from "./SourceViewer";

const labels: Record<string, string> = {
  prepared: "已预览，待确认", confirmed: "已确认，待执行", running: "本地识别中",
  review_required: "识别完成，待核对原图与文字", succeeded: "识别结果已确认",
  partial: "部分识别完成", failed: "识别失败",
};

export function MaterialOCRPanel({ subjectId, material, onChanged }: {
  subjectId: string; material: MaterialView; onChanged: (value: MaterialView) => void;
}) {
  const [preview, setPreview] = useState<MaterialOCR | null>(material.ocr ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  useEffect(() => setPreview(material.ocr ?? null), [material.ocr]);
  const current = preview ?? material.ocr;
  const applicable = material.requires_ocr || current?.state === "review_required" || current?.state === "running";
  if (!applicable && !current) return null;

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await action(); }
    catch (cause) { setError(cause); setUncertain(true); }
    finally { setBusy(false); }
  }

  function saved(value: MaterialView) {
    onChanged(value); setPreview(value.ocr ?? null); setUncertain(false);
  }

  return <Space orientation="vertical" style={{ width: "100%" }}>
    <Typography.Text strong>本地 OCR</Typography.Text>
    <Typography.Paragraph type="secondary">
      使用已配置的本地 RapidOCR 识别已保留的扫描页或图片。先预览范围并确认，识别后核对原图与文字；随后再确认知识组织和索引。
    </Typography.Paragraph>
    {current ? <Typography.Text>
      {labels[current.state] ?? current.state}；{current.page_count} 页／图片，本轮完成 {current.completed_pages ?? 0} 个；
      {current.adapter}／{current.model}，远程请求 0 次。
    </Typography.Text> : null}
    {material.requires_ocr && (!current || ["failed", "partial"].includes(current.state)) ? <Button disabled={busy || uncertain}
      onClick={() => run(async () => {
        setPreview(await previewMaterialOCR(material.id, crypto.randomUUID()));
      })}>预览本地 OCR 范围</Button> : null}
    {current?.state === "prepared" ? <Button disabled={busy || uncertain} loading={busy}
      onClick={() => run(async () => {
        const confirmed = await confirmMaterialOCR(material.id, current);
        setPreview(confirmed);
      })}>确认本地 OCR 范围</Button> : null}
    {current?.state === "confirmed" ? <Button disabled={busy || uncertain} loading={busy}
      onClick={() => run(async () => saved(await executeMaterialOCR(subjectId, material.id, current.id)))}>
      执行已确认本地 OCR
    </Button> : null}
    {current && ["prepared", "confirmed"].includes(current.state) ? <Button disabled={busy || uncertain}
      onClick={() => run(async () => setPreview(await previewMaterialOCR(material.id, crypto.randomUUID())))}>
      重新预览本地 OCR
    </Button> : null}
    {current?.state === "review_required" ? <>
      <SourceViewer subjectId={subjectId} material={material} />
      <Button disabled={busy || uncertain} loading={busy}
        onClick={() => run(async () => saved(await reviewMaterialOCR(subjectId, material.id, current)))}>
        已核对原图，确认识别结果
      </Button>
    </> : null}
    {current?.state === "running" ? <Alert type="info" title="本地识别仍在运行"
      description="请求等待结束不会停止原生识别。可刷新状态；运行结束前不会启动另一份 OCR。" /> : null}
    {current?.state === "running" ? <Button disabled={busy || uncertain}
      onClick={() => run(async () => saved(await recoverMaterialOCR(subjectId, material.id, current.id)))}>
      核实执行者已停止并恢复本地 OCR 状态
    </Button> : null}
    {current && ["failed", "partial"].includes(current.state) ? <Alert type="warning"
      title={labels[current.state]} description="原图与已完成文字均保留；未完整识别或未确认的资料不能建立完整索引。重新预览只处理剩余图片。" /> : null}
    {current || uncertain ? <Button disabled={busy} onClick={() => run(async () => saved(await getMaterial(subjectId, material.id)))}>
      读取本地 OCR 状态
    </Button> : null}
    {uncertain ? <Alert type="warning" title="请先读取本地 OCR 状态"
      description="页面不会自动再次执行。读取已有状态后再选择下一步。" /> : null}
    {error ? <Alert type="error" title="本地 OCR 操作未完成" description={error instanceof Error ? error.message : "操作失败"} /> : null}
  </Space>;
}
