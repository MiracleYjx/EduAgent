import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SettingsSessionProvider } from "../../auth/SettingsSession";
import { errorFixture, exchangeFixture, jsonResponse, settingsViewFixture } from "../../test/fixtures";
import { draftFixture, DRAFT_ID, PROFILE_ID } from "../onboarding/wizardFixtures";
import { CandidateEditor } from "./SettingsEditor";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/settings"); });
function setup(conflict = false, overridden = false) {
  window.history.replaceState(null, "", "/settings#code=synthetic-once");
  const candidate = draftFixture(true);
  const initial = settingsViewFixture({ values: candidate.values, sources: overridden ? { "business.jwt_expire_minutes": { source: "process_env", effective: true } } : {} });
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input); const body = JSON.parse(String(init?.body ?? "{}")); calls.push({ path, body });
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path.endsWith("/drafts")) return conflict ? jsonResponse(errorFixture("REVISION_CONFLICT", "版本已更新。"), 409) : jsonResponse(candidate, 201);
    if (path.endsWith("/validate")) return jsonResponse({ valid: true, revision_id: DRAFT_ID, errors: [], affected_tasks: [], restart_required: false });
    if (path.endsWith("/probes")) return jsonResponse({ probe_id: "synthetic-probe", profile_id: PROFILE_ID, capability: "text", status: "failed", result_code: "SERVICE_UNREACHABLE", message: "目标不可达。", tested_at: "2026-10-18T00:00:00Z", external_requests: 1, may_have_been_billed: true, correlation_id: "synthetic" });
    if (path === "/api/settings/") return jsonResponse(initial);
    throw new Error("Unexpected endpoint");
  }));
  render(<SettingsSessionProvider><CandidateEditor initial={initial} catalog={[{ kind: "llm", adapter_id: "openai_compatible", capabilities: ["text", "json_object"], canonical_id: "openai_compatible", lifecycle: "owned_per_run", parameters: {} }]} /></SettingsSessionProvider>);
  return calls;
}
it("saves, validates and probes separately; failed probing preserves candidate and active", async () => {
  const calls = setup(); await waitFor(() => expect(window.location.hash).toBe(""));
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "保存候选草稿" })); expect(calls.some(item => item.path.endsWith("/drafts"))).toBe(true); });
  expect(await screen.findByText(/草稿已保存；校验/)).toBeInTheDocument();
  expect(calls.filter(item => item.path.endsWith("/probes") || item.path.endsWith("/activate"))).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "校验已保存草稿" }));
  expect(await screen.findByText("草稿校验通过（尚未激活）")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "能力检测" }));
  fireEvent.change(screen.getByLabelText("检测模型"), { target: { value: PROFILE_ID } });
  fireEvent.change(screen.getByLabelText("检测能力"), { target: { value: "text" } });
  expect(screen.getByRole("button", { name: "单独检测所选能力" })).toBeDisabled();
  fireEvent.click(screen.getByText("确认本次能力检测可能产生费用"));
  fireEvent.click(screen.getByRole("button", { name: "单独检测所选能力" }));
  expect(await screen.findByText(/SERVICE_UNREACHABLE/)).toBeInTheDocument();
  expect(screen.getByText(new RegExp(`候选草稿：${DRAFT_ID}`))).toBeInTheDocument();
  expect(screen.getByText(/当前生效：synthetic-active-revision/)).toBeInTheDocument();
  expect(calls.filter(item => item.path.endsWith("/probes"))).toHaveLength(1);
  expect(calls.find(item => item.path.endsWith("/probes"))?.body).toMatchObject({ allow_billable: true, draft_revision: DRAFT_ID, capability: "text" });
  expect(calls.some(item => item.path.endsWith("/activate"))).toBe(false);
  expect(screen.getByRole("checkbox", { name: "确认本次能力检测可能产生费用" })).not.toBeChecked();
});
it("a conflict keeps the original active value and requires explicit reload", async () => {
  const calls = setup(true);
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "保存候选草稿" })); expect(calls.some(item => item.path.endsWith("/drafts"))).toBe(true); });
  expect(await screen.findByText(/REVISION_CONFLICT/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存候选草稿" })).toBeDisabled();
  expect(screen.getByText(/当前生效：synthetic-active-revision/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "丢弃编辑并重载当前配置" }));
  expect(await screen.findByText(/已重新加载当前配置/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存候选草稿" })).not.toBeDisabled();
});

it("external overrides require explicit acknowledgement before saving", async () => {
  const calls = setup(false, true);
  expect(screen.getByRole("button", { name: "保存候选草稿" })).toBeDisabled();
  fireEvent.click(screen.getByText("我知晓外部环境可能覆盖保存值；保存不修改环境变量"));
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "保存候选草稿" })); expect(calls.some(item => item.path.endsWith("/drafts"))).toBe(true); });
  expect(calls.find(item => item.path.endsWith("/drafts"))?.body.acknowledge_override).toBe(true);
});
