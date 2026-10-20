import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SettingsSessionProvider } from "../../auth/SettingsSession";
import { errorFixture, exchangeFixture, jsonResponse, settingsViewFixture } from "../../test/fixtures";
import { SettingsPage } from "./SettingsPage";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/settings"); });
function page() { return render(<SettingsSessionProvider><SettingsPage /></SettingsSessionProvider>); }
function authorize() { window.history.replaceState(null, "", "/settings#code=synthetic-once"); }

it("does not request protected settings without authorization", () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); page();
  expect(fetchMock).not.toHaveBeenCalled();
});
it("reads current facts without persisting the management token", async () => {
  authorize();
  const storage = vi.spyOn(Storage.prototype, "setItem");
  vi.stubGlobal("fetch", vi.fn(async (path: RequestInfo | URL) => jsonResponse(
    String(path).endsWith("exchange") ? exchangeFixture() : settingsViewFixture(),
  )));
  page();
  expect(await screen.findByText("synthetic-active-revision")).toBeInTheDocument();
  expect(storage).not.toHaveBeenCalled(); expect(window.location.hash).toBe("");
});
it.each([401, 403])("revokes the session on HTTP %s", async (status) => {
  authorize();
  vi.stubGlobal("fetch", vi.fn(async (path: RequestInfo | URL) => String(path).endsWith("exchange")
    ? jsonResponse(exchangeFixture()) : jsonResponse(errorFixture("DENIED", "授权失效。"), status)));
  page();
  await waitFor(() => expect(screen.getByText("需要重新授权")).toBeInTheDocument());
  expect(screen.queryByText("synthetic-active-revision")).not.toBeInTheDocument();
});
it.each([409, 503])("retains the safe HTTP %s failure instead of stale success", async (status) => {
  authorize();
  vi.stubGlobal("fetch", vi.fn(async (path: RequestInfo | URL) => String(path).endsWith("exchange")
    ? jsonResponse(exchangeFixture()) : jsonResponse(errorFixture("SAFE_FAILURE", "操作未完成。"), status)));
  page();
  expect(await screen.findByText("SAFE_FAILURE：操作未完成。")).toBeInTheDocument();
});
it("aborts the settings read when leaving and ignores a late response", async () => {
  authorize(); let signal: AbortSignal | null | undefined;
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
    if (String(path).endsWith("exchange")) return jsonResponse(exchangeFixture());
    signal = init?.signal;
    return await new Promise<Response>((done) => { resolve = done; });
  }));
  const view = page(); await waitFor(() => expect(signal).toBeDefined());
  view.unmount(); expect(signal?.aborted).toBe(true);
  await act(async () => { resolve(jsonResponse(settingsViewFixture())); });
  expect(screen.queryByText("synthetic-active-revision")).not.toBeInTheDocument();
});
