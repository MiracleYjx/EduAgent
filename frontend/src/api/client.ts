import type {
  ApiErrorEnvelope,
  HealthResponse,
  ReadinessReport,
  SessionExchangeResponse,
  SettingsView,
  SetupReadyResponse,
} from "./contracts";
import {
  isApiErrorEnvelope,
  isSessionExchangeResponse,
  isSetupReadyResponse,
} from "./contracts";

import type { components } from "./generated";
import type {
  ActivationView, CredentialStatusView, DiagnosticsReport, DraftView,
  InitializationPreview, OnboardingReport, OnboardingView, OperationView,
  ProbeView, ProviderCatalogEntry, RestartView, SetupDataState, SetupRecommendation,
  ValidationReport,
} from "./contracts";

const SETTINGS_PREFIX = "/api/settings";

export class SettingsApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly stage?: string;
  readonly field?: string;
  readonly retryable: boolean;
  readonly correlationId?: string;

  constructor(
    message: string,
    options: {
      status: number;
      code: string;
      stage?: string;
      field?: string;
      retryable?: boolean;
      correlationId?: string;
    },
  ) {
    super(message);
    this.name = "SettingsApiError";
    this.status = options.status;
    this.code = options.code;
    this.stage = options.stage;
    this.field = options.field;
    this.retryable = options.retryable ?? false;
    this.correlationId = options.correlationId;
  }
}

function errorFromEnvelope(
  envelope: ApiErrorEnvelope,
  status: number,
): SettingsApiError {
  return new SettingsApiError(envelope.message, {
    status,
    code: envelope.code,
    stage: envelope.stage,
    field: envelope.field ?? undefined,
    retryable: envelope.retryable,
    correlationId: envelope.correlation_id ?? undefined,
  });
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function fallbackHttpError(status: number): SettingsApiError {
  return new SettingsApiError(`设置服务返回 HTTP ${status}。`, {
    status,
    code: `HTTP_${status}`,
    retryable: status >= 500,
  });
}

async function responseError(response: Response): Promise<SettingsApiError> {
  const body = await parseJson(response);
  if (isApiErrorEnvelope(body)) {
    return errorFromEnvelope(body, response.status);
  }
  if (
    typeof body === "object" &&
    body !== null &&
    Array.isArray((body as { detail?: unknown }).detail)
  ) {
    return new SettingsApiError("请求参数无效，请检查输入。", {
      status: response.status,
      code: "REQUEST_INVALID",
    });
  }
  return fallbackHttpError(response.status);
}

function networkError(): SettingsApiError {
  return new SettingsApiError(
    "无法连接 EduAgent 设置服务，请确认后端仍在运行。",
    {
      status: 0,
      code: "NETWORK_ERROR",
      retryable: true,
    },
  );
}

async function request(
  path: string,
  init: RequestInit,
  token?: string,
): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (token !== undefined) {
    headers.set("Authorization", `Setup ${token}`);
  }
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw networkError();
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return response;
}

async function readJson(response: Response): Promise<unknown> {
  const body = await parseJson(response);
  if (body === null) {
    throw new SettingsApiError("设置服务返回了无法解析的响应。", {
      status: response.status,
      code: "INVALID_RESPONSE",
    });
  }
  return body;
}

export async function exchangeOneTimeCode(
  oneTimeCode: string,
): Promise<SessionExchangeResponse> {
  const response = await request(`${SETTINGS_PREFIX}/session/exchange`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ one_time_code: oneTimeCode }),
  });
  const body = await readJson(response);
  if (!isSessionExchangeResponse(body)) {
    throw new SettingsApiError("授权响应缺少必要字段。", {
      status: response.status,
      code: "INVALID_AUTHORIZATION_RESPONSE",
    });
  }
  return body;
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await request("/health", { method: "GET" });
  const body = await readJson(response);
  if (
    typeof body !== "object" ||
    body === null ||
    (body as { status?: unknown }).status !== "ok"
  ) {
    throw new SettingsApiError("存活检查响应无效。", {
      status: response.status,
      code: "INVALID_RESPONSE",
    });
  }
  return body as HealthResponse;
}

