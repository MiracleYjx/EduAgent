"""Settings API 边界：T217 授权 + T219 只读 + T220 管理面。

授权由 `core.settings_security` 的中间件在路由前执行：业务 JWT/RBAC 无权进入，
Setup 令牌也不能授权任何 `/api` 业务端点。T220 只把已有领域服务接到真实 HTTP，
不在这里复制校验、不读写业务数据、不调用模型；秘密只 write-only。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.settings_security import (
    SETTINGS_PATH_PREFIX,
    SettingsSecurityError,
    SetupSecurityService,
    error_response,
    require_management_session,
)
from backend.app.deployment.configuration_editor import LegacyImportPreview
from backend.app.schemas.settings import (
    ActivationRequest,
    ActivationView,
    AdoptionConfirmRequest,
    AdoptionPreview,
    AdoptionPreviewRequest,
    ApiErrorEnvelope,
    CredentialBindingRequest,
    CredentialCreateRequest,
    CredentialStatusView,
    CredentialVersionRequest,
    DiagnosticsReport,
    DiagnosticsRequest,
    DraftCreateRequest,
    DraftPatchRequest,
    DraftValidateRequest,
    DraftView,
    ExportRequest,
    IndexIdentityList,
    IndexIdentityView,
    InitializationApplyRequest,
    InitializationPreview,
    InitializationPreviewRequest,
    LegacyImportApplyRequest,
    LegacyImportPreviewRequest,
    LegacySourceList,
    OnboardingReport,
    OnboardingUpdateRequest,
    OnboardingView,
    OperationView,
    ProbeCreateRequest,
    ProbeView,
    ProviderCatalogResponse,
    ReadinessReport,
    RecoveryRestoreRequest,
    RecoveryView,
    RestartRequest,
    RestartView,
    SessionExchangeResponse,
    SettingsView,
    ValidationIssue,
    ValidationReport,
)
from backend.app.services.settings import readiness
from backend.app.services.settings.config_service import ConfigServiceError
from backend.app.services.settings.index_identity import IndexIdentityError
from backend.app.services.settings.management import (
    SettingsManagementError,
    SettingsRuntime,
    status_for,
)
from backend.app.services.settings.mounted_secrets import MountedSecretError
from backend.app.services.settings.probe_service import ProbeRequestError, ProbeResult
from backend.app.services.settings.provider_registry import RegistryValidationError


class SafeSettingsRoute(APIRoute):
    """设置输入错误不回显 input、秘密、未知字段名或原始 JSON。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error_response("INVALID_CONFIGURATION", 422, "validation")

        return safe_handler


router = APIRouter(
    prefix=SETTINGS_PATH_PREFIX, tags=["settings"], route_class=SafeSettingsRoute,
    responses={
        **{status: {"model": ApiErrorEnvelope} for status in (401, 403, 409, 503)},
        # HTTPStatus's 422 phrase differs across supported Python versions.
        422: {"model": ApiErrorEnvelope, "description": "Unprocessable Content"},
    },
)

