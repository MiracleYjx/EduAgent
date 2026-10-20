import { MemoryRouter } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "../../test/fixtures";
import type { MaterialView } from "../../api/materialsClient";
import { MaterialsPage } from "./MaterialsPage";

type FetchHandler = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

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
    chunk_count: 3,
    issues: ["PDF_REQUIRES_OCR"],
    requires_ocr: true,
    blocks: [{ type: "text", anchor: "/w:document/w:body/w:p[1]", text: "第一章 函数", page: null }],
    assets: [],
    series_id: null,
    version_id: null,
    version_label: null,
    default_version_id: null,
    ...overrides,
  };
}

const knowledge = { subject_id: "s-1", confirmed: false, chapters: [], knowledge_points: [] };

function install(handler: FetchHandler) {
  const fetchMock = vi.fn(handler);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("MaterialsPage", () => {
  it("lists real material state and opens the source without fabricating pages", async () => {
    install(async input => {
      const url = urlOf(input);
      if (url.endsWith("/materials")) return jsonResponse({ materials: [material()] });
      if (url.endsWith("/material-series")) return jsonResponse({ series: [] });
      if (url.endsWith("/knowledge")) return jsonResponse(knowledge);
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><MaterialsPage subjectId="s-1" /></MemoryRouter>);

    expect(await screen.findByText("教材.docx")).toBeInTheDocument();
    expect(screen.getByText(/状态：已解析／解析：解析完成／片段 3 个／需 OCR／疑点：PDF_REQUIRES_OCR/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "打开原文" }));

    expect(await screen.findByText("原文：教材.docx")).toBeInTheDocument();
    expect(screen.getByText(/原对象锚点 \/w:document\/w:body\/w:p\[1\]/)).toBeInTheDocument();
    expect(screen.queryByText(/^第 \d+ 页$/)).not.toBeInTheDocument();
  });

  it("uploads multipart metadata plus the chosen file and refreshes the list", async () => {
    let captured: { url: string; body: FormData | null } | null = null;
    const fetchMock = install(async (input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/materials")) return jsonResponse({ materials: [] });
      if (url.endsWith("/material-series")) return jsonResponse({ series: [] });
      if (url.endsWith("/knowledge")) return jsonResponse(knowledge);
      if (url.endsWith("/uploads")) {
        captured = { url, body: (init?.body as FormData) ?? null };
        return jsonResponse(material({ id: "m-2", original_filename: "notes.txt", chunk_count: 1 }), 201);
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><MaterialsPage subjectId="s-1" /></MemoryRouter>);
    await screen.findByText(/还没有资料/);

    const file = new File(["第 1 段 教学资料正文。"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText(/文件（PDF/), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "上传并解析" }));

    await waitFor(() => expect(captured).not.toBeNull());
    const { url, body } = captured as unknown as { url: string; body: FormData };
    expect(url).toBe("/api/learning/subjects/s-1/uploads");
    expect(body.get("metadata")).toBe(JSON.stringify({ purpose: "study_material" }));
    expect((body.get("file") as File).name).toBe("notes.txt");
    expect(await screen.findByText(/notes.txt 已上传/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("surfaces the server error code instead of pretending success", async () => {
    install(async (input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/materials")) return jsonResponse({ materials: [] });
      if (url.endsWith("/material-series")) return jsonResponse({ series: [] });
      if (url.endsWith("/knowledge")) return jsonResponse(knowledge);
      if (url.endsWith("/uploads")) {
        expect(init?.method).toBe("POST");
        return jsonResponse({ code: "LIMIT_EXCEEDED", message: "超过上限", stage: "personal_learning", retryable: false }, 413);
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><MaterialsPage subjectId="s-1" /></MemoryRouter>);
    await screen.findByText(/还没有资料/);

    const file = new File(["x"], "huge.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText(/文件（PDF/), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "上传并解析" }));

    expect(await screen.findByText(/LIMIT_EXCEEDED/)).toBeInTheDocument();
    expect(screen.getByText(/系统不会自动续跑/)).toBeInTheDocument();
  });

  it("switches the default version only for later requests", async () => {
    const series = {
      id: "g-1",
      subject_id: "s-1",
      name: "数学 2024",
      default_version_id: "v-1",
      versions: [
        { id: "v-1", document_id: "m-1", label: "上册", is_default: true },
        { id: "v-2", document_id: "m-2", label: "下册", is_default: false },
      ],
    };
    install(async (input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/materials")) return jsonResponse({ materials: [material()] });
      if (url.endsWith("/material-series")) return jsonResponse({ series: [series] });
      if (url.endsWith("/knowledge")) return jsonResponse(knowledge);
      if (url.endsWith("/material-series/g-1/default-version")) {
        expect(init?.method).toBe("POST");
        return jsonResponse({ ...series, default_version_id: "v-2" });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><MaterialsPage subjectId="s-1" /></MemoryRouter>);
    await screen.findByRole("heading", { name: "数学 2024" });
    fireEvent.click(screen.getByRole("button", { name: "设为默认" }));

    expect(await screen.findByText(/默认版本已切换为「下册」/)).toBeInTheDocument();
  });
});
