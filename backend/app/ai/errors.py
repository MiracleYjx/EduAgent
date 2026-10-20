"""AI 配置门禁：未配置 Provider 时在构造客户端之前显式失败。"""

from __future__ import annotations


class AIConfigurationError(RuntimeError):
    """目标能力尚未配置；不是网络/模型错误，也不携带任何秘密。"""

    code = "AI_NOT_CONFIGURED"
    status_code = 503

    def __init__(self, capability: str = "AI") -> None:
        self.capability = capability
        super().__init__(
            f"{capability} 尚未配置：请先在设置中完成 Provider、凭据与检测。"
        )


__all__ = ["AIConfigurationError"]
