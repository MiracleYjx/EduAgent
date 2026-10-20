import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../../app/App";
import { errorFixture, exchangeFixture, jsonResponse } from "../../test/fixtures";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });
function setup(ready: boolean, expired = false) {
  window.history.replaceState(null, "", "/business#code=synthetic-nav-once");
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("exchange")) return jsonResponse(exchangeFixture());
    if (path === "/api/learning/subjects") return ready ? jsonResponse({ subjects: [] }) : jsonResponse(errorFixture("LEARNING_DATABASE_NOT_INITIALIZED", "需要初始化"), 503);
    if (path.startsWith("/api/settings/")) return expired ? jsonResponse(errorFixture("UNAUTHORIZED", "会话过期"), 401) : jsonResponse({});
    throw new Error("Unexpected endpoint");
  });
  vi.stubGlobal("fetch", fetcher); render(<App />); return fetcher;
}
it("redirects the former entry to personal subjects without sharing management secrets", async () => {
  const fetcher = setup(true);
  expect(await screen.findByText("暂无科目")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /登录/ })).toBeNull();
  expect(window.location.pathname).toBe("/");
  expect(window.location.hash).toBe("");
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  expect(fetcher.mock.calls.every(([path]) => !String(path).includes("/gradio"))).toBe(true);
});
it("reports real dependency failure and refreshes readonly subject facts", async () => {
  const fetcher = setup(false);
  expect(await screen.findByText(/LEARNING_DATABASE_NOT_INITIALIZED/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "进入原业务登录页" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "刷新科目" }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => String(path) === "/api/learning/subjects")).toHaveLength(2));
  expect(screen.queryByText("暂无科目")).toBeNull();
});
it("expires management authorization without treating it as business login", async () => {
  setup(true, true);
  await screen.findByText("暂无科目");
  fireEvent.click(screen.getByRole("menuitem", { name: "设置概览" }));
  expect(await screen.findByText("UNAUTHORIZED：会话过期")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "进入原业务登录页" })).toBeNull();
});
