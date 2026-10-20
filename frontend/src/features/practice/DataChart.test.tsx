import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { DataChart } from "./DataChart";
import type { LearningChart } from "../../api/learningClient";

export const chart: LearningChart = { kind: "line", title: "位移随时间变化", x_axis: { label: "时间", unit: "s" }, y_axis: { label: "位移", unit: "m" }, points: [{ x: 0, y: 0 }, { x: 1, y: 2 }], basis: "题设给定两个测量点" };

it("renders controlled SVG and the exact data/axes/units/source without scripts", () => {
  const { container } = render(<DataChart chart={chart} review="consistent" />);
  expect(screen.getByRole("img", { name: chart.title })).toBeVisible();
  expect(screen.getAllByText("时间 (s)").length).toBeGreaterThan(0);
  expect(screen.getAllByText("位移 (m)").length).toBeGreaterThan(0);
  expect(screen.getByText("依据：题设给定两个测量点", { exact: true })).toBeVisible();
  expect(screen.getByRole("table", { name: "图表数据" }).querySelectorAll("tr")).toHaveLength(3);
  expect(container.querySelectorAll("polyline")).toHaveLength(1);
  expect(container.querySelector("script,foreignObject")).toBeNull();
});

it("keeps very large finite coordinates finite and escapes model strings", () => {
  const { container } = render(<DataChart chart={{ ...chart, title: "<script>doNotRun()</script>", points: [{ x: -1e308, y: -1e308 }, { x: 1e308, y: 1e308 }] }} />);
  const polyline = container.querySelector("polyline")!;
  expect(polyline.getAttribute("points")).not.toMatch(/NaN|Infinity/);
  expect(container.querySelector("script")).toBeNull();
});

it("retains contradictory data for inspection and explicitly defers use", () => {
  const { rerender, container } = render(<DataChart chart={chart} review="conflicting" />);
  expect(screen.getByText(/数据与答案依据冲突/)).toBeVisible();
  rerender(<DataChart chart={{ ...chart, kind: "bar" }} />);
  expect(screen.getByText(/图表依据待确认/)).toBeVisible();
  expect(container.querySelectorAll("rect[data-value]")).toHaveLength(2);
});
