import { Tag } from "antd";

import type { ReadinessCheck } from "../api/contracts";

export function ReadyTag({
  ready,
  readyText = "可用",
  notReadyText = "不可用",
}: {
  ready: boolean;
  readyText?: string;
  notReadyText?: string;
}) {
  return (
    <Tag color={ready ? "success" : "warning"}>
      {ready ? readyText : notReadyText}
    </Tag>
  );
}

const checkColors: Record<ReadinessCheck["status"], string> = {
  ok: "success",
  failed: "error",
  unknown: "warning",
  skipped: "default",
};

const checkLabels: Record<ReadinessCheck["status"], string> = {
  ok: "正常",
  failed: "失败",
  unknown: "未知",
  skipped: "未执行",
};

export function CheckStatusTag({
  status,
}: {
  status: ReadinessCheck["status"];
}) {
  return <Tag color={checkColors[status]}>{checkLabels[status]}</Tag>;
}

export function EvaluationTag({
  evaluated,
  status,
}: {
  evaluated: boolean;
  status: string;
}) {
  if (!evaluated) {
    return <Tag color="default">未评估</Tag>;
  }
  const color =
    status === "ready"
      ? "success"
      : status === "not_configured"
        ? "default"
        : "warning";
  return <Tag color={color}>{status}</Tag>;
}
