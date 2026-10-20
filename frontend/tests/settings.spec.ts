// TCR-034: integrated DOM workflows; actual PC viewport checks run separately.
import { createElement } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../src/app/App";
import { errorFixture, exchangeFixture, jsonResponse, readinessFixture, settingsViewFixture, setupReadyFixture } from "../src/test/fixtures";
import { draftFixture, DRAFT_ID, PROFILE_ID } from "../src/features/onboarding/wizardFixtures";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });
async function start() {
  window.history.replaceState(null, "", "/settings#code=synthetic-pc-once");
  let unauthorized = false;
  let draft = draftFixture(true);
  const calls: { path: string; method: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), method = init?.method ?? "GET", body = JSON.parse(String(init?.body ?? "{}"));
    calls.push({ path, method, body });
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path === "/api/learning/subjects") return jsonResponse({ subjects: [] });
    if (unauthorized) return jsonResponse(errorFixture("SESSION_EXPIRED", "管理会话已过期"), 401);
    if (path === "/setup-ready") return jsonResponse(setupReadyFixture());
    if (path.endsWith("/readiness")) return jsonResponse(readinessFixture());
    if (path === "/api/settings/") return jsonResponse(settingsViewFixture({ values: draftFixture(true).values }));
    if (path.endsWith("/providers")) return jsonResponse({ providers: [{ kind: "llm", adapter_id: "openai_compatible", canonical_id: "openai_compatible", capabilities: ["text", "json_object"], lifecycle: "owned_per_run", parameters: {} }] });
    if (path.endsWith("/drafts")) { draft = { ...draft, values: body.changes as typeof draft.values }; return jsonResponse(draft, 201); }
    if (path.endsWith("/validate")) return jsonResponse({ valid: true, revision_id: DRAFT_ID, errors: [], affected_tasks: [], restart_required: false });
    if (path.endsWith("/probes")) return jsonResponse({ probe_id: "synthetic-pc-probe", profile_id: PROFILE_ID, capability: "text", status: "failed", result_code: "UNKNOWN", message: "未知错误，不能推断为密钥错误", tested_at: "2026-10-18T00:00:00Z", external_requests: 1, may_have_been_billed: true, correlation_id: "synthetic-pc" });
    throw new Error("Unexpected test endpoint");
  }));
  render(createElement(App));
  await screen.findByRole("tab", { name: "凭据" });
  return { calls, user: userEvent.setup(), expire: () => { unauthorized = true; } };
}
it("keyboard navigation enters credential inputs and tab changes clear secrets", async () => {
  const { calls, user } = await start();
  const tabs = within(screen.getByRole("tablist"));
  const models = tabs.getByRole("tab", { name: "模型与任务" }); models.focus();
  await user.keyboard("{ArrowRight}{ArrowRight}{Enter}");
  const input = await screen.findByLabelText("新凭据原文");
  expect(input).toHaveAttribute("type", "password"); expect(input).toHaveAttribute("autocomplete", "new-password");
  fireEvent.change(input, { target: { value: "synthetic-never-save-secret" } });
  await user.click(tabs.getByRole("tab", { name: "环境与来源" }));
  expect(screen.queryByLabelText("新凭据原文")).toBeNull();
  await user.click(tabs.getByRole("tab", { name: "凭据" }));
  expect(screen.getByLabelText("新凭据原文")).toHaveValue("");
  expect(JSON.stringify(calls)).not.toContain("synthetic-never-save-secret");
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
});
it("leaving the settings route clears secret input and preserves independent navigation", async () => {
  const { user } = await start();
  await user.click(screen.getByRole("tab", { name: "凭据" }));
  fireEvent.change(screen.getByLabelText("新凭据原文"), { target: { value: "synthetic-leaving-secret" } });
  await user.click(screen.getByRole("menuitem", { name: "我的科目" }));
  await screen.findByText("暂无科目");
  expect(screen.queryByLabelText("新凭据原文")).toBeNull();
  await user.click(screen.getByRole("menuitem", { name: "设置概览" }));
  await user.click(await screen.findByRole("tab", { name: "凭据" }));
  expect(screen.getByLabelText("新凭据原文")).toHaveValue("");
  expect(document.body.textContent).not.toContain("synthetic-leaving-secret");
});
it("session expiry removes the live secret form and blocks further writes", async () => {
  const { calls, user, expire } = await start();
  await user.click(screen.getByRole("tab", { name: "凭据" }));
  fireEvent.change(screen.getByLabelText("新凭据原文"), { target: { value: "synthetic-expired-secret" } });
  expire(); await user.click(screen.getByRole("button", { name: "丢弃编辑并重载当前配置" }));
  await screen.findByText("SESSION_EXPIRED：管理会话已过期");
  expect(screen.queryByLabelText("新凭据原文")).toBeNull();
  expect(screen.queryByRole("button", { name: "安全保存新凭据" })).toBeNull();
  expect(JSON.stringify(calls)).not.toContain("synthetic-expired-secret");
});
it("keeps candidate edits across tabs and separates save, validation, paid probe and activation", async () => {
  const { calls, user } = await start();
  fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "PC 候选模型" } });
  await user.click(screen.getByRole("tab", { name: "环境与来源" }));
  await user.click(screen.getByRole("tab", { name: "模型与任务" }));
  expect(screen.getByLabelText("显示名称")).toHaveValue("PC 候选模型");
  await user.click(screen.getByRole("button", { name: "保存候选草稿" }));
  await screen.findByText(/草稿已保存；校验/);
  expect(calls.filter(item => item.method === "POST" && !item.path.endsWith("exchange"))).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "校验已保存草稿" }));
  await screen.findByText("草稿校验通过（尚未激活）");
  await user.click(screen.getByRole("tab", { name: "能力检测" }));
  await user.selectOptions(screen.getByLabelText("检测模型"), PROFILE_ID);
  await user.selectOptions(screen.getByLabelText("检测能力"), "text");
  expect(screen.getByRole("button", { name: "单独检测所选能力" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: "确认本次能力检测可能产生费用" }));
  await user.click(screen.getByRole("button", { name: "单独检测所选能力" }));
  await screen.findByText(/UNKNOWN/);
  expect(screen.getByText(/未知错误，不能推断为密钥错误/)).toBeInTheDocument();
  expect(calls.filter(item => item.path.endsWith("/probes"))).toHaveLength(1);
  expect(calls.some(item => item.path.endsWith("/activate"))).toBe(false);
  expect(screen.getByText(/当前生效：synthetic-active-revision/)).toBeInTheDocument();
}, 15000);
it("requests only relative same-origin API paths and leaves browser persistence empty", async () => {
  const { calls, user } = await start();
  await user.click(screen.getByRole("tab", { name: "维护与恢复" }));
  await screen.findByRole("button", { name: "读取可恢复修订" });
  await waitFor(() => expect(calls.length).toBeGreaterThan(2));
  expect(calls.every(item => item.path.startsWith("/api/settings/"))).toBe(true);
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0); expect(document.cookie).toBe("");
  expect([...document.querySelectorAll("script[src],link[rel=stylesheet]")].every(node => !/^https?:/.test(node.getAttribute("src") ?? node.getAttribute("href") ?? ""))).toBe(true);
});
