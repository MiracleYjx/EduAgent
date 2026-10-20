"""TCR-S3-SETTINGS-SECURITY-019（T217）：本机管理通道接缝单元契约。

验证唯一发行入口、操作者身份、重复领码作废旧码、fragment 授权 URL 与控制消息
白名单；不启动 HTTP 服务，不打印真实授权码。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from backend.app.core.settings_security import (
    ONE_TIME_CODE_LIFETIME,
    OperatorIdentity,
    PrincipalKind,
    SettingsSecurityError,
    SetupSecurityService,
)
from backend.app.deployment.local_management import (
    CONTAINER_OPERATOR_ID,
    LOCAL_OPERATOR_FALLBACK,
    LocalManagementAuthority,
    LocalManagementError,
    authorization_url,
    resolve_operator_identity,
)
from backend.app.schemas.settings import EnvironmentName, RuntimeKind

ENVIRONMENT_ID = UUID("55555555-5555-4555-8555-555555555555")
START = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


@pytest.fixture()
def service() -> SetupSecurityService:
    return SetupSecurityService(
        environment_id=ENVIRONMENT_ID,
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.WINDOWS,
        clock=lambda: START,
    )


def test_local_operator_uses_the_real_os_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "synthetic-windows-user")

    identity = resolve_operator_identity(runtime_kind=RuntimeKind.WINDOWS)

    assert identity == OperatorIdentity(
        kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-windows-user"
    )


def test_local_operator_falls_back_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> str:
        raise OSError("no account available")

    monkeypatch.setattr("getpass.getuser", unavailable)

    identity = resolve_operator_identity(runtime_kind=RuntimeKind.PYTHON)

    assert identity.kind is PrincipalKind.LOCAL_OS
    assert identity.principal_id == LOCAL_OPERATOR_FALLBACK


def test_docker_operator_is_container_operator_without_guessing_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_be_called() -> str:
        raise AssertionError("Docker 不得猜宿主用户名")

    monkeypatch.setattr("getpass.getuser", must_not_be_called)

    identity = resolve_operator_identity(runtime_kind=RuntimeKind.DOCKER)

    assert identity.kind is PrincipalKind.CONTAINER_OPERATOR
    assert identity.principal_id == CONTAINER_OPERATOR_ID


def test_authority_issues_codes_only_through_the_local_seam(
    service: SetupSecurityService,
) -> None:
    authority = LocalManagementAuthority(
        service,
        operator=OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-operator"
        ),
    )

    grant = authority.request_one_time_code()

    assert grant.environment_id == ENVIRONMENT_ID
    assert grant.instance_id == service.instance_id
    assert grant.expires_at == START + ONE_TIME_CODE_LIFETIME
    assert service.exchange(grant.code, peer="127.0.0.1").token


def test_repeated_requests_supersede_previous_code(
    service: SetupSecurityService,
) -> None:
    authority = LocalManagementAuthority(
        service,
        operator=OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-operator"
        ),
    )

    first = authority.request_one_time_code()
    second = authority.request_one_time_code()

    with pytest.raises(SettingsSecurityError) as superseded:
        service.exchange(first.code, peer="127.0.0.1")
    assert superseded.value.code == "CODE_EXPIRED"
    assert service.exchange(second.code, peer="127.0.0.1").token


def test_authorization_url_keeps_the_code_in_the_fragment() -> None:
    url = authorization_url("http://127.0.0.1:8000/settings", "synthetic-code+/=")
    parts = urlsplit(url)

    assert parts.query == ""
    assert parse_qs(parts.query) == {}
    assert parts.fragment == "code=synthetic-code%2B%2F%3D"
    assert "synthetic-code" not in parts.path


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8000/settings?code=leak",
        "http://127.0.0.1:8000/settings#fragment",
        "ftp://127.0.0.1/settings",
        "http://user:password@127.0.0.1:8000/settings",
        "http:///settings",
        "not-a-url",
    ],
)
def test_authorization_url_rejects_unsafe_bases(base_url: str) -> None:
    with pytest.raises(LocalManagementError) as error:
        authorization_url(base_url, "synthetic-code")
    assert error.value.code == "AUTHORIZATION_BASE_URL_INVALID"


def test_control_message_allowlist(service: SetupSecurityService) -> None:
    authority = LocalManagementAuthority(
        service,
        operator=OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-operator"
        ),
    )

    payload = authority.handle_control_message(
        {"type": "open_settings", "settings_base_url": "http://127.0.0.1:8000/settings"}
    )

    assert payload["type"] == "one_time_code_grant"
    assert payload["operator"] == {
        "kind": "local_os",
        "principal_id": "synthetic-operator",
    }
    assert "management_token" not in payload
    assert urlsplit(str(payload["authorization_url"])).fragment.startswith("code=")

    with pytest.raises(LocalManagementError) as unsupported:
        authority.handle_control_message({"type": "run_shell"})
    assert unsupported.value.code == "CONTROL_MESSAGE_UNSUPPORTED"

    with pytest.raises(LocalManagementError) as extra:
        authority.handle_control_message({"type": "reauthorize", "command": "whoami"})
    assert extra.value.code == "CONTROL_MESSAGE_INVALID"

    with pytest.raises(LocalManagementError) as missing_type:
        authority.handle_control_message({})
    assert missing_type.value.code == "CONTROL_MESSAGE_UNSUPPORTED"


def test_control_message_without_base_url_still_returns_a_grant(
    service: SetupSecurityService,
) -> None:
    authority = LocalManagementAuthority(
        service,
        operator=OperatorIdentity(
            kind=PrincipalKind.CONTAINER_OPERATOR, principal_id=CONTAINER_OPERATOR_ID
        ),
    )

    payload = authority.handle_control_message({"type": "reauthorize"})

    operator = payload["operator"]
    assert isinstance(operator, dict)
    assert payload["authorization_url"] is None
    assert operator["kind"] == "container_operator"
    assert isinstance(payload["expires_at"], str)
