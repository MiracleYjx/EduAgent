import type { components, paths } from "./generated";

type Schema = components["schemas"];
export type LearningOperation = Schema["LearningOperationView"];
export type LearningPreview = paths["/api/learning/operations/preview"]["post"]["requestBody"]["content"]["application/json"];
export type LearningConfirmation = Schema["LearningConfirmRequest"];
export type Subject = Schema["SubjectView"];
export type SubjectCreate = Schema["SubjectCreate"];
export type SubjectPatch = Schema["SubjectPatch"];
export type QuestionContentView = Schema["QuestionContentView"];
export type ContentBlock = QuestionContentView["stem_blocks"][number];
export type MathNode = Schema["MathNode-Output"];
export type LearningChart = Schema["LearningChart"];
const prefix = "/api/learning/operations";

export class LearningApiError extends Error {
  constructor(readonly code: string, readonly status: number, readonly details?: unknown) {
    super(code === "GRADING_OPERATION_REQUIRED"
      ? "评分需要先预览并明确确认本次费用与请求范围，本次未发送模型请求。"
      : `学习操作未完成（${code}）。`);
    this.name = "LearningApiError";
  }
}

export function isLearningOperation(value: unknown): value is LearningOperation {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.id === "string" && typeof v.preview_digest === "string"
    && typeof v.runtime_snapshot_id === "string" && typeof v.purpose === "string"
    && typeof v.state === "string" && ["prepared", "confirmed", "running", "succeeded", "failed", "partial", "outcome_unknown"].includes(v.state)
    && typeof v.request_budget === "number" && Number.isInteger(v.request_budget) && v.request_budget >= 0
    && typeof v.actual_requests === "number" && Number.isInteger(v.actual_requests)
    && v.actual_requests >= 0 && v.actual_requests <= v.request_budget && Array.isArray(v.steps);
}

export async function learningRequest(path: string, method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE", body?: unknown, signal?: AbortSignal): Promise<{ value: unknown; status: number }> {
  let response: Response;
  try {
    response = await fetch(path, {
      method, signal, credentials: "omit", redirect: "error", cache: "no-store",
      headers: { Accept: "application/json", ...(method !== "GET" ? { "Content-Type": "application/json" } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new LearningApiError("NETWORK_ERROR", 0);
  }
  let value: unknown;
  try { value = await response.json(); }
  catch { throw new LearningApiError("INVALID_RESPONSE", response.status); }
  if (!response.ok) {
    const code = typeof value === "object" && value !== null && "code" in value && typeof value.code === "string" && /^[A-Z][A-Z0-9_]+$/.test(value.code) ? value.code : `HTTP_${response.status}`;
    throw new LearningApiError(code, response.status, value);
  }
  return { value, status: response.status };
}

async function request(path: string, method: "GET" | "POST", body?: unknown, signal?: AbortSignal): Promise<LearningOperation> {
  const { value, status } = await learningRequest(path, method, body, signal);
  if (!isLearningOperation(value)) throw new LearningApiError("INVALID_RESPONSE", status);
  return value;
}

function subject(value: unknown): value is Subject {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === "string" && typeof row.name === "string" && row.name.trim().length > 0
    && (row.description === null || typeof row.description === "string");
}

const subjectsPath = "/api/learning/subjects";
export async function getSubjects(signal?: AbortSignal): Promise<Subject[]> {
  const { value, status } = await learningRequest(subjectsPath, "GET", undefined, signal);
  if (typeof value !== "object" || value === null || !("subjects" in value)
    || !Array.isArray(value.subjects) || !value.subjects.every(subject))
    throw new LearningApiError("INVALID_RESPONSE", status);
  return value.subjects;
}
async function subjectRequest(path: string, method: "GET" | "POST" | "PATCH" | "PUT", body?: unknown, signal?: AbortSignal): Promise<Subject> {
  const { value, status } = await learningRequest(path, method, body, signal);
  if (!subject(value)) throw new LearningApiError("INVALID_RESPONSE", status);
  return value;
}
export const getSubject = (id: string, signal?: AbortSignal) =>
  subjectRequest(`${subjectsPath}/${encodeURIComponent(id)}`, "GET", undefined, signal);
export const createSubject = (body: SubjectCreate) => subjectRequest(subjectsPath, "POST", body);
export const patchSubject = (id: string, body: SubjectPatch) =>
  subjectRequest(`${subjectsPath}/${encodeURIComponent(id)}`, "PATCH", body);

export function contentPath(subjectId: string, practiceId: string, itemId: string): string {
  return `${subjectsPath}/${encodeURIComponent(subjectId)}/practices/${encodeURIComponent(practiceId)}/items/${encodeURIComponent(itemId)}`;
}
export const contentAssetUrl = (subjectId: string, practiceId: string, itemId: string, assetId: string) =>
  `${contentPath(subjectId, practiceId, itemId)}/assets/${encodeURIComponent(assetId)}`;
export async function getQuestionContent(subjectId: string, practiceId: string, itemId: string, signal?: AbortSignal): Promise<QuestionContentView> {
  const { value, status } = await learningRequest(`${contentPath(subjectId, practiceId, itemId)}/content`, "GET", undefined, signal);
  if (typeof value !== "object" || value === null || !("phase" in value)
    || !["answering", "review"].includes(String(value.phase)) || !("stem_blocks" in value) || !Array.isArray(value.stem_blocks)
    || !("options" in value) || !Array.isArray(value.options)) throw new LearningApiError("INVALID_RESPONSE", status);
  return value as QuestionContentView;
}

export const previewLearningOperation = (body: LearningPreview, signal?: AbortSignal) =>
  request(`${prefix}/preview`, "POST", body, signal);
export const confirmLearningOperation = (id: string, body: LearningConfirmation, signal?: AbortSignal) =>
  request(`${prefix}/${encodeURIComponent(id)}/confirm`, "POST", body, signal);
export const getLearningOperation = (id: string, signal?: AbortSignal) =>
  request(`${prefix}/${encodeURIComponent(id)}`, "GET", undefined, signal);
