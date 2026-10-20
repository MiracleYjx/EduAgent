import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { ProbeView } from "../../api/contracts";
import { draftFixture, PROFILE_ID } from "../onboarding/wizardFixtures";
import { ProbePanel } from "./ProbePanel";

function probe(status: ProbeView["status"], code: string): ProbeView {
  return { probe_id: "synthetic-probe", profile_id: PROFILE_ID, capability: "text", status, result_code: code, message: "检测未完成。", tested_at: "2026-10-18T00:00:00Z", may_have_been_billed: true, external_requests: 1, retryable: true, correlation_id: "synthetic-correlation" };
}
function setup(probes: ProbeView[] = []) {
  const onProbe = vi.fn(), onRead = vi.fn(), onCancel = vi.fn();
  const props = { draft: draftFixture(true), probes, busy: false, onProbe, onRead, onCancel };
  return { ...render(<ProbePanel {...props} />), props, onProbe, onRead, onCancel };
}
it("keyboard confirmation permits one explicit capability call and resets consent", async () => {
  const { onProbe } = setup(); const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("检测模型"), PROFILE_ID);
  await user.selectOptions(screen.getByLabelText("检测能力"), "text");
  expect(screen.getByRole("button", { name: "单独检测所选能力" })).toBeDisabled();
  const confirmation = screen.getByRole("checkbox"); confirmation.focus(); await user.keyboard(" ");
  await user.tab(); await user.keyboard("{Enter}");
  expect(onProbe).toHaveBeenCalledExactlyOnceWith(PROFILE_ID, "text");
  expect(confirmation).not.toBeChecked(); expect(screen.getByRole("button", { name: "单独检测所选能力" })).toBeDisabled();
});
it("refresh and cancel only address the existing running probe; billing remains visible", () => {
  const { onProbe, onRead, onCancel } = setup([probe("running", "RUNNING")]);
  fireEvent.click(screen.getByRole("button", { name: "刷新本次检测" }));
  fireEvent.click(screen.getByRole("button", { name: "取消本次检测" }));
  expect(onRead).toHaveBeenCalledExactlyOnceWith("synthetic-probe"); expect(onCancel).toHaveBeenCalledExactlyOnceWith("synthetic-probe");
  expect(onProbe).not.toHaveBeenCalled(); expect(screen.getByText(/候选有效期.*可能已计费/)).toBeInTheDocument();
});
it.each(["SERVICE_UNREACHABLE", "UNKNOWN_PROVIDER_ERROR", "CANCELLED"])("shows %s and its original safe facts without assuming authentication failure", code => {
  setup([probe(code === "CANCELLED" ? "cancelled" : "failed", code)]);
  expect(screen.getByText(new RegExp(`text.*${code}`))).toBeInTheDocument();
  expect(screen.getByText(/synthetic-correlation/)).toBeInTheDocument();
  expect(screen.queryByText(/KEY_INVALID/)).not.toBeInTheDocument();
  if (code === "UNKNOWN_PROVIDER_ERROR") expect(screen.getByText("原因未确定，不推测为密码错误。")).toBeInTheDocument();
});
it("changing capability discards confirmation instead of carrying it into another call", () => {
  const { onProbe } = setup();
  fireEvent.change(screen.getByLabelText("检测模型"), { target: { value: PROFILE_ID } });
  fireEvent.change(screen.getByLabelText("检测能力"), { target: { value: "text" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("检测能力"), { target: { value: "json_object" } });
  expect(screen.getByRole("checkbox")).not.toBeChecked(); expect(onProbe).not.toHaveBeenCalled();
});
