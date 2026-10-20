import type { components } from "./generated";
import { learningRequest, LearningApiError, type LearningOperation } from "./learningClient";
import type { MaterialView } from "./materialsClient";

type S = components["schemas"];
export type Paper = S["PersonalPaperView"];
export type PaperQuestion = S["PersonalPaperQuestionView"];
export type Question = S["PersonalQuestionView"];
export type PaperEdit = S["PersonalPaperEdit"];
export type Duplicate = S["DuplicateView"];
const p = "/api/learning";
const id = encodeURIComponent;
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
async function json<T>(path: string, method: "GET" | "POST" | "PATCH",
  body: unknown, valid: (value: unknown) => boolean, signal?: AbortSignal): Promise<T> {
  const { value, status } = await learningRequest(path, method, body, signal);
  if (!valid(value)) throw new LearningApiError("INVALID_RESPONSE", status);
  return value as T;
}
const paper = (v: unknown) => object(v) && typeof v.id === "string" && typeof v.subject_id === "string"
  && Array.isArray(v.questions) && Array.isArray(v.assets);
const question = (v: unknown) => object(v) && typeof v.id === "string" && typeof v.revision_id === "string"
  && Array.isArray(v.stem_blocks) && Array.isArray(v.options);
const operation = (v: unknown) => object(v) && typeof v.id === "string" && typeof v.state === "string"
  && typeof v.preview_digest === "string" && typeof v.request_budget === "number";
export const createPaper = (subject: string, document: string) =>
  json<Paper>(p+"/subjects/"+id(subject)+"/imports", "POST", { document_id: document }, paper);
export const getPaper = (identity: string, signal?: AbortSignal) =>
  json<Paper>(p+"/imports/"+id(identity), "GET", undefined, paper, signal);
export const editPaper = (identity: string, questionId: string, body: PaperEdit) =>
  json<Paper>(p+"/imports/"+id(identity)+"/questions/"+id(questionId), "PATCH", body, paper);
export const confirmPaper = (value: Paper, selected: string[]) =>
  json<Paper>(p+"/imports/"+id(value.id)+"/confirm", "POST", {
    question_ids: selected,
    expected_revisions: Object.fromEntries(value.questions.filter(q => selected.includes(q.id)).map(q => [q.id,q.revision])),
  }, paper);
export const previewExtraction = (identity: string, retryOf?:string, acknowledgeUnknown=false) =>
  json<LearningOperation>(p+"/imports/"+id(identity)+"/extraction-preview", "POST",
    { idempotency_key: crypto.randomUUID(), retry_of_id:retryOf??null, acknowledge_unknown:acknowledgeUnknown }, operation);
export const extractPaper = (identity: string, op: string) =>
  json<Paper | LearningOperation>(p+"/imports/"+id(identity)+"/extract/"+id(op), "POST", {}, v => paper(v) || operation(v));
export const getPaperMaterial = (identity: string, signal?: AbortSignal) =>
  json<MaterialView>(p+"/materials/"+id(identity), "GET", undefined,
    v => object(v) && typeof v.id === "string" && Array.isArray(v.blocks), signal);
export const getQuestion = (identity: string, signal?: AbortSignal) =>
  json<Question>(p+"/questions/"+id(identity), "GET", undefined, question, signal);
export const reviseQuestion = (identity: string, body: PaperEdit) =>
  json<Question>(p+"/questions/"+id(identity)+"/revisions", "POST", body, question);
export const questionAssetUrl = (identity: string, asset: string) =>
  p+"/questions/"+id(identity)+"/assets/"+id(asset);
export async function listQuestions(subject: string, filters: Record<string,string>, signal?: AbortSignal): Promise<Question[]> {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([k,v]) => { if (v !== "") params.append(k,v); });
  const result = await json<S["PersonalQuestionList"]>(
    p+"/subjects/"+id(subject)+"/questions?"+params, "GET", undefined,
    v => object(v) && Array.isArray(v.questions) && v.questions.every(question), signal);
  return result.questions;
}
export const listDuplicates = (subject: string, signal?: AbortSignal) =>
  json<Duplicate[]>(p+"/subjects/"+id(subject)+"/duplicate-candidates", "GET", undefined,
    v => Array.isArray(v) && v.every(x => object(x) && typeof x.id === "string" && object(x.revisions)), signal);
export const resolveDuplicate = (value: Duplicate, decision: "same" | "different") =>
  json<Duplicate>(p+"/duplicate-candidates/"+id(value.id)+"/resolve", "POST",
    { decision, expected_revisions: value.revisions }, v => object(v) && typeof v.id === "string");
export const previewAnswer = (value: Question, mode: "complete" | "review") =>
  json<LearningOperation>(p+"/questions/"+id(value.id)+"/answer-preview", "POST",
    { mode, expected_revision: value.revision, idempotency_key: crypto.randomUUID() }, operation);
export const completeAnswer = (identity: string, op: string) =>
  json<Question | LearningOperation>(p+"/questions/"+id(identity)+"/answer/"+id(op), "POST", {},
    v => question(v) || operation(v));

export const processLocalOCR = (identity:string) => json<Paper>(p+"/imports/"+id(identity)+"/local-ocr", "POST", {}, paper);