export async function getSetupReady(): Promise<SetupReadyResponse> {
  let response: Response;
  try {
    response = await fetch("/setup-ready", {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw networkError();
  }
  const body = await parseJson(response);
  if (response.status !== 200 && response.status !== 503) {
    if (isApiErrorEnvelope(body)) {
      throw errorFromEnvelope(body, response.status);
    }
    throw fallbackHttpError(response.status);
  }
  if (!isSetupReadyResponse(body)) {
    throw new SettingsApiError("Setup 状态响应无效。", {
      status: response.status,
      code: "INVALID_RESPONSE",
    });
  }
  return body;
}

export async function getBusinessReady(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch("/ready", {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw networkError();
  }
  if (response.status === 200) return true;
  if (response.status === 503) return false;
  const body = await parseJson(response);
  if (isApiErrorEnvelope(body)) {
    throw errorFromEnvelope(body, response.status);
  }
  throw fallbackHttpError(response.status);
}

export async function getReadiness(token: string, signal?: AbortSignal): Promise<ReadinessReport> {
  const response = await request(
    `${SETTINGS_PREFIX}/readiness`,
    { method: "GET", signal },
    token,
  );
  return (await readJson(response)) as ReadinessReport;
}

export async function getSettingsView(token: string, signal?: AbortSignal): Promise<SettingsView> {
  const response = await request(`${SETTINGS_PREFIX}/`, { method: "GET", signal }, token);
  return (await readJson(response)) as SettingsView;
}

type Schema = components["schemas"];

async function settingsJson<T>(
  token: string, path: string, method = "GET", body?: unknown, signal?: AbortSignal,
): Promise<T> {
  const response = await request(`${SETTINGS_PREFIX}${path}`, {
    method,
    signal,
    ...(method === "GET" ? {} : {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }),
  }, token);
  return await readJson(response) as T;
}

export const getOnboarding = (token: string, signal?: AbortSignal) =>
  settingsJson<OnboardingReport>(token, "/onboarding", "GET", undefined, signal);
export const getSetupRecommendation = (token: string, signal?: AbortSignal) =>
  settingsJson<SetupRecommendation>(token, "/onboarding/recommendation", "GET", undefined, signal);
export const getSetupDataState = (token: string, signal?: AbortSignal) =>
  settingsJson<SetupDataState>(token, "/onboarding/data-state", "GET", undefined, signal);
export const getProviderCatalog = async (token: string) =>
  (await settingsJson<{ providers: ProviderCatalogEntry[] }>(token, "/providers")).providers;
export const saveOnboarding = (token: string, body: Schema["OnboardingUpdateRequest"]) =>
  settingsJson<OnboardingView>(token, "/onboarding", "PATCH", body);
export const createDraft = (token: string, body: Schema["DraftCreateRequest"]) =>
  settingsJson<DraftView>(token, "/drafts", "POST", body);
export const patchDraft = (token: string, id: string, body: Schema["DraftPatchRequest"]) =>
  settingsJson<DraftView>(token, `/drafts/${encodeURIComponent(id)}`, "PATCH", body);
export const validateDraft = (token: string, id: string, body: Schema["DraftValidateRequest"]) =>
  settingsJson<ValidationReport>(token, `/drafts/${encodeURIComponent(id)}/validate`, "POST", body);
export const createCredential = (token: string, body: Schema["CredentialCreateRequest"]) =>
  settingsJson<CredentialStatusView>(token, "/credentials", "POST", body);
export const bindCredential = (token: string, id: string, body: Schema["CredentialBindingRequest"]) =>
  settingsJson<DraftView>(token, `/drafts/${encodeURIComponent(id)}/credential-bindings`, "POST", body);
export const createProbe = (token: string, body: Schema["ProbeCreateRequest"]) =>
  settingsJson<ProbeView>(token, "/probes", "POST", body);
export const getProbe = (token: string, id: string) =>
  settingsJson<ProbeView>(token, `/probes/${encodeURIComponent(id)}`);
export const cancelProbe = (token: string, id: string) =>
  settingsJson<ProbeView>(token, `/probes/${encodeURIComponent(id)}`, "DELETE");
export const previewInitialization = (token: string, body: Schema["InitializationPreviewRequest"]) =>
  settingsJson<InitializationPreview>(token, "/initialization/preview", "POST", body);
export const applyInitialization = (token: string, body: Schema["InitializationApplyRequest"]) =>
  settingsJson<OperationView>(token, "/initialization/apply", "POST", body);
export const getOperation = (token: string, id: string) =>
  settingsJson<OperationView>(token, `/operations/${encodeURIComponent(id)}`);
export const activateDraft = (token: string, id: string, body: Schema["ActivationRequest"]) =>
  settingsJson<ActivationView>(token, `/drafts/${encodeURIComponent(id)}/activate`, "POST", body);
export const restartApplication = (token: string, body: Schema["RestartRequest"]) =>
  settingsJson<RestartView>(token, "/restart", "POST", body);
export const runDependencyDiagnostics = (token: string) =>
  settingsJson<DiagnosticsReport>(token, "/diagnostics", "POST", {
    checks: ["postgres", "redis", "storage", "migrations"],
  } satisfies Schema["DiagnosticsRequest"]);


export const createCredentialVersion = (token: string, id: string, body: Schema["CredentialVersionRequest"]) =>
  settingsJson<CredentialStatusView>(token, `/credentials/${encodeURIComponent(id)}/versions`, "POST", body);
export async function deleteCredentialVersion(token: string, id: string, version: string): Promise<void> {
  await request(`${SETTINGS_PREFIX}/credentials/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`, {
    method: "DELETE", headers: { "Content-Type": "application/json" }, body: "{}",
  }, token);
}

export async function getDiagnosticsExport(token: string): Promise<Blob> {
  const response = await request(`${SETTINGS_PREFIX}/diagnostics/export`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include: ["configuration", "readiness", "versions"] } satisfies Schema["ExportRequest"]),
  }, token);
  return response.blob();
}

