import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QuestionEditor } from "./QuestionEditor";
import { OperationConfirmation } from "./OperationConfirmation";
import type { PaperEdit } from "../../api/papersClient";

const initial:PaperEdit={
  expected_revision:3, question_type:"multiple_choice",
  stem_blocks:[{type:"text",text:"Read the figure."}],
  options:[{id:"A",blocks:[{type:"image",asset_id:"one",alt:"Original option"}]},
    {id:"B",blocks:[{type:"text",text:"Second"}]}],
  source_locations:[{document_id:"doc",anchor:"object-1"}],
  reference_answer:null,score:"8",uncertainties:["Image needs checking"],
};
describe("personal question correction",()=>{
  it("changes option image ownership only on explicit save and keeps missing answer",()=>{
    const save=vi.fn();
    render(<QuestionEditor initial={initial} assets={[{id:"one",label:"First object"},{id:"two",label:"Second object"}]}
      knowledge={[]} mediaUrl={a=>"/synthetic/"+a} busy={false} onSave={save} onCancel={()=>{}} />);
    fireEvent.change(screen.getByLabelText("选项 1图像 1"),{target:{value:"two"}});
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button",{name:"保存本题校正"}));
    expect(save).toHaveBeenCalledOnce();
    const body=save.mock.calls[0][0] as PaperEdit;
    expect(body.options?.[0].blocks[0]).toMatchObject({type:"image",asset_id:"two"});
    expect(body.reference_answer).toBeNull();
    expect(body.expected_revision).toBe(3);
    expect(body.score).toBe("8");
    expect(body.uncertainties).toEqual(["Image needs checking"]);
  });
  it("requires a real selected source and prevents writes while busy",()=>{
    const save=vi.fn();
    const {rerender}=render(<QuestionEditor initial={{...initial,source_locations:[]}}
      assets={[]} knowledge={[]} mediaUrl={()=>""} busy={false} onSave={save} onCancel={()=>{}} />);
    expect(screen.getByRole("button",{name:"保存本题校正"})).toBeDisabled();
    rerender(<QuestionEditor initial={initial} assets={[]} knowledge={[]} mediaUrl={()=>""}
      busy={true} onSave={save} onCancel={()=>{}} />);
    expect(screen.getByLabelText("题目难度")).toBeDisabled();
    expect(save).not.toHaveBeenCalled();
  });
  it("shows the actual operation scope/model and does not execute on mount",()=>{
    const confirm=vi.fn();
    render(<OperationConfirmation busy={false} onConfirm={confirm} value={{
      id:"op",purpose:"paper_extraction",preview_digest:"a".repeat(64),runtime_snapshot_id:"snapshot",
      state:"prepared",request_budget:1,actual_requests:0,estimate_state:"unknown",
      steps:[{task_kind:"paper_extraction",model:"synthetic-model",request_budget:1}],
    }} />);
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByText(/synthetic-model/)).toBeVisible();
    expect(screen.getByText(/估算未知/)).toBeVisible();
    fireEvent.click(screen.getByRole("button",{name:"确认本次范围并执行"}));
    expect(confirm).toHaveBeenCalledOnce();
  });
});
