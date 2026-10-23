# TCR：个人入口来源边界装配修复（T083 前置）

## 变更对象

- `backend/app/core/bootstrap.py::create_application`

## 为什么需要

T083 真实验收在显式 `settings` 装配路径（测试/嵌入入口）下无法访问任何个人学习端点，全部返回
`503 LEARNING_SOURCE_UNAVAILABLE`。

根因链：

1. `create_app` 用 `build_bootstrap_security(context)` 生成 `SetupSecurityService`，其
   `SetupSourcePolicy.allowed_hosts = settings_authorities(context.bind)`。
2. `settings_authorities(None)` 返回 `frozenset()`，据此 `check_personal_source`
   （`settings_security.py:665`）判定 `allowed_hosts` 为空并拒绝请求。
3. `context.bind` 仅由 `EDUAGENT_BIND_HOST/EDUAGENT_BIND_PORT` 决定；生产 Windows 启动器
   （`windows_launcher.py:411-412`）会设置这两个变量，而显式 `settings` 的测试/嵌入调用方不会，
   于是 `bind is None`，来源边界退化为空。

实测：修复前 `allowed_hosts == []`；修复后为 `{127.0.0.1:8000, localhost:8000}`。

## 变更内容

显式 `settings` 分支中，若 `resolved.bind is None`，补 `BootstrapOptions(port=DEFAULT_BIND_PORT)`
（回环 127.0.0.1）。不触碰 `build_process_setup_security()` 的无参退化路径，不修改
`settings_security.py` 的校验逻辑，不改 `create_app` 公共签名，不新增依赖。

安全取向：只补回环默认绑定，不扩大允许集合；真实端口仍由 `EDUAGENT_BIND_HOST/PORT` 决定，
其它端口/主机继续 `HOST_NOT_ALLOWED`，跨站 Origin 继续 `ORIGIN_NOT_ALLOWED`。

## 断言变化

新增（`tests/integration/test_personal_learning_foundation.py`）：

- `test_explicit_settings_assembly_keeps_the_loopback_source_boundary`
  - 显式装配后 `app.state.learning_source_policy.allowed_hosts` 非空且等于
    `{127.0.0.1:8000, localhost:8000}`；
  - 回环 Host 通过来源判定；
  - `evil.example:8000`、`127.0.0.1:9999` 仍抛 `HOST_NOT_ALLOWED`；
  - 跨站 Origin 仍抛 `ORIGIN_NOT_ALLOWED`。

未改变：既有来源安全契约（`tests/contract/test_settings_security_api.py`，11 passed）、
无显式 `settings` 的生产装配路径行为。

## 验证

| 项 | 结果 |
| --- | --- |
| 新增用例 | passed（1） |
| `tests/contract/test_settings_security_api.py` | 11 passed |
| 全量 `pytest tests/ -q` | 见 T087 记录（本轮环境限制如实标注） |

## 影响面

- 显式 `settings` 装配（测试、嵌入、脚本）获得与生产一致的回环来源边界；
- 生产/EXE 路径未进入新增分支（该路径本就有 `EDUAGENT_BIND_*`），行为不变。

## T083 Embedding encoding_format 修复

### 必要性

openai SDK 2.54.0 在省略 `encoding_format` 时默认注入 `base64`，与后端期望的浮点数组不兼容，导致 SiliconFlow 的 `BAAI/bge-m3` 响应解析失败（`MODEL_BUDGET_CHANGED`）。

### 变更

在 `backend/app/ai/embedding/providers/openai_compatible.py` 的 `embeddings.create` 调用中显式传 `encoding_format="float"`，覆盖 SDK 的默认编码格式。

### 覆盖

- 新增测试：请求包含 `encoding_format="float"`
- 新增测试：浮点数组解析为 1024 维
- 新增测试：Base64 响应触发明确的维度错误，不静默进入索引
- 更新既有云端 Embedding 测试替身，记录并断言请求格式

### 边界

- 不改其他 Embedding Provider
- 不改业务代码
- 不降级为 Base64 解码

## T083 G2 空正文与 reasoning 预算修复

### 必要性

