import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MaterialView } from "../../api/materialsClient";
import { SourceViewer } from "./SourceViewer";

function material(overrides: Partial<MaterialView> = {}): MaterialView {
  return {
    id: "m-1",
    subject_id: "s-1",
    purpose: "study_material",
    original_filename: "教材.docx",
    file_format: "docx",
    state: "parsed",
    parse_state: "parsed",
    error_code: null,
    error_message: null,
    retryable: false,
    optional_metadata: {},
    chunk_count: 2,
    issues: [],
    requires_ocr: false,
    blocks: [],
    assets: [],
    series_id: null,
    version_id: null,
    version_label: null,
    default_version_id: null,
    ...overrides,
  };
}

describe("SourceViewer", () => {
  it("uses OOXML anchors for DOCX and never invents page numbers", () => {
    render(
      <SourceViewer
        subjectId="s-1"
        material={material({
          blocks: [
            { type: "text", anchor: "/w:document/w:body/w:p[1]", text: "第一章 函数", page: null },
          ],
        })}
      />,
    );

    expect(screen.getByText("第一章 函数")).toBeInTheDocument();
    expect(screen.getByText(/原对象锚点 \/w:document\/w:body\/w:p\[1\]/)).toBeInTheDocument();
    expect(screen.queryByText(/^第 \d+ 页$/)).not.toBeInTheDocument();
  });

  it("shows the real page number when the format has pages", () => {
    render(
      <SourceViewer
        subjectId="s-1"
        material={material({
          original_filename: "扫描资料.pdf",
          file_format: "pdf",
          blocks: [{ type: "image", anchor: "page-1", page: 1, sha256: "a".repeat(64), media_type: "image/png" }],
        })}
      />,
    );

    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    expect(screen.getByAltText("page-1")).toHaveAttribute(
      "src",
      `/api/learning/subjects/s-1/materials/m-1/assets/${"a".repeat(64)}`,
    );
  });

  it("keeps unsupported objects visible with their reason", () => {
    render(
      <SourceViewer
        subjectId="s-1"
        material={material({
          blocks: [{ type: "original", anchor: "/w:document/w:body/w:p[2]", reason: "OOXML_UNSUPPORTED_PICT" }],
        })}
      />,
    );

    expect(screen.getByText(/未渲染对象（OOXML_UNSUPPORTED_PICT）/)).toBeInTheDocument();
  });
  it("reports a failed original image instead of silently showing a broken image", () => {
    render(
      <SourceViewer
        subjectId="s-1"
        material={material({
          blocks: [{ type: "image", anchor: "page-1", page: 1, sha256: "b".repeat(64), media_type: "image/png" }],
        })}
      />,
    );

    fireEvent.error(screen.getByAltText("page-1"));
    expect(screen.getByText("原图加载失败")).toBeInTheDocument();
    expect(screen.queryByAltText("page-1")).not.toBeInTheDocument();
  });

});
