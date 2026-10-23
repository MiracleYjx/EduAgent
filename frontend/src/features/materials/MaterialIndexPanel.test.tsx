import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LearningOperation } from "../../api/learningClient";
import type { MaterialView } from "../../api/materialsClient";
import { jsonResponse } from "../../test/fixtures";
import { MaterialIndexPanel } from "./MaterialIndexPanel";

const material = (overrides: Partial<MaterialView> = {}): MaterialView => ({
  id: "material-real", subject_id: "subject-real", purpose: "study_material", original_filename: "notes.txt",
  file_format: "txt", state: "parsed", parse_state: "parsed", chunk_count: 2, requires_ocr: false,
  blocks: [], assets: [], issues: [], retryable: false, ...overrides,
});
const operation = (overrides: Partial<LearningOperation> = {}): LearningOperation => ({
  id: "operation-server-real", purpose: "material_index:material-real", preview_digest: "digest-frozen",
  runtime_snapshot_id: "snapshot-real", state: "prepared", request_budget: 1, actual_requests: 0,
  steps: [{ task_kind: "embedding", model: "embedding-preview-model", scope_summary: "2 个真实片段，1 次远程请求" }],
  estimate_state: "unknown", estimated_cost: null, ...overrides,
});

afterEach(() => vi.unstubAllGlobals());