G2 真实响应为 HTTP 200、`finish_reason=length`、`content=""`，而 `reasoning_content` 已以 2,000 completion tokens 占满输出预算。既有适配器虽不把 reasoning 当正文，但空正文错误在操作层被折叠为 `PROVIDER_FAILED`，且 `finish_reason=length` 未被显式拒绝。

### 变更

- DeepSeek/OpenAI-compatible 适配器显式分离 `content`、`reasoning_content` 与 `finish_reason`；空正文保留明确码 `ProviderEmptyResponse` 且改为不可重试，非空但 `length` 返回 `ProviderOutputTruncated`。
- `LearningOperations` 保留 Provider 的真实错误码，不再把所有 Provider 异常折叠为 `PROVIDER_FAILED`。
- 增加默认 `None` 的可选 `reasoning_effort` 配置；默认请求体和冻结预算不变，启用需重新 G-P 确认。
- 出题服务在操作成功后仍要求结构化结果存在，空/缺失结果直接阻断，不进入 `personal_admission`。
- trace 错误码白名单纳入 `ProviderOutputTruncated`，避免截断失败被收敛为未知码。

### 断言变化

- 既有“空响应重试至耗尽”断言改为单次发送、`ProviderEmptyResponse`、`retryable=False`；这是本轮明确要求的 L04/L07 行为。
- 新增 reasoning-only、content 缺失、`finish_reason=length`、合法 content、Schema 失败、单次发送计数和可选 `reasoning_effort` 透传断言。

### 边界

- 不改变模型、端点、凭据、既有 `max_tokens` 或 G-P 冻结值。
- 不把 reasoning 当答案，不自动重试，不补发 unknown。

## T083 剩余六次验收的离线准备（2026-10-22）

### 必要性

当前活跃修订已接纳 text/json_object 等 target，但缺少独立 vision 绑定。
使用现有 Pytest/settings 框架验证只新增 vision 绑定时可沿用既有
activation_validation，并仍要求新候选自己的 vision Probe，避免重复验证
未变化的能力。此测试不证明真实 AI 或视觉效果。

### 覆盖

- 在 `tests/unit/settings/test_config_activation.py` 增加一个聚焦用例。
- MockTransport 仅替代远程响应；实际 ConfigService、ProbeService、Registry
  与 RuntimeSnapshotService 负责修订、接纳和快照行为。
- 新增 target 仅为 `vision:vision`；旧 target identity 与接纳记录保持一致。
- 缺少新 Probe 或仅提交旧候选的 vision Probe 时拒绝激活；新候选一次
  vision Probe 后激活，旧 text/json_object Probe 不重发。
- 新快照可解析 vision，旧快照仍引用旧修订，不获取业务客户端。

### 边界

不修改既有断言、冻结样例或 oracle；不读取 `.env`/真实凭据，不连接 PG/Redis，
不发送真实模型请求。真实配置仅作非秘密元数据的只读投影检查，不激活修改。


## P1 OCR asset scope and explicit retry regression (2026-10-22)

### Necessity

The persisted P1 response references an allowed image asset with source_locations
pointing to its OCR block. The image anchor is `第 1 页`; the OCR anchor is
`第 1 页:ocr` with persisted `ocr_source_anchor=第 1 页`. The validator ignores
that producer-owned relationship and rejects the candidate with
PAPER_ASSET_SCOPE_INVALID. Both attempts have identical source messages/assets.
Repeated explicit retry preparation also rejects the same idempotency key after
the import reference advances; serialize the transition and return that retry.

### Assertions and coverage

Add focused unit tests in tests/unit/services/test_personal_paper_asset_scope.py:
- Accept an OCR source only through a persisted text block linking the same
  document/page to the allowed image anchor; preserve exact OriginalBlock source.
- Reject unknown/foreign assets, wrong page/document, missing or wrong OCR link,
  and unrelated images on the same page; retain direct anchor behavior.
- Explicit retry updates only the import's current operation reference, retains
  the old failed audit facts and identical source messages/asset catalog.
- Duplicate and controlled concurrent retries using the same key return one
  retry; a different stale key is rejected, without another retry.

### Boundary

