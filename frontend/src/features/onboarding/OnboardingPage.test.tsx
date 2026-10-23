import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../../app/App";
import {
  errorFixture,
  exchangeFixture,
  jsonResponse,
  readinessFixture,
  setupReadyFixture,
  settingsViewFixture,
} from "../../test/fixtures";

type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

function stubFetch(handler: FetchHandler) {
  const mocked = vi.fn(handler);
  vi.stubGlobal("fetch", mocked);
  return mocked;
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;
}

beforeEach(() => {
  window.history.replaceState({}, "", "/setup/advanced");
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/setup");
});

describe("OnboardingPage", () => {
  it("renders setup status and requests reauthorization without a token", async () => {
    stubFetch(async (input) => {
      const url = urlOf(input);
      if (url === "/setup-ready") {
        return jsonResponse(setupReadyFixture());
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<App />);

    expect(await screen.findByText("Setup 服务可用")).toBeInTheDocument();
    expect(screen.getAllByText("需要重新授权").length).toBeGreaterThan(0);
    expect(screen.getByText(/授权后可恢复已保存草稿与向导进度/)).toBeInTheDocument();
  });

  it("clears #code before exchange and keeps the token out of storage", async () => {
    window.history.replaceState({}, "", "/setup/advanced#code=synthetic-one-time-code");
    let hashAtExchange = "not-called";
    const fetchMock = stubFetch(async (input, init) => {
      const url = urlOf(input);
      if (url === "/setup-ready") {
        return jsonResponse(setupReadyFixture());
      }
      if (url.endsWith("/session/exchange")) {
        hashAtExchange = window.location.hash;
        expect(init?.body).toBe(
          JSON.stringify({ one_time_code: "synthetic-one-time-code" }),
        );
        return jsonResponse(exchangeFixture());
      }
      if (url.endsWith("/onboarding")) return jsonResponse({
        progress: { revision: 0, current_step: 0, draft_revision: null, completed_steps: [] },
        draft: null, probes: [], initialization: null, readiness: readinessFixture(),
      });
      if (url === "/api/settings/") return jsonResponse(settingsViewFixture());
      if (url.endsWith("/providers")) return jsonResponse({ providers: [] });
      if (url.endsWith("/readiness")) {
        const headers = new Headers(init?.headers);
        expect(headers.get("Authorization")).toBe(
          "Setup synthetic-management-token",
        );
        return jsonResponse(
          readinessFixture({
            business: {
              ready: true,
              assembly: "mounted",
              recovery: "completed",
              reason: null,
            },
            ai: {
              evaluated: true,
              status: "ready",
              reason: "已激活绑定可用",
              tasks: {},
            },
          }),
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<App />);

    expect(await screen.findByText("业务就绪")).toBeInTheDocument();
    expect(screen.getByText("已激活绑定可用")).toBeInTheDocument();
    expect(hashAtExchange).toBe("");
    expect(window.location.hash).toBe("");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(fetchMock).toHaveBeenCalled();
  });

  it("shows a safe error when the one-time code is expired", async () => {
    window.history.replaceState({}, "", "/setup/advanced#code=expired-code");
    stubFetch(async (input) => {
      const url = urlOf(input);
      if (url === "/setup-ready") {
        return jsonResponse(setupReadyFixture());
      }
      if (url.endsWith("/session/exchange")) {
        return jsonResponse(
          errorFixture("CODE_EXPIRED", "授权码已过期，请重新授权。", {
            stage: "settings_session",
            retryable: true,
          }),
          401,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<App />);

    expect(await screen.findByText(/授权码已过期/)).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("navigates to diagnostics through the client router", async () => {
    stubFetch(async (input) => {
      const url = urlOf(input);
      if (url === "/setup-ready") {
        return jsonResponse(setupReadyFixture());
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Setup 服务可用");
    await user.click(screen.getByText("基础诊断"));

    await waitFor(() => {
      expect(window.location.pathname).toBe("/diagnostics");
    });
    expect(
      screen.getByRole("heading", { name: "基础诊断" }),
    ).toBeInTheDocument();
  });
});
