import { EMPTY_AI_READINESS } from "../api/contracts";
import type {
  ApiErrorEnvelope,
  ReadinessReport,
  SessionExchangeResponse,
  SettingsView,
  SetupReadyResponse,
} from "../api/contracts";

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function setupReadyFixture(
  overrides: Partial<SetupReadyResponse> = {},
): SetupReadyResponse {
  return {
    status: "ok",
    service: "backend",
    setup: {
      configuration: "available",
      settings_authorization: "available",
      static_resources: "available",
    },
    ...overrides,
  };
}

export function exchangeFixture(
  overrides: Partial<SessionExchangeResponse> = {},
): SessionExchangeResponse {
  return {
    management_token: "synthetic-management-token",
    expires_at: "2026-10-18T01:30:00Z",
    environment_id: "11111111-1111-4111-8111-111111111111",
    instance_id: "22222222-2222-4222-8222-222222222222",
    ...overrides,
  };
}

const okCheck = { status: "ok" as const, code: null, detail: null };

export function readinessFixture(
  overrides: Partial<ReadinessReport> = {},
): ReadinessReport {
  const base: ReadinessReport = {
    environment: "development",
    setup: { ready: true, reason: null },
    business_prerequisites: {
      ready: true,
      configuration_revision: "synthetic-active-revision",
      checks: {
        business_config: okCheck,
        jwt_reference: okCheck,
        postgres: okCheck,
        vector: okCheck,
        migration: okCheck,
        schema: okCheck,
        redis: okCheck,
        storage: okCheck,
      },
    },
    business: {
      ready: false,
      assembly: "not_mounted",
      recovery: "not_started",
      reason: "业务配置尚未激活",
    },
    ai: {
      evaluated: true,
      status: "not_configured",
      reason: "没有有效的 AI 绑定",
      tasks: {},
    },
    static_resources: "available",
    restart_required: false,
    pending_revision: null,
    candidate_diagnostics: null,
  };
  return {
    ...base,
    ...overrides,
    setup: { ...base.setup, ...overrides.setup },
    business_prerequisites: {
      ...base.business_prerequisites,
      ...overrides.business_prerequisites,
      checks:
        overrides.business_prerequisites?.checks ??
        base.business_prerequisites.checks,
    },
    business: { ...base.business, ...overrides.business },
    ai: { ...(base.ai ?? EMPTY_AI_READINESS), ...(overrides.ai ?? {}) },
  };
}

export function settingsViewFixture(
  overrides: Partial<SettingsView> = {},
): SettingsView {
  return {
    environment: "development",
    environment_id: "11111111-1111-4111-8111-111111111111",
    configuration: "available",
    active_revision: "synthetic-active-revision",
    pending_revision: null,
    restart_required: false,
    values: null,
    sources: {},
    effective_errors: [],
    credential_statuses: [],
    warnings: [],
    business_assembly: "not_mounted",
    static_resources: "available",
    ...overrides,
  };
}

export function errorFixture(
  code: string,
  message: string,
  overrides: Partial<ApiErrorEnvelope> = {},
): ApiErrorEnvelope {
  return {
    code,
    message,
    stage: "settings",
    retryable: false,
    correlation_id: "synthetic-correlation-id",
    ...overrides,
  };
}