These are isolated unit tests with in-memory operation/session doubles, not real
operations, previews, provider requests, or acceptance evidence. No MockTransport,
shared Test database writes, credentials, frozen oracle, ledger or task changes.
The controlled concurrency test checks serialization intent and duplicate behavior;
it does not claim PostgreSQL row-lock integration coverage. No data migration.

## R1 acceptance helper numeric score comparison (2026-10-22)

### Necessity

The existing successful R1 response serializes its Decimal score as "10.00".
The acceptance helper compares that value with the string "10" and stops before
acceptance despite the required numeric score being present. The user approved
this helper-only correction and reuse of the saved R1 result without resending.

### Assertion change and coverage

In .cache/t083-batch1/a4_batch2.py, compare the current score with Decimal("10")
using Decimal(checks["score"]). Keep the exact history list ["8.00", "10.00"]
and all dispute, reference-answer, acceptance and restoration checks unchanged.
Check equivalent decimal representations and reject non-10 scores offline;
then validate the saved R1 result before normal business acceptance, with the
provider transport blocked. No new provider request or R1 execution is allowed.

### Boundary

This changes the acceptance helper only, not product code, frozen samples/oracle,
model budgets, grading records or historical ledger. Acceptance itself uses the
existing business endpoint and preserves both grading rounds and operation audit.
No migration, commit, push or acceptance report file is part of this change.

## T083 rerank projection must not inherit the default LLM (2026-10-22)

### Necessity

`app_settings_for()` unconditionally resolved the `rerank` task, so a configuration
without a rerank binding inherited `default_llm` through
`ProviderRegistry.resolve_binding`. With DeepSeek as the default LLM the projection
produced `rerank_provider="deepseek"`, which is not in `SUPPORTED_RERANK_PROVIDERS`
(`cross_encoder`, `llm`, `none`, `openai_compatible`). `AppSettings` then raised
`ValidationError`, `app_settings_for()` converted it into
`SettingsManagementError(INVALID_CONFIGURATION, stage="assembly")`, and
`effective_business_settings()` returned `(None, INVALID_CONFIGURATION)`.
`_settings_for_runs()` dropped that reason, so `BusinessRunFacts.bindings()` reported
`LEGACY_RUN_INDEX_UNAVAILABLE` before any SQL ran. The failure is a projection bug,
not a missing index or migration.

### Assertion change and coverage

`backend/app/services/settings/management.py` keeps the existing resolution path but
only asks for a rerank profile when a `rerank` binding exists and is either explicit
(`profile_id` set) or explicitly inheriting (`inherit_default`). A missing or disabled
binding projects as `none`. The Registry, admission gates and profile resolution are
unchanged.

A parameterized regression in `tests/integration/test_settings_management.py` reuses
the existing `runtime`/`profile` fixtures and synthetic credentials:

- DeepSeek default LLM available and no rerank binding: the LLM stays `deepseek` and
  rerank is `none`.
- Explicit `TaskBinding(task_kind="rerank")` with nothing selected: rerank is `none`.
- Explicitly bound rerank profile: adapter and model are preserved.
- `app_settings_for()` does not mutate the supplied configuration.

### Boundary

No Registry refactor, compatibility layer, migration or index rebuild, and no
activation-gate bypass. Offline fixtures only: no provider request, no PG/Redis
connection, no credential or secret read, and no change to frozen samples, oracle,
ledger or task markers.

## T083 image input must not be charged as base64 text (2026-10-22)

### Necessity

`GuardedTransport` counted the whole request body against the per-request input
budget. The frozen 32,642-byte V1 PNG becomes 43,524 base64 characters, and the
deployed counter's conservative bound `(len(bytes) + 1) // 2` turns that into about
21,762 tokens. With the authorized 6,000-token vision budget the request was refused
as `MODEL_BUDGET_REQUIRED` before any network call, even though the prompt text itself
was well inside the limit. The request-byte limit already excludes image payloads for
the same reason; the token budget did not.

### Assertion change and coverage

