"""Non-secret configuration and environment contracts, independent of business DB."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class ProviderCatalogEntry(SafeModel):
    kind: str
    adapter_id: str
    capabilities: list[str]
    lifecycle: str
    canonical_id: str
    parameters: dict[str, Any]


class ProviderCatalogResponse(SafeModel):
    providers: list[ProviderCatalogEntry]


class SessionExchangeResponse(SafeModel):
    management_token: str
    expires_at: datetime
    environment_id: UUID
    instance_id: UUID


class ApiErrorEnvelope(SafeModel):
    code: str
    message: str
    stage: str = "settings"
    field: str | None = None
    retryable: bool = False
    correlation_id: str | None = None


class SetupAvailability(SafeModel):
    configuration: Literal["available", "invalid"]
    settings_authorization: Literal["available", "unavailable"]
    static_resources: str


class SetupReadyResponse(SafeModel):
    status: Literal["ok", "not_ready"]
    service: str = "backend"
    setup: SetupAvailability
    failed_check: str | None = None
    detail: str | None = None


class PublicHealthResponse(SafeModel):
    status: Literal["ok"]
    service: str


class EnvironmentName(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    DEMO = "demo"


class RuntimeKind(StrEnum):
    WINDOWS = "windows"
    PYTHON = "python"
    DOCKER = "docker"


class CredentialReference(SafeModel):
    credential_id: UUID
    version: UUID
    environment_id: UUID
    backend: Literal["dpapi", "mounted_secret", "process_env"]
    purpose: str
    external_secret_id: str | None = None


CredentialState = Literal["absent", "available", "invalid", "unavailable", "revoked", "reentry_required"]


class SecretWrite(SafeModel):
    purpose: Literal["provider", "database", "redis", "jwt"]
    secret_value: SecretStr = Field(repr=False, json_schema_extra={"writeOnly": True})

    @field_validator("secret_value")
    @classmethod
    def nonblank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("凭据不能为空，请明确执行新增、替换或删除。")
        return value


class CredentialStatus(SafeModel):
    credential_ref: UUID
    version: UUID
    status: CredentialState


class DatabaseLocator(SafeModel):
    host: Annotated[str, Field(min_length=1)]
    port: int = Field(default=5432, ge=1, le=65535)
    database: Annotated[str, Field(min_length=1)]
    user: Annotated[str, Field(min_length=1)]
    schema_name: str = Field(
        default="public", validation_alias=AliasChoices("schema", "schema_name"),
        serialization_alias="schema",
    )

    @field_validator("host", "database", "user", "schema_name")
    @classmethod
    def plain_locator(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "/@?#\n\r"):
            raise ValueError("invalid locator")
        return value


class RedisLocator(SafeModel):
    host: Annotated[str, Field(min_length=1)]
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)

    @field_validator("host")
    @classmethod
    def plain_host(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "/@?#\n\r"):
            raise ValueError("invalid locator")
        return value


class BootstrapOptions(SafeModel):
    bind_host: Literal["127.0.0.1", "::1", "0.0.0.0"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class BootstrapSettings(SafeModel):
    environment: EnvironmentName
    environment_id: UUID
    runtime_kind: RuntimeKind
    config_root: Path
    static_root: Path | None = None
    options: BootstrapOptions = Field(default_factory=BootstrapOptions)


class BusinessSettings(SafeModel):
    database_locator: DatabaseLocator | None = None
    database_password_ref: CredentialReference | None = None
    redis_locator: RedisLocator | None = None
    redis_password_ref: CredentialReference | None = None
    storage_root: Path | None = None
    jwt_secret_ref: CredentialReference | None = None
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)
    dev_mode: bool = False

    @field_validator("storage_root")
    @classmethod
    def absolute_storage(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("absolute storage path required")
        return value


class ProviderProfile(SafeModel):
    profile_id: UUID
    profile_revision: UUID
    display_name: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    base_url: str | None = None
    credential_ref: CredentialReference | None = None
    auth_mode: Literal["api_key", "none"] = "api_key"
    declared_capabilities: tuple[str, ...] = ()
    # Registry validates the adapter-specific scalar whitelist before use. No
    # headers, SDK kwargs or secret objects are accepted by the persisted shape.
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    enabled: bool = False
    resource_directory: Path | None = None

    @field_validator("parameters")
    @classmethod
    def registered_parameters(
        cls, value: dict[str, str | int | float | bool | None], info: ValidationInfo,
    ) -> dict[str, str | int | float | bool | None]:
        if not value:
            return {}
        # Import only at validation time: Registry imports these DB-independent
        # shapes, but neither constructs a client nor reads process settings.
        from backend.app.services.settings.provider_registry import (
            ProviderRegistry,
            get_provider_registry,
        )

        owner = info.context.get("provider_registry") if isinstance(info.context, dict) else None
        if not isinstance(owner, ProviderRegistry):
            owner = get_provider_registry()
        try:
            schema = owner.parameter_schema(
                info.data.get("adapter_id", ""), info.data.get("declared_capabilities", ()),
            )
            return schema.model_validate(value).model_dump(exclude_none=True)
        except ValueError:
            raise ValueError("registered non-secret provider parameters required") from None

    @field_validator("display_name", "adapter_id", "model_id")
    @classmethod
    def nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nonblank provider identity required")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def nonsecret_url(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parsed = urlsplit(value)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and not any(ord(character) <= 32 for character in value)
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                )
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("non-secret HTTP endpoint required")
        return value


class TaskBinding(SafeModel):
    task_kind: str
    profile_id: UUID | None = None
    profile_revision: UUID | None = None
    inherit_default: bool = False
    retrieval_mode: str | None = None
    required_capabilities: tuple[str, ...] = ()


class ConfigurationValues(SafeModel):
    bootstrap: BootstrapOptions = Field(default_factory=BootstrapOptions)
    business: BusinessSettings = Field(default_factory=BusinessSettings)
    providers: dict[str, ProviderProfile] = Field(default_factory=dict)
    bindings: dict[str, TaskBinding] = Field(default_factory=dict)
    # Typed policies; arbitrary SDK kwargs/headers are deliberately not accepted.
    policies: Policies = Field(default_factory=lambda: Policies())


class Policies(SafeModel):
    confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    hybrid_weight: float = Field(default=0.5, ge=0, le=1)
    rerank_max_candidates: int = Field(default=5, gt=0)
    rerank_timeout_seconds: float = Field(default=30.0, gt=0)


ConfigurationValues.model_rebuild()


class EnvironmentProfile(BootstrapSettings):
    schema_version: Literal[1] = 1
    storage_root: Path
    cache_root: Path
    log_root: Path
    temp_root: Path
    results_root: Path
    database_locator: DatabaseLocator | None = None
    redis_locator: RedisLocator | None = None
    source_policy: str = "explicit"

    @property
    def credentials_root(self) -> Path:
        return self.config_root.parent / "credentials"

    def validate_paths(self) -> EnvironmentProfile:
        root = self.config_root.parent
        if self.environment_id.int == 0 or not root.is_absolute() or root != root.resolve():
            raise ValueError("ENVIRONMENT_ROOT_UNVERIFIED")
        if self.config_root != root / "config" or self.config_root != self.config_root.resolve():
            raise ValueError("ENVIRONMENT_CONFIG_PATH_MISMATCH")
        for field, directory in (("cache_root", "cache"), ("log_root", "logs"),
                                 ("temp_root", "temp"), ("results_root", "results")):
            path = getattr(self, field)
            if path != root / directory or path != path.resolve():
                raise ValueError("ENVIRONMENT_PATH_MISMATCH")
        if self.credentials_root != self.credentials_root.resolve():
            raise ValueError("ENVIRONMENT_CREDENTIALS_PATH_MISMATCH")
        if not self.storage_root.is_absolute() or self.storage_root != self.storage_root.resolve():
            raise ValueError("ENVIRONMENT_STORAGE_UNVERIFIED")
        if self.storage_root != root / "storage" and not (
            self.environment == EnvironmentName.DEVELOPMENT and self.source_policy == "legacy_compatibility"
        ):
            raise ValueError("ENVIRONMENT_STORAGE_PATH_MISMATCH")
        return self

    @model_validator(mode="after")
    def controlled_paths(self) -> EnvironmentProfile:
        return self.validate_paths()


class DatabaseIdentity(DatabaseLocator):
    instance_id: str


class TestResourceProfile(SafeModel):
    environment: Literal[EnvironmentName.TEST]
    environment_id: UUID
    database_identity: DatabaseIdentity
    maintenance_identity: DatabaseIdentity | None = None
    redis_instance_id: str | None = None
    root: Path


class OwnedResource(SafeModel):
    kind: Literal["schema", "file", "redis_key"]
    target: str
    owner: str
    run_id: UUID
    created_at: datetime


class ResourceManifest(SafeModel):
    environment: EnvironmentName
    environment_id: UUID
    run_id: UUID
    database_identity: DatabaseIdentity | None = None
    redis_instance_id: str | None = None
    root: Path
    resources: tuple[OwnedResource, ...] = ()
    cleanup_state: Literal["not_started", "completed", "failed"] = "not_started"
    residual_targets: tuple[str, ...] = ()
    cleanup_error_type: str | None = None


class FieldSource(SafeModel):
    source: Literal["default", "saved", "mounted_config", "dotenv", "process_env"]
    effective: bool = True


class SegmentError(SafeModel):
    segment: str
    fields: tuple[str, ...]
    code: str = "INVALID_CONFIGURATION"


class ResolvedConfiguration(SafeModel):
    # Invalid segments remain unavailable, never replaced by a lower value.
    segments: dict[str, BootstrapOptions | BusinessSettings | Policies | dict]
    sources: dict[str, FieldSource]
    errors: tuple[SegmentError, ...] = ()


class ConfigurationManifest(SafeModel):
    schema_version: Literal[1] = 1
    environment_id: UUID
    active_revision: UUID | None = None
    pending_restart_revision: UUID | None = None

    @property
    def head_revision(self) -> UUID | None:
        return self.pending_restart_revision or self.active_revision


class ActivationValidation(SafeModel):
    task_kind: str
    profile_revision: UUID
    credential_version: UUID | None = None
    probe_id: UUID
    capability: str
    tested_at: datetime
    accepted_at: datetime

class SettingsRevision(SafeModel):
    schema_version: Literal[1] = 1
    revision_id: UUID
    parent_revision: UUID | None
    environment_id: UUID
    values: ConfigurationValues
    created_at: datetime
    activated_at: datetime | None = None
    source_metadata: dict[str, FieldSource] = Field(default_factory=dict)
    activation_validation: dict[str, ActivationValidation] = Field(default_factory=dict)
    status: Literal["draft", "validated", "active", "pending_restart"] = "draft"


class ReadinessCheck(SafeModel):
    """单项就绪检查：状态 + 稳定安全码 + 固定安全说明。

    `code` 属于既有失败分类（如 `postgres_unavailable`），`detail` 不得包含路径、
    连接串、凭据或异常原文。`unknown` 表示本进程无法判定（例如找不到迁移头文件），
    `skipped` 表示因缺少前置事实而未执行。
    """

    status: Literal["ok", "failed", "unknown", "skipped"]
    code: str | None = None
    detail: str | None = None


class ReadinessChecks(SafeModel):
    """业务前置条件分项；字段集合与 data-model §8 一致。

    `schema` 与 pydantic 的废弃方法名冲突，Python 侧用 `schema_check` + 别名保留 JSON 字段名。
    """

    model_config = ConfigDict(populate_by_name=True)

    business_config: ReadinessCheck
    jwt_reference: ReadinessCheck
    postgres: ReadinessCheck
    vector: ReadinessCheck
    migration: ReadinessCheck
    schema_check: ReadinessCheck = Field(alias="schema")
    redis: ReadinessCheck
    storage: ReadinessCheck


class SetupReadiness(SafeModel):
    ready: bool
    reason: str | None = None


class BusinessPrerequisitesReadiness(SafeModel):
    ready: bool
    configuration_revision: str | None = None
    checks: ReadinessChecks


class BusinessReadiness(SafeModel):
    """Business Ready = 前置依赖 + 装配 + 本进程原恢复启动阶段。"""

    ready: bool
    assembly: Literal["not_mounted", "mounted", "failed"]
    recovery: Literal["not_started", "completed", "failed"]
    reason: str | None = None


class AiProbeView(SafeModel):
    """已持久接纳的检测事实；不重新调用 Probe，也不扩展 10 分钟接纳窗口。"""

    status: Literal["succeeded", "unknown"] = "unknown"
    tested_at: datetime | None = None
    activation_expires_at: datetime | None = None


class AiTaskReadiness(SafeModel):
    """逐任务 AI 就绪：只投影绑定/能力/凭据/接纳/依赖等既有事实。"""

    configured: bool = False
    capability: tuple[str, ...] = ()
    credential: Literal[
        "available", "missing", "unavailable", "not_required", "not_configured"
    ] = "not_configured"
    activation_validation: Literal[
        "accepted", "unverified", "revalidation_required", "not_applicable"
    ] = "not_applicable"
    last_probe: AiProbeView | None = None
    last_runtime_result: str | None = None
    ready: bool = False
    reason: str = "not_configured"
    dependencies: tuple[str, ...] = ()


class AiReadiness(SafeModel):
    """早期 E1 尚未接 AI 计算时必须显式 `not_evaluated`，不得伪造可用性。"""

    evaluated: bool = False
    status: str = "not_evaluated"
    reason: str = "not_evaluated"
    tasks: dict[str, AiTaskReadiness] = Field(default_factory=dict)


class ReadinessReport(SafeModel):
    environment: str | None = None
    setup: SetupReadiness
    business_prerequisites: BusinessPrerequisitesReadiness
    business: BusinessReadiness
    ai: AiReadiness = Field(default_factory=AiReadiness)
    static_resources: str = "not_bundled"
    restart_required: bool = False
    pending_revision: str | None = None
    candidate_diagnostics: ReadinessChecks | None = None


class SettingsSafeView(SafeModel):
    """T219 只读安全视图；字段来源/凭据状态/绑定由 T220 扩展。"""

    environment: str | None = None
    environment_id: str | None = None
    configuration: Literal["available", "not_configured"]
    active_revision: str | None = None
    pending_revision: str | None = None
    restart_required: bool = False
    static_resources: str = "not_bundled"
    business_assembly: Literal["not_mounted", "mounted", "failed"] = "not_mounted"


class DiagnosticsRequest(SafeModel):
    """只允许四项核心依赖检查；不接受任意 URL/命令。"""

    checks: tuple[Literal["postgres", "redis", "storage", "migrations"], ...] = (
        "postgres",
        "redis",
        "storage",
        "migrations",
    )

    @field_validator("checks")
    @classmethod
    def unique_checks(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value:
            raise ValueError("至少选择一项检查。")
        if len(set(value)) != len(value):
            raise ValueError("检查项不能重复。")
        return value


class DiagnosticsReport(SafeModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, ReadinessCheck]
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# T220：完整 Settings API DTO（settings-api §2/§3）
# ---------------------------------------------------------------------------


class CredentialStatusView(SafeModel):
    """凭据状态投影；只暴露 opaque 引用与可用性，不含任何秘密。

    `environment_id`/`backend` 用于让客户端拼出同环境的完整 `CredentialReference`；
    两者都不是秘密。
    """

    credential_ref: UUID
    version: UUID
    purpose: str
    status: CredentialState
    environment_id: UUID | None = None
    backend: Literal["dpapi", "mounted_secret", "process_env"] | None = None
    external_secret_id: str | None = None


class SettingsView(SafeModel):
    """SettingsView：active/pending 修订、非敏感值、来源与凭据状态。"""

    environment: str | None = None
    environment_id: str | None = None
    configuration: Literal["available", "not_configured", "invalid"]
    active_revision: str | None = None
    pending_revision: str | None = None
    restart_required: bool = False
    values: ConfigurationValues | None = None
    pending_values: ConfigurationValues | None = None
    effective_values: ConfigurationValues | None = None
    effective_errors: tuple[SegmentError, ...] = ()
    sources: dict[str, FieldSource] = Field(default_factory=dict)
    credential_statuses: tuple[CredentialStatusView, ...] = ()
    warnings: tuple[str, ...] = ()
    business_assembly: Literal["not_mounted", "mounted", "failed"] = "not_mounted"
    static_resources: str = "not_bundled"


class DraftCreateRequest(SafeModel):
    """保存草稿；秘密不进 `changes`，必须走凭据端点或显式绑定。

    `changes` 是部分非敏感字段：未给出的字段不会因此被重置为默认值以外的内容，
    并统一经 `ConfigurationValues` 完整校验（未知/秘密字段 422）。
    """

    base_revision: UUID | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    acknowledge_override: bool = False


class DraftPatchRequest(SafeModel):
    # 首个草稿的父修订为 null；null 是合法期望值，不是缺失。
    expected_revision: UUID | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    acknowledge_override: bool = False


class DraftView(SafeModel):
    revision_id: UUID
    parent_revision: UUID | None = None
    status: Literal["draft", "validated"]
    created_at: datetime
    values: ConfigurationValues
    restart_required: bool = False


class DraftValidateRequest(SafeModel):
    expected_revision: UUID | None = None


class ValidationIssue(SafeModel):
    field: str
    code: str
    message: str


class ValidationReport(SafeModel):
    valid: bool
    revision_id: UUID
    errors: tuple[ValidationIssue, ...] = ()
    affected_tasks: tuple[str, ...] = ()
    restart_required: bool = False


class CredentialCreateRequest(SafeModel):
    purpose: Literal["provider", "database", "redis", "jwt"]
    secret_value: SecretStr = Field(repr=False, json_schema_extra={"writeOnly": True})

    @field_validator("secret_value")
    @classmethod
    def nonblank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("凭据不能为空。")
        return value


class CredentialVersionRequest(SafeModel):
    secret_value: SecretStr = Field(repr=False, json_schema_extra={"writeOnly": True})

    @field_validator("secret_value")
    @classmethod
    def nonblank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("凭据不能为空。")
        return value


class CredentialBindingRequest(SafeModel):
    """把已存在的安全引用绑定到草稿中的 Profile；不写入秘密。"""

    profile_id: UUID
    credential_ref: UUID
    version: UUID


class ProbeCreateRequest(SafeModel):
    draft_revision: UUID
    profile_id: UUID
    capability: str = Field(min_length=1, max_length=64)
    allow_billable: bool = False
    timeout_seconds: float = Field(default=15, ge=5, le=30)


class ProbeView(SafeModel):
    """Probe 状态；不返回 SDK 正文或凭据，取消可能已计费。"""

    probe_id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    result_code: str
    message: str
    capability: str
    profile_id: UUID
    tested_at: datetime
    expires_at: datetime | None = None
    external_requests: int = 0
    may_have_been_billed: bool = False
    retryable: bool = False
    retry_after: float | None = None
    correlation_id: str


class ActivationRequest(SafeModel):
    expected_active_revision: UUID | None = None
    probe_ids: tuple[UUID, ...] = ()
    acknowledge_override: bool = False

    @field_validator("probe_ids")
    @classmethod
    def unique_probes(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Probe 不能重复。")
        return value


class ActivationView(SafeModel):
    revision_id: UUID
    status: Literal["active", "pending_restart"]
    restart_required: bool
    activation_keys: tuple[str, ...] = ()


class SetupRecommendedBusiness(SafeModel):
    """T088 推荐的非秘密业务定位；不含密码、引用或连接串。"""

    database_locator: DatabaseLocator | None = None
    redis_locator: RedisLocator | None = None
    storage_root: Path | None = None
    dev_mode: bool = False


class SetupRecommendation(SafeModel):
    """T088 只读推荐值；`auto_apply` 只表示可安全预填，不代表已写入。"""

    available: bool
    auto_apply: bool = False
    mode: Literal["docker", "python", "unknown"] = "unknown"
    business: SetupRecommendedBusiness = Field(default_factory=SetupRecommendedBusiness)
    sources: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None
    warnings: tuple[str, ...] = ()


class SetupDatabaseData(SafeModel):
    """T089 数据库只读探测结果；不暴露连接串或异常原文。"""

    status: Literal["empty", "existing", "needs_confirmation", "unavailable"]
    table_count: int | None = None
    business_tables: tuple[str, ...] = ()
    code: str | None = None
    detail: str | None = None


class SetupStorageData(SafeModel):
    """T089 存储根只读探测结果；只报告条目数量，不枚举文件名。"""

    status: Literal["empty", "existing", "unavailable"]
    entry_count: int | None = None
    code: str | None = None
    detail: str | None = None


class SetupDataState(SafeModel):
    """T089 首次设置前的已有数据状态；`new` 才允许自动初始化。"""

    status: Literal["new", "existing", "needs_confirmation", "unknown"]
    can_initialize: bool
    reason: str | None = None
    database: SetupDatabaseData
    storage: SetupStorageData
    checked_at: datetime


class OnboardingView(SafeModel):
    revision: int
    current_step: int = 0
    draft_revision: UUID | None = None
    updated_at: datetime | None = None
    completed_steps: tuple[int, ...] = ()


class OnboardingUpdateRequest(SafeModel):
    expected_revision: int | None = None
    current_step: int = Field(ge=0, le=32)
    draft_revision: UUID | None = None


class OnboardingReport(SafeModel):
    """向导进度 + 当前真实就绪；步骤前进不代表依赖通过。"""

    progress: OnboardingView
    readiness: ReadinessReport
    draft: DraftView | None = None
    probes: tuple[ProbeView, ...] = ()
    initialization: OperationView | None = None


class InitializationPreviewRequest(SafeModel):
    draft_revision: UUID


class InitializationPreview(SafeModel):
    preview_id: UUID
    draft_revision: UUID
    environment: str
    target_identity: str
    current_head: str | None = None
    target_head: str | None = None
    allowed: bool
    destructive: bool = False
    steps: tuple[str, ...] = ()
    expires_at: datetime


class InitializationApplyRequest(SafeModel):
    confirmation_id: UUID
    expected_revision: UUID


class OperationView(SafeModel):
    operation_id: UUID
    kind: Literal["initialization", "restart"]
    status: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    steps: tuple[str, ...] = ()
    error_code: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


OnboardingReport.model_rebuild()


class RestartRequest(SafeModel):
    expected_revision: UUID


class RestartView(SafeModel):
    status: Literal["restart_requested", "manual_restart_required"]
    reason: str
    pending_revision: str | None = None


class ExportRequest(SafeModel):
    include: tuple[Literal["configuration", "readiness", "versions"], ...] = (
        "configuration",
        "readiness",
        "versions",
    )

    @field_validator("include")
    @classmethod
    def unique_sections(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("至少选择一项导出内容。")
        if len(set(value)) != len(value):
            raise ValueError("导出项不能重复。")
        return value


class LegacyImportPreviewRequest(SafeModel):
    source_id: str = Field(min_length=1, max_length=64)


class LegacyImportApplyRequest(SafeModel):
    preview_id: UUID
    confirm: bool = False


class AdoptionPreviewRequest(SafeModel):
    expected_identity_revision: int | None = None
    proposed_identity: dict[str, Any]
    evidence: tuple[dict[str, Any], ...] = ()
    operator_attestation: dict[str, Any] = Field(default_factory=dict)


class AdoptionConfirmRequest(SafeModel):
    preview_id: UUID
    expected_identity_revision: int | None = None
    confirm: bool = False


class IndexIdentityView(BaseModel):
    """索引身份只读视图；保留服务端口返回的额外非敏感字段。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    kb_id: str
    status: str
    dimension: int | None = None
    stored_dimension: int | None = None
    model_id: str | None = None
    model_version: str | None = None
    provider: str | None = None
    adapter: str | None = None
    identity_revision: int | None = None
    adoption: dict[str, Any] | None = None
    audits: tuple[dict[str, Any], ...] = ()
    verified: bool = False


class IndexIdentityList(SafeModel):
    identities: tuple[IndexIdentityView, ...] = ()



class RecoveryRevisionView(SafeModel):
    revision_id: UUID
    available: bool
    created_at: datetime | None = None
    status: str | None = None
    values: ConfigurationValues | None = None
    error_code: str | None = None


class RecoveryView(SafeModel):
    current_revision: UUID | None = None
    damaged: bool = False
    manifest_fingerprint: str | None = None
    revisions: tuple[RecoveryRevisionView, ...] = ()


class RecoveryRestoreRequest(SafeModel):
    revision_id: UUID
    expected_revision: UUID | None = None
    expected_manifest_fingerprint: str | None = None
    confirm: bool = False


class LegacySourceList(SafeModel):
    sources: tuple[str, ...] = ()


class AdoptionPreview(SafeModel):
    preview_id: UUID
    environment_id: str
    kb_id: str
    expected_identity_revision: int | None
    previous_status: str
    previous_identity: dict[str, Any] | None
    proposed_identity: dict[str, Any]
    accepted_identity: dict[str, Any]
    verified_facts: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]
    operator_attestation: dict[str, Any]
    expires_at: datetime
    requires_confirmation: bool
    modifies_vectors: bool
