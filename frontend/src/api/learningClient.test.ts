import { afterEach, describe, expect, it, vi } from "vitest";
import { createSubject, getLearningOperation, previewLearningOperation } from "./learningClient";

afterEach(() => vi.unstubAllGlobals());
const view = { id: "operation-id", purpose: "generation", preview_digest: "a".repeat(64), runtime_snapshot_id: "snapshot-id", state: "prepared", request_budget: 1, actual_requests: 0, steps: [], estimate_state: "unknown", estimated_cost: null };

describe("personal operation client", () => {
  it("uses generated requests without Setup or business authorization", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(view), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await previewLearningOperation({ purpose: "generation", idempotency_key: "key", scope: { subject_id: "subject" } })).toEqual(view);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("/api/learning/operations/preview");
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
    expect(init.credentials).toBe("omit");
  });
  it("preserves outcome_unknown and never retries provider failures", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...view, state: "outcome_unknown", actual_requests: 1 }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    expect((await getLearningOperation("id/with/slash")).state).toBe("outcome_unknown");
    expect(fetcher.mock.calls[0][0]).toBe("/api/learning/operations/id%2Fwith%2Fslash");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("reports safe API errors without retry", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "PREVIEW_CHANGED" }), { status: 409 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(getLearningOperation("id")).rejects.toMatchObject({ code: "PREVIEW_CHANGED", status: 409 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("rejects malformed success and network failure", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"state":"succeeded"}', { status: 200 })).mockRejectedValueOnce(new TypeError("offline"));
    vi.stubGlobal("fetch", fetcher);
    await expect(getLearningOperation("id")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    await expect(getLearningOperation("id")).rejects.toMatchObject({ code: "NETWORK_ERROR", status: 0 });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("keeps the real HTTP status when a subject creation response is malformed", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"name":"phantom"}', { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(createSubject({ name: "数学" })).rejects.toMatchObject({ code: "INVALID_RESPONSE", status: 201 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