`learning_operations.py` gains `IMAGE_INPUT_TOKEN_RESERVATION = 1_500` and
`budget_input_tokens(counter, model, body)`: image parts are removed from the counted
scope and charged a fixed reservation each, while text is still counted by the
injected counter. Bodies without images are counted exactly as before. Tests in
`tests/contract/test_learning_paid_budgets.py` pin that:

- the frozen 32,642-byte PNG counts at most 6,000 and equals text plus one reservation;
- a text-only body returns the injected counter's value unchanged;
- image parts and base64 payloads never reach the counter, and the request body is not
  mutated.

### Boundary

No change to model, endpoint, credential, output limit or the 6,000 input cap, and no
retry. Image bytes stay bounded by `MAX_REQUEST_BYTES`. This does not relax the
paid-send gate: a missing `token_counter`, non-integer counts and genuine text overages
still raise `MODEL_BUDGET_REQUIRED`. No frozen samples, oracle, ledger history or task
markers are touched.

## Phase 13 M1: setup recommendation and read-only data detection (2026-10-23)

### Change objects

- `backend/app/schemas/settings.py`: new `SetupRecommendation`, `SetupRecommendedBusiness`, `SetupDataState`, `SetupDatabaseData`, `SetupStorageData` DTOs.
- `backend/app/services/settings/setup_state.py`: non-secret recommendation projection and read-only database/storage probes.
- `backend/app/services/settings/management.py`: `setup_recommendation()` and `setup_data_state()` only call the read-only projection/probes.
- `backend/app/api/settings.py`: `GET /api/settings/onboarding/recommendation` and `GET /api/settings/onboarding/data-state`; both remain behind the existing Setup authorization middleware.
- `frontend/src/features/onboarding/SetupHomePage.tsx`: main `/setup` path; database/Redis/path fields move to `/setup/advanced`; AI is optional.
- `frontend/src/app/router.tsx`: `/setup` main path, `/setup/advanced` full wizard.

### Necessity

The accepted V1 wizard exposed database host/port/name/user/schema, Redis host/port/database, absolute storage paths and internal draft/probe terminology on the first screen. Docker already supplies these non-secret values through `DATABASE_URL`, `REDIS_URL` and `STORAGE_ROOT`. M1 projects them without writing, detects existing data before offering creation, and keeps the full wizard reachable for advanced users.

### Assertion changes

- New `tests/unit/settings/test_setup_state.py` (12): recommendation source/auto-apply/no-secret behavior, active-over-environment precedence, missing-source unavailability, all detector states, storage probe read-only behavior, and no-locator failure.
- New `tests/integration/test_settings_setup_state.py` (5): HTTP recommendation/data-state through the real setup boundary, no-token/Bearer/unknown-token rejection, and no config-file writes.
- `frontend/tests/onboarding.spec.ts`: route and copy assertions updated for `/setup/advanced` and plain-language labels; new main-path tests cover recommendation rendering, draft creation, existing-data disablement, saved-draft recovery, and advanced entry. No old security assertion was weakened.
- `frontend/src/features/onboarding/OnboardingPage.test.tsx`: setup-status tests now target `/setup/advanced`; authorization, hash clearing and storage assertions are unchanged.

### Verification

| Check | Result |
| --- | --- |
| T088/T089 unit + HTTP + settings regression | 83 passed |
| Full frontend suite | 32 files / 187 tests passed |
| Live empty Docker environment | recommendation `available=true`, `auto_apply=true`; data-state `new`, `can_initialize=true` |
| Live synthetic existing data | `existing`, `can_initialize=false`; temporary table/file removed and state restored to `new` |
| Unauthorized live GET/POST | `401` for both new GETs and draft creation |
| Real model sends | 0 |

### Boundary

No change to T001–T087, 87/87, T083–T087 evidence or V1 acceptance conclusions. Fields are folded, not deleted; `/setup/advanced` remains editable. The recommendation endpoint never writes a draft or configuration; the data-state endpoint only performs bounded SELECT/`scandir` reads and returns safe codes on failure. Setup authorization, Host/Origin, DPAPI, maintenance authorization and paid-operation confirmations are unchanged. No commit, push or acceptance report is part of this change.

## M1 override equality and conditional setup copy (2026-10-23)

### Change objects