describe("MaterialIndexPanel", () => {
  it("previews only on request and cancellation sends no confirm or execute", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe("/api/learning/materials/material-real/index-preview");
      return jsonResponse(operation());
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={vi.fn()} />);
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    expect(await screen.findByText(/索引操作 ID：operation-server-real/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消索引确认" }));
    expect(screen.queryByRole("button", { name: "确认本次范围并执行" })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("confirms the frozen digest and executes the server operation identity", async () => {
    const indexed = material({ state: "indexed", index_operation_id: "operation-server-real" });
    const onIndexed = vi.fn();
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("index-preview")) return jsonResponse(operation());
      if (url.endsWith("/operation-server-real/confirm")) {
        expect(JSON.parse(String(init?.body))).toEqual({ preview_digest: "digest-frozen" });
        return jsonResponse(operation({ state: "confirmed" }));
      }
      if (url.endsWith("/operation-server-real/execute")) return jsonResponse(indexed);
      throw new Error(`unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={onIndexed} />);
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认本次范围并执行" }));
    await waitFor(() => expect(onIndexed).toHaveBeenCalledWith(indexed));
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("restores a confirmed operation with GET and continues only after a click", async () => {
    const onIndexed = vi.fn();
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/learning/operations/operation-server-real") {
        expect(init?.method).toBe("GET");
        return jsonResponse(operation({ state: "confirmed" }));
      }
      expect(String(input)).toBe("/api/learning/materials/material-real/index/operation-server-real/execute");
      return jsonResponse(material({ state: "indexed", index_operation_id: "operation-server-real" }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real"
      material={material({ index_operation_id: "operation-server-real" })} onIndexed={onIndexed} />);
    const button = await screen.findByRole("button", { name: "执行已确认索引" });
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(button);
    await waitFor(() => expect(onIndexed).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("publishes an already successful operation only after explicitly syncing saved results", async () => {
    const onIndexed = vi.fn();
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/learning/operations/operation-server-real")
        return jsonResponse(operation({ state: "succeeded", actual_requests: 1 }));
      expect(String(input)).toBe("/api/learning/materials/material-real/index/operation-server-real/execute");
      return jsonResponse(material({ state: "indexed", index_operation_id: "operation-server-real" }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real"
      material={material({ index_operation_id: "operation-server-real" })} onIndexed={onIndexed} />);
    const button = await screen.findByRole("button", { name: "同步已保存索引结果（不补发）" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(onIndexed).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(onIndexed).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps unknown outcomes visible and never automatically resends", async () => {
    const onIndexed = vi.fn();
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("index-preview")) return jsonResponse(operation());
      if (url.endsWith("/confirm")) return jsonResponse(operation({ state: "confirmed" }));
      return jsonResponse(operation({ state: "outcome_unknown", actual_requests: 1 }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={onIndexed} />);
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认本次范围并执行" }));
    expect(await screen.findByText("索引结果未知，需要人工核对")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "执行已确认索引" })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(onIndexed).not.toHaveBeenCalled();
  });

  it("rejects a stale confirmation and requires another preview before execution", async () => {
    const onIndexed = vi.fn();
    const fetch = vi.fn(async (input: RequestInfo | URL) => String(input).endsWith("index-preview")
      ? jsonResponse(operation()) : jsonResponse({ code: "MATERIAL_INDEX_INPUT_CHANGED" }, 409));
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={onIndexed} />);
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认本次范围并执行" }));
    expect(await screen.findByText(/MATERIAL_INDEX_INPUT_CHANGED/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /确认本次范围并执行/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新预览实际范围与费用" })).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(onIndexed).not.toHaveBeenCalled();
  });

  it("keeps incomplete OCR out of the indexing confirmation flow", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real"
      material={material({ parse_state: "partial", requires_ocr: true })} onIndexed={vi.fn()} />);
    expect(screen.getByRole("button", { name: "预览资料索引与费用" })).toBeDisabled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("previews the remaining partial work only after an explicit click without confirming or executing", async () => {
    const requests: string[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); requests.push(url);
      if (url === "/api/learning/operations/operation-server-real")
        return jsonResponse(operation({ state: "partial", request_budget: 2, actual_requests: 1 }));
      expect(url).toBe("/api/learning/materials/material-real/index-preview");
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body)).idempotency_key).toEqual(expect.any(String));
      return jsonResponse(operation({ id: "remaining-operation-real", preview_digest: "remaining-digest",
        steps: [{ task_kind: "embedding", scope_summary: "仅剩余 1 个片段，保留已完成结果", request_budget: 1 }] }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real"
      material={material({ index_operation_id: "operation-server-real" })} onIndexed={vi.fn()} />);
    const retry = await screen.findByRole("button", { name: "重新预览实际范围与费用" });
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(retry);
    expect(await screen.findByText(/索引操作 ID：remaining-operation-real/)).toBeInTheDocument();
    expect(screen.getByText(/仅剩余 1 个片段，保留已完成结果/)).toBeInTheDocument();
    expect(requests).toEqual(["/api/learning/operations/operation-server-real",
      "/api/learning/materials/material-real/index-preview"]);
  });

  it("retains identity after a network interruption and reads status before another action", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("index-preview")) return jsonResponse(operation());
      if (url.endsWith("/confirm")) return jsonResponse(operation({ state: "confirmed" }));
      if (url.endsWith("/execute")) throw new TypeError("network interrupted");
      expect(url).toBe("/api/learning/operations/operation-server-real");
      return jsonResponse(operation({ state: "outcome_unknown", actual_requests: 1 }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认本次范围并执行" }));
    expect(await screen.findByText("索引请求结果尚未核实")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "执行已确认索引" })).toBeDisabled();
    expect(fetch).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole("button", { name: "读取索引操作状态" }));
    expect(await screen.findByText("索引结果未知，需要人工核对")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("displays real zero-request local previews and rejects another material as success", async () => {
    const onIndexed = vi.fn();
    const local = operation({ request_budget: 0, actual_requests: 0, estimate_state: "known", estimated_cost: "0",
      steps: [{ task_kind: "embedding", local_only: true, scope_summary: "本地执行；零 HTTP 请求" }] });
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("index-preview")) return jsonResponse(local);
      if (url.endsWith("/confirm")) return jsonResponse({ ...local, state: "confirmed" });
      return jsonResponse(material({ id: "another-material", state: "indexed", index_operation_id: local.id }));
    });
    vi.stubGlobal("fetch", fetch);
    render(<MaterialIndexPanel subjectId="subject-real" material={material()} onIndexed={onIndexed} />);
    fireEvent.click(screen.getByRole("button", { name: "预览资料索引与费用" }));
    expect(await screen.findByText(/本次最多 0 次请求，已尝试 0 次/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认本次范围并执行" }));
    expect(await screen.findByText(/INVALID_RESPONSE/)).toBeInTheDocument();
    expect(onIndexed).not.toHaveBeenCalled();
  });
});
