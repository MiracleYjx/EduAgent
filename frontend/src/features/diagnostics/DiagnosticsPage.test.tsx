import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../../app/App";
import {
  errorFixture,
  exchangeFixture,
  jsonResponse,
  readinessFixture,
} from "../../test/fixtures";

type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;
}

function renderAuthorizedDiagnostics(readiness: FetchHandler) {
  window.history.replaceState({}, "", "/diagnostics#code=synthetic-code");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.endsWith("/session/exchange")) {
        return jsonResponse(exchangeFixture());
      }
      if (url.endsWith("/readiness")) {
        return readiness(input, init);
      }
      throw new Error(`unexpected request: ${url}`);
    }),
  );
  render(<App />);
}

beforeEach(() => {
  window.history.replaceState({}, "", "/diagnostics");
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/diagnostics");
});

describe("DiagnosticsPage", () => {
  it("renders setup, business and AI facts without upgrading them", async () => {
    renderAuthorizedDiagnostics(async () =>
      jsonResponse(
        readinessFixture({
          ai: {
            evaluated: false,
            status: "not_evaluated",
            reason: "not_evaluated",
            tasks: {},
          },
        }),
      ),
    );

    expect(await screen.findByText("业务前置检查")).toBeInTheDocument();
    expect(screen.getByText("未评估")).toBeInTheDocument();
    expect(screen.getByText(/不会把未评估显示为可用/)).toBeInTheDocument();
  });

  it("handles an expired session from readiness", async () => {
    renderAuthorizedDiagnostics(async () =>
      jsonResponse(
        errorFixture("SESSION_EXPIRED", "管理会话已过期，请重新授权。", {
          stage: "settings_authorization",
          retryable: true,
        }),
        401,
      ),
    );

    expect(
      await screen.findByText(/管理会话已过期/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("需要重新授权").length).toBeGreaterThan(0);
  });

  it("shows a source-check failure for 403", async () => {
    renderAuthorizedDiagnostics(async () =>
      jsonResponse(
        errorFixture("ORIGIN_NOT_ALLOWED", "请求来源不是本机设置服务。", {
          stage: "settings_authorization",
        }),
        403,
      ),
    );

    expect(await screen.findByText("来源校验失败")).toBeInTheDocument();
    expect(screen.getByText(/ORIGIN_NOT_ALLOWED/)).toBeInTheDocument();
  });

  it("shows a service warning for 503", async () => {
    renderAuthorizedDiagnostics(async () =>
      jsonResponse(
        errorFixture(
          "DEPENDENCY_UNAVAILABLE",
          "目标依赖不可用，无法完成该设置操作。",
          { stage: "diagnostics", retryable: true },
        ),
        503,
      ),
    );

    expect(
      await screen.findByText("设置依赖暂不可用"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/DEPENDENCY_UNAVAILABLE/),
    ).toBeInTheDocument();
  });

  it("shows a network error without inventing readiness state", async () => {
    renderAuthorizedDiagnostics(async () => {
      throw new TypeError("fetch failed");
    });

    expect(await screen.findByText("诊断读取失败")).toBeInTheDocument();
    expect(screen.getByText(/NETWORK_ERROR/)).toBeInTheDocument();
    expect(screen.queryByText("业务前置检查")).not.toBeInTheDocument();
  });
});

it("shows per-task unknown facts without promoting them to ready or invalid credentials", async () => {
  renderAuthorizedDiagnostics(async () => jsonResponse(readinessFixture({ ai: {
    evaluated: true, status: "partial", reason: "some_tasks_unready", tasks: {
      question_generation: { configured: true, capability: ["text", "json_object"], credential: "available", activation_validation: "unverified", last_probe: { status: "unknown", tested_at: null }, last_runtime_result: "UNKNOWN_PROVIDER_ERROR", ready: false, reason: "unknown", dependencies: [] },
    },
  } })));
  expect(await screen.findByText("question_generation")).toBeInTheDocument();
  expect(screen.getByText("状态：not_ready · unknown")).toBeInTheDocument();
  expect(screen.getByText(/最近检测：unknown.*无检测时间/)).toBeInTheDocument();
  expect(screen.queryByText(/KEY_INVALID/)).not.toBeInTheDocument();
});
it("exports only the server blob and releases the temporary download URL", async () => {
  window.history.replaceState(null, "", "/diagnostics#code=synthetic");
  const requests: string[] = []; const create = vi.fn(() => "blob:synthetic"), revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input); requests.push(path);
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path.endsWith("/export")) return jsonResponse({ versions: { environment: "test" } });
    return jsonResponse(readinessFixture());
  }));
  render(<App />); await screen.findByText("业务前置检查");
  fireEvent.click(screen.getByRole("button", { name: "下载服务端脱敏诊断" }));
  await waitFor(() => expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:synthetic"));
  expect(create).toHaveBeenCalledTimes(1); expect(click).toHaveBeenCalledTimes(1);
  expect(requests.filter(path => path.endsWith("/export"))).toHaveLength(1);
  expect(requests.some(path => path.endsWith("/probes"))).toBe(false); click.mockRestore();
});
it("refreshes existing facts without creating a model probe", async () => {
  const read = vi.fn(async () => jsonResponse(readinessFixture()));
  renderAuthorizedDiagnostics(read); await screen.findByText("业务前置检查");
  fireEvent.click(screen.getByRole("button", { name: "刷新就绪事实" }));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
});