- `backend/app/services/settings/config_service.py`: `_process_env_overrides()` compares saved values with the resolved environment values. Provenance alone is no longer an override; unparseable/missing comparisons remain conservative. `create_draft()` and `activate()` share the same rule.
- `frontend/src/features/onboarding/SetupHomePage.tsx`: top copy is conditional on `recommendation.available`; unavailable environments no longer claim setup is already detected.
- `tests/unit/settings/test_config_persistence.py`: same-value no-ack, different-value still-422, and no-env behavior.
- `frontend/tests/onboarding.spec.ts`: unavailable recommendation shows the conditional copy and keeps only the advanced exit.

### Assertion changes

- Same `STORAGE_ROOT` / `JWT_EXPIRE_MINUTES` values: draft creation and activation succeed without `acknowledge_override`.
- Different environment value: draft creation and activation still raise `OVERRIDE_ACKNOWLEDGEMENT_REQUIRED`.
- No environment value: no process-env source is invented.
- `DEV_MODE=true` in the host `.env` remains a real override versus the recommended default `false`; the backend correctly rejects it without ack. This is not relaxed by the fix.

### Verification

| Check | Result |
| --- | --- |
| `test_config_persistence.py` + `test_settings_management.py` | 66 passed |
| `frontend/tests/onboarding.spec.ts` | 34 passed |
| Ruff | passed |
| Real browser, clean `DEV_MODE=false` volume | draft created without ack; advanced/initialization/activation reached; `/` opened after 9 user clicks but showed `LEARNING_CONFIGURATION_UNAVAILABLE` |
| `/ready` after activation | **503**, storage root `/app/storage/storage` absent |
| Manual test dir creation + backend restart | `/ready` 200 and `/` loaded without learning error; confirms the remaining blocker is storage-root creation, not override logic |
| Real model sends | 0 |

### Boundary

The override fix does not acknowledge true differences. No fields, authorization middleware, DPAPI, Host/Origin or paid-operation gates were changed. A new M1 blocker remains: Docker mounts `/app/storage` but `STORAGE_ROOT=/app/storage/storage`; readiness requires that nested directory, and the normal flow does not create it. This TCR does not claim full browser acceptance. No commit, push or acceptance report file is part of this change.

## Phase 13 M1: STORAGE_ROOT preparation and DEV_MODE recommendation (2026-10-23)

### Change objects

- `backend/app/services/settings/storage_preparation.py`: new bounded helper that creates the selected storage root and verifies it with a temporary, ignored readiness probe.
- `backend/app/services/settings/management.py`: initialization records the environment storage root and calls the preparation helper before target verification/migration; failures end the operation with `STORAGE_ROOT_UNVERIFIED`, `STORAGE_ROOT_UNAVAILABLE` or `STORAGE_PREPARATION_FAILED` without reporting success. `SettingsRuntime.initialization()` passes the profile-verified storage root.
- `scripts/docker-entrypoint.sh`, `Dockerfile`: container startup creates and exports the default/overridden `STORAGE_ROOT` before Uvicorn starts.
- `backend/app/schemas/settings.py`, `backend/app/services/settings/setup_state.py`: `SetupRecommendedBusiness.dev_mode` and the read-only recommendation now project the `DEV_MODE` process value with source `process_env`; active configuration still takes precedence. Invalid values produce `INVALID_DEV_MODE` and are not auto-applied.
- `frontend/src/api/generated.ts`, `frontend/src/features/onboarding/wizardFixtures.ts`, `frontend/tests/onboarding.spec.ts`: generated type and Setup fixture/assertion include the recommended `dev_mode` value.

### Necessity

A fresh Compose volume mounts `/app/storage` but sets `STORAGE_ROOT=/app/storage/storage`. The nested directory was absent, so migration and activation could succeed while business assembly failed and `/ready` stayed 503. Independently, the host `.env` contains `DEV_MODE=true`; the recommendation defaulted to `false`, so the first recommended draft was a real environment override and correctly returned 422. The recommendation must prefill the runtime value rather than weaken override enforcement.

### Assertion changes

