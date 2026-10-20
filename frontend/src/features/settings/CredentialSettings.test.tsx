import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SettingsSessionProvider } from "../../auth/SettingsSession";
import type { CredentialStatusView } from "../../api/contracts";
import { exchangeFixture, errorFixture, jsonResponse } from "../../test/fixtures";
import { CredentialSettings } from "./CredentialSettings";

const credential: CredentialStatusView = { credential_ref: "11111111-1111-4111-8111-111111111111", version: "22222222-2222-4222-8222-222222222222", environment_id: "33333333-3333-4333-8333-333333333333", backend: "dpapi", purpose: "provider", status: "available" };
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/settings"); });
function setup(options: { status?: CredentialStatusView["status"]; external?: boolean; failure?: string; empty?: boolean } = {}) {
  window.history.replaceState(null, "", "/settings#code=synthetic");
  const changed = vi.fn(); const requests: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("exchange")) return jsonResponse(exchangeFixture());
    requests.push(init!);
    if (options.failure) return jsonResponse(errorFixture(options.failure, "操作未完成。"), options.failure === "IN_USE" ? 409 : 503);
    return init?.method === "DELETE" ? new Response(null, { status: 204 }) : jsonResponse({ ...credential, version: "44444444-4444-4444-8444-444444444444" }, 201);
  }));
  const props = { credentials: options.empty ? [] : [{ ...credential, status: options.status ?? "available", backend: options.external ? "mounted_secret" as const : "dpapi" as const }], externalOnly: options.external ?? false, onChanged: changed };
  const view = render(<SettingsSessionProvider><CredentialSettings {...props} /></SettingsSessionProvider>);
  return { ...view, requests, changed };
}
it.each([["invalid", "无效"], ["unavailable", "不可用（尚未核实或无法读取）"], ["reentry_required", "需重新录入（凭据不可解密或已丢失）"]] as const)("shows %s without inventing a successful credential", async (status, label) => {
  setup({ status }); expect(await screen.findByText(label)).toBeInTheDocument();
});
it("shows missing credentials explicitly", () => { setup({ empty: true }); expect(screen.getByText("尚无已登记凭据")).toBeInTheDocument(); });
it("clears the secret before a failed replacement and keeps the old reference", async () => {
  const storage = vi.spyOn(Storage.prototype, "setItem");
  const { changed, requests } = setup({ failure: "CREDENTIAL_BACKEND_UNAVAILABLE" });
  fireEvent.change(screen.getByLabelText("操作目标"), { target: { value: `${credential.credential_ref}:${credential.version}` } });
  fireEvent.change(screen.getByLabelText("新凭据原文"), { target: { value: "synthetic-secret-only-in-memory" } });
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "保存新的候选版本" })); expect(requests).toHaveLength(1); });
  expect(screen.getByLabelText("新凭据原文")).toHaveValue("");
  expect(await screen.findByText(/CREDENTIAL_BACKEND_UNAVAILABLE/)).toBeInTheDocument();
  expect(screen.getByText(credential.version)).toBeInTheDocument(); expect(changed).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled();
});
it("IN_USE deletion preserves the original version and never retries", async () => {
  const { requests, changed } = setup({ failure: "IN_USE" });
  fireEvent.change(screen.getByLabelText("操作目标"), { target: { value: `${credential.credential_ref}:${credential.version}` } });
  expect(screen.getByRole("button", { name: "删除所选版本" })).toBeDisabled();
  fireEvent.click(screen.getByText("确认删除所选凭据版本"));
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "删除所选版本" })); expect(requests).toHaveLength(1); });
  expect(await screen.findByText("IN_USE：操作未完成。")).toBeInTheDocument(); expect(changed).not.toHaveBeenCalled(); expect(screen.getByText(credential.version)).toBeInTheDocument();
});
it("a successful replacement adds a candidate without rewriting existing bindings", async () => {
  const { changed, requests } = setup();
  fireEvent.change(screen.getByLabelText("操作目标"), { target: { value: `${credential.credential_ref}:${credential.version}` } });
  fireEvent.change(screen.getByLabelText("新凭据原文"), { target: { value: "synthetic-secret" } });
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "保存新的候选版本" })); expect(requests).toHaveLength(1); });
  await waitFor(() => expect(changed).toHaveBeenCalled()); expect(changed.mock.calls[0][0]).toHaveLength(2);
  expect(changed.mock.calls[0][0][0]).toMatchObject(credential);
  expect(requests[0].method).toBe("POST"); expect(screen.getByLabelText("新凭据原文")).toHaveValue("");
});
it("external Secret mode exposes status and host guidance without write controls", () => {
  setup({ external: true }); expect(screen.getByText(/由宿主挂载或进程环境维护/)).toBeInTheDocument();
  expect(screen.queryByLabelText("新凭据原文")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /保存|删除/ })).not.toBeInTheDocument();
});
it("unmounting discards typed secrets and a fresh visit starts blank", () => {
  const first = setup(); fireEvent.change(screen.getByLabelText("新凭据原文"), { target: { value: "synthetic-secret" } }); first.unmount();
  setup(); expect(screen.getByLabelText("新凭据原文")).toHaveValue("");
});
