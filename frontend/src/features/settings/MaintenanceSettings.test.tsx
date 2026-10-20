import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SettingsSessionProvider } from "../../auth/SettingsSession";
import { errorFixture, exchangeFixture, jsonResponse, settingsViewFixture } from "../../test/fixtures";
import { draftFixture, DRAFT_ID } from "../onboarding/wizardFixtures";
import { MaintenanceSettings } from "./MaintenanceSettings";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/settings"); });
function setup(conflict = false) {
  window.history.replaceState(null, "", "/settings#code=synthetic");
  const onImported = vi.fn(), onRestored = vi.fn(); const calls: { path: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), body = JSON.parse(String(init?.body ?? "{}")); calls.push({ path, body });
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path.endsWith("/sources")) return jsonResponse({ sources: ["selected"] });
    if (path.endsWith("/preview")) return jsonResponse({ preview_id: "legacy-preview", source_id: "selected", expires_at: "2099-01-01T00:00:00Z", warnings: ["LEGACY_PLAINTEXT_REMAINS"], missing_fields: [], credential_purposes: ["jwt"], provider_adapters: [], restart_required: true });
    if (path.endsWith("/apply")) return jsonResponse(draftFixture());
    if (path.endsWith("/recovery")) return jsonResponse({ damaged: true, current_revision: null, manifest_fingerprint: "synthetic-fingerprint", revisions: [{ revision_id: DRAFT_ID, available: true, values: draftFixture().values, created_at: "2026-10-18T00:00:00Z", status: "active" }] });
    if (path.endsWith("/restore")) return conflict ? jsonResponse(errorFixture("REVISION_CONFLICT", "指针已变化。"), 409) : jsonResponse({ revision_id: DRAFT_ID, status: "pending_restart", restart_required: true, activation_keys: [] });
    if (path === "/api/settings/") return jsonResponse(settingsViewFixture({ pending_revision: DRAFT_ID, restart_required: true }));
    throw new Error("Unexpected request");
  }));
  render(<SettingsSessionProvider><MaintenanceSettings settings={settingsViewFixture()} onImported={onImported} onRestored={onRestored} /></SettingsSessionProvider>);
  return { calls, onImported, onRestored };
}
it("requires a fresh import preview and confirmation, returning only a candidate", async () => {
  const { calls, onImported } = setup();
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "读取已授权的旧配置来源" })); expect(calls.some(item => item.path.endsWith("/sources"))).toBe(true); });
  await screen.findByText("请选择已授权的旧配置来源。");
  fireEvent.change(screen.getByLabelText("旧配置来源名称"), { target: { value: "selected" } });
  fireEvent.click(screen.getByRole("button", { name: "预览旧配置导入" }));
  await screen.findByText("导入仅产生候选草稿，旧文件中的明文仍然存在");
  expect(screen.getByRole("button", { name: "确认导入候选" })).toBeDisabled();
  expect(calls.some(item => item.path.endsWith("/apply"))).toBe(false);
  fireEvent.click(screen.getByText("确认按此预览导入，不改删旧文件")); fireEvent.click(screen.getByRole("button", { name: "确认导入候选" }));
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1)); expect(calls.some(item => item.path.endsWith("/activate"))).toBe(false);
});
it.each([false, true])("requires explicit recovery confirmation and preserves a conflict: %s", async conflict => {
  const { calls, onRestored } = setup(conflict);
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "读取可恢复修订" })); expect(calls.some(item => item.path.endsWith("/recovery"))).toBe(true); });
  await screen.findByText("当前配置指针损坏，未自动修复");
  fireEvent.change(screen.getByLabelText("恢复目标修订"), { target: { value: DRAFT_ID } });
  expect(screen.getByRole("button", { name: "确认恢复所选修订" })).toBeDisabled();
  fireEvent.click(screen.getByText("已核对目标，确认恢复为待重启修订")); fireEvent.click(screen.getByRole("button", { name: "确认恢复所选修订" }));
  if (conflict) { await screen.findByText(/REVISION_CONFLICT/); expect(onRestored).not.toHaveBeenCalled(); expect(screen.queryByRole("button", { name: "确认恢复所选修订" })).not.toBeInTheDocument(); }
  else await waitFor(() => expect(onRestored).toHaveBeenCalledTimes(1));
  expect(calls.filter(item => item.path.endsWith("/restore"))).toHaveLength(1);
  expect(calls.find(item => item.path.endsWith("/restore"))?.body).toMatchObject({ confirm: true, expected_revision: null, expected_manifest_fingerprint: "synthetic-fingerprint" });
});
