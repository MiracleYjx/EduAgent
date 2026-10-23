// TCR-027: synthetic API responses used only by tests, never by production UI.
import { vi } from "vitest";
import type { components } from "../../api/generated";
import {
  errorFixture, exchangeFixture, jsonResponse, readinessFixture,
  settingsViewFixture, setupReadyFixture,
} from "../../test/fixtures";

type Draft = components["schemas"]["DraftView"];
export const DRAFT_ID = "33333333-3333-4333-8333-333333333333";
export const PROFILE_ID = "44444444-4444-4444-8444-444444444444";
export const PUBLISHED_ID = "55555555-5555-4555-8555-555555555555";
export const OPERATION_ID = "66666666-6666-4666-8666-666666666666";
export const syntheticReference = {
  credential_id: "77777777-7777-4777-8777-777777777777",
  version: "88888888-8888-4888-8888-888888888888",
  environment_id: "11111111-1111-4111-8111-111111111111",
  backend: "dpapi" as const,
  purpose: "jwt",
};

export function draftFixture(withModel = false): Draft {
  return {
    revision_id: DRAFT_ID, parent_revision: null, status: "draft",
    created_at: "2026-10-18T02:00:00Z", restart_required: false,
    values: {
      bootstrap: { bind_host: "127.0.0.1", port: 8000 },
      business: {
        database_locator: { host: "127.0.0.1", port: 15432, database: "eduagent_test", user: "test_runner", schema: "test_t223" },
        redis_locator: { host: "127.0.0.1", port: 16379, database: 0 },
        storage_root: "C:/synthetic-test/storage", jwt_secret_ref: syntheticReference,
        jwt_algorithm: "HS256", jwt_expire_minutes: 60, dev_mode: false,
      },
      providers: withModel ? {
        [PROFILE_ID]: {
          profile_id: PROFILE_ID, profile_revision: PROFILE_ID, display_name: "合成文本模型",
          adapter_id: "openai_compatible", model_id: "synthetic-text", base_url: "http://127.0.0.1:9999/v1",
          credential_ref: null, auth_mode: "none", declared_capabilities: ["text", "json_object"], enabled: true,
        },
      } : {},
      bindings: withModel ? {
        default_llm: { task_kind: "default_llm", profile_id: PROFILE_ID, profile_revision: PROFILE_ID, required_capabilities: ["text", "json_object"], inherit_default: false },
      } : {},
      policies: { confidence_threshold: 0.8, hybrid_weight: 0.5, rerank_max_candidates: 5, rerank_timeout_seconds: 30 },
    },
  };
}

