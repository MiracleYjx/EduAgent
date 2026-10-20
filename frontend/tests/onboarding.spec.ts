// TCR-027. Mock browser-DOM flows (Vitest/jsdom); not a real-browser receipt.
import { createElement, StrictMode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/app/App";
import { errorFixture, jsonResponse, settingsViewFixture } from "../src/test/fixtures";
import { draftFixture, wizardApi } from "../src/features/onboarding/wizardFixtures";

beforeEach(() => window.history.replaceState({}, "", "/setup#code=synthetic-code"));
afterEach(() => vi.unstubAllGlobals());

async function start(options: Parameters<typeof wizardApi>[0] = {}) {
  const api = wizardApi(options);
  render(createElement(App));
  await screen.findByRole("heading", { name: "首次启动向导" });
  return { api, user: userEvent.setup() };
}

describe("T223 first-run flows", () => {
  it("completes an empty Business-only configuration with explicit saves, AI skip, preview and activation", async () => {
    const { api, user } = await start();
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    for (const [label, value] of [["持久目录（绝对路径）", "C:/synthetic-test/storage"], ["数据库主机", "127.0.0.1"], ["数据库名称", "eduagent_test"], ["数据库用户", "test_runner"], ["Redis 主机", "127.0.0.1"]]) {
      await user.type(screen.getByLabelText(label), value);
    }
    await user.click(screen.getByRole("button", { name: "生成并安全保存 JWT" }));
    await screen.findByText(/JWT 凭据已保存/);
    await user.click(screen.getByRole("button", { name: "保存目录与依赖草稿" }));
    await screen.findByText("草稿已保存");
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await user.click(screen.getByRole("button", { name: "跳过 AI 并保存草稿" }));
    await waitFor(() => expect(api.writes().filter(c => c.path.includes("/drafts/") && c.method === "PATCH")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "校验已保存草稿" }));
    await screen.findByText("配置校验通过");
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    expect(screen.getByText(/尚未配置默认 AI/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await user.click(screen.getByRole("button", { name: "预览初始化目标" }));
    await screen.findByText(/test:127.0.0.1:15432/);
    await user.click(screen.getByRole("checkbox", { name: /我已核对上述环境、数据库和迁移目标/ }));
    await user.click(screen.getByRole("button", { name: "确认执行本次初始化" }));
    await screen.findByText("初始化已完成（本次操作）");
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await user.click(screen.getByRole("checkbox", { name: /我确认激活此草稿/ }));
    await user.click(screen.getByRole("button", { name: "确认激活配置" }));
    await screen.findByText(/配置已激活；当前业务和 AI 状态仍以 readiness 为准/);
    expect(screen.getByText(/Business 尚未就绪/)).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/probes"))).toHaveLength(0);
    expect(api.writes().filter(c => c.path.endsWith("/initialization/apply"))).toHaveLength(1);
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(1);
    const secret = api.writes().find(c => c.path.endsWith("/credentials"))?.body.secret_value as string;
    expect(secret.length).toBeGreaterThanOrEqual(32);
    expect(JSON.stringify(api.writes().filter(c => !c.path.endsWith("/credentials")))).not.toContain(secret);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  }, 20000);

  it("clears a live secret form when the independent readiness read returns 403", async () => {
    const api = wizardApi({ step: 2, draft: draftFixture() });
    let rejectRead: (response: Response) => void = () => undefined;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/readiness") ? new Promise<Response>(resolve => { rejectRead = resolve; }) : api.fetch(input, init));
    render(createElement(App));
    await screen.findByLabelText("JWT 密钥输入");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("JWT 密钥输入"), "synthetic-in-memory-only");
    rejectRead(jsonResponse(errorFixture("ORIGIN_NOT_ALLOWED", "本次来源已失效，请重新授权。"), 403));
    await screen.findByText(/本次来源已失效/);
    expect(screen.queryByLabelText("JWT 密钥输入")).not.toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });

  it("does not rebuild a writable form from stale data after an explicit reload fails", async () => {
    const api = wizardApi({ step: 2, draft: draftFixture() });
    let reads = 0;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/onboarding") && (init?.method ?? "GET") === "GET" && reads++ > 0) {
        return Promise.resolve(jsonResponse(errorFixture("DEPENDENCY_UNAVAILABLE", "重新读取未成功，旧状态不可冒充最新。"), 503));
      }
      return api.fetch(input, init);
    });
    render(createElement(App));
    await screen.findByLabelText("数据库名称");
    await userEvent.setup().click(screen.getByRole("button", { name: "重新读取已保存状态（丢弃未保存输入）" }));
    await screen.findByText(/重新读取未成功/);
    expect(screen.queryByRole("button", { name: "保存目录与依赖草稿" })).not.toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });

  it("discards an old operation poll arriving after a new draft is saved", async () => {
    const api = wizardApi({ step: 2, draft: draftFixture(), runningOperation: true });
    let completePoll: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input).includes("/operations/")
      ? new Promise<Response>(resolve => { completePoll = resolve; }) : api.fetch(input, init));
    render(createElement(App));
    await screen.findByLabelText("数据库名称");
    await waitFor(() => expect(completePoll).toBeTypeOf("function"), { timeout: 3000 });
    await userEvent.setup().click(screen.getByRole("button", { name: "保存目录与依赖草稿" }));
    await waitFor(() => expect(api.writes().some(c => c.path.endsWith("/onboarding"))).toBe(true));
    await act(async () => { completePoll!(jsonResponse({ operation_id: "old-operation", kind: "initialization", status: "succeeded", steps: [], started_at: "2026-10-18T02:00:00Z" })); });
    await waitFor(() => expect(screen.getByText("初始化未完成")).toBeInTheDocument());
    expect(screen.queryByText("初始化已完成（本次操作）")).not.toBeInTheDocument();
  });

  it.each([401, 403] as const)("observes a late HTTP %s even after a sibling read has already failed", async status => {
    const api = wizardApi();
    let rejectLast: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/providers")) return Promise.resolve(jsonResponse(errorFixture("DEPENDENCY_UNAVAILABLE", "目录读取暂不可用。"), 503));
      if (path.endsWith("/onboarding")) return new Promise<Response>(resolve => { rejectLast = resolve; });
      return api.fetch(input, init);
    });
    render(createElement(App));
    await screen.findByText(/目录读取暂不可用/);
    await act(async () => { rejectLast!(jsonResponse(errorFixture("SESSION_EXPIRED", "晚到的授权失败也必须清空。"), status)); });
    await screen.findByText(/晚到的授权失败也必须清空/);
    expect(screen.getAllByText("需要重新授权").length).toBeGreaterThan(0);
    expect(api.writes()).toEqual([]);
  });

  it("does not let a stale running Probe poll overwrite a confirmed cancellation", async () => {
    const api = wizardApi({ step: 4, draft: draftFixture(true), queueProbe: true });
    let finishOldRead: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input).includes("/probes/") && (init?.method ?? "GET") === "GET"
      ? new Promise<Response>(resolve => { finishOldRead = resolve; }) : api.fetch(input, init));
    render(createElement(App));
    await screen.findByRole("heading", { name: "首次启动向导" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: /我确认本次 text 检测可能收费/ }));
    await user.click(screen.getByRole("button", { name: "检测 text" }));
    await waitFor(() => expect(finishOldRead).toBeTypeOf("function"), { timeout: 3000 });
    await user.click(screen.getByRole("button", { name: "取消本次检测" }));
    await screen.findByText(/CANCELLED/);
    await act(async () => { finishOldRead!(jsonResponse({ probe_id: "99999999-9999-4999-8999-000000000001", status: "running", result_code: "RUNNING", message: "stale", capability: "text", profile_id: "44444444-4444-4444-8444-444444444444", tested_at: "2026-10-18T02:00:00Z", correlation_id: "synthetic" })); });
    expect(screen.getByText(/CANCELLED/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消本次检测" })).not.toBeInTheDocument();
  });

  it.each([true, false])("keeps mounted Secrets read-only even with an empty catalog (registered=%s)", async registered => {
    const api = wizardApi({ step: 2, draft: draftFixture() });
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/settings/" ? Promise.resolve(jsonResponse(settingsViewFixture({
      active_revision: null, warnings: ["EXTERNALLY_MANAGED"], credential_statuses: registered ? [{ credential_ref: "77777777-7777-4777-8777-777777777777", version: "88888888-8888-4888-8888-888888888888", purpose: "jwt", status: "unavailable", backend: "mounted_secret", environment_id: "11111111-1111-4111-8111-111111111111" }] : [],
    }))) : api.fetch(input, init));
    render(createElement(App));
    await screen.findByRole("heading", { name: "首次启动向导" });
    expect(screen.queryByLabelText("JWT 密钥输入")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成并安全保存 JWT" })).not.toBeInTheDocument();
    expect(screen.getAllByText(/外部 Secret 仅由宿主维护/).length).toBeGreaterThan(0);
    expect(api.writes()).toEqual([]);
  });

  it("discards unsubmitted secret inputs when leaving and returning to a step", async () => {
    const { api, user } = await start({ step: 2, draft: draftFixture() });
    await user.type(screen.getByLabelText("JWT 密钥输入"), "synthetic-unsubmitted-secret");
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await user.click(screen.getByRole("button", { name: "查看上一步" }));
    expect(screen.getByLabelText("JWT 密钥输入")).toHaveValue("");
    expect(api.writes()).toEqual([]);
  });

  it("does not retry a revision conflict or silently activate another revision", async () => {
    const { api, user } = await start({ step: 2, draft: draftFixture(), draftConflict: true });
    await user.click(screen.getByRole("button", { name: "保存目录与依赖草稿" }));
    await screen.findByText(/REVISION_CONFLICT/);
    expect(screen.getByLabelText("数据库名称")).toHaveValue("eduagent_test");
    expect(api.writes().filter(c => c.path.includes("/drafts/"))).toHaveLength(1);
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
  });

  it("permits explicit cancellation of an in-flight Probe and stops further creates", async () => {
    const { api, user } = await start({ step: 4, draft: draftFixture(true), queueProbe: true });
    await user.click(screen.getByRole("checkbox", { name: /我确认本次 text 检测可能收费/ }));
    await user.click(screen.getByRole("button", { name: "检测 text" }));
    await user.click(await screen.findByRole("button", { name: "取消本次检测" }));
    await screen.findByText(/CANCELLED/);
    expect(api.writes().filter(c => c.path.endsWith("/probes"))).toHaveLength(1);
    expect(api.writes().filter(c => c.method === "DELETE")).toHaveLength(1);
  });

  it("links to personal learning independently of the legacy login", async () => {
    const { api } = await start({ step: 6, draft: draftFixture(), businessReady: true });
    expect(screen.getByRole("link", { name: "进入个人学习" })).toHaveAttribute("href", "/");
    expect(api.writes()).toEqual([]);
  });
  it("allows personal setup completion without a JWT when the actual subject service is available", async () => {
    const draft = draftFixture();
    if (!draft.values.business) throw new Error("synthetic business fixture required");
    draft.values.business.jwt_secret_ref = null;
    const { api } = await start({ step: 6, draft, businessReady: false, personalReady: true });
    await screen.findByText("个人科目服务可用");
    expect(screen.getByRole("button", { name: "记录向导完成" })).toBeEnabled();
    expect(screen.getByRole("link", { name: "进入个人学习" })).toHaveAttribute("href", "/");
    expect(api.writes()).toEqual([]);
  });
  it("loads and changes steps without implicit draft, Probe, migration or activation writes", async () => {
    const { api, user } = await start();
    expect(screen.getByText("草稿尚未保存")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看下一步" }));
    await screen.findByRole("button", { name: "显式运行依赖诊断" });
    expect(api.writes()).toEqual([]);
    expect(screen.queryByRole("link", { name: "进入旧业务（仍需原登录）" })).not.toBeInTheDocument();
  });

  it("recovers a saved partial draft and its step using reads only", async () => {
    const { api } = await start({ step: 2, draft: draftFixture() });
    expect(screen.getByLabelText("数据库名称")).toHaveValue("eduagent_test");
    expect(screen.getByLabelText("持久目录（绝对路径）")).toHaveValue("C:/synthetic-test/storage");
    expect(screen.getByText("草稿已保存")).toBeInTheDocument();
    expect(screen.getByText("配置尚未校验通过")).toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });

  it("shows validation field errors without declaring Probe or Business success", async () => {
    const { api, user } = await start({ step: 2, draft: draftFixture(), invalid: true });
    await user.click(screen.getByRole("button", { name: "校验已保存草稿" }));
    expect(await screen.findByText(/business.jwt_secret_ref.*CREDENTIAL_UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText("配置尚未校验通过")).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
    expect(api.writes().filter(c => c.path.endsWith("/probes"))).toHaveLength(0);
  });

  it.each([401, 403] as const)("clears secret input and session on HTTP %s without replay", async status => {
    const { api, user } = await start({ step: 2, draft: draftFixture(), authorizationFailure: status });
    await user.type(screen.getByLabelText("JWT 密钥输入"), "synthetic-only-secret-not-a-real-key");
    await user.click(screen.getByRole("button", { name: "安全保存 JWT 凭据" }));
    expect(await screen.findByText(/授权或来源已失效/)).toBeInTheDocument();
    expect(screen.queryByLabelText("JWT 密钥输入")).not.toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/credentials"))).toHaveLength(1);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("clears a successful secret submission and stores only its reference in a draft", async () => {
    const { api, user } = await start({ step: 2, draft: draftFixture() });
    const secret = "synthetic-only-jwt-not-a-real-key-123456789";
    await user.type(screen.getByLabelText("JWT 密钥输入"), secret);
    await user.click(screen.getByRole("button", { name: "安全保存 JWT 凭据" }));
    expect(await screen.findByText(/JWT 凭据已保存/)).toBeInTheDocument();
    expect(screen.getByLabelText("JWT 密钥输入")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "保存目录与依赖草稿" }));
    await waitFor(() => expect(api.writes().some(c => c.path.endsWith("/onboarding"))).toBe(true));
    const draftCall = api.writes().find(c => c.path.includes("/drafts/"));
    expect(JSON.stringify(draftCall?.body)).not.toContain(secret);
    expect(JSON.stringify(draftCall?.body)).toContain("jwt_secret_ref");
  });

  it("does not count repeated text successes as verification of another capability", async () => {
    const { api, user } = await start({ step: 4, draft: draftFixture(true) });
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await user.click(screen.getByRole("checkbox", { name: /我确认本次 text 检测可能收费/ }));
      await user.click(screen.getByRole("button", { name: "检测 text" }));
      await waitFor(() => expect(api.writes().filter(c => c.path.endsWith("/probes"))).toHaveLength(attempt + 1));
    }
    expect(screen.getByText("1 项候选能力 Probe 已验证")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /我确认本次 json_object 检测可能收费/ }));
    await user.click(screen.getByRole("button", { name: "检测 json_object" }));
    expect(await screen.findByText("2 项候选能力 Probe 已验证")).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
  });

  it("requires per-capability fee confirmation and preserves a failed Probe as failure", async () => {
    const { api, user } = await start({ step: 4, draft: draftFixture(true), probeFailure: true });
    const probeButton = screen.getByRole("button", { name: "检测 text" });
    expect(probeButton).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /我确认本次 text 检测可能收费/ }));
    await user.click(probeButton);
    expect(await screen.findByText(/SERVICE_UNREACHABLE/)).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/probes"))).toHaveLength(1);
    expect(api.writes().find(c => c.path.endsWith("/probes"))?.body.allow_billable).toBe(true);
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
    expect(screen.getAllByText(/可能已计费/).length).toBeGreaterThan(0);
  });

  it("requires a target preview and confirmation, reports initialization failure and never activates", async () => {
    const { api, user } = await start({ step: 5, draft: draftFixture(), initializationFailure: true });
    expect(screen.queryByRole("button", { name: "确认执行本次初始化" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "预览初始化目标" }));
    expect(await screen.findByText(/test:127.0.0.1:15432/)).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: "确认执行本次初始化" });
    expect(apply).toBeDisabled();
    expect(api.writes().filter(c => c.path.endsWith("/initialization/apply"))).toHaveLength(0);
    await user.click(screen.getByRole("checkbox", { name: /我已核对上述环境、数据库和迁移目标/ }));
    await user.click(apply);
    expect(await screen.findByText(/MIGRATION_FAILED/)).toBeInTheDocument();
    expect(screen.getByText("初始化未完成")).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
  });

  it("retries failed initialization only after a new explicit preview and confirmation", async () => {
    const options = { step: 5, draft: draftFixture(), initializationFailure: true };
    const { api, user } = await start(options);
    async function confirmOne() {
      await user.click(screen.getByRole("button", { name: "预览初始化目标" }));
      await screen.findByText(/test:127.0.0.1:15432/);
      await user.click(screen.getByRole("checkbox", { name: /我已核对上述环境、数据库和迁移目标/ }));
      await user.click(screen.getByRole("button", { name: "确认执行本次初始化" }));
    }
    await confirmOne();
    await screen.findByText(/MIGRATION_FAILED/);
    expect(api.writes().filter(c => c.path.endsWith("/initialization/apply"))).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "确认执行本次初始化" })).not.toBeInTheDocument();
    options.initializationFailure = false;
    await confirmOne();
    await screen.findByText("初始化已完成（本次操作）");
    expect(api.writes().filter(c => c.path.endsWith("/initialization/apply"))).toHaveLength(2);
    expect(api.writes().filter(c => c.path.endsWith("/initialization/preview"))).toHaveLength(2);
    expect(api.writes().filter(c => c.path.endsWith("/activate"))).toHaveLength(0);
  });

  it("restores interrupted initialization without replay and offers a fresh preview", async () => {
    const { api } = await start({ step: 5, draft: draftFixture(), operationInterrupted: true });
    expect(screen.getByText(/初始化已中断/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "预览初始化目标" })).toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });

  it("shows pending_restart and manual restart without bypassing the Business gate", async () => {
    const draft = { ...draftFixture(), status: "validated" as const };
    const { api, user } = await start({ step: 6, draft, pending: true });
    await user.click(screen.getByRole("checkbox", { name: /我确认激活此草稿/ }));
    await user.click(screen.getByRole("button", { name: "确认激活配置" }));
    expect(await screen.findByText(/pending_restart：配置已接纳，等待重启/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "进入旧业务（仍需原登录）" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /我确认请求重启/ }));
    await user.click(screen.getByRole("button", { name: "请求重启或查看手工指引" }));
    expect(await screen.findByText(/请手动重启 Test 进程/)).toBeInTheDocument();
    expect(api.writes().filter(c => c.path.endsWith("/restart"))).toHaveLength(1);
  });

  it.each(["/#code=synthetic-root-code", "/setup#code=synthetic-setup-code"])("exchanges %s once even with StrictMode effects", async path => {
    window.history.replaceState({}, "", path);
    const api = wizardApi();
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/learning/subjects"
      ? Promise.resolve(jsonResponse({ subjects: [] })) : api.fetch(input, init));
    render(createElement(StrictMode, null, createElement(App)));
    await screen.findByRole("heading", { name: path.startsWith("/#") ? "我的科目" : "首次启动向导" });
    await waitFor(() => expect(api.calls.filter(c => c.path.endsWith("/session/exchange"))).toHaveLength(1));
    expect(window.location.hash).toBe("");
    expect(api.calls.filter(c => c.path.endsWith("/session/exchange"))).toHaveLength(1);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(api.writes()).toEqual([]);
  });
});
