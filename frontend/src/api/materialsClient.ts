import type { components } from "./generated";
import { LearningApiError } from "./learningClient";

export { LearningApiError };

type Schema = components["schemas"];

export type MaterialView = Schema["MaterialView"];
export type MaterialBlock = NonNullable<MaterialView["blocks"]>[number];
export type MaterialAsset = NonNullable<MaterialView["assets"]>[number];
export type MaterialSeriesView = Schema["MaterialSeriesView"];
export type MaterialVersionView = MaterialSeriesView["versions"][number];
export type KnowledgeView = Schema["KnowledgeView"];
export type KnowledgeChapterInput = Schema["KnowledgeChapterInput"];
export type KnowledgePointInput = Schema["KnowledgePointInput"];
export type MaterialPurpose = Schema["MaterialPurpose"];

export type MaterialUploadInput = {
  purpose: MaterialPurpose;
  year?: number | null;
  publisher?: string | null;
  isbn?: string | null;
  exam_name?: string | null;
};

export type MaterialVersionInput = {
  document_id: string;
  series_id?: string | null;
  series_name?: string | null;
  label: string;
};

const base = "/api/learning";

/** 已批准上限的界面提示；服务端仍是唯一裁决者，错误码照实展示。 */
export const APPROVED_UPLOAD_BYTES = 20 * 1024 * 1024;

function failure(status: number, value: unknown): LearningApiError {
  const code =
    typeof value === "object" && value !== null && "code" in value && typeof value.code === "string"
      ? value.code
      : `HTTP_${status}`;
  return new LearningApiError(code, status);
}

async function request(
  path: string,
  method: "GET" | "POST",
  body?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  let response: Response;
  try {
    const multipart = body instanceof FormData;
    response = await fetch(path, {
      method,
      signal,
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(method === "POST" && !multipart ? { "Content-Type": "application/json" } : {}),
      },
      ...(body === undefined
        ? {}
        : { body: multipart ? (body as FormData) : JSON.stringify(body) }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new LearningApiError("NETWORK_ERROR", 0);
  }
  let value: unknown = null;
  try {
    value = await response.json();
  } catch {
    if (response.ok) throw new LearningApiError("INVALID_RESPONSE", response.status);
  }
  if (!response.ok) throw failure(response.status, value);
  return value;
}

function isMaterial(value: unknown): value is MaterialView {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.id === "string" &&
    typeof row.subject_id === "string" &&
    typeof row.original_filename === "string" &&
    typeof row.state === "string" &&
    typeof row.parse_state === "string" &&
    Array.isArray(row.blocks) &&
    Array.isArray(row.assets) &&
    Array.isArray(row.issues)
  );
}

export async function listMaterials(subjectId: string, signal?: AbortSignal): Promise<MaterialView[]> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/materials`, "GET", undefined, signal);
  if (typeof value !== "object" || value === null || !("materials" in value) || !Array.isArray(value.materials)
    || !value.materials.every(isMaterial))
    throw new LearningApiError("INVALID_RESPONSE", 200);
  return value.materials;
}

export async function uploadMaterial(
  subjectId: string,
  file: File,
  input: MaterialUploadInput,
  signal?: AbortSignal,
): Promise<MaterialView> {
  const metadata: Record<string, unknown> = { purpose: input.purpose };
  for (const key of ["year", "publisher", "isbn", "exam_name"] as const) {
    const value = input[key];
    if (value !== undefined && value !== null && value !== "") metadata[key] = value;
  }
  const body = new FormData();
  body.set("metadata", JSON.stringify(metadata));
  body.set("file", file, file.name);
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/uploads`, "POST", body, signal);
  if (!isMaterial(value)) throw new LearningApiError("INVALID_RESPONSE", 201);
  return value;
}

/** 原图地址只按科目、资料身份和内容 digest 组装，不含可猜测文件名。 */
export function materialAssetUrl(subjectId: string, materialId: string, digest: string): string {
  return `${base}/subjects/${encodeURIComponent(subjectId)}/materials/${encodeURIComponent(materialId)}/assets/${encodeURIComponent(digest)}`;
}

function isSeries(value: unknown): value is MaterialSeriesView {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === "string" && typeof row.name === "string" && Array.isArray(row.versions);
}

export async function listSeries(subjectId: string, signal?: AbortSignal): Promise<MaterialSeriesView[]> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/material-series`, "GET", undefined, signal);
  if (typeof value !== "object" || value === null || !("series" in value) || !Array.isArray(value.series)
    || !value.series.every(isSeries))
    throw new LearningApiError("INVALID_RESPONSE", 200);
  return value.series;
}

export async function createVersion(subjectId: string, body: MaterialVersionInput): Promise<MaterialSeriesView> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/material-series`, "POST", body);
  if (!isSeries(value)) throw new LearningApiError("INVALID_RESPONSE", 200);
  return value;
}

export async function setDefaultVersion(seriesId: string, versionId: string): Promise<MaterialSeriesView> {
  const value = await request(
    `${base}/material-series/${encodeURIComponent(seriesId)}/default-version`,
    "POST",
    { version_id: versionId },
  );
  if (!isSeries(value)) throw new LearningApiError("INVALID_RESPONSE", 200);
  return value;
}

function isKnowledge(value: unknown): value is KnowledgeView {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return typeof row.subject_id === "string" && typeof row.confirmed === "boolean"
    && Array.isArray(row.chapters) && Array.isArray(row.knowledge_points);
}

export async function getKnowledge(subjectId: string, signal?: AbortSignal): Promise<KnowledgeView> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/knowledge`, "GET", undefined, signal);
  if (!isKnowledge(value)) throw new LearningApiError("INVALID_RESPONSE", 200);
  return value;
}

export async function suggestKnowledge(subjectId: string): Promise<KnowledgeView> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/knowledge/suggest`, "POST");
  if (!isKnowledge(value)) throw new LearningApiError("INVALID_RESPONSE", 200);
  return value;
}

export async function confirmKnowledge(
  subjectId: string,
  body: { knowledge_points: KnowledgePointInput[]; chapters: KnowledgeChapterInput[] },
): Promise<KnowledgeView> {
  const value = await request(`${base}/subjects/${encodeURIComponent(subjectId)}/knowledge/confirm`, "POST", body);
  if (!isKnowledge(value)) throw new LearningApiError("INVALID_RESPONSE", 200);
  return value;
}
