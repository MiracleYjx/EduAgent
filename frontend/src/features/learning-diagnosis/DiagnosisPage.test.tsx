import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { DiagnosisPage } from "./DiagnosisPage";
import * as client from "../../api/diagnosisClient";
vi.mock("../../api/diagnosisClient",()=>({getDiagnosis:vi.fn()}));
vi.mock("../teacher/TeacherPanel",()=>({TeacherPanel:()=>null}));
const stats={practice_count:3,valid_count:1,excluded_count:1,unanswered_count:1,exclusion_counts:{failed:1},
  first:{count:1,score_rate:"0",choice_count:1,choice_correct_rate:"0"},repractice:{count:0,score_rate:null,choice_count:0,choice_correct_rate:null},
  recent:{count:1,score_rate:"0",choice_count:1,choice_correct_rate:"0"},previous:{count:0,score_rate:null,choice_count:0,choice_correct_rate:null},
  status:"insufficient_evidence",repeated_wrong_question_ids:[]} as const;
const value={subject_id:"subject",source_kind:null,answer_origin:null,statistics:stats,evidence:[],knowledge_points:[{
  id:"point",name:"Algebra",statistics:stats,suggestion:{knowledge_point_id:"point",message:"没有已确认关联的学习资料",sources:[],
    practice_conditions:{mode:"weak"}}}]} as unknown as client.PersonalDiagnosis;
beforeEach(()=>{vi.clearAllMocks();vi.mocked(client.getDiagnosis).mockResolvedValue(value);});
const page=()=>render(<MemoryRouter><ConfigProvider theme={{token:{motion:false}}}><DiagnosisPage subjectId="subject" /></ConfigProvider></MemoryRouter>);

it("shows valid zero, unknown evidence and excluded/unanswered independently without paid actions",async()=>{
  page();expect(await screen.findByText("Algebra · 关联题表现")).toBeVisible();
  expect(screen.getAllByText(/练习 3 次；有效已答 1；问题排除 1；未答 1/)).toHaveLength(2);
  expect(screen.getAllByText("0.00%").length).toBeGreaterThan(0);
  expect(screen.getAllByText("无有效样本").length).toBeGreaterThan(0);
  expect(screen.getByText(/没有已确认关联/)).toBeVisible();
  expect(screen.queryByRole("button",{name:/执行|确认费用/})).toBeNull();
});
it("opens a modifiable normal practice entry, without starting a practice",async()=>{
  page();const link=await screen.findByRole("link",{name:"调整并预览针对性练习"});
  expect(link).toHaveAttribute("href","/subjects/subject/practice?knowledge_point=point&mode=weak");
  expect(client.getDiagnosis).toHaveBeenCalledTimes(1);
});
it("requeries with explicit source filters",async()=>{
  page();await screen.findByText("Algebra · 关联题表现");
  fireEvent.change(screen.getByLabelText("诊断题目来源"),{target:{value:"ai"}});
  await waitFor(()=>expect(client.getDiagnosis).toHaveBeenLastCalledWith("subject",{source_kind:"ai",answer_origin:""},expect.any(AbortSignal)));
});
it("shows failed read instead of manufacturing empty history",async()=>{
  vi.mocked(client.getDiagnosis).mockRejectedValue(new Error("409 conflict"));page();
  expect(await screen.findByText("409 conflict")).toBeVisible();
  expect(screen.queryByText("暂无已提交作答记录。")).toBeNull();
});
