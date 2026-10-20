import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "../../test/fixtures";
import { KnowledgeConfirm } from "./KnowledgeConfirm";

afterEach(() => vi.unstubAllGlobals());

describe("KnowledgeConfirm scope associations", () => {
  it.each([false, true])("preserves an explicit chapter association change (clear=%s)", async clear => {
    const chapter = {
      id: "c-1", title: "Functions / Definition", parent_id: null,
      confirmation_state: "suggested", document_id: "m-1", anchors: ["heading-definition"],
      knowledge_point_ids: clear ? ["k-1"] : [],
    };
    const view = {
      subject_id: "s-1", confirmed: false, chapters: [chapter],
      knowledge_points: [{ id: "k-1", name: "Definition", confirmation_state: "pending" }],
    };
    let captured: { chapters: { knowledge_point_ids: string[] }[] } | undefined;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/knowledge/confirm")) {
        captured = JSON.parse(String(init?.body));
        return jsonResponse({ ...view, confirmed: true });
      }
      return jsonResponse(view);
    }));
    const user = userEvent.setup();
    render(<KnowledgeConfirm subjectId="s-1" />);

    const associations = await screen.findByRole("listbox", { name: "章节 Functions / Definition 的知识点" });
    expect(captured).toBeUndefined();
    if (clear) await user.deselectOptions(associations, "k-1");
    else await user.selectOptions(associations, "k-1");
    await user.click(screen.getByRole("button", { name: "确认修改" }));
    await waitFor(() => expect(captured?.chapters[0]?.knowledge_point_ids).toEqual(clear ? [] : ["k-1"]));
  });
});