export function wizardApi(options: {
  step?: number; draft?: Draft | null; invalid?: boolean;
  probeFailure?: boolean; initializationFailure?: boolean; pending?: boolean;
  authorizationFailure?: 401 | 403; operationInterrupted?: boolean;
  queueProbe?: boolean; businessReady?: boolean; personalReady?: boolean; draftConflict?: boolean; runningOperation?: boolean;
} = {}) {
  let draft = options.draft ?? null;
  let step = options.step ?? 0;
  let progressRevision = 0;
  let published = false;
  let operation: components["schemas"]["OperationView"] | null = options.operationInterrupted || options.runningOperation ? {
    operation_id: OPERATION_ID, kind: "initialization", status: options.runningOperation ? "running" : "interrupted",
    steps: [], error_code: null, started_at: "2026-10-18T02:00:00Z",
  } : null;
  const probes: components["schemas"]["ProbeView"][] = [];
  const calls: { path: string; method: string; body: Record<string, unknown> }[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : {};
    calls.push({ path, method, body });
    if (path === "/setup-ready") return jsonResponse(setupReadyFixture());
    if (path.endsWith("/session/exchange")) return jsonResponse(exchangeFixture());
    if (path === "/api/learning/subjects") return options.personalReady ?? options.businessReady
      ? jsonResponse({ subjects: [] }) : jsonResponse(errorFixture("LEARNING_CONFIGURATION_UNAVAILABLE", "未配置"), 503);
    if (options.authorizationFailure && method !== "GET") {
      return jsonResponse(errorFixture(
        options.authorizationFailure === 401 ? "SESSION_EXPIRED" : "ORIGIN_NOT_ALLOWED",
        "授权或来源已失效，请重新授权。",
      ), options.authorizationFailure);
    }
    const report = readinessFixture({
      business: options.businessReady ? { ready: true, assembly: "mounted", recovery: "completed", reason: null } : { ready: false, assembly: "not_mounted", recovery: "not_started", reason: "需重启" },
      restart_required: Boolean(options.pending && published),
      pending_revision: options.pending && published ? PUBLISHED_ID : null,
    });
    if (path.endsWith("/readiness")) return jsonResponse(report);
    if (path === "/api/settings/") return jsonResponse(settingsViewFixture({
      active_revision: published && !options.pending ? PUBLISHED_ID : null,
      pending_revision: published && options.pending ? PUBLISHED_ID : null,
      configuration: published ? "available" : "not_configured",
      values: published ? draft?.values ?? null : null,
      restart_required: Boolean(published && options.pending),
    }));
    if (path.endsWith("/providers")) return jsonResponse({ providers: [{
      kind: "llm", adapter_id: "openai_compatible", capabilities: ["text", "json_object"],
      lifecycle: "owned_per_run", canonical_id: "openai_compatible", parameters: {},
    }] });
    if (path.endsWith("/onboarding") && method === "GET") return jsonResponse({
      progress: { revision: progressRevision, current_step: step, draft_revision: draft?.revision_id ?? null, completed_steps: [] },
      readiness: report, draft, probes, initialization: operation,
    });
    if (path.endsWith("/onboarding") && method === "PATCH") {
      step = Number(body.current_step);
      progressRevision += 1;
      return jsonResponse({ revision: progressRevision, current_step: step, draft_revision: body.draft_revision, completed_steps: [] });
    }
    if (path.endsWith("/drafts") || (path.includes("/drafts/") && method === "PATCH")) {
      if (options.draftConflict) return jsonResponse(errorFixture("REVISION_CONFLICT", "修订冲突，请重新读取。"), 409);
      draft = draftFixture(Boolean(draft?.values.bindings?.default_llm?.profile_id));
      return jsonResponse(draft, method === "POST" ? 201 : 200);
    }
    if (path.endsWith("/validate")) {
      if (!options.invalid && draft) draft = { ...draft, status: "validated" };
      return jsonResponse({ valid: !options.invalid, revision_id: DRAFT_ID,
        errors: options.invalid ? [{ field: "business.database_password_ref", code: "CREDENTIAL_UNAVAILABLE", message: "数据库凭据引用不可用。" }] : [], affected_tasks: [], restart_required: false });
    }
    if (path.endsWith("/credentials")) return jsonResponse({
      credential_ref: syntheticReference.credential_id, ...syntheticReference, status: "available",
    }, 201);
    if (path.endsWith("/credential-bindings")) return jsonResponse(draft);
    if (path.endsWith("/probes") && method === "POST") {
      const probe = {
        probe_id: `99999999-9999-4999-8999-${String(probes.length + 1).padStart(12, "0")}`,
        status: options.queueProbe ? "running" as const : options.probeFailure ? "failed" as const : "succeeded" as const,
        result_code: options.queueProbe ? "RUNNING" : options.probeFailure ? "SERVICE_UNREACHABLE" : "SUCCESS",
        message: options.probeFailure ? "无法连接目标服务。" : "本次能力检测成功。",
        capability: String(body.capability), profile_id: PROFILE_ID,
        tested_at: "2026-10-18T02:00:00Z", expires_at: "2026-10-18T02:10:00Z",
        external_requests: 1, may_have_been_billed: true, retryable: false, correlation_id: "synthetic-correlation",
      };
      probes.push(probe);
      return jsonResponse(probe, 202);
    }
    if (path.includes("/probes/") && method === "GET") return jsonResponse(probes.find(p => path.endsWith(p.probe_id)));
    if (path.includes("/probes/") && method === "DELETE") {
      const previous = probes.find(p => path.endsWith(p.probe_id));
      return jsonResponse({ ...previous, status: "cancelled", result_code: "CANCELLED", message: "检测已取消，可能已计费。", may_have_been_billed: true });
    }
    if (path.endsWith("/initialization/preview")) return jsonResponse({
      preview_id: DRAFT_ID, draft_revision: DRAFT_ID, environment: "test", target_identity: "test:127.0.0.1:15432/eduagent_test#test_t223",
      current_head: null, target_head: "0024_embedding_index_identity", allowed: true, destructive: false, steps: ["upgrade_to_target_head"], expires_at: "2099-01-01T00:00:00Z",
    });
    if (path.endsWith("/initialization/apply")) {
      operation = { operation_id: OPERATION_ID, kind: "initialization", status: options.initializationFailure ? "failed" : "succeeded",
        steps: ["upgrade_to_target_head"], error_code: options.initializationFailure ? "MIGRATION_FAILED" : null, started_at: "2026-10-18T02:00:00Z" };
      return jsonResponse(operation, 202);
    }
    if (path.includes("/operations/")) return jsonResponse(operation);
    if (path.endsWith("/activate")) {
      published = true;
      return jsonResponse({ revision_id: PUBLISHED_ID, status: options.pending ? "pending_restart" : "active", restart_required: Boolean(options.pending), activation_keys: [] }, options.pending ? 202 : 200);
    }
    if (path.endsWith("/restart")) return jsonResponse({ status: "manual_restart_required", reason: "请手动重启 Test 进程。", pending_revision: options.pending ? PUBLISHED_ID : null });
    if (path.endsWith("/diagnostics")) return jsonResponse({ status: "degraded", checks: { postgres: { status: "failed", code: "postgres_unavailable" } }, evaluated_at: "2026-10-18T02:00:00Z" });
    return jsonResponse(errorFixture("NOT_FOUND", "未提供的合成场景。"), 404);
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, calls, writes: () => calls.filter(call => call.method !== "GET" && !call.path.endsWith("/session/exchange")) };
}
