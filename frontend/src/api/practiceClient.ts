import type { components } from "./generated";
import { learningRequest, LearningApiError } from "./learningClient";
type S=components["schemas"];
export type PracticeConditions=S["PracticeConditions"];
export type PracticePreview=S["PracticePreviewView"];
export type PersonalPractice=S["PersonalPracticeView"];
export type GenerationRequest=S["GenerationRequest"];
export type PracticeAnswer=S["PracticeAnswerView"];
export type PracticeStart=S["PracticeStartRequest"];
export type PreviewChanged=S["PracticePreviewChanged"];
export type PracticeReport=S["PracticeReport"];
export type PracticeItemResult=S["PracticeItemResult"];
export type PracticeAttempt=S["PracticeAttemptView"];
export type PracticeScoringFeedback=S["PracticeScoringFeedback"];
export type ScoreDispute=S["ScoreDisputeView"];
export type PracticeSubmit=S["PracticeSubmitRequest"];
const base="/api/learning";
export const defaultPracticeConditions: PracticeConditions={
  source:"mixed",mode:"balanced",count:20,prefer_unseen:true,allow_practiced:false,
  knowledge_point_ids:[],question_types:["single_choice","multiple_choice","short_answer"],source_document_ids:[],
};
function preview(value: unknown): value is PracticePreview {
  if(typeof value!=="object" || value===null) return false;
  const v=value as Record<string,unknown>;
  return typeof v.id==="string" && typeof v.subject_id==="string" && typeof v.preview_digest==="string"
    && Array.isArray(v.items) && typeof v.eligible_count==="number" && typeof v.unseen_count==="number"
    && typeof v.can_start==="boolean" && typeof v.selected_count==="number";
}
function report(value: unknown): value is PracticeReport {
  if(typeof value!=="object" || value===null) return false;
  const v=value as Record<string,unknown>;
  return typeof v.practice_id==="string" && typeof v.subject_id==="string"
    && ["completed","partial_failed"].includes(String(v.state)) && Array.isArray(v.items)
    && typeof v.completion_rate==="string" && typeof v.unresolved_dispute_count==="number";
}
function dispute(value: unknown): value is ScoreDispute {
  if(typeof value!=="object" || value===null) return false;
  const v=value as Record<string,unknown>;
  return typeof v.id==="string" && typeof v.item_id==="string" && typeof v.answer_id==="string"
    && ["pending","awaiting_acceptance","resolved"].includes(String(v.state))
    && typeof v.reason==="string" && typeof v.disputed_attempt_id==="string";
}
function practice(value: unknown): value is PersonalPractice {
  if(typeof value!=="object" || value===null) return false;
  const v=value as Record<string,unknown>;
  return typeof v.id==="string" && typeof v.subject_id==="string" && typeof v.state==="string" && Array.isArray(v.items);
}
export async function previewPractice(subjectId:string,body:PracticeConditions) {
  const r=await learningRequest(base+"/subjects/"+encodeURIComponent(subjectId)+"/practices/preview","POST",body);
  if(!preview(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function getPracticePreview(id:string) {
  const r=await learningRequest(base+"/practice-previews/"+encodeURIComponent(id),"GET");
  if(!preview(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function startPractice(id:string,body:PracticeStart) {
  const r=await learningRequest(base+"/practice-previews/"+encodeURIComponent(id)+"/start","POST",body);
  if(!practice(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function getPractice(id:string) {
  const r=await learningRequest(base+"/practices/"+encodeURIComponent(id),"GET");
  if(!practice(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function savePracticeAnswer(id:string,itemId:string,body:S["PracticeAnswerInput"]) {
  const r=await learningRequest(base+"/practices/"+encodeURIComponent(id)+"/items/"+encodeURIComponent(itemId)+"/answer","PUT",body);
  return r.value as PracticeAnswer;
}
export async function submitPractice(id:string,blankConfirmed=false) {
  const r=await learningRequest(base+"/practices/"+encodeURIComponent(id)+"/submit","POST",{blank_confirmed:blankConfirmed});
  if(!report(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function getPracticeReport(id:string,signal?:AbortSignal) {
  const r=await learningRequest(base+"/practices/"+encodeURIComponent(id)+"/results","GET",undefined,signal);
  if(!report(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function raiseScoreDispute(practiceId:string,itemId:string,reason:string) {
  const r=await learningRequest(base+"/practices/"+encodeURIComponent(practiceId)+"/disputes","POST",{item_id:itemId,reason});
  if(!dispute(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function regradeScoreDispute(disputeId:string,body:S["ScoreDisputeRegradeRequest"]={}) {
  const r=await learningRequest(base+"/disputes/"+encodeURIComponent(disputeId)+"/regrade","POST",body);
  if(!dispute(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function acceptScoreDispute(disputeId:string,attemptId:string) {
  const r=await learningRequest(base+"/disputes/"+encodeURIComponent(disputeId)+"/accept","POST",{attempt_id:attemptId});
  if(!dispute(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function getScoreDispute(disputeId:string,signal?:AbortSignal) {
  const r=await learningRequest(base+"/disputes/"+encodeURIComponent(disputeId),"GET",undefined,signal);
  if(!dispute(r.value)) throw new LearningApiError("INVALID_RESPONSE",r.status);
  return r.value;
}
export async function previewGeneration(id:string,body:GenerationRequest) {
  const r=await learningRequest(base+"/practice-previews/"+encodeURIComponent(id)+"/generation-preview","POST",body);
  return r.value as S["LearningOperationView"];
}
export async function executeGeneration(id:string,operationId:string) {
  const r=await learningRequest(base+"/practice-previews/"+encodeURIComponent(id)+"/generate/"+encodeURIComponent(operationId),"POST");
  return r.value as PracticePreview | S["LearningOperationView"];
}
export function changedPreview(error:unknown): PreviewChanged | null {
  if(!(error instanceof LearningApiError) || error.code!=="PRACTICE_PREVIEW_CHANGED") return null;
  const d=error.details;
  if(typeof d==="object" && d!==null && "current_preview" in d && preview(d.current_preview)) return d as PreviewChanged;
  return null;
}
