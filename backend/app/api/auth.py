"""登录、当前用户和认证错误处理端点。"""

from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.security import (
    CurrentUser,
    authentication_http_exception,
    get_auth_service,
    get_user_roles,
)
from backend.app.services.auth_service import AuthenticationError, AuthService

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    """登录请求体，支持 identifier、username 或 email 字段。"""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    identifier: str = Field(
        min_length=1,
        max_length=255,
        description="用户名或邮箱。",
        validation_alias=AliasChoices("identifier", "username", "email"),
    )
    password: str = Field(
        min_length=1,
        max_length=1024,
        description="登录密码。",
    )

    @field_validator("identifier", mode="before")
    @classmethod
    def validate_identifier(cls, value: Any) -> str:
        """拒绝空白身份标识并统一去除首尾空格。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("用户名或邮箱不能为空。")
        return value.strip()

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, value: Any) -> str:
        """拒绝空密码，同时保留密码本身的空格。"""

        if not isinstance(value, str) or not value:
            raise ValueError("密码不能为空。")
        return value


class UserSummary(BaseModel):
    """认证响应中的安全用户摘要，不返回密码或密钥。"""

    id: str
    username: str
    email: str
    roles: list[str]


class LoginResponse(BaseModel):
    """登录成功后返回的访问令牌和用户摘要。"""

    access_token: str
    token_type: str = "bearer"
    user: UserSummary


class MeResponse(UserSummary):
    """当前登录用户信息。"""


def _form_values(body: bytes) -> dict[str, str]:
    """解析标准 URL 编码表单，重复字段取最后一个值。"""

    parsed = parse_qs(
        body.decode("utf-8", errors="strict"),
        keep_blank_values=True,
    )
    return {key: values[-1] for key, values in parsed.items() if values}


async def parse_login_request(request: Request) -> LoginRequest:
    """兼容 JSON 和 OAuth2 常用表单格式，并统一校验错误提示。"""

    content_type = request.headers.get("content-type", "").lower()
    try:
        body = await request.body()
        if "application/json" in content_type or not content_type:
            data = json.loads(body.decode("utf-8")) if body else {}
        elif "application/x-www-form-urlencoded" in content_type:
            data = _form_values(body)
        elif "multipart/form-data" in content_type:
            form = await request.form()
            data = {key: str(value) for key, value in form.items()}
        else:
            raise ValueError("不支持的登录请求格式。")
        return LoginRequest.model_validate(data)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="登录参数无效，请提供用户名或邮箱及密码。",
        ) from None


def _user_summary(user: Any) -> UserSummary:
    """构造不泄露敏感字段的用户响应。"""

    return UserSummary(
        id=str(user.id),
        username=user.username,
        email=user.email,
        roles=[
            role.value
            for role in sorted(
                get_user_roles(user),
                key=lambda item: item.value,
            )
        ],
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: Annotated[LoginRequest, Depends(parse_login_request)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> LoginResponse:
    """校验账号密码并签发 Bearer JWT。"""

    try:
        user = auth_service.authenticate(payload.identifier, payload.password)
        token = auth_service.issue_access_token(user)
    except AuthenticationError as exc:
        raise authentication_http_exception(exc) from None
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务暂时不可用，请稍后重试。",
        ) from None

    return LoginResponse(
        access_token=token,
        user=_user_summary(user),
    )


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser) -> MeResponse:
    """返回当前 Bearer JWT 对应的用户摘要。"""

    return MeResponse.model_validate(_user_summary(user).model_dump())


__all__ = [
    "LoginRequest",
    "LoginResponse",
    "MeResponse",
    "UserSummary",
    "login",
    "me",
    "parse_login_request",
    "router",
]
