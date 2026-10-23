import type { components } from "./generated";
import { learningRequest, LearningApiError, isLearningOperation } from "./learningClient";
type S=components["schemas"];
export type Conversation=S["ConversationSummary"];
export type ConversationView=S["ConversationView"];
export type TeacherSend=S["TeacherSend"];
export type TeacherTurn=S["TeacherTurn"];
const base="/api/learning";
const id=encodeURIComponent;
const path=(subject:string,conversation:string,suffix="")=>`${base}/conversations/${id(conversation)}${suffix}?subject_id=${id(subject)}`;
function summary(value:unknown):value is Conversation {
  return typeof value==="object"&&value!==null&&"id" in value&&typeof value.id==="string"
    &&"subject_id" in value&&typeof value.subject_id==="string"&&"title" in value&&typeof value.title==="string";
}
function conversation(value:unknown):value is ConversationView {
  return summary(value)&&"messages" in value&&Array.isArray(value.messages);
}
async function read<T>(url:string, method:"GET"|"POST"|"DELETE", body:unknown, valid:(v:unknown)=>v is T):Promise<T> {
  const {value,status}=await learningRequest(url,method,body);
  if(!valid(value))throw new LearningApiError("INVALID_RESPONSE",status);
  return value;
}
export const listConversations=(subject:string)=>read(`${base}/subjects/${id(subject)}/conversations`,"GET",undefined,
  (v):v is Conversation[]=>Array.isArray(v)&&v.every(row=>summary(row)&&row.subject_id===subject));
export const createConversation=(subject:string)=>read(`${base}/subjects/${id(subject)}/conversations`,"POST",{},summary);
export const getConversation=(subject:string,cid:string)=>read(path(subject,cid),"GET",undefined,
  (v):v is ConversationView=>conversation(v)&&v.subject_id===subject&&v.id===cid);
export const deleteConversation=(subject:string,cid:string)=>read(path(subject,cid),"DELETE",{},summary);
const turn=(v:unknown):v is TeacherTurn=>typeof v==="object"&&v!==null&&"conversation" in v&&conversation(v.conversation)
  &&"operation" in v&&isLearningOperation(v.operation)&&"requires_confirmation" in v&&typeof v.requires_confirmation==="boolean";
export const previewMessage=(subject:string,cid:string,body:TeacherSend)=>read(path(subject,cid,"/messages/preview"),"POST",body,turn);
export const confirmMessage=(subject:string,cid:string,operation:string,preview_digest:string)=>
  read(path(subject,cid,`/messages/${id(operation)}/confirm`),"POST",{preview_digest},isLearningOperation);
export const executeMessage=(subject:string,cid:string,operation:string)=>read(path(subject,cid,`/messages/${id(operation)}/execute`),"POST",{},turn);
export const cancelMessage=(subject:string,cid:string,operation:string)=>read(path(subject,cid,`/messages/${id(operation)}/cancel`),"POST",{},conversation);