#: 稳定错误码 → 固定中文说明；不含输入、路径或异常原文。
_MESSAGES: dict[str, str] = {
    "INVALID_CONFIGURATION": "配置结构无效，请检查字段取值。",
    "BUSINESS_CONFIGURATION_INCOMPLETE": "业务配置尚不完整，缺少数据库、Redis、存储或 JWT 引用。",
    "JWT_SECRET_TOO_WEAK": "JWT 签名密钥必须为非空且至少 32 个字符，未保存激活。",
    "CREDENTIAL_UNAVAILABLE": "目标凭据不可用，请重新录入或选择其他版本。",
    "CREDENTIAL_NOT_FOUND": "未找到该凭据引用。",
    "CREDENTIAL_RECOVERY_REQUIRED": "凭据库无法核实，需按恢复流程显式重建。",
    "CREDENTIAL_BACKEND_UNAVAILABLE": "凭据后端不可用，未降级为明文或匿名连接。",
    "EXTERNALLY_MANAGED": "外部 Secret 由部署配置管理，本接口不能写入。",
    "IN_USE": "该凭据版本仍被有效、待重启或非终态运行引用。",
    "REFERENCE_INDEX_UNAVAILABLE": "无法核实凭据引用事实，拒绝删除。",
    "REVISION_CONFLICT": "修订已被更新，请重新读取后再提交。",
    "CONFIG_WRITER_LOCKED": "另一个进程正在写入配置。",
    "PROBE_REQUIRED": "新增或变更的 AI 能力需要一次成功的检测。",
    "PROBE_EXPIRED": "检测证据已过期，请重新检测。",
    "PROBE_NOT_FOUND": "未找到该检测记录。",
    "PROBE_SERVICE_UNAVAILABLE": "检测服务不可用，请检查凭据后端。",
    "BILLABLE_CONFIRMATION_REQUIRED": "外部检测可能产生费用，需显式确认。",
    "ACTIVATION_GATE_REQUIRED": "业务配置未通过激活门禁。",
    "LEGACY_RUN_ACTIVATION_BLOCKED": "存在无快照的非终态运行，拒绝相关模型或策略激活。",
    "LEGACY_RUN_INDEX_UNAVAILABLE": "无法核实遗留运行事实，已闭锁相关激活。",
    "IDENTITY_CONFLICT": "索引身份已变化，请重新读取后重试。",
    "INDEX_BUSY": "索引正在摄取或身份已变化，请稍后重试。",
    "EMBEDDING_INCOMPATIBLE": "目标索引维度或语义身份不兼容。",
    "EVIDENCE_INSUFFICIENT": "历史接纳证据不足。",
    "EVIDENCE_CONFLICT": "证据与拟接纳身份不一致。",
    "CONFIRMATION_REQUIRED": "该操作需要显式确认。",
    "OPERATOR_REQUIRED": "缺少可信操作者会话。",
    "PREVIEW_EXPIRED": "预览已过期，请重新预览。",
    "PREVIEW_NOT_FOUND": "未找到该预览或确认记录。",
    "INITIALIZATION_RUNNER_UNAVAILABLE": "当前部署没有受控迁移执行器，请按运行方式执行已审查迁移。",
    "DEPENDENCY_UNAVAILABLE": "目标依赖不可用，无法完成该设置操作。",
    "OPERATION_NOT_FOUND": "未找到该操作记录。",
    "CORRUPT_ONBOARDING": "向导进度无法解析，未自动覆盖。",
    "CORRUPT_OPERATION": "操作记录无法解析。",
    "INVALID_LEGACY_IMPORT_SOURCE": "旧配置来源不在本机允许列表内。",
    "LEGACY_SOURCE_NOT_ALLOWED": "旧配置来源不在本机允许列表内。",
    "SETTINGS_STORAGE_UNAVAILABLE": "设置存储不可用，请检查数据目录与写入权限。",
    "SETTINGS_SECURITY_UNAVAILABLE": "设置授权服务不可用。",
    "INVALID_SECRET_WRITE": "凭据写入无效。",
    "CREDENTIAL_DELETE_FAILED": "凭据删除失败，原密文保留。",
    "PROFILE_NOT_FOUND": "草稿中不存在该 Provider Profile。",
    "CAPABILITY_UNSUPPORTED": "目标 Provider 未声明该能力。",
    "ADAPTER_UNSUPPORTED": "目标 Provider 不受 Registry 支持。",
    "PROFILE_DISABLED": "目标 Profile 未启用。",
}
_STAGE_MESSAGES = {
    "credentials": "凭据操作未完成，请检查输入或凭据后端。",
    "activation": "配置未能激活，原生效配置保持不变。",
    "draft": "草稿未能保存，请检查字段。",
    "validation": "配置校验未通过。",
    "initialization": "初始化操作未完成。",
}


