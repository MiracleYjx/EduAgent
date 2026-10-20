import { useState } from "react";
import { Alert, Card, Space, Typography } from "antd";

import {
  materialAssetUrl,
  type MaterialBlock,
  type MaterialView,
} from "../../api/materialsClient";

/** 定位说明只使用真实事实：有页码时给页码，否则给原对象锚点（DOCX 不伪造页码）。 */
export function blockLocation(block: MaterialBlock): string {
  return typeof block.page === "number" ? `第 ${block.page} 页` : `原对象锚点 ${block.anchor}`;
}

function BlockBody({ subjectId, material, block }: {
  subjectId: string;
  material: MaterialView;
  block: MaterialBlock;
}) {
  const [imageFailed, setImageFailed] = useState(false);
  if (block.type === "text") return <Typography.Paragraph>{block.text}</Typography.Paragraph>;
  if (block.type === "formula") {
    return (
      <Typography.Paragraph>
        <code>{block.expression || block.mathml || "公式"}</code>
      </Typography.Paragraph>
    );
  }
  if (block.type === "table") {
    return (
      <table className="source-table">
        <tbody>
          {block.rows?.map((row: string[], rowIndex: number) => (
            <tr key={rowIndex}>
              {row.map((cell: string, cellIndex: number) => (
                <td key={cellIndex}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (block.type === "image") {
    if (imageFailed) {
      return <Alert type="error" showIcon title="原图加载失败" description="原图不可用或完整性核验失败，请检查资料后重新打开。" />;
    }
    if (!block.sha256) {
      return <Typography.Text type="warning">图像缺少内容摘要，无法打开原图。</Typography.Text>;
    }
    return (
      <img
        className="source-image"
        src={materialAssetUrl(subjectId, material.id, block.sha256)}
        alt={block.anchor}
        loading="lazy"
        onError={() => setImageFailed(true)}
      />
    );
  }
  return (
    <Typography.Text type="warning">
      未渲染对象{block.reason ? `（${block.reason}）` : ""}；原对象仍保留在源文件中，不做猜测。
    </Typography.Text>
  );
}

export function SourceViewer({ subjectId, material }: {
  subjectId: string;
  material: MaterialView;
}) {
  return (
    <Card
      size="small"
      title={`原文：${material.original_filename}`}
      extra={<Typography.Text type="secondary">{material.file_format.toUpperCase()}</Typography.Text>}
    >
      <Space orientation="vertical" style={{ width: "100%" }} size={12}>
        {material.requires_ocr ? (
          <Alert
            type="info"
            showIcon
            title="此资料需要 OCR"
            description="正文没有伪造文本；扫描页按原图保留，需在具备本地 OCR 能力时处理。"
          />
        ) : null}
        {material.issues?.length ? (
          <Alert type="warning" showIcon title="解析疑点（不静默丢弃）" description={(material.issues ?? []).join("、")} />
        ) : null}
        <ol className="source-blocks">
          {(material.blocks ?? []).map((block, index) => (
            <li key={`${block.anchor}-${index}`} data-testid="source-block" data-anchor={block.anchor}>
              <Typography.Text type="secondary">{blockLocation(block)}</Typography.Text>
              <BlockBody subjectId={subjectId} material={material} block={block} />
            </li>
          ))}
        </ol>
        {material.blocks?.length === 0 ? (
          <Typography.Text type="secondary">该资料没有可展示的内容块。</Typography.Text>
        ) : null}
      </Space>
    </Card>
  );
}
