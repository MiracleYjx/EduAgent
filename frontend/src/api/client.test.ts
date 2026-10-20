import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getReadiness,
  createProbe,
  getSetupReady,
  createDraft,
  cancelProbe,
  SettingsApiError,
} from "./client";
import {
  errorFixture,
  jsonResponse,
  readinessFixture,
  setupReadyFixture,
} from "../test/fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("settings API client", () => {
  it("sends explicit JSON writes and never retries a lost response", async () => {
    const fetchMock = vi.fn(async () => { throw new TypeError("synthetic lost response"); });
    vi.stubGlobal("fetch", fetchMock);
    await expect(createDraft("synthetic-token", { base_revision: null, changes: {}, acknowledge_override: false })).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(call[1].headers);
    expect(call[1].method).toBe("POST");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Authorization")).toBe("Setup synthetic-token");
    expect(call[1].cache).toBe("no-store");
    expect(call[1].credentials).toBe("omit");
    expect(call[1].redirect).toBe("error");
  });

  it("cancels with the existing authenticated JSON DELETE contract", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ status: "cancelled", result_code: "CANCELLED" }));
    vi.stubGlobal("fetch", fetchMock);
    await cancelProbe("synthetic-token", "synthetic-probe");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("DELETE");
    expect(init.body).toBe("{}");
    expect(new Headers(init.headers).get("Authorization")).toBe("Setup synthetic-token");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("uses the Setup token and non-persistent no-store request options", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(readinessFixture()),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getReadiness("synthetic-token");

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Setup synthetic-token");
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
  });

  it("accepts a 503 setup-ready body as a real not-ready state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          setupReadyFixture({
            status: "not_ready",
            setup: {
              configuration: "invalid",
              settings_authorization: "available",
              static_resources: "not_bundled",
            },
            failed_check: "config",
            detail: "配置不可读取。",
          }),
          503,
        ),
      ),
    );

    const body = await getSetupReady();

    expect(body.status).toBe("not_ready");
    expect(body.failed_check).toBe("config");
    expect(body.setup.static_resources).toBe("not_bundled");
  });

  it("preserves safe error envelopes and status codes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          errorFixture("DEPENDENCY_UNAVAILABLE", "依赖不可用。", {
            retryable: true,
          }),
          503,
        ),
      ),
    );

    await expect(getReadiness("synthetic-token")).rejects.toMatchObject({
      name: "SettingsApiError",
      status: 503,
      code: "DEPENDENCY_UNAVAILABLE",
      retryable: true,
    });
  });

  it("maps fetch failures to a retryable network error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("connection refused");
      }),
    );

    await expect(getReadiness("synthetic-token")).rejects.toEqual(
      expect.objectContaining<Partial<SettingsApiError>>({
        status: 0,
        code: "NETWORK_ERROR",
        retryable: true,
      }),
    );
  });
});

it("propagates read cancellation without converting it into a network failure", async () => {
  const controller = new AbortController();
  const aborted = new DOMException("Aborted", "AbortError");
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.signal).toBe(controller.signal);
    throw aborted;
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(getReadiness("synthetic-token", controller.signal)).rejects.toBe(aborted);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it.each([[422, "BILLABLE_CONFIRMATION_REQUIRED"], [409, "REVISION_CONFLICT"]])(
  "does not retry or reinterpret HTTP %s", async (status, code) => {
    const fetchMock = vi.fn(async () => jsonResponse(errorFixture(code, "请求未执行。"), status));
    vi.stubGlobal("fetch", fetchMock);
    await expect(createProbe("synthetic-token", { draft_revision: "candidate", profile_id: "profile", capability: "chat", allow_billable: false, timeout_seconds: 15 }))
      .rejects.toMatchObject({ status, code });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  },
);
