import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { PracticeBuilder } from "./PracticeBuilder";
import * as client from "../../api/practiceClient";
import * as learning from "../../api/learningClient";
vi.mock("../../api/practiceClient",async()=>({...await vi.importActual("../../api/practiceClient"),
  getPracticePreview:vi.fn(),previewPractice:vi.fn(),startPractice:vi.fn(),previewGeneration:vi.fn(),executeGeneration:vi.fn(),
  getPractice:vi.fn(),savePracticeAnswer:vi.fn(),submitPractice:vi.fn(),
  previewGrading:vi.fn(),executeGrading:vi.fn(),
}));
vi.mock("../../api/learningClient",async()=>({...await vi.importActual("../../api/learningClient"),
  getLearningOperation:vi.fn(),confirmLearningOperation:vi.fn(),
}));
vi.mock("../../api/materialsClient",()=>({
  getKnowledge:vi.fn(async()=>({subject_id:"subject",confirmed:true,chapters:[],knowledge_points:[{id:"kp",name:"algebra",confirmation_state:"confirmed"}]})),
  listMaterials:vi.fn(async()=>[]),listSeries:vi.fn(async()=>[]),
}));
const fixture={
  id:"preview",subject_id:"subject",conditions:{...client.defaultPracticeConditions},preview_digest:"a".repeat(64),
  eligible_count:8,uploaded_count:8,ai_count:0,unseen_count:3,practiced_count:5,excluded_count:2,
  candidate_shortage:12,unseen_shortage:17,selected_count:3,coverage:{kp:3},weak_knowledge_point_ids:[],
  other_count:3,original_wrong_count:0,can_start:false,items:[],
};

it("prefills diagnosis scope but allows edits before an explicit preview",async()=>{
  render(<MemoryRouter initialEntries={["/subjects/subject/practice?knowledge_point=kp&mode=weak"]}><PracticeBuilder subjectId="subject" /></MemoryRouter>);
  await screen.findByRole("option",{name:"algebra"});
  expect(screen.getByLabelText("练习模式")).toHaveValue("weak");
  expect(screen.getByLabelText("练习知识点")).toHaveValue(["kp"]);
  expect(client.previewPractice).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("练习模式"),{target:{value:"random"}});
  expect(screen.getByLabelText("练习模式")).toHaveValue("random");
  expect(client.startPractice).not.toHaveBeenCalled();
});
beforeEach(()=>{vi.clearAllMocks();vi.mocked(client.previewPractice).mockResolvedValue(fixture);vi.mocked(client.getPracticePreview).mockResolvedValue(fixture);});
function mount(){render(<MemoryRouter><PracticeBuilder subjectId="subject" /></MemoryRouter>);}

const shortContent = {
  question_id:"q-1",revision_id:"r-1",question_type:"short_answer" as const,phase:"answering" as const,
  stem_blocks:[{type:"text" as const,text:"请说明变量作用域。"}],options:[],explanation_blocks:null,
  automatic_grading_ready:true,issues:[],asset_sources:{},
};
const practiceFixture = {
  id:"practice-1",subject_id:"subject",state:"in_progress",
  items:[{
    id:"item-1",position:1,canonical_id:"canonical-1",score:"10.00",knowledge_point_ids:[],
    source_kind:"uploaded" as const,grounding:"general_knowledge" as const,content:shortContent,
    answer:{item_id:"item-1",answer_state:"unanswered" as const,choice_ids:null,text_answer:null},
  }],
};

const feePreview:learning.LearningOperation={id:"real-operation",purpose:"practice_grading:practice-1",
  runtime_snapshot_id:"snapshot",preview_digest:"a".repeat(64),state:"prepared",request_budget:1,
  actual_requests:0,steps:[{task_kind:"subjective_grading",model:"frozen-model"}],estimate_state:"unknown"};

