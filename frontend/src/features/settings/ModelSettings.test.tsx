import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ProviderCatalogEntry } from "../../api/contracts";
import { draftFixture, PROFILE_ID } from "../onboarding/wizardFixtures";
import { ModelSettings } from "./ModelSettings";

export const modelCatalog: ProviderCatalogEntry[] = [
  { kind: "llm", adapter_id: "openai_compatible", capabilities: ["text", "json_object"], canonical_id: "openai_compatible", lifecycle: "owned_per_run", parameters: { properties: { temperature: { anyOf: [{ type: "number", minimum: 0, maximum: 2 }, { type: "null" }] } } } },
  { kind: "embedding", adapter_id: "local", capabilities: ["embedding"], canonical_id: "local", lifecycle: "local_until_restart", parameters: {} },
  { kind: "vision", adapter_id: "deepseek", capabilities: ["vision", "text", "json_object"], canonical_id: "deepseek", lifecycle: "owned_per_run", parameters: {} },
  { kind: "rerank", adapter_id: "openai_compatible", capabilities: ["llm_rerank"], canonical_id: "llm", lifecycle: "owned_per_run", parameters: {} },
];
function setup() {
  const onChange = vi.fn();
  const values = draftFixture(true).values;
  render(<ModelSettings values={values} catalog={modelCatalog} credentials={[]} busy={false} onChange={onChange} />);
  return { onChange, values };
}
it("keeps Reviewer rule based, inherited tasks explicit and independent tasks separate", () => {
  setup();
  expect(screen.getByText(/Reviewer.*规则复核.*不适用/)).toBeInTheDocument();
  expect(screen.getByText(/诊断、语义核验、拆题.*继承默认/)).toBeInTheDocument();
  expect(screen.getByLabelText("出题模型")).toHaveValue("inherit");
  const embedding = screen.getByLabelText("Embedding模型");
  expect(embedding).not.toHaveTextContent("继承默认");
  expect(embedding).not.toHaveTextContent("合成文本模型");
  expect(screen.getByLabelText("Vision模型")).not.toHaveTextContent("合成文本模型");
});
it("only exposes registered providers and explains rerank aliases", () => {
  setup();
  expect(screen.getByLabelText("新增适配器")).toHaveTextContent("rerank / openai_compatible（别名：llm）");
  expect(screen.getByLabelText("新增适配器")).not.toHaveTextContent("anthropic");
});
it("changing a profile renews its revision and its existing bindings without activating", () => {
  const { onChange, values } = setup();
  fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "changed-model" } });
  const next = onChange.mock.calls[0][0];
  expect(next.providers[PROFILE_ID].model_id).toBe("changed-model");
  expect(next.providers[PROFILE_ID].profile_revision).not.toBe(values.providers![PROFILE_ID].profile_revision);
  expect(next.bindings.default_llm.profile_revision).toBe(next.providers[PROFILE_ID].profile_revision);
  expect(values.providers![PROFILE_ID].model_id).toBe("synthetic-text");
});
it("renders bounded numeric parameters from Registry", () => {
  setup();
  const parameter = screen.getByLabelText("temperature");
  expect(parameter).toHaveAttribute("min", "0"); expect(parameter).toHaveAttribute("max", "2");
});
it("clears an override into an explicit inherited binding", () => {
  const { onChange } = setup();
  fireEvent.change(screen.getByLabelText("出题模型"), { target: { value: PROFILE_ID } });
  expect(onChange.mock.calls[0][0].bindings.question_generation).toMatchObject({ profile_id: PROFILE_ID, inherit_default: false });
  fireEvent.change(screen.getByLabelText("出题模型"), { target: { value: "inherit" } });
  expect(onChange.mock.calls[1][0].bindings.question_generation).toMatchObject({ profile_id: null, profile_revision: null, inherit_default: true });
});
