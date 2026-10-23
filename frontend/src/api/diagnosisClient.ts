import type { components } from "./generated";
import { learningRequest, LearningApiError } from "./learningClient";
export type PersonalDiagnosis = components["schemas"]["PersonalDiagnosis"];
export type DiagnosisStatistics = components["schemas"]["DiagnosisStatistics"];
export type DiagnosisFilters = { source_kind: "" | "uploaded" | "ai"; answer_origin: string };

export async function getDiagnosis(subjectId: string, filters: DiagnosisFilters, signal?: AbortSignal): Promise<PersonalDiagnosis> {
  const params = new URLSearchParams();
  if (filters.source_kind) params.set("source_kind", filters.source_kind);
  if (filters.answer_origin) params.set("answer_origin", filters.answer_origin);
  const {value,status} = await learningRequest(`/api/learning/subjects/${encodeURIComponent(subjectId)}/diagnosis?${params}`, "GET", undefined, signal);
  if (typeof value !== "object" || value === null || !("subject_id" in value) || value.subject_id !== subjectId
    || !("statistics" in value) || !value.statistics || !("knowledge_points" in value) || !Array.isArray(value.knowledge_points)
    || !("evidence" in value) || !Array.isArray(value.evidence)) throw new LearningApiError("INVALID_RESPONSE",status);
  return value as PersonalDiagnosis;
}
