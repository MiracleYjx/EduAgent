import { useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Typography } from "antd";
import { getLearningOperation, LearningApiError, type LearningOperation } from "../../api/learningClient";
import { confirmMaterialIndex, executeMaterialIndex, previewMaterialIndex, type MaterialView } from "../../api/materialsClient";
import { OperationConfirmation } from "../paper-import/OperationConfirmation";

const states: Record<string, string> = {
  prepared: "已生成索引预览，尚未确认", confirmed: "索引已确认，待执行",
  running: "正在执行索引", succeeded: "索引操作已成功，等待同步已保存结果",
  failed: "索引执行失败", partial: "索引部分完成", outcome_unknown: "索引结果未知，需要人工核对",
};

export function MaterialIndexPanel({ subjectId, material, onIndexed }: {
  subjectId: string; material: MaterialView; onIndexed: (value: MaterialView) => void;
}) {
  const [operation, setOperation] = useState<LearningOperation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const key = useRef(crypto.randomUUID());
  const savedId = material.index_operation_id;

  async function read(identity: string, signal?: AbortSignal) {
    const value = await getLearningOperation(identity, signal);
    if (value.id !== identity || value.purpose !== `material_index:${material.id}`)
      throw new LearningApiError("INVALID_RESPONSE", 200);
    return value;
  }

  useEffect(() => {
    const controller = new AbortController();
    if (savedId && material.state !== "indexed") {
      setBusy(true);
      read(savedId, controller.signal).then(value => {
        if (!controller.signal.aborted) { setOperation(value); setUncertain(false); }
      }).catch(cause => {
        if (!controller.signal.aborted) { setError(cause); setUncertain(true); }
      }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    }
    return () => controller.abort();
  }, [material.id, material.state, savedId]);

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await action(); }
    catch (cause) {
      setError(cause);
      if (cause instanceof LearningApiError && cause.code === "NETWORK_ERROR") setUncertain(true);
    } finally { setBusy(false); }
  }

  async function execute(value: LearningOperation) {
    const result = await executeMaterialIndex(subjectId, material.id, value.id);
    if ("parse_state" in result) { onIndexed(result); setOperation(null); setDismissed(false); }
    else { setOperation(result); setUncertain(false); }
  }

  const identity = operation?.id ?? savedId;
  const indexed = material.state === "indexed";
  const needsPreview = error instanceof LearningApiError && error.status === 409
    && operation?.actual_requests === 0 && ["prepared", "confirmed"].includes(operation.state);
  return <Space orientation="vertical" style={{ width: "100%" }}>
    <Typography.Text type="secondary">
      {indexed ? "资料已索引，可在本科目的资料范围内检索。" : "解析完成后，请预览并确认索引，资料才会进入教材检索。"}
    </Typography.Text>
    {!indexed && (!operation || dismissed) ? <Button disabled={busy || material.parse_state !== "parsed" || material.requires_ocr || material.chunk_count === 0}
      onClick={() => run(async () => {
        const value = identity ? await read(identity) : await previewMaterialIndex(material.id, key.current);
        setOperation(value); setDismissed(false); setUncertain(false);
      })}>{identity ? "查看已保存索引操作" : "预览资料索引与费用"}</Button> : null}
    {operation && !dismissed && !indexed ? <>
      <Typography.Text>索引操作 ID：{operation.id}；{states[operation.state] ?? operation.state}</Typography.Text>
      <Typography.Paragraph>仅使用这里预览的资料片段与模型。费用估算不是供应商账单硬上限；本地执行与远程请求次数以服务端预览为准。</Typography.Paragraph>
      {operation.state === "prepared" ? <>
        <OperationConfirmation value={operation} busy={busy} disabled={uncertain || needsPreview} onConfirm={() => run(async () => {
          const confirmed = await confirmMaterialIndex(material.id, operation);
          setOperation(confirmed);
          if (confirmed.state === "confirmed") await execute(confirmed);
        })} />
        <Button disabled={busy} onClick={() => setDismissed(true)}>取消索引确认</Button>
      </> : null}
      {operation.state === "confirmed" ? <Button disabled={busy || uncertain || needsPreview} loading={busy}
        onClick={() => run(() => execute(operation))}>执行已确认索引</Button> : null}
      {operation.state === "succeeded" ? <Button disabled={busy || uncertain}
        onClick={() => run(() => execute(operation))}>同步已保存索引结果（不补发）</Button> : null}
      {["failed", "partial", "outcome_unknown"].includes(operation.state) ? <Alert type="warning"
        title={states[operation.state]} description="已保留真实操作状态，不会自动重试或补发。请先核对失败原因与已有请求。" /> : null}
    </> : null}
    {identity && !indexed ? <Button disabled={busy} onClick={() => run(async () => {
      setOperation(await read(identity)); setUncertain(false); setDismissed(false);
    })}>读取索引操作状态</Button> : null}
    {needsPreview || operation?.state === "failed" || operation?.state === "partial" ? <Button disabled={busy || uncertain} onClick={() => run(async () => {
      key.current = crypto.randomUUID();
      setOperation(await previewMaterialIndex(material.id, key.current)); setDismissed(false);
    })}>重新预览实际范围与费用</Button> : null}
    {busy ? <Typography.Text>正在处理索引操作…</Typography.Text> : null}
    {uncertain ? <Alert type="warning" title="索引请求结果尚未核实"
      description="页面不会自动补发。请先读取已有操作状态，再决定下一步。" /> : null}
    {error ? <Alert type="error" title="资料索引未完成"
      description={error instanceof Error ? error.message : "请求失败"} /> : null}
  </Space>;
}