export const getRecovery = (token: string) => settingsJson<Schema["RecoveryView"]>(token, "/recovery");
export const restoreRevision = (token: string, body: Schema["RecoveryRestoreRequest"]) => settingsJson<ActivationView>(token, "/recovery/restore", "POST", body);
export const getLegacySources = (token: string) => settingsJson<Schema["LegacySourceList"]>(token, "/legacy-import/sources");
export const previewLegacyImport = (token: string, source_id: string) => settingsJson<Schema["LegacyImportPreview"]>(token, "/legacy-import/preview", "POST", { source_id });
export const applyLegacyImport = (token: string, body: Schema["LegacyImportApplyRequest"]) => settingsJson<DraftView>(token, "/legacy-import/apply", "POST", body);
export const getIndexIdentities = (token: string) => settingsJson<Schema["IndexIdentityList"]>(token, "/index-identities");
export const getIndexIdentity = (token: string, id: string) => settingsJson<Schema["IndexIdentityView"]>(token, `/index-identities/${encodeURIComponent(id)}`);
export const previewIndexAdoption = (token: string, id: string, body: Schema["AdoptionPreviewRequest"]) => settingsJson<Schema["AdoptionPreview"]>(token, `/index-identities/${encodeURIComponent(id)}/adoption-preview`, "POST", body);
export const confirmIndexAdoption = (token: string, id: string, body: Schema["AdoptionConfirmRequest"]) => settingsJson<Schema["IndexIdentityView"]>(token, `/index-identities/${encodeURIComponent(id)}/adoption-confirm`, "POST", body);
