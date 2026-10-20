"""真实进程桩：只用控制通道与合成 Setup 授权服务，不连数据库/模型。

由 `tests/integration/test_launcher_control_process.py` 以独立进程启动，用来验证
T221 的匿名控制管道、注册确认、授权码发行与受控重启退出码。
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.settings_security import (
    EnvironmentName,
    RuntimeKind,
    SetupSecurityService,
    SetupSourcePolicy,
)
from backend.app.deployment.control_channel import (
    RESTART_EXIT_CODE,
    ControlServer,
)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18121
    base_url = f"http://127.0.0.1:{port}"
    security = SetupSecurityService(
        environment_id=uuid4(),
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.PYTHON,
        policy=SetupSourcePolicy(
            environment=EnvironmentName.DEVELOPMENT,
            runtime_kind=RuntimeKind.PYTHON,
            allowed_hosts=frozenset(
                {f"127.0.0.1:{port}", f"localhost:{port}"}
            ),
        ),
    )
    exit_code = {"value": 0}

    def on_restart() -> None:
        exit_code["value"] = RESTART_EXIT_CODE

    def on_lost() -> None:
        exit_code["value"] = 3

    server = ControlServer(
        security,
        settings_base_url=base_url,
        reader=sys.stdin,
        writer=sys.stdout,
        on_restart=on_restart,
        on_channel_lost=on_lost,
    )
    # 非控制行必须被 Launcher 忽略（不解析、不回显）。
    print("noise: worker starting", flush=True)
    server.write(server.registration())
    server.serve_forever()
    return exit_code["value"]


if __name__ == "__main__":
    raise SystemExit(main())
