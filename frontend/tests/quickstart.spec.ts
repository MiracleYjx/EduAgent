// T093 synthetic browser-DOM flows for the three-screen QuickStart.
import { createElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import type { SetupDataState } from "../src/api/contracts";
import { errorFixture, jsonResponse } from "../src/test/fixtures";
import { draftFixture, wizardApi } from "../src/features/onboarding/wizardFixtures";

beforeEach(() =>
  window.history.replaceState({}, "", "/setup/quickstart#code=synthetic-code"),
);
afterEach(() => vi.unstubAllGlobals());

const existingState: SetupDataState = {
  status: "existing",
  can_initialize: false,
  reason: "EXISTING_DATA_DETECTED",
  database: { status: "existing", table_count: 3, business_tables: ["subjects"] },
  storage: { status: "existing", entry_count: 2 },
  checked_at: "2026-10-18T02:00:00Z",
};

const needsConfirmationState: SetupDataState = {
  status: "needs_confirmation",
  can_initialize: false,
  reason: "SCHEMA_WITHOUT_ROWS",
  database: {
    status: "needs_confirmation",
    table_count: 4,
    business_tables: ["subjects"],
    code: "SCHEMA_WITHOUT_ROWS",
  },
  storage: { status: "empty", entry_count: 0 },
  checked_at: "2026-10-18T02:00:00Z",
};

const unknownState: SetupDataState = {
  status: "unknown",
  can_initialize: false,
  reason: "DATABASE_NOT_CONFIGURED",
  database: {
    status: "unavailable",
    code: "DATABASE_NOT_CONFIGURED",
    business_tables: [],
  },
  storage: { status: "unavailable", code: "STORAGE_NOT_CONFIGURED" },
  checked_at: "2026-10-18T02:00:00Z",
};

async function openQuickStart(options: Parameters<typeof wizardApi>[0] = {}) {
  const api = wizardApi(options);
  render(createElement(App));
  await screen.findByRole("heading", { name: "开始使用 EduAgent" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /开始使用/ }));
  await screen.findByRole("heading", { name: "准备学习空间" });
  return { api, user };
}