it("previews and confirms grading without sending until explicit execution",async()=>{
  vi.mocked(client.getPractice).mockResolvedValue(practiceFixture as client.PersonalPractice);
  vi.mocked(client.savePracticeAnswer).mockResolvedValue(practiceFixture.items[0].answer);
  vi.mocked(client.submitPractice).mockRejectedValue(new learning.LearningApiError("GRADING_OPERATION_REQUIRED",409));
  vi.mocked(client.previewGrading).mockResolvedValue(feePreview);
  vi.mocked(learning.confirmLearningOperation).mockResolvedValue({...feePreview,state:"confirmed"});
  vi.mocked(client.executeGrading).mockRejectedValue(new learning.LearningApiError("NETWORK_ERROR",0));
  vi.mocked(learning.getLearningOperation).mockResolvedValue({...feePreview,state:"outcome_unknown",actual_requests:1});
  render(<ConfigProvider theme={{token:{motion:false}}}><MemoryRouter initialEntries={["/subjects/subject/practice?practice=practice-1"]}><PracticeBuilder subjectId="subject" /></MemoryRouter></ConfigProvider>);
  const text=await screen.findByRole("textbox",{name:"文字作答"});
  await waitFor(()=>expect(text).toBeEnabled());
  fireEvent.change(text,{target:{value:"my answer"}});
  expect(text).toHaveValue("my answer");
  fireEvent.click(screen.getByRole("button",{name:"整组提交并评分"}));
  expect(await screen.findByText("已生成预览，尚未确认费用")).toBeVisible();
  expect(client.executeGrading).not.toHaveBeenCalled();
  expect(learning.confirmLearningOperation).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"取消费用确认"}));
  expect(client.executeGrading).not.toHaveBeenCalled();
  const submit=await screen.findByRole("button",{name:"整组提交并评分"});
  await waitFor(()=>expect(submit).toBeEnabled());
  fireEvent.click(submit);
  fireEvent.click(await screen.findByRole("button",{name:"确认本次费用与范围"}));
  expect(await screen.findByText("已确认，待执行")).toBeVisible();
  expect(client.executeGrading).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"执行已确认评分"}));
  await waitFor(()=>expect(client.executeGrading).toHaveBeenCalledWith("practice-1","real-operation"));
  expect(await screen.findByText(/服务端结果尚未核实/)).toBeVisible();
  expect(screen.getByRole("button",{name:/执行已确认评分/})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"读取操作状态"}));
  expect(await screen.findByText("结果未知，需要人工核对")).toBeVisible();
  expect(client.executeGrading).toHaveBeenCalledTimes(1);
});

it("restores confirmed operation with reads only and exposes confirmation conflicts",async()=>{
  vi.mocked(client.getPractice).mockResolvedValue({...practiceFixture,grading_operation_id:feePreview.id} as client.PersonalPractice);
  vi.mocked(learning.getLearningOperation).mockResolvedValue(feePreview);
  vi.mocked(learning.confirmLearningOperation).mockRejectedValue(new learning.LearningApiError("PREVIEW_CHANGED",409));
  render(<MemoryRouter initialEntries={["/subjects/subject/practice?practice=practice-1"]}><PracticeBuilder subjectId="subject" /></MemoryRouter>);
  expect(await screen.findByText("已生成预览，尚未确认费用")).toBeVisible();
  expect(client.previewGrading).not.toHaveBeenCalled();expect(client.submitPractice).not.toHaveBeenCalled();
  expect(client.executeGrading).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"确认本次费用与范围"}));
  expect(await screen.findByText(/PREVIEW_CHANGED/)).toBeVisible();
  expect(client.executeGrading).not.toHaveBeenCalled();
  expect(screen.queryByText("执行成功，结果已保存")).toBeNull();
});

