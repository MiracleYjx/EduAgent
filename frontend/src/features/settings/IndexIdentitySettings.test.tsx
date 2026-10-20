import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SettingsSessionProvider, useSettingsSession } from "../../auth/SettingsSession";
import { errorFixture, exchangeFixture, jsonResponse } from "../../test/fixtures";
import { IndexIdentitySettings } from "./IndexIdentitySettings";

const kb = "11111111-1111-4111-8111-111111111111";
const legacy = { kb_id: kb, status: "legacy_unknown", model_id: null, dimension: 1024, stored_dimension: 1024, identity_revision: 0, adoption: null, audits: [], verified: false };
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/settings"); });
function Gate() { const session = useSettingsSession(); return session.status === "unauthorized" ? <p>需要重新授权</p> : <IndexIdentitySettings />; }
async function setup(failure?: "preview" | "confirm" | "expired") {
  window.history.replaceState(null, "", "/settings#code=synthetic"); let adopted = false;
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), body = JSON.parse(String(init?.body ?? "{}")); calls.push({ path, body });
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path.endsWith("/index-identities")) return jsonResponse({ identities: [legacy] });
    if (path.endsWith("/adoption-preview")) return failure === "preview" ? jsonResponse(errorFixture("EVIDENCE_INSUFFICIENT", "证据不足。"), 422) : failure === "expired" ? jsonResponse(errorFixture("SESSION_EXPIRED", "会话失效。"), 401) : jsonResponse({ preview_id: "preview", environment_id: "test", kb_id: kb, expected_identity_revision: 0, previous_status: "legacy_unknown", previous_identity: null, proposed_identity: body.proposed_identity, accepted_identity: body.proposed_identity, verified_facts: { stored_dimension: 1024 }, evidence: body.evidence, limitations: ["model_version_unknown"], operator_attestation: body.operator_attestation, expires_at: "2099-01-01T00:00:00Z", requires_confirmation: true, modifies_vectors: false });
    if (path.endsWith("/adoption-confirm")) { if (failure === "confirm") return jsonResponse(errorFixture("IDENTITY_CONFLICT", "身份已变化。"), 409); adopted = true; return jsonResponse({ ...legacy, status: "legacy_adopted" }); }
    if (path.endsWith(kb)) return jsonResponse(adopted ? { ...legacy, status: "legacy_adopted", model_id: "historical-model", identity_revision: 1, adoption: { status: "legacy_adopted" }, audits: [{ action: "adopt" }], verified: true } : legacy);
    throw new Error("Unexpected request");
  }));
  render(<SettingsSessionProvider><Gate /></SettingsSessionProvider>);
  await waitFor(() => { fireEvent.click(screen.getByRole("button", { name: "读取知识库索引身份" })); expect(calls.some(item => item.path.endsWith("/index-identities"))).toBe(true); });
  await screen.findByRole("option", { name: `${kb} · legacy_unknown` });
  fireEvent.change(screen.getByLabelText("知识库身份"), { target: { value: kb } }); await screen.findByLabelText("历史模型名称");
  return calls;
}
function fill() {
  for (const [label, value] of [["历史 Provider", "historical-provider"], ["历史适配器", "openai_compatible"], ["历史模型名称", "historical-model"], ["证据中的向量维度", "1024"], ["历史语义参数（JSON 对象）", "{}"], ["记录定位 1", "record-id"], ["记录日期（YYYY-MM-DD） 1", "2025-01-01"], ["脱敏摘要（无密钥、无业务正文） 1", "synthetic historical record"], ["证据支持的字段（JSON 对象，手工核对） 1", '{"provider":"historical-provider","adapter":"openai_compatible","model_id":"historical-model","dimension":1024,"semantic_parameters":{}}'], ["已核实的历史范围", "synthetic legacy KB"]]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByText("我已依据上述记录核实历史身份与知识库范围")); fireEvent.click(screen.getByRole("button", { name: "预览历史身份接纳" }));
}
it("leaves historical identity empty until manual evidence, then confirms and reads audit", async () => {
  const calls = await setup(); expect(screen.getByLabelText("历史模型名称")).toHaveValue(""); expect(screen.getByLabelText("证据中的向量维度")).toHaveValue(null);
  expect(calls.some(item => item.path.endsWith("/providers"))).toBe(false); fill(); await screen.findByText("预览尚未接纳");
  expect(screen.getByText(/model_version_unknown/)).toBeInTheDocument(); expect(screen.getByRole("button", { name: "确认接纳历史身份" })).toBeDisabled();
  fireEvent.click(screen.getByText("已核对预览限制，确认仅登记身份与审计")); fireEvent.click(screen.getByRole("button", { name: "确认接纳历史身份" }));
  expect(await screen.findByText("已读回接纳记录")).toBeInTheDocument(); expect(screen.getByText(/"action": "adopt"/)).toBeInTheDocument();
  expect(calls.filter(item => item.path.endsWith("/adoption-confirm"))).toHaveLength(1); expect(calls.filter(item => item.path.endsWith(kb))).toHaveLength(2);
});
it.each(["preview", "confirm", "expired"] as const)("a %s failure cannot replay stale confirmation or replace historical values", async failure => {
  const calls = await setup(failure); fill();
  if (failure === "confirm") { await screen.findByText("预览尚未接纳"); fireEvent.click(screen.getByText("已核对预览限制，确认仅登记身份与审计")); fireEvent.click(screen.getByRole("button", { name: "确认接纳历史身份" })); }
  await screen.findByText(failure === "expired" ? "需要重新授权" : failure === "preview" ? /EVIDENCE_INSUFFICIENT/ : /IDENTITY_CONFLICT/);
  expect(screen.queryByRole("button", { name: "确认接纳历史身份" })).not.toBeInTheDocument(); expect(screen.queryByText("已读回接纳记录")).not.toBeInTheDocument();
  expect(calls.filter(item => item.path.endsWith("/adoption-confirm"))).toHaveLength(failure === "confirm" ? 1 : 0);
});