- Storage preparation: nested root creation, existing-root preservation, writable probe cleanup, file/relative-path rejection, initialization failure without migration, and integration assertion that API initialization creates the profile storage root.
- Docker lifecycle: the entrypoint-created empty root is removed before initialization; the flow must recreate it and reach `/ready=200` after the deployment restart. A direct image run verifies a nested custom `STORAGE_ROOT`.
- `DEV_MODE`: recommendation source/value, active-configuration precedence, invalid-value safe handling, same-value draft creation without acknowledgement, and manual `dev_mode` change still returning `OVERRIDE_ACKNOWLEDGEMENT_REQUIRED`.
- Frontend passes the recommended `dev_mode` in the main-path draft payload.

### Verification

| Check | Result |
| --- | --- |
| Targeted backend unit/integration tests | 50 passed |
| Config persistence + settings management regression | 66 passed |
| Settings unit/integration regression excluding existing async tests requiring absent `pytest-asyncio` | 820 passed |
| Explicit Docker settings lifecycle, real image/volumes | 1 passed |
| Frontend full suite | 32 files / 188 tests passed |
| Frontend typecheck + OpenAPI type check | passed |
| Ruff | passed |
| Real Chrome, clean `DEV_MODE=false` | 9 core clicks; initialization/activation; controlled backend restart; `/ready=200`; subject created on learning page; 11 total clicks including subject creation |
| Real Chrome, current `.env` value `DEV_MODE=true` | 9 core clicks; draft `dev_mode=true` with no 422; `/ready=200`; subject created; 11 total clicks including subject creation |
| Dependency-unavailable copy (no database/Redis; Docker container only supplies the authorization socket) | conditional copy shown; no automatic create; advanced entry reachable |
| Existing data | `existing`; automatic create disabled; advanced entry present; existing “进入学习” exit left unchanged for M2 |
| Refresh and 401 | refresh recovered draft without extra config writes; onboarding GETs and draft POST returned 401 |
| Real model/Probe/external sends | 0 |

### Boundary

The fix creates only the selected storage root; bucket parents such as `materials/<id>/assets` remain lazily created by the existing file writer. The existing-data read-only probe still does not create directories. Override semantics are unchanged: equal values do not require acknowledgement, different values still return 422. The fresh activation required one controlled backend restart because business assembly only occurs in a new process; this restart is a deployment step, not a user click. No commit, push, T093/M2 work, acceptance report file, front-12-Phase edit, 87/87 edit, or T083–T087 evidence change is included.

## T093/M2 three-screen QuickStart (2026-10-23)

### Change objects

- `frontend/src/features/onboarding/QuickStartPage.tsx`: new `/setup/quickstart` welcome → prepare → enter flow. It reuses the existing recommendation, data-state, onboarding, draft, validation, initialization preview/apply, operation polling, activation, restart and readiness APIs. The only automatic orchestration condition is `recommendation.available && recommendation.auto_apply && data_state.status == "new" && data_state.can_initialize && no draft && no active/pending configuration`. Every call keeps `acknowledge_override: false` and no Probe/credential creation is performed.
- `frontend/src/app/router.tsx`, `frontend/src/app/AppShell.tsx`, `frontend/src/features/onboarding/OnboardingPage.tsx`: add `/setup/quickstart`, make “首次设置” land there, and keep the M1 `/setup` recommendation page and `/setup/advanced` editable wizard reachable. The advanced return button now returns to QuickStart.
- `frontend/src/api/client.ts`: add the public `/ready` poll helper. It returns `true` for 200, `false` for 503, and never attaches the management token.
- `backend/app/services/settings/management.py`: add startup-only `SettingsRuntime.bind_restart_channel()`, which keeps the first channel and never exposes this binding through HTTP.
- `backend/app/deployment/container_control.py`: add `EDUAGENT_CONTAINER_RESTART` opt-in parsing and a single-use channel that lets the `/restart` HTTP response flush, then sends `SIGTERM`; Compose `restart: unless-stopped` starts the new process. Without the opt-in, a non-Docker runtime, or a bound Launcher channel, the existing `manual_restart_required` response is unchanged.
- `docker-compose.yml`: set `EDUAGENT_CONTAINER_RESTART` for the deployment-level controlled restart. This is not an initialization or authorization bypass.
- `frontend/tests/quickstart.spec.ts`, `frontend/src/features/onboarding/wizardFixtures.ts`: 12 synthetic browser-DOM assertions for the complete two-click path, existing data, needs-confirmation, unavailable recommendation, saved draft, initialization failure without automatic retry, activation/draft override 422, restart unavailable, and 401.
- `tests/unit/settings/test_container_restart.py`, `tests/unit/settings/test_settings_restart_channel.py`: explicit opt-in, single-use deferred termination, and first-channel retention.

