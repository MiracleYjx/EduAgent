import type { components } from "./generated";

export type AiReadiness = components["schemas"]["AiReadiness"];
export type ReadinessReport = components["schemas"]["ReadinessReport"];
export type ReadinessChecks = components["schemas"]["ReadinessChecks"];
export type ReadinessCheck = components["schemas"]["ReadinessCheck"];
export type SettingsView = components["schemas"]["SettingsView"];
export type ConfigurationValues = components["schemas"]["ConfigurationValues"];
export type CredentialReference = components["schemas"]["CredentialReference"];
export type CredentialStatusView = components["schemas"]["CredentialStatusView"];
export type DraftView = components["schemas"]["DraftView"];
export type ValidationReport = components["schemas"]["ValidationReport"];
export type ProbeView = components["schemas"]["ProbeView"];
export type OnboardingReport = components["schemas"]["OnboardingReport"];
export type SetupRecommendation = components["schemas"]["SetupRecommendation"];
export type SetupRecommendedBusiness = components["schemas"]["SetupRecommendedBusiness"];
export type SetupDataState = components["schemas"]["SetupDataState"];
export type SetupDatabaseData = components["schemas"]["SetupDatabaseData"];
export type SetupStorageData = components["schemas"]["SetupStorageData"];
export type OnboardingView = components["schemas"]["OnboardingView"];
export type InitializationPreview = components["schemas"]["InitializationPreview"];
export type OperationView = components["schemas"]["OperationView"];
export type ActivationView = components["schemas"]["ActivationView"];
export type RestartView = components["schemas"]["RestartView"];
export type DiagnosticsReport = components["schemas"]["DiagnosticsReport"];

export type ProviderCatalogEntry = components["schemas"]["ProviderCatalogEntry"];

export const EMPTY_AI_READINESS: AiReadiness = {
  evaluated: false,
  status: "not_evaluated",
  reason: "not_evaluated",
  tasks: {},
};

export function aiReadiness(report: ReadinessReport): AiReadiness {
  return report.ai ?? EMPTY_AI_READINESS;
}

export type SessionExchangeResponse = components["schemas"]["SessionExchangeResponse"];

export type ApiErrorEnvelope = components["schemas"]["ApiErrorEnvelope"];

export type SetupReadyResponse = components["schemas"]["SetupReadyResponse"];

export type HealthResponse = components["schemas"]["PublicHealthResponse"];

export function isApiErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return typeof candidate.code === "string" && typeof candidate.message === "string";
}

export function isSetupReadyResponse(value: unknown): value is SetupReadyResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  if (candidate.status !== "ok" && candidate.status !== "not_ready") {
    return false;
  }
  if (typeof candidate.setup !== "object" || candidate.setup === null) {
    return false;
  }
  const setup = candidate.setup as Record<string, unknown>;
  return (
    (setup.configuration === "available" || setup.configuration === "invalid") &&
    (setup.settings_authorization === "available" ||
      setup.settings_authorization === "unavailable") &&
    typeof setup.static_resources === "string"
  );
}

export function isSessionExchangeResponse(
  value: unknown,
): value is SessionExchangeResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.management_token === "string" &&
    candidate.management_token.length > 0 &&
    typeof candidate.expires_at === "string" &&
    typeof candidate.environment_id === "string" &&
    typeof candidate.instance_id === "string"
  );
}
