"""类型化配置与启动失败脱敏测试。

TCR（2026-09-14，H03）：新增默认维度、空值拒绝和启动维度不匹配测试，
防止错误配置进入 vector(1024) 摄取链路；保留原有 JWT 脱敏断言。
"""

import traceback

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.core.app import create_app
from backend.app.core.config import (
    ConfigurationError,
    reset_settings_cache,
)
from tests.unit.settings_helpers import build_test_settings


def test_embedding_dimension_defaults_to_schema_dimension(monkeypatch, tmp_path) -> None:
    """未填写维度时使用 1024，与现有应用启动用例共享此缺省配置。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EMBEDDING_DIMENSION", raising=False)
    assert build_test_settings().embedding_dimension == 1024


@pytest.mark.parametrize("dimension", [None, "", 0])
def test_embedding_dimension_rejects_empty_or_invalid_value(dimension) -> None:
    """显式空值或非正维度不再视为自动推断。"""

    with pytest.raises(ValidationError, match="embedding_dimension"):
        build_test_settings(embedding_dimension=dimension)


@pytest.mark.parametrize("dimension", [384, 768, 1536])
def test_embedding_dimension_mismatch_stops_startup(dimension: int) -> None:
    """应用创建前阻止错误维度，并提供明确中文修复信息。"""

    with pytest.raises(ConfigurationError) as error:
        create_app(settings=build_test_settings(embedding_dimension=dimension))

    assert error.value.fields == ("EMBEDDING_DIMENSION",)
    assert f"当前为 {dimension}" in str(error.value)
    assert "迁移 0004" in str(error.value)
    assert "vector(1024)" in str(error.value)


@pytest.mark.parametrize("secret", ["", " " * 32, "short-sensitive-secret", "x" * 31])
def test_invalid_jwt_key_is_rejected_without_exposure(secret: str) -> None:
    """短密钥和空白密钥不能通过校验，异常展示不得包含输入值。"""

    with pytest.raises(ValidationError) as error:
        build_test_settings(JWT_SECRET_KEY=secret)
    assert "JWT_SECRET_KEY" in str(error.value)
    if secret.strip():
        assert secret not in str(error.value)


def test_jwt_settings_defaults_and_redaction() -> None:
    """32 字符是合法边界，公开配置只显示脱敏密钥。"""

    secret = "s" * 32
    settings = build_test_settings(JWT_SECRET_KEY=secret)
    assert isinstance(settings.JWT_SECRET_KEY, SecretStr)
    assert settings.JWT_SECRET_KEY.get_secret_value() == secret
    assert settings.JWT_ALGORITHM == "HS256"
    assert settings.JWT_EXPIRE_MINUTES == 60
    assert secret not in repr(settings)
    assert secret not in str(settings.public_dict())


@pytest.mark.parametrize("value", [None, "", "short-sensitive-secret"])
def test_invalid_jwt_configuration_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog, value: str | None
) -> None:
    """缺失或无效密钥在启动时失败，完整异常链和日志也不包含明文。"""

    settings = build_test_settings()
    monkeypatch.chdir(tmp_path)
    for field, raw in settings.model_dump().items():
        if field == "JWT_SECRET_KEY":
            continue
        if isinstance(raw, SecretStr):
            raw = raw.get_secret_value()
        monkeypatch.setenv(field.upper(), str(raw))
    if value is None:
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("JWT_SECRET_KEY", value)
    reset_settings_cache()
    try:
        with pytest.raises(ConfigurationError, match="JWT_SECRET_KEY") as error:
            create_app()
        output = "".join(traceback.format_exception(error.value)) + caplog.text
        assert "32" in str(error.value)
        if value:
            assert value not in output
    finally:
        reset_settings_cache()


@pytest.mark.parametrize(
    "overrides", [{"JWT_ALGORITHM": "none"}, {"JWT_EXPIRE_MINUTES": 0}]
)
def test_unsupported_jwt_configuration_is_rejected(overrides) -> None:
    """拒绝当前实现不支持的算法以及非正有效期。"""

    with pytest.raises(ValidationError):
        build_test_settings(**overrides)
