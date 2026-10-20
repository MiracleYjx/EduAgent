import { createElement, useEffect, useState } from "react";
import { Alert, Typography } from "antd";
import type { ContentBlock, MathNode, QuestionContentView } from "../../api/learningClient";
import { DataChart } from "./DataChart";
import "./content.css";

type MediaUrl = (assetId: string) => string;

function ManagedImage({ url, alt }: { url: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  return failed ? <Alert type="warning" title="资源不可用，请核对原内容" role="status" />
    : <img src={url} alt={alt} referrerPolicy="no-referrer" onError={() => setFailed(true)} />;
}

function Formula({ node }: { node: MathNode }) {
  const tags = { row: "mrow", identifier: "mi", number: "mn", operator: "mo", text: "mtext", sup: "msup", sub: "msub", fraction: "mfrac", sqrt: "msqrt" } as const;
  return createElement(tags[node.kind], null, node.text ?? node.children?.map((child, index) => <Formula key={index} node={child} />));
}

export function QuestionBlocks({ blocks, mediaUrl }: { blocks: ContentBlock[]; mediaUrl: MediaUrl }) {
  return <>{blocks.map((block, index) => {
    let body;
    switch (block.type) {
      case "text": body = <p className="question-text">{block.text}</p>; break;
      case "image": body = <ManagedImage url={mediaUrl(block.asset_id)} alt={block.alt ?? "题目图片"} />; break;
      case "formula": body = createElement("math", { xmlns: "http://www.w3.org/1998/Math/MathML", display: "block", "aria-label": "公式" }, <Formula node={block.expression} />); break;
      case "chart": body = <DataChart chart={block.chart} />; break;
      case "table": body = <div className="question-table-scroll"><table><thead><tr>{block.headers.map((h, i) => <th scope="col" key={i}>{h}</th>)}</tr></thead><tbody>{block.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}</tbody></table></div>; break;
      case "original": body = <>
        <Alert type="warning" title={`待确认：${block.reason}`} />
        <ManagedImage url={mediaUrl(block.asset_id)} alt="待核对原内容" />
        <Typography.Paragraph type="secondary">来源文件 {block.source.document_id} · {block.source.page ? `第 ${block.source.page} 页` : block.source.anchor}</Typography.Paragraph>
      </>; break;
    }
    return <div className="question-block" data-content-type={block.type} key={index}>{body}</div>;
  })}</>;
}

/** Phase is server-derived. Answering never mounts explanation content/media. */
export function QuestionContent({ content, mediaUrl }: { content: QuestionContentView; mediaUrl: MediaUrl }) {
  return <article className="question-content">
    {Boolean(content.issues?.length) && <Alert type="warning" title={content.issues?.includes("CONTENT_RESOURCE_UNAVAILABLE") ? "部分资源不可用，请核对原内容" : "来源定位待确认，请核对原内容"} />}
    <section aria-label="题干"><QuestionBlocks blocks={content.stem_blocks} mediaUrl={mediaUrl} /></section>
    {content.options.length > 0 && <ol className="question-options">{content.options.map((option, i) =>
      <li key={option.id} data-option-id={option.id}><section aria-label={`选项 ${i + 1}`}><QuestionBlocks blocks={option.blocks} mediaUrl={mediaUrl} /></section></li>)}</ol>}
    {content.phase === "review" && content.explanation_blocks && <section aria-label="解析"><Typography.Title level={4}>解析</Typography.Title><QuestionBlocks blocks={content.explanation_blocks} mediaUrl={mediaUrl} /></section>}
  </article>;
}
