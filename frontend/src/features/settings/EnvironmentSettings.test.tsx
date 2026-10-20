import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { settingsViewFixture } from "../../test/fixtures";
import { draftFixture } from "../onboarding/wizardFixtures";
import { EnvironmentSettings } from "./EnvironmentSettings";

it("displays saved, effective and pending values from separate server fields", () => {
  const values = draftFixture().values;
  const settings = settingsViewFixture({ values, effective_values: { ...values, business: { ...values.business!, jwt_expire_minutes: 95 } }, pending_values: { ...values, business: { ...values.business!, jwt_expire_minutes: 120 } }, sources: { "business.jwt_expire_minutes": { source: "process_env", effective: true } } });
  render(<EnvironmentSettings values={values} settings={settings} credentials={[]} busy={false} onChange={vi.fn()} />);
  expect(screen.getByText("process_env")).toBeInTheDocument(); expect(screen.getByText("60")).toBeInTheDocument(); expect(screen.getByText("95")).toBeInTheDocument(); expect(screen.getByText("120")).toBeInTheDocument();
});
it("unavailable effective values stay unavailable and edits affect only the candidate", () => {
  const values = draftFixture().values, changed = vi.fn();
  const settings = settingsViewFixture({ values, effective_values: null, effective_errors: [{ segment: "business", fields: ["business.storage_root"], code: "INVALID_CONFIGURATION" }], sources: { "business.storage_root": { source: "process_env", effective: true } } });
  render(<EnvironmentSettings values={values} settings={settings} credentials={[]} busy={false} onChange={changed} />);
  expect(screen.getByText("不可用")).toBeInTheDocument(); expect(screen.getByRole("alert")).toHaveTextContent("未回落保存值");
  fireEvent.change(screen.getByLabelText("存储目录（绝对路径）"), { target: { value: "C:/synthetic-new" } });
  expect(changed.mock.calls[0][0].business.storage_root).toBe("C:/synthetic-new");
  expect(values.business!.storage_root).toBe("C:/synthetic-test/storage");
});
