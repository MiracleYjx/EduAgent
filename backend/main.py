"""EduAgent 后端进程入口点。

启动分两路（T218）：

- 业务配置在**当前进程环境**中显式齐备：装配原完整应用（业务路由、JWT/RBAC、
  Workflow、Gradio），行为与本轮之前一致；
- 否则：只启动最小引导应用，业务入口返回 `503 BUSINESS_NOT_READY`，设置面仍可用。

`backend.main` 不在导入期强校验业务配置，也不隐式读取仓库/当前目录 `.env`；配置来源
仍由 `AppSettings` 的显式环境决定，已保存配置的激活与装配协调归 T220。
"""

from __future__ import annotations

from backend.app.core.bootstrap import create_application, listen_address
from backend.app.deployment.container_control import install_container_control

app = create_application()
install_container_control(app)


def main() -> None:
    """当作为 Python 模块调用时运行后端。"""

    import uvicorn

    host, port = listen_address()
    uvicorn.run("backend.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
