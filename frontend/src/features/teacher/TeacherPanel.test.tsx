import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { TeacherPanel } from "./TeacherPanel";
import * as client from "../../api/teacherClient";
import * as learning from "../../api/learningClient";
vi.mock("../../api/materialsClient",()=>({listSeries:vi.fn(async()=>[]),getMaterial:vi.fn()}));
vi.mock("../../api/learningClient",async()=>({...await vi.importActual("../../api/learningClient"),getLearningOperation:vi.fn()}));
vi.mock("../../api/teacherClient",()=>({listConversations:vi.fn(),createConversation:vi.fn(),getConversation:vi.fn(),
  deleteConversation:vi.fn(),previewMessage:vi.fn(),confirmMessage:vi.fn(),executeMessage:vi.fn(),cancelMessage:vi.fn()}));
beforeEach(()=>{vi.clearAllMocks();vi.mocked(client.listConversations).mockResolvedValue([]);});

it("loading a subject does not send, preview or confirm a paid message",async()=>{
  render(<MemoryRouter><TeacherPanel subjectId="subject" /></MemoryRouter>);
  expect(await screen.findByText("暂无科目对话。")).toBeVisible();
  expect(client.previewMessage).not.toHaveBeenCalled();expect(client.executeMessage).not.toHaveBeenCalled();
});

it("shows network failure without inventing a successful conversation",async()=>{
  vi.mocked(client.listConversations).mockRejectedValue(new Error("409 conflict"));
  render(<MemoryRouter><TeacherPanel subjectId="subject" /></MemoryRouter>);
  expect(await screen.findByText("409 conflict")).toBeVisible();
  expect(client.createConversation).not.toHaveBeenCalled();
});

const prepared={id:"operation",purpose:"teacher:chat",preview_digest:"a".repeat(64),runtime_snapshot_id:"snapshot",
  state:"prepared",request_budget:1,actual_requests:0,estimate_state:"unknown",steps:[{step_id:"answer",model:"fake-model",scope_summary:"1次请求；费用未知"}]} as learning.LearningOperation;
const chat={id:"chat",subject_id:"subject",title:"测试对话",created_at:"2026-01-01T00:00:00Z",pending_operation_id:null,messages:[]} as client.ConversationView;
function active(){
  vi.mocked(client.getConversation).mockResolvedValue(chat);
  vi.mocked(client.previewMessage).mockResolvedValue({conversation:{...chat,pending_operation_id:prepared.id},operation:prepared,requires_confirmation:true});
  vi.mocked(client.confirmMessage).mockResolvedValue({...prepared,state:"confirmed"});
  vi.mocked(client.executeMessage).mockResolvedValue({conversation:{...chat,messages:[]},operation:{...prepared,state:"succeeded",actual_requests:1},requires_confirmation:false});
  return render(<MemoryRouter initialEntries={["/?conversation=chat"]}><ConfigProvider theme={{token:{motion:false}}}><TeacherPanel subjectId="subject" /></ConfigProvider></MemoryRouter>);
}
it("requires explicit first fee confirmation and explicit execution",async()=>{
  active();fireEvent.change(await screen.findByRole("textbox",{name:"教师消息"}),{target:{value:"explain"}});
  fireEvent.click(screen.getByRole("button",{name:"发送（收费）"}));
  const confirm=await screen.findByRole("button",{name:"确认本会话当前费用条件"});
  expect(client.executeMessage).not.toHaveBeenCalled();
  fireEvent.click(confirm);
  await waitFor(()=>expect(client.confirmMessage).toHaveBeenCalledWith("subject","chat","operation","a".repeat(64)));
  expect(client.executeMessage).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button",{name:"执行本条已确认消息"}));
  expect(await screen.findByText("执行成功")).toBeVisible();
  expect(client.executeMessage).toHaveBeenCalledTimes(1);
});
it("cancelled previews and follow-up suggestions never call the model",async()=>{
  active();fireEvent.click(await screen.findByRole("button",{name:"请换一个例子"}));
  expect(client.previewMessage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"发送（收费）"}));
  vi.mocked(client.cancelMessage).mockResolvedValue(chat);
  fireEvent.click(await screen.findByRole("button",{name:"取消本条预览"}));
  await waitFor(()=>expect(client.cancelMessage).toHaveBeenCalledWith("subject","chat","operation"));
  expect(client.executeMessage).not.toHaveBeenCalled();
});
it("restores unknown status by GET only and blocks another charged send",async()=>{
  vi.mocked(client.getConversation).mockResolvedValue({...chat,pending_operation_id:prepared.id});
  vi.mocked(learning.getLearningOperation).mockResolvedValue({...prepared,state:"outcome_unknown",actual_requests:1});
  render(<MemoryRouter initialEntries={["/?conversation=chat"]}><TeacherPanel subjectId="subject" /></MemoryRouter>);
  expect(await screen.findByText("结果未知；可能已产生费用，需要人工核对")).toBeVisible();
  expect(screen.getByRole("button",{name:"发送（收费）"})).toBeDisabled();
  expect(client.confirmMessage).not.toHaveBeenCalled();expect(client.executeMessage).not.toHaveBeenCalled();
});
it("renders returned citations and keeps the source reference actionable",async()=>{
  vi.mocked(client.getConversation).mockResolvedValue({...chat,messages:[{
    id:"assistant-message",role:"assistant",text:"斜率表示每单位 x 变化时 y 的变化量。",state:"succeeded",
    operation_id:"operation",reply:{answer_body:"同上",basis:"uploaded_material",citations:["document"],uncertainties:[],practice_knowledge_point_ids:[]},
    citations:[{id:"document",title:"一次函数讲义",document_id:"document",location:{anchor:"前言"}}],
    error_code:null,runtime_snapshot_id:"snapshot",created_at:"2026-01-01T00:00:00Z"
  }]} as client.ConversationView);
  render(<MemoryRouter initialEntries={["/?conversation=chat"]}><TeacherPanel subjectId="subject" /></MemoryRouter>);
  expect(await screen.findByText(/一次函数讲义/)).toBeVisible();
  expect(screen.getByRole("button",{name:"打开教师引用原文"})).toBeVisible();
});

it("keeps a 409 conflict visible without claiming success",async()=>{
  active();vi.mocked(client.confirmMessage).mockRejectedValue(new Error("409 PREVIEW_CHANGED"));
  fireEvent.change(await screen.findByRole("textbox",{name:"教师消息"}),{target:{value:"explain"}});
  fireEvent.click(screen.getByRole("button",{name:"发送（收费）"}));
  fireEvent.click(await screen.findByRole("button",{name:"确认本会话当前费用条件"}));
  expect(await screen.findByText("409 PREVIEW_CHANGED")).toBeVisible();
  expect(client.executeMessage).not.toHaveBeenCalled();
  expect(screen.getByRole("button",{name:"确认本会话当前费用条件"})).toBeDisabled();
});
