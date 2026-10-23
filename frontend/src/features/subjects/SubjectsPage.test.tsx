import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../../app/App";
import { jsonResponse } from "../../test/fixtures";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

it("opens the personal home, creates and reopens a subject without authorization", async () => {
  const rows: { id: string; name: string; description: string | null }[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    expect(new Headers(init?.headers).has("Authorization")).toBe(false);
    if (path === "/api/learning/subjects" && init?.method === "POST") {
      const row = { id: "subject-a", ...JSON.parse(String(init.body)) };
      rows.push(row); return jsonResponse(row, 201);
    }
    if (path === "/api/learning/subjects") return jsonResponse({ subjects: rows });
    if (path === "/api/learning/subjects/subject-a") return jsonResponse(rows[0]);
    throw new Error("Unexpected endpoint " + path);
  });
  vi.stubGlobal("fetch", fetcher);
  const view = render(<App />);
  expect(await screen.findByText("暂无科目")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "创建科目" }));
  fireEvent.change(screen.getByLabelText("科目名称"), { target: { value: "数学" } });
  fireEvent.click(screen.getByRole("button", { name: "保存科目" }));
  fireEvent.click(await screen.findByRole("link", { name: "数学" }));
  expect(await screen.findByRole("heading", { name: "数学", level: 2 })).toBeInTheDocument();
  for (const name of ["资料", "题库", "练习", "学习诊断", "AI 教师"])
    expect(screen.getByRole("link", { name })).toHaveAttribute("href", expect.stringContaining("/subjects/subject-a/"));
  expect(screen.queryByRole("link", { name: /登录|Gradio/ })).toBeNull();
  view.unmount(); render(<App />);
  expect(await screen.findByRole("heading", { name: "数学", level: 2 })).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([p]) => String(p).includes("/api/settings"))).toBe(false);
});

it("shows missing configuration as a failure with settings navigation", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ code: "LEARNING_CONFIGURATION_UNAVAILABLE" }, 503)));
  render(<App />);
  expect(await screen.findByText(/LEARNING_CONFIGURATION_UNAVAILABLE/)).toBeInTheDocument();
  expect(screen.queryByText("暂无科目")).toBeNull();
  expect(screen.getByRole("link", { name: "打开设置中心" })).toHaveAttribute("href", "/settings");
});

it("preserves server errors and does not create a phantom subject", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST" ? jsonResponse({ code: "LEARNING_DATABASE_UNAVAILABLE" }, 503) : jsonResponse({ subjects: [] }));
  vi.stubGlobal("fetch", fetcher); render(<App />);
  await screen.findByText("暂无科目");
  fireEvent.click(screen.getByRole("button", { name: "创建科目" }));
  fireEvent.change(screen.getByLabelText("科目名称"), { target: { value: "物理" } });
  fireEvent.click(screen.getByRole("button", { name: "保存科目" }));
  expect(await screen.findByText(/LEARNING_DATABASE_UNAVAILABLE/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "物理" })).toBeNull();
  await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1));
});

it("edits the persisted subject and opens the implemented teacher without paid sends", async () => {
  window.history.replaceState(null, "", "/subjects/subject-a");
  let row = { id: "subject-a", name: "数学", description: "原说明" as string | null };
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/conversations")) return jsonResponse([]);
    if (String(input).endsWith("/material-series")) return jsonResponse({series:[]});
    if (init?.method === "PATCH") row = { ...row, ...JSON.parse(String(init.body)) };
    return jsonResponse(row);
  });
  vi.stubGlobal("fetch",fetcher);
  render(<App />);
  await screen.findByRole("heading", { name: "数学", level: 2 });
  fireEvent.click(screen.getByRole("button", { name: "修改科目" }));
  fireEvent.change(screen.getByLabelText("科目名称"), { target: { value: "高等数学" } });
  fireEvent.change(screen.getByLabelText("科目说明（可选）"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "保存科目" }));
  expect(await screen.findByRole("heading", { name: "高等数学", level: 2 })).toBeInTheDocument();
  expect(row.description).toBeNull();
  fireEvent.click(screen.getByRole("link", { name: "AI 教师" }));
  expect(await screen.findByRole("heading",{name:"科目 AI 教师"})).toBeInTheDocument();
  expect(await screen.findByText("暂无科目对话。")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(fetcher.mock.calls.some(([,init])=>init?.method==="POST")).toBe(false);
});
