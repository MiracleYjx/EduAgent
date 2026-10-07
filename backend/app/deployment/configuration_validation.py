"""Offline configuration completeness checks shared by the Windows entry points."""

from backend.app.core.config import (
    EMBEDDING_DIMENSION_DEFAULT,
    AppSettings,
    ConfigurationError,
)


def validate_configuration(settings: AppSettings) -> None:
    """Report actionable field names without connections, model loading or secrets."""
    issues: list[str] = []
    fields: list[str] = []
    if any(
        host.get("username") == "USER" or host.get("password") == "PASSWORD"
        for host in settings.database_url.hosts()
    ):
        issues.append("DATABASE_URL 仍含模板账号或密码，请填写真实 PostgreSQL 连接地址")
        fields.append("DATABASE_URL")
    if settings.embedding_provider == "openai_compatible":
        required = (
            ("EMBEDDING_MODEL", settings.embedding_model),
            (
                "EMBEDDING_API_KEY",
                (
                    settings.embedding_api_key.get_secret_value()
                    if settings.embedding_api_key is not None
                    else None
                ),
            ),
        )
        missing = [name for name, value in required if not value or not value.strip()]
        if missing:
            issues.append(
                "云 Embedding 缺少 "
                + ", ".join(missing)
                + "；请填写支持 1024 维的模型及其独立 Key，或显式选择本地模型；DeepSeek 文字 Key 不会自动用于 Embedding"
            )
            fields.extend(missing)
    elif settings.embedding_provider in {"local", "bge", "huggingface"}:
        if not settings.embedding_model:
            issues.append("EMBEDDING_MODEL 未填写，请选择完整外置本地模型目录")
            fields.append("EMBEDDING_MODEL")
    if settings.embedding_dimension != EMBEDDING_DIMENSION_DEFAULT:
        issues.append("EMBEDDING_DIMENSION 必须为 1024，与现有向量表一致")
        fields.append("EMBEDDING_DIMENSION")
    if issues:
        raise ConfigurationError(
            "运行配置未完成：" + "；".join(issues) + "。", tuple(fields)
        )