### Automatic boundary and failure handling

- `new` + `available` + `auto_apply` + no saved configuration/draft: the user click creates one draft, validates it, previews and applies one initialization, activates with no Probe IDs, requests one controlled restart, then polls public `/ready`. The UI shows the four stage labels and succeeds only after `/ready=200`.
- `existing`: no automatic initialization. “进入学习” is shown only when `business.ready` is true and no draft exists; otherwise only the advanced exit is shown. An existing space normally has an active revision, so that fact must not hide the direct-entry exit; it only blocks automatic creation.
- `needs_confirmation`, `unknown`, unavailable recommendation, saved draft, or active/pending configuration without `new`: no automatic creation; clear “去高级设置” exit.
- Any failure stops the flow. “重试” is an explicit click; there is no timer-based retry. A failed initialization clears its receipt so the next explicit retry re-previews instead of silently replaying. A running initialization receipt is polled by its operation ID.
- 401/403 expires the memory-only session and shows the reauthorization notice. `OVERRIDE_ACKNOWLEDGEMENT_REQUIRED` is shown as the real conflict; the three-screen flow never sends `acknowledge_override: true`.

### Verification

| Check | Result |
| --- | --- |
| Frontend `npm run typecheck` + OpenAPI type check | passed |
| Frontend `npm run build` | passed (existing bundle-size warning only) |
| Ruff (changed backend files) | passed |
| Mypy (changed files) | not claimed as passed: 45 pre-existing errors in 9 imported files; none reported on the added lines |
| Frontend QuickStart synthetic tests | 12 passed |
| Full frontend Vitest | 33 files / 200 tests passed |
| Backend new restart tests + T088/T089 setup tests | 16 passed |
| Backend activation/config focused regression | 38 passed |
| Backend settings unit + lifecycle/data-state integration | 814 passed; an earlier run had 15 async failures solely because `pytest-asyncio` was absent; after installing the declared dev dependency the full suite passed |
| Real Chrome, final empty Docker project `eduagent-t093b` | 2 clicks; welcome/prepare/new-state seen; success in 11.037 s; `/ready=200` in 10.781 s; total 12.747 s; final `/` showed “我的科目”; 1 controlled restart; 1 draft / 1 revision / 1 operation; 0 Probe files |
| Real Chrome, existing Docker project `eduagent-t093` | 2 clicks; `existing` state; “进入学习” direct entry; success in 0.125 s; total 2.098 s; final `/` showed “我的科目”; draft/revision/operation counts 1/1/1 before and after, so no setup writes |
| Unauthenticated live GET data-state / POST draft | both 401 `SESSION_REQUIRED` |
| Live override difference with `acknowledge_override=false` | 422 `OVERRIDE_ACKNOWLEDGEMENT_REQUIRED` at draft save; no ack sent |
| Real model / Probe / external sends | 0 |

### Boundary

The three-screen flow adds no backend initialization endpoint and does not auto-initialize existing data. Setup authorization, one-time code exchange, Host/Origin checks, DPAPI/mounted-secret handling, paid-operation confirmation, and override enforcement are unchanged. The M1 `/setup` page and all advanced database/Redis/storage fields remain; `/setup/advanced` is still reachable. The Docker restart channel is explicit deployment configuration, single-use, and only terminates the current process after the restart request has passed the existing management-session and expected-revision checks; without it the previous manual-restart response remains. No commit, push, acceptance report, front-12-Phase edit, 87/87 edit, T088–T092 checkbox change, or T083–T087 evidence change is included.
