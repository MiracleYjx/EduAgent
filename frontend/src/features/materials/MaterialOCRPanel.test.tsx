import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../../test/fixtures";
import type { MaterialOCR, MaterialView } from "../../api/materialsClient";
import { MaterialOCRPanel } from "./MaterialOCRPanel";

const preview: MaterialOCR = {
  id: "p-1", material_id: "m-1", runtime_snapshot_id: "snapshot-1", preview_digest: "a".repeat(64),
  state: "prepared", page_count: 1, completed_pages: 0, adapter: "rapidocr", model: "local-ocr", request_budget: 0,
};
function material(overrides: Partial<MaterialView> = {}): MaterialView {
  return { id: "m-1", subject_id: "s-1", purpose: "study_material", original_filename: "scan.png", file_format: "png",
    state: "parsed", parse_state: "partial", requires_ocr: true, chunk_count: 0, issues: [], retryable: false,
    blocks: [{ type: "image", anchor: "page-1", page: 1, sha256: "b".repeat(64), media_type: "image/png" }],
    assets: [], ...overrides };
}
function urlOf(input: RequestInfo | URL) { return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url; }
afterEach(() => vi.unstubAllGlobals());

describe("MaterialOCRPanel", () => {
  it("requires explicit preview, confirmation, execution, and review of actual source text", async () => {
    const finished = material({ requires_ocr: false, chunk_count: 1, issues: ["OCR_REQUIRES_REVIEW"],
      ocr: { ...preview, state: "review_required", completed_pages: 1 }, blocks: [
        ...material().blocks!, { type: "text", anchor: "page-1:ocr", page: 1, text: "Actual OCR",
          ocr_source_anchor: "page-1" },
      ] });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.endsWith("/ocr-preview")) return jsonResponse(preview);
      if (url.endsWith("/confirm")) {
        expect(JSON.parse(init?.body as string)).toEqual({ preview_digest: preview.preview_digest });
        return jsonResponse({ ...preview, state: "confirmed" });
      }
      if (url.endsWith("/execute")) return jsonResponse(finished);
      if (url.endsWith("/review")) return jsonResponse({ ...finished, parse_state: "parsed", issues: [], ocr: { ...preview, state: "succeeded" } });
      throw new Error(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    const changed = vi.fn();
    const view = render(<MaterialOCRPanel subjectId="s-1" material={material()} onChanged={changed} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "预览本地 OCR 范围" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认本地 OCR 范围" }));
    expect(fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith("/execute"))).toHaveLength(0);
    fireEvent.click(await screen.findByRole("button", { name: "执行已确认本地 OCR" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith(finished));
    view.rerender(<MaterialOCRPanel subjectId="s-1" material={finished} onChanged={changed} />);
    expect(await screen.findByText("Actual OCR")).toBeInTheDocument();
    expect(screen.getByText(/OCR 识别文字；原图定位/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith("/review"))).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "已核对原图，确认识别结果" }));
    await waitFor(() => expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ parse_state: "parsed" })));
  });

  it("opening a saved preview never executes and failed transport requires a state read", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (urlOf(input).endsWith("/execute")) throw new TypeError("connection lost");
      if (urlOf(input).endsWith("/materials/m-1")) return jsonResponse(material({ ocr: { ...preview, state: "running" } }));
      throw new Error(urlOf(input));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MaterialOCRPanel subjectId="s-1" material={material({ ocr: { ...preview, state: "confirmed" } })} onChanged={vi.fn()} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "执行已确认本地 OCR" }));
    await screen.findByText("请先读取本地 OCR 状态");
    expect(screen.getByRole("button", { name: /执行已确认本地 OCR/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "读取本地 OCR 状态" }));
    await screen.findByText("本地识别仍在运行");
    expect(fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith("/execute"))).toHaveLength(1);
  });
});