class SessionExchangeRequest(BaseModel):
    """交换请求体：一次性码是 write-only SecretStr，错误不回显、不落日志。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # 长度只做粗粒度边界校验；真实有效性由授权服务判断，避免用格式差异泄露信息。
    one_time_code: SecretStr = Field(min_length=1, max_length=256)


@router.post("/session/exchange", response_model=SessionExchangeResponse, summary="用本机一次性授权码换取短期管理令牌")
def exchange_management_session(
    payload: SessionExchangeRequest, request: Request
) -> Response:
    """交换一次性码；敏感操作必须携带合法 Origin。"""

    service = getattr(request.app.state, "setup_security", None)
    if not isinstance(service, SetupSecurityService):
        return error_response(
            SettingsSecurityError(
                "SETTINGS_SECURITY_UNAVAILABLE",
                "设置授权服务不可用。",
                status_code=503,
            )
        )
    try:
        source = service.policy.check_request(request)
        service.policy.check_origin(request, source=source, required=True)
        session = service.exchange(
            payload.one_time_code.get_secret_value(), peer=source.peer
        )
    except SettingsSecurityError as error:
        return error_response(error)
    return JSONResponse(
        status_code=200,
        content={
            "management_token": session.token,
            "expires_at": session.expires_at.isoformat(),
            "environment_id": str(session.environment_id),
            "instance_id": str(session.instance_id),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# 共享辅助
# ---------------------------------------------------------------------------


def _runtime(request: Request) -> SettingsRuntime:
    runtime = getattr(request.app.state, "settings_runtime", None)
    if not isinstance(runtime, SettingsRuntime):
        raise SettingsManagementError(
            "SETTINGS_STORAGE_UNAVAILABLE", stage="config", status_code=503
        )
    return runtime


def _static_resources(request: Request) -> str:
    context = getattr(request.app.state, "bootstrap", None)
    return getattr(context, "static_resources", "not_bundled") if context else "not_bundled"


def _readiness_inputs(request: Request) -> readiness.ReadinessInputs:
    """有运行时时以已保存修订为准；没有运行时保持 T219 的安全回退。"""

    runtime = getattr(request.app.state, "settings_runtime", None)
    if isinstance(runtime, SettingsRuntime):
        return runtime.readiness_inputs(request.app)
    application = request.app
    return readiness.inputs_for_app(
        application, business_settings=getattr(application.state, "settings", None)
    )


def _ai_readiness(request: Request) -> Any:
    runtime = getattr(request.app.state, "settings_runtime", None)
    return runtime.ai_readiness() if isinstance(runtime, SettingsRuntime) else None


def _error_response(code: str, status_code: int, stage: str) -> JSONResponse:
    message = _MESSAGES.get(code) or _STAGE_MESSAGES.get(stage) or "设置操作未完成。"
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "stage": stage,
            "retryable": status_code in {429, 503},
            "correlation_id": uuid4().hex,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _management_error_handler(_request: Request, exc: Exception) -> Response:
    code = str(getattr(exc, "code", None) or type(exc).__name__)
    status = int(getattr(exc, "status_code", 0) or status_for(code))
    return _error_response(code, status, str(getattr(exc, "stage", "settings")))


def install_settings_api(app: Any) -> None:
    """注册设置管理错误转换；不改业务异常语义。"""

    for exc_type in (
        SettingsManagementError,
        ConfigServiceError,
        ProbeRequestError,
        IndexIdentityError,
        RegistryValidationError,
        MountedSecretError,
    ):
        app.add_exception_handler(exc_type, _management_error_handler)


def _draft_view(revision: Any) -> DraftView:
    return DraftView(
        revision_id=revision.revision_id,
        parent_revision=revision.parent_revision,
        status="validated" if revision.status == "validated" else "draft",
        created_at=revision.created_at,
        values=revision.values,
        restart_required=False,
    )


def _probe_view(result: ProbeResult) -> ProbeView:
    return ProbeView(
        probe_id=result.probe_id,
        status=result.status,
        result_code=result.safe_error_code,
        message=result.message,
        capability=result.capability,
        profile_id=result.profile_id,
        tested_at=result.tested_at,
        expires_at=result.expires_at,
        external_requests=result.external_requests,
        may_have_been_billed=result.may_have_been_billed,
        retryable=result.retryable,
        retry_after=result.retry_after,
        correlation_id=result.correlation_id,
    )


_KIND_BY_CAPABILITY = {
    "text": "llm",
    "json_object": "llm",
    "embedding": "embedding",
    "vision": "vision",
    "llm_rerank": "rerank",
    "local_rerank": "rerank",
    "ocr": "ocr",
}


# ---------------------------------------------------------------------------
# 只读视图与就绪
# ---------------------------------------------------------------------------


@router.get("/", response_model=SettingsView, summary="设置视图")
def settings_view(request: Request) -> SettingsView:
    application = request.app
    runtime = getattr(application.state, "settings_runtime", None)
    assembly = getattr(application.state, "business_assembly", "not_assembled")
    static_resources = _static_resources(request)
    if isinstance(runtime, SettingsRuntime):
        return runtime.view(
            assembly=assembly, static_resources=static_resources, app=application
        )
    inputs = _readiness_inputs(request)
    return SettingsView(
        environment=inputs.environment,
        environment_id=inputs.environment_id,
        configuration=(
            "available" if inputs.business_settings is not None else "not_configured"
        ),
        active_revision=inputs.configuration_revision,
        pending_revision=inputs.pending_revision,
        restart_required=inputs.pending_revision is not None,
        static_resources=static_resources,
        business_assembly=readiness.assembly_state(assembly),
    )


@router.get("/providers", response_model=ProviderCatalogResponse, summary="实际 Registry 能力与参数 Schema")
def settings_providers(request: Request) -> dict[str, Any]:
    return {"providers": list(_runtime(request).provider_catalog())}


@router.get("/readiness", response_model=ReadinessReport, summary="分层就绪报告")
def readiness_report(request: Request) -> ReadinessReport:
    """查询不触发 Probe/模型；AI 段只投影已持久化的逐任务事实。"""

    return readiness.evaluate_readiness(
        _readiness_inputs(request), ai=_ai_readiness(request)
    )


@router.post("/diagnostics", response_model=DiagnosticsReport, summary="核心依赖短时诊断")
def dependency_diagnostics(
    payload: DiagnosticsRequest, request: Request
) -> DiagnosticsReport:
    """仅 postgres/redis/storage/migrations 四项；不接受任意 URL 或命令。"""

    return readiness.run_diagnostics(_readiness_inputs(request), tuple(payload.checks))


@router.post("/diagnostics/export", summary="脱敏导出设置与就绪事实")
def export_diagnostics(payload: ExportRequest, request: Request) -> Response:
    sections = _runtime(request).export_sections(request.app, tuple(payload.include))
    body = json.dumps(sections, ensure_ascii=False, indent=2, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="eduagent-settings-export.json"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


# ---------------------------------------------------------------------------
# 草稿 / 校验 / 激活
# ---------------------------------------------------------------------------


@router.post("/drafts", status_code=201, response_model=DraftView, summary="保存非敏感草稿")
def create_draft(payload: DraftCreateRequest, request: Request) -> Response:
    revision = _runtime(request).create_draft(
        base_revision=payload.base_revision,
        changes=payload.changes,
        acknowledge_override=payload.acknowledge_override,
    )
    return JSONResponse(status_code=201, content=_draft_view(revision).model_dump(mode="json", by_alias=True))


@router.patch("/drafts/{draft_id}", summary="更新草稿并产生新 revision")
def update_draft(
    draft_id: UUID, payload: DraftPatchRequest, request: Request
) -> DraftView:
    revision = _runtime(request).update_draft(
        draft_id,
        expected_revision=payload.expected_revision,
        changes=payload.changes,
        acknowledge_override=payload.acknowledge_override,
    )
    return _draft_view(revision)


@router.post("/drafts/{draft_id}/validate", response_model=ValidationReport, summary="校验草稿")
def validate_draft(
    draft_id: UUID, payload: DraftValidateRequest, request: Request
) -> ValidationReport:
    runtime = _runtime(request)
    try:
        return runtime.validate_draft(
            draft_id, expected_revision=payload.expected_revision
        )
    except SettingsManagementError as error:
        if error.code in {"REVISION_CONFLICT", "CONFIG_WRITER_LOCKED"}:
            raise
        return ValidationReport(
            valid=False,
            revision_id=draft_id,
            errors=(
                ValidationIssue(
                    field="configuration",
                    code=error.code,
                    message=_MESSAGES.get(error.code, "配置校验未通过。"),
                ),
            ),
        )


@router.post(
    "/drafts/{draft_id}/activate", response_model=ActivationView,
    responses={202: {"model": ActivationView}},
    summary="激活草稿（active 或 pending_restart）",
)
def activate_draft(
    draft_id: UUID, payload: ActivationRequest, request: Request
) -> Response:
    view: ActivationView = _runtime(request).activate(
        draft_id,
        probe_ids=payload.probe_ids,
        acknowledge_override=payload.acknowledge_override,
        expected_active_revision=payload.expected_active_revision,
    )
    return JSONResponse(
        status_code=202 if view.status == "pending_restart" else 200,
        content=view.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# 凭据与绑定
# ---------------------------------------------------------------------------


@router.post("/credentials", status_code=201, response_model=CredentialStatusView, summary="写入新凭据（write-only）")
def create_credential(payload: CredentialCreateRequest, request: Request) -> Response:
    reference = _runtime(request).create_credential(
        payload.purpose, payload.secret_value
    )
    view = CredentialStatusView(
        credential_ref=reference.credential_id,
        version=reference.version,
        purpose=reference.purpose,
        status="available",
        environment_id=reference.environment_id,
        backend=reference.backend,
    )
    return JSONResponse(status_code=201, content=view.model_dump(mode="json"))


@router.post("/credentials/{credential_ref}/versions", status_code=201, response_model=CredentialStatusView, summary="新增候选凭据版本")
def create_credential_version(
    credential_ref: UUID, payload: CredentialVersionRequest, request: Request
) -> Response:
    reference = _runtime(request).add_credential_version(
        credential_ref, payload.secret_value
    )
    view = CredentialStatusView(
        credential_ref=reference.credential_id,
        version=reference.version,
        purpose=reference.purpose,
        status="available",
        environment_id=reference.environment_id,
        backend=reference.backend,
    )
    return JSONResponse(status_code=201, content=view.model_dump(mode="json"))


@router.delete(
    "/credentials/{credential_ref}/versions/{version}",
    status_code=204,
    summary="删除未被引用的凭据版本",
)
def delete_credential_version(
    credential_ref: UUID, version: UUID, request: Request
) -> Response:
    _runtime(request).delete_credential_version(credential_ref, version)
    return Response(status_code=204)


@router.post("/drafts/{draft_id}/credential-bindings", summary="把安全引用绑定到草稿 Profile")
def bind_credential(
    draft_id: UUID, payload: CredentialBindingRequest, request: Request
) -> DraftView:
    revision = _runtime(request).bind_credential(draft_id, binding=payload)
    return _draft_view(revision)


def _require_head(request: Request) -> UUID | None:
    contract = _runtime(request)._require_config()
    manifest = contract.read_manifest()
    return manifest.head_revision


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


@router.post("/probes", status_code=202, summary="对单个能力做一次受控检测")
async def create_probe(payload: ProbeCreateRequest, request: Request) -> Response:
    runtime = _runtime(request)
    contract = runtime._require_config()
    draft = contract.get_draft(payload.draft_revision)
    profile = draft.values.providers.get(str(payload.profile_id))
    if profile is None:
        raise SettingsManagementError("PROFILE_NOT_FOUND", status_code=404)
    kind = _KIND_BY_CAPABILITY.get(payload.capability)
    if kind is None:
        raise SettingsManagementError("CAPABILITY_UNSUPPORTED")
    try:
        entry = runtime.registry.entry(kind, profile.adapter_id)
    except RegistryValidationError:
        raise SettingsManagementError("ADAPTER_UNSUPPORTED") from None
    if not profile.enabled:
        raise SettingsManagementError("PROFILE_DISABLED")
    if not payload.allow_billable and entry.lifecycle == "owned_per_run":
        raise SettingsManagementError("BILLABLE_CONFIRMATION_REQUIRED")
    # 只有显式写请求才能按需开户；读取已有 Probe/向导绝不初始化凭据库。
    runtime.credential_store()
    result = await runtime.start_probe(
        candidate_revision=payload.draft_revision,
        profile=profile,
        capability=payload.capability,
        allow_billable=payload.allow_billable,
        timeout_seconds=payload.timeout_seconds,
    )
    return JSONResponse(
        status_code=202, content=_probe_view(result).model_dump(mode="json")
    )


@router.get("/probes/{probe_id}", response_model=ProbeView, summary="读取检测结果")
def get_probe(probe_id: UUID, request: Request) -> ProbeView:
    return _probe_view(_runtime(request).probes().get(probe_id))


@router.delete("/probes/{probe_id}", response_model=ProbeView, summary="请求取消检测")
def cancel_probe(probe_id: UUID, request: Request) -> Response:
    result = _runtime(request).probes().cancel(probe_id)
    return JSONResponse(
        status_code=202 if result.status == "running" else 200,
        content=_probe_view(result).model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# 向导 / 初始化 / 操作 / 重启
# ---------------------------------------------------------------------------


@router.get("/onboarding", response_model=OnboardingReport, summary="向导进度与真实诊断")
def get_onboarding(request: Request) -> OnboardingReport:
    runtime = _runtime(request)
    progress = runtime.onboarding()
    draft = runtime.onboarding_draft(progress.draft_revision)
    return OnboardingReport(
        progress=progress,
        readiness=readiness.evaluate_readiness(
            runtime.readiness_inputs(request.app), ai=runtime.ai_readiness()
        ),
        draft=_draft_view(draft) if draft is not None else None,
        probes=tuple(_probe_view(result) for result in runtime.existing_probes(progress.draft_revision)),
        initialization=runtime.operations().latest_initialization(progress.draft_revision),
    )


@router.patch("/onboarding", response_model=OnboardingView, summary="只保存向导流程")
def update_onboarding(
    payload: OnboardingUpdateRequest, request: Request
) -> OnboardingView:
    return _runtime(request).update_onboarding(
        expected_revision=payload.expected_revision,
        current_step=payload.current_step,
        draft_revision=payload.draft_revision,
    )


@router.post(
    "/initialization/preview",
    response_model=InitializationPreview,
    summary="非破坏性迁移预览",
)
def preview_initialization(
    payload: InitializationPreviewRequest, request: Request
) -> InitializationPreview:
    return _runtime(request).initialization().preview(payload.draft_revision)


@router.post("/initialization/apply", status_code=202, summary="执行已预览的允许迁移")
def apply_initialization(
    payload: InitializationApplyRequest, request: Request
) -> OperationView:
    return _runtime(request).initialization().apply(
        payload.confirmation_id, payload.expected_revision
    )


@router.get("/operations/{operation_id}", response_model=OperationView, summary="操作状态")
def get_operation(operation_id: UUID, request: Request) -> OperationView:
    return _runtime(request).operations().get(operation_id)


@router.post("/restart", summary="请求受控重启或给出手工指引")
def request_restart(payload: RestartRequest, request: Request) -> RestartView:
    status, reason, pending = _runtime(request).restart(
        expected_revision=payload.expected_revision
    )
    return RestartView(
        status="restart_requested" if status == "restart_requested" else "manual_restart_required",
        reason=reason,
        pending_revision=pending,
    )


# ---------------------------------------------------------------------------
# 旧配置导入
# ---------------------------------------------------------------------------


@router.post(
    "/legacy-import/preview",
    response_model=LegacyImportPreview,
    summary="旧 .env 显式导入预览",
)
def preview_legacy_import(
    payload: LegacyImportPreviewRequest, request: Request
) -> LegacyImportPreview:
    importer = _runtime(request).legacy_importer()
    return importer.preview(
        source_id=payload.source_id, expected_revision=_require_head(request)
    )


@router.post("/legacy-import/apply", summary="确认导入为新草稿")
def apply_legacy_import(
    payload: LegacyImportApplyRequest, request: Request
) -> DraftView:
    runtime = _runtime(request)
    importer = runtime.legacy_importer()
    if payload.confirm:
        runtime.credential_store()
    revision = importer.apply(preview_id=payload.preview_id, confirm=payload.confirm)
    return _draft_view(revision)


@router.get("/legacy-import/sources", response_model=LegacySourceList)
def legacy_import_sources(request: Request) -> LegacySourceList:
    return LegacySourceList(sources=tuple(sorted(_runtime(request).legacy_importer().sources)))


@router.get("/recovery", response_model=RecoveryView)
def recovery_view(request: Request) -> RecoveryView:
    return _runtime(request)._require_config().recovery_view()


@router.post("/recovery/restore", response_model=ActivationView)
def restore_configuration(payload: RecoveryRestoreRequest, request: Request) -> ActivationView:
    if not payload.confirm:
        raise SettingsManagementError("RESTORE_CONFIRMATION_REQUIRED", stage="restore")
    runtime = _runtime(request)
    runtime.credential_store()
    runtime._require_config().restore_revision(
        payload.revision_id, expected_revision=payload.expected_revision,
        expected_manifest_fingerprint=payload.expected_manifest_fingerprint,
    )
    return ActivationView(revision_id=payload.revision_id,
                          status="pending_restart", restart_required=True)


# ---------------------------------------------------------------------------
# 索引身份
# ---------------------------------------------------------------------------


@router.get("/index-identities", response_model=IndexIdentityList, summary="索引身份列表")
def list_index_identities(request: Request) -> IndexIdentityList:
    session = require_management_session(request)
    runtime = _runtime(request)
    from sqlalchemy import select

    from backend.app.models.knowledge_base import KnowledgeBase

    try:
        with runtime.index_identity(session) as (service, db):
            ids = [row[0] for row in db.execute(select(KnowledgeBase.id)).all()]
            views = [
                IndexIdentityView.model_validate(service.view(kb_id=kb_id))
                for kb_id in ids
            ]
    except SQLAlchemyError:
        raise SettingsManagementError("DEPENDENCY_UNAVAILABLE", status_code=503) from None
    return IndexIdentityList(identities=tuple(views))


@router.get(
    "/index-identities/{kb_id}",
    response_model=IndexIdentityView,
    summary="单个知识库索引身份",
)
def get_index_identity(kb_id: UUID, request: Request) -> IndexIdentityView:
    session = require_management_session(request)
    with _runtime(request).index_identity(session) as (service, _db):
        return IndexIdentityView.model_validate(service.view(kb_id=kb_id))


@router.post(
    "/index-identities/{kb_id}/adoption-preview", response_model=AdoptionPreview, summary="历史索引身份接纳预览"
)
def preview_index_adoption(
    kb_id: UUID, payload: AdoptionPreviewRequest, request: Request
) -> dict[str, Any]:
    session = require_management_session(request)
    with _runtime(request).index_identity(session) as (service, _db):
        return service.preview_adoption(
            kb_id=kb_id,
            expected_identity_revision=payload.expected_identity_revision,
            proposed_identity=payload.proposed_identity,
            evidence=payload.evidence,
            operator_attestation=payload.operator_attestation,
            management_session_id=str(session.session_id),
        )


@router.post(
    "/index-identities/{kb_id}/adoption-confirm", response_model=IndexIdentityView, summary="确认历史索引身份接纳"
)
def confirm_index_adoption(
    kb_id: UUID, payload: AdoptionConfirmRequest, request: Request
) -> dict[str, Any]:
    session = require_management_session(request)
    with _runtime(request).index_identity(session) as (service, _db):
        return service.confirm_adoption(
            preview_id=payload.preview_id,
            expected_kb_id=kb_id,
            expected_identity_revision=payload.expected_identity_revision,
            confirmation_id=payload.preview_id,
            management_session_id=str(session.session_id),
            confirm=payload.confirm,
        )


__all__ = [
    "SessionExchangeRequest",
    "install_settings_api",
    "router",
]