describe("T093 three-screen QuickStart", () => {
  it("prepares an empty learning space with two clicks and no paid or Probe calls", async () => {
    const { api, user } = await openQuickStart({
      businessReady: true,
      restartRequested: true,
    });

    expect(screen.getByText("将创建全新学习空间")).toBeInTheDocument();
    expect(screen.getByText("学习资料保存位置")).toBeInTheDocument();
    expect(screen.getByText("C:/synthetic-test/storage")).toBeInTheDocument();
    expect(screen.queryByLabelText("数据库主机")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "创建并进入" }));

    expect(
      await screen.findByRole("heading", { name: "学习空间已准备好" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/"), { timeout: 3000 });

    const writes = api.writes();
    expect(writes.map(call => `${call.method} ${call.path}`)).toEqual([
      "POST /api/settings/drafts",
      "POST /api/settings/drafts/33333333-3333-4333-8333-333333333333/validate",
      "POST /api/settings/initialization/preview",
      "POST /api/settings/initialization/apply",
      "POST /api/settings/drafts/33333333-3333-4333-8333-333333333333/activate",
      "POST /api/settings/restart",
    ]);
    expect(writes.every(call => call.body.acknowledge_override !== true)).toBe(true);
    expect(writes.filter(call => call.path.endsWith("/probes"))).toHaveLength(0);
    expect(writes.filter(call => call.path.endsWith("/credentials"))).toHaveLength(0);
    expect(api.calls.filter(call => call.path === "/ready").length).toBeGreaterThan(0);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("enters an existing ready learning space without any setup write", async () => {
    const { api, user } = await openQuickStart({
      dataState: existingState,
      businessReady: true,
      savedConfiguration: true,
    });

    expect(await screen.findByText("检测到已有学习空间，将直接进入")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并进入" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "进入学习" }));

    expect(
      await screen.findByRole("heading", { name: "学习空间已准备好" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/"), { timeout: 3000 });
    expect(api.writes()).toEqual([]);
  });

  it("does not initialize existing data when business is not ready", async () => {
    const { api, user } = await openQuickStart({ dataState: existingState });

    expect(await screen.findByText("检测到已有学习空间，将直接进入")).toBeInTheDocument();
    expect(screen.getByText("已有学习空间，但业务还没有就绪")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并进入" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "进入学习" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "去高级设置" }));
    await waitFor(() => expect(window.location.pathname).toBe("/setup/advanced"));
    expect(api.writes()).toEqual([]);
  });

  it("sends needs_confirmation only to advanced setup", async () => {
    const { api, user } = await openQuickStart({ dataState: needsConfirmationState });

    expect(await screen.findByText("检测到部分已有数据，需要确认")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并进入" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "去高级设置" }));
    await waitFor(() => expect(window.location.pathname).toBe("/setup/advanced"));
    expect(api.writes()).toEqual([]);
  });

  it("falls back to advanced setup when the recommendation cannot be auto-applied", async () => {
    const { api, user } = await openQuickStart({
      recommendation: {
        available: false,
        auto_apply: false,
        mode: "python",
        business: { dev_mode: false },
        sources: {},
        reason: "DATABASE_NOT_CONFIGURED",
        warnings: [],
      },
      dataState: unknownState,
    });

    expect(await screen.findByText("没有检测到可自动应用的完整推荐配置")).toBeInTheDocument();
    expect(screen.getByText("暂时无法确认数据状态")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并进入" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "去高级设置" }));
    await waitFor(() => expect(window.location.pathname).toBe("/setup/advanced"));
    expect(api.writes()).toEqual([]);
  });

  it("does not overwrite an already saved draft", async () => {
    const { api } = await openQuickStart({ draft: draftFixture(), businessReady: false });

    expect(await screen.findByText("检测到已暂存的设置")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并进入" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "高级设置" })).toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });

  it("stops on initialization failure and retries only after an explicit click", async () => {
    const { api, user } = await openQuickStart({
      initializationFailure: true,
      restartRequested: true,
    });

    await user.click(screen.getByRole("button", { name: "创建并进入" }));
    expect(await screen.findByText("本次准备没有完成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "高级设置" })).toBeInTheDocument();
    expect(api.writes().filter(call => call.path.endsWith("/initialization/apply"))).toHaveLength(1);

    await new Promise(resolve => window.setTimeout(resolve, 400));
    expect(api.writes().filter(call => call.path.endsWith("/initialization/apply"))).toHaveLength(1);
    expect(api.writes().filter(call => call.path.endsWith("/activate"))).toHaveLength(0);
    expect(api.writes().filter(call => call.path.endsWith("/restart"))).toHaveLength(0);
  });

  it("does not bypass an override conflict or request a restart", async () => {
    const { api, user } = await openQuickStart({
      activationConflict: true,
      restartRequested: true,
    });

    await user.click(screen.getByRole("button", { name: "创建并进入" }));
    expect(await screen.findByText(/没有替你绕过确认/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeInTheDocument();
    expect(api.writes().filter(call => call.path.endsWith("/activate"))).toHaveLength(1);
    expect(api.writes().filter(call => call.path.endsWith("/restart"))).toHaveLength(0);
    expect(api.writes().every(call => call.body.acknowledge_override !== true)).toBe(true);
  });

  it("surfaces the real draft-stage override conflict without acknowledging it", async () => {
    const { api, user } = await openQuickStart({
      draftOverrideConflict: true,
      restartRequested: true,
    });

    await user.click(screen.getByRole("button", { name: "创建并进入" }));
    expect(await screen.findByText(/没有替你绕过确认/)).toBeInTheDocument();
    expect(api.writes().filter(call => call.path.endsWith("/drafts"))).toHaveLength(1);
    expect(api.writes().filter(call => call.path.endsWith("/validate"))).toHaveLength(0);
    expect(api.writes().filter(call => call.path.endsWith("/activate"))).toHaveLength(0);
    expect(api.writes().every(call => call.body.acknowledge_override !== true)).toBe(true);
  });

  it("reports an unavailable controlled restart without sending a second request", async () => {
    const { api, user } = await openQuickStart({ restartRequested: false });

    await user.click(screen.getByRole("button", { name: "创建并进入" }));
    expect(await screen.findByText(/当前启动方式不能自动完成重启/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();
    expect(api.writes().filter(call => call.path.endsWith("/restart"))).toHaveLength(1);
  });

  it("expires the session when the three-screen write returns 401", async () => {
    const { api, user } = await openQuickStart({ authorizationFailure: 401 });

    await user.click(screen.getByRole("button", { name: "创建并进入" }));
    expect((await screen.findAllByText("需要重新授权")).length).toBeGreaterThan(0);
    expect(api.writes().filter(call => call.path.endsWith("/drafts"))).toHaveLength(1);
    expect(api.writes().filter(call => call.path.endsWith("/validate"))).toHaveLength(0);
  });

  it("does not render a writable QuickStart from a 401 read", async () => {
    const api = wizardApi();
    const fallback = api.fetch;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/onboarding/recommendation")
        ? Promise.resolve(jsonResponse(errorFixture("SESSION_EXPIRED", "授权已过期。"), 401))
        : fallback(input, init),
    );
    render(createElement(App));
    expect((await screen.findAllByText("需要重新授权")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "开始使用" })).not.toBeInTheDocument();
    expect(api.writes()).toEqual([]);
  });
});