it("submits the whole group only after explicit blank confirmation",async()=>{
  vi.mocked(client.getPractice).mockResolvedValue(practiceFixture as client.PersonalPractice);
  vi.mocked(client.savePracticeAnswer).mockResolvedValue(practiceFixture.items[0].answer);
  vi.mocked(client.submitPractice).mockResolvedValue({practice_id:"practice-1"} as client.PracticeReport);
  render(<ConfigProvider theme={{token:{motion:false}}}><MemoryRouter initialEntries={["/subjects/subject/practice?practice=practice-1"]}><PracticeBuilder subjectId="subject" /></MemoryRouter></ConfigProvider>);
  expect(await screen.findByRole("button",{name:"整组提交并评分"})).toBeEnabled();
  expect(client.submitPractice).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"整组提交并评分"}));
  const confirm=await screen.findByRole("button",{name:"仍然提交"});
  expect(confirm).toBeInTheDocument();
  expect(client.submitPractice).not.toHaveBeenCalled();
  fireEvent.click(confirm);
  await waitFor(()=>expect(client.savePracticeAnswer).toHaveBeenCalledWith("practice-1","item-1",{text_answer:""}));
  await waitFor(()=>expect(client.submitPractice).toHaveBeenCalledWith("practice-1",true));
});
it("shows 20/8/3 and does not generate or start automatically",async()=>{
  mount();fireEvent.click(screen.getByRole("button",{name:"预览候选与覆盖"}));
  expect(await screen.findByText(/候选题不足 12/)).toBeVisible();
  expect(screen.getByRole("button",{name:"确认题量、分值并开始练习"})).toBeDisabled();
  expect(client.previewGeneration).not.toHaveBeenCalled();expect(client.startPractice).not.toHaveBeenCalled();
  expect(client.previewPractice).toHaveBeenCalledWith("subject",expect.objectContaining({
    source:"mixed",mode:"balanced",count:20,prefer_unseen:true,allow_practiced:false,
  }));
});
it("reduces only after an explicit action and keeps selected conditions",async()=>{
  mount();fireEvent.click(screen.getByRole("button",{name:"预览候选与覆盖"}));
  fireEvent.click(await screen.findByRole("button",{name:"减少到 3 题"}));
  await waitFor(()=>expect(client.previewPractice).toHaveBeenLastCalledWith("subject",
    expect.objectContaining({count:3,source:"mixed",mode:"balanced",allow_practiced:false})));
});
it("requires explicit repractice and an explicit grounding before generation preview",async()=>{
  mount();fireEvent.click(screen.getByRole("button",{name:"预览候选与覆盖"}));
  fireEvent.click(await screen.findByRole("button",{name:"允许重练并重新预览"}));
  await waitFor(()=>expect(client.previewPractice).toHaveBeenLastCalledWith("subject",expect.objectContaining({allow_practiced:true})));
  expect(screen.getByRole("button",{name:"请求 AI 补充并预览费用"})).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox",{name:"AI 出题依据"}),{target:{value:"general_knowledge"}});
  fireEvent.click(screen.getByRole("button",{name:"请求 AI 补充并预览费用"}));
  await waitFor(()=>expect(client.previewGeneration).toHaveBeenCalledWith("preview",expect.objectContaining({
    grounding:"general_knowledge",material_version_ids:[],
  })));
  expect(client.executeGeneration).not.toHaveBeenCalled();
});

it("does not restore a just-created preview over newer filter edits",async()=>{
  mount();fireEvent.click(screen.getByRole("button",{name:/预览候选与覆盖$/}));
  await screen.findByText(/候选题不足 12/);
  await waitFor(()=>expect(screen.getByRole("button",{name:/预览候选与覆盖$/})).toBeEnabled());
  fireEvent.change(screen.getByRole("combobox",{name:"练习模式"}),{target:{value:"weak"}});
  fireEvent.change(screen.getByRole("combobox",{name:"出题来源"}),{target:{value:"uploaded"}});
  fireEvent.change(screen.getByRole("textbox",{name:"练习难度"}),{target:{value:"基础"}});
  expect(client.getPracticePreview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:/预览候选与覆盖$/}));
  await waitFor(()=>expect(client.previewPractice).toHaveBeenLastCalledWith("subject",expect.objectContaining({
    mode:"weak",source:"uploaded",difficulty:"基础",
  })));
});
