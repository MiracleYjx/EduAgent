"""Windows single-host launcher with explicit configuration and owned child."""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from dotenv import dotenv_values

from backend.app.core.config import AppSettings, ConfigurationError, get_settings

_LOCAL_HTTP = build_opener(ProxyHandler({}))


class LaunchError(RuntimeError):
    def __init__(self, step: str, detail: str = "", action: str = "") -> None:
        self.step = step
        super().__init__(f"[{step}] {detail}；下一步：{action}")


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def configure(config: Path) -> AppSettings:
    config = config.expanduser().resolve()
    if not config.is_file():
        raise LaunchError(
            "外置配置",
            f"未找到 {config}",
            "运行 EduAgent.exe --configure 编辑配置，或复制 config.env.example 后填写",
        )
    if getattr(sys, "frozen", False) and config.is_relative_to(resource_root()):
        raise LaunchError("外置配置", "配置位于应用资源目录", "移至用户可写目录")
    # An explicit file wins over inherited variables; secrets never enter argv.
    for name, value in dotenv_values(config, encoding="utf-8").items():
        if value is not None:
            os.environ[name] = value
    os.chdir(config.parent)
    get_settings.cache_clear()
    try:
        return get_settings()
    except ConfigurationError as exc:
        raise LaunchError(
            "配置校验", str(exc), "使用 --configure 修正列出的配置项"
        ) from exc


def check_storage(root: Path, *, frozen: bool) -> None:
    if frozen and (
        root.is_relative_to(resource_root())
        or root.is_relative_to(Path(sys.executable).parent)
    ):
        raise LaunchError(
            "持久目录", "STORAGE_ROOT 位于安装资源目录", "设置独立用户数据目录"
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-check-", dir=root) as stream:
            stream.write(b"EduAgent")
            stream.flush()
        (root / "logs").mkdir(exist_ok=True)
    except OSError as exc:
        raise LaunchError(
            "持久目录", type(exc).__name__, "检查 STORAGE_ROOT 的写入权限与可用磁盘"
        ) from exc


def check_local_model(value: str | None, purpose: str) -> None:
    if not value or not Path(value).expanduser().is_dir():
        raise LaunchError(
            "外置模型",
            f"{purpose} 模型目录未提供或不存在",
            "预置完整本地模型并配置绝对目录；云 Embedding 可选择 openai_compatible",
        )
    if getattr(sys, "frozen", False) and Path(value).resolve().is_relative_to(
        resource_root()
    ):
        raise LaunchError("外置模型", f"{purpose} 模型位于资源包", "配置外置模型目录")
    if importlib.util.find_spec("sentence_transformers") is None:
        raise LaunchError(
            "外置模型",
            "缺本地推理依赖",
            "源码安装 rerank-local；EXE 使用包含本地模型运行库的构建",
        )


def check_models(settings: AppSettings) -> None:
    from backend.app.ai.embedding.factory import create_embedding_provider
    from backend.app.ai.llm.factory import create_llm_provider

    if importlib.util.find_spec("openai") is None:
        raise LaunchError(
            "模型运行库", "缺少 OpenAI-compatible SDK", "恢复打包依赖或安装核心运行依赖"
        )
    if settings.embedding_provider in {"bge", "huggingface", "local"}:
        check_local_model(settings.embedding_model, "Embedding")
    if settings.rerank_provider == "cross_encoder":
        check_local_model(settings.rerank_model, "Rerank")
    try:
        create_llm_provider(settings)
        create_embedding_provider(settings)
    except Exception as exc:
        raise LaunchError(
            "模型配置",
            type(exc).__name__,
            "检查当前 Provider、模型、必填凭据及所选运行库",
        ) from exc
    if settings.ocr_enabled:
        from backend.app.ai.ingestion.ocr.rapidocr import MODEL_ID, _model_paths

        if (
            settings.ocr_provider != "rapidocr"
            or settings.ocr_model != MODEL_ID
            or settings.ocr_model_dir is None
        ):
            raise LaunchError(
                "OCR",
                "缺明确的 RapidOCR / PP-OCRv5-mobile / OCR_MODEL_DIR",
                "按 T155 选型配置预置权重",
            )
        try:
            if (
                importlib.util.find_spec("rapidocr") is None
                or importlib.util.find_spec("onnxruntime") is None
            ):
                raise ModuleNotFoundError("OCR runtime")
            if getattr(
                sys, "frozen", False
            ) and settings.ocr_model_dir.resolve().is_relative_to(resource_root()):
                raise ValueError("OCR weights must be external")
            _model_paths(settings.ocr_model_dir)
        except Exception as exc:
            raise LaunchError(
                "OCR", type(exc).__name__, "安装 OCR 运行库并核对三份已锁定的外置权重"
            ) from exc


def check_dependencies(settings: AppSettings) -> None:
    from sqlalchemy import text

    from backend.app.core.database import (
        check_postgres_ready,
        create_database_engine,
        initialize_pgvector_extension,
    )
    from backend.app.core.redis import check_redis_ready, create_redis_client

    engine = create_database_engine(settings, connect_timeout=3)
    try:
        check_postgres_ready(engine)
        with engine.connect() as conn:
            if not conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='vector')"
                )
            ):
                raise LaunchError(
                    "pgvector",
                    "服务器未安装 vector 扩展",
                    "为 PostgreSQL 安装 pgvector",
                )
        initialize_pgvector_extension(engine)
    except LaunchError:
        raise
    except Exception as exc:
        raise LaunchError(
            "PostgreSQL / pgvector",
            type(exc).__name__,
            "启动数据库，核对 DATABASE_URL 与扩展创建权限",
        ) from exc
    finally:
        engine.dispose()
    client = create_redis_client(settings, socket_connect_timeout=3, socket_timeout=3)
    try:
        check_redis_ready(client)
    except Exception as exc:
        raise LaunchError(
            "Redis", type(exc).__name__, "启动 Redis 并核对 REDIS_URL"
        ) from exc
    finally:
        client.close()


def check_network(settings: AppSettings) -> None:
    urls = {str(settings.deepseek_base_url)}
    if (
        settings.embedding_provider == "openai_compatible"
        and settings.embedding_base_url
    ):
        urls.add(str(settings.embedding_base_url))
    for value in urls:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host:
            raise LaunchError("模型网络", "端点缺主机", "修正模型端点")
        try:
            with socket.create_connection(
                (host, parsed.port or (443 if parsed.scheme == "https" else 80)),
                timeout=4,
            ) as conn:
                if parsed.scheme == "https":
                    with ssl.create_default_context().wrap_socket(
                        conn, server_hostname=host
                    ):
                        pass
        except (OSError, ValueError) as exc:
            raise LaunchError(
                "模型网络",
                f"{host}：{type(exc).__name__}",
                "检查网络、代理和端点 TLS；此探测不证明凭据或模型调用成功",
            ) from exc


def migrate() -> None:
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from backend.app.core.database import create_database_engine

    config = Config(str(resource_root() / "alembic.ini"))
    config.set_main_option("script_location", str(resource_root() / "migrations"))
    engine = create_database_engine()
    try:
        command.upgrade(config, "head")
        expected = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as conn:
            actual = set(MigrationContext.configure(conn).get_current_heads())
        if actual != expected:
            raise RuntimeError("migration head not applied")
    except Exception as exc:
        raise LaunchError(
            "数据库迁移",
            type(exc).__name__,
            "查看迁移日志；修复后重启，不手工伪造版本标记",
        ) from exc
    finally:
        engine.dispose()


def http_ready(port: int) -> bool:
    try:
        for path in ("/ready", "/gradio/"):
            with _LOCAL_HTTP.open(
                f"http://127.0.0.1:{port}{path}", timeout=2
            ) as response:
                if response.status != 200:
                    return False
        return True
    except OSError:
        return False


def wait_ready(child: subprocess.Popen, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise LaunchError(
                "应用启动",
                f"所属进程已退出，退出码 {child.returncode}",
                "查看 STORAGE_ROOT/logs/application.log",
            )
        if http_ready(port):
            return
        time.sleep(0.2)
    raise LaunchError(
        "应用就绪",
        "readiness 或 Gradio 未在限定时间内可用",
        "查看 application.log 与数据库/Redis 状态",
    )


def stop_owned(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def serve(port: int, *, await_preflight: bool = False) -> int:
    import uvicorn

    from backend.app.core.app import create_app

    application = create_app()
    if await_preflight:
        print("应用预加载完成，等待启动预检。", flush=True)
        if sys.stdin is None or sys.stdin.readline() != "SERVE\n":
            raise LaunchError("启动预检", "未收到预检通过信号", "查看启动器失败步骤")
    uvicorn.run(application, host="127.0.0.1", port=port, workers=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EduAgent Windows 单机启动器；退出只停止所属应用进程"
    )
    data_root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "EduAgent"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--configure", action="store_true", help="打开本机配置窗口，保存后退出"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--child", choices=["serve"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--await-preflight", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    explicit_config = args.config is not None
    args.config = (args.config or data_root / "config.env").expanduser().resolve()
    if args.configure and args.child:
        parser.error("--configure 不能与内部工作进程参数一起使用")
    child = None
    try:
        if not 1 <= args.port <= 65535 or args.startup_timeout <= 0:
            raise LaunchError(
                "启动参数", "端口或超时无效", "端口应为1–65535，超时应大于0"
            )
        first_configuration = (
            bool(getattr(sys, "frozen", False))
            and not args.child
            and not explicit_config
            and not args.no_browser
            and not args.config.exists()
        )
        if args.configure or first_configuration:
            from backend.app.deployment.configuration_editor import open_configuration

            template = (
                Path(sys.executable).parent / "config.env.example"
                if getattr(sys, "frozen", False)
                else resource_root() / "config/windows.env.example"
            )
            saved = open_configuration(
                args.config,
                template,
                continue_launch=first_configuration and not args.configure,
            )
            if args.configure:
                print(
                    (
                        "配置已保存，重新启动 EduAgent 后生效。"
                        if saved
                        else "已取消配置，未保存。"
                    ),
                    flush=True,
                )
                return 0
            if not saved:
                print("已取消首次配置，未启动业务服务。", flush=True)
                return 0
        from backend.app.deployment.windows_tls import install_windows_tls_loader

        install_windows_tls_loader()
        settings = configure(args.config)
        if args.child:
            return serve(args.port, await_preflight=args.await_preflight)
        print("检查：持久目录", flush=True)
        check_storage(settings.storage_root, frozen=bool(getattr(sys, "frozen", False)))
        print("通过：持久目录", flush=True)

        def spawn(log, *, preload: bool):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", args.port))
                except OSError as exc:
                    raise LaunchError(
                        "服务端口",
                        "已被占用",
                        "停止已确认的旧实例或选择另一个 --port；启动器不会停止无关进程",
                    ) from exc
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(resource_root() / "scripts/launch_windows.py"))
            command += [
                "--child",
                "serve",
                "--config",
                str(args.config),
                "--port",
                str(args.port),
            ]
            if preload:
                command.append("--await-preflight")
            env = os.environ.copy()
            env["GRADIO_ANALYTICS_ENABLED"] = "False"
            env["PYTHONIOENCODING"] = "utf-8"
            return subprocess.Popen(
                command,
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.PIPE if preload else None,
                text=True,
                encoding="utf-8",
            )

        with (settings.storage_root / "logs/application.log").open(
            "a", encoding="utf-8"
        ) as log:
            if getattr(sys, "frozen", False):
                child = spawn(log, preload=True)
            for label, operation in (
                ("模型配置", lambda: check_models(settings)),
                ("PostgreSQL / pgvector / Redis", lambda: check_dependencies(settings)),
                ("必要网络", lambda: check_network(settings)),
                ("数据库迁移", migrate),
            ):
                print(f"检查：{label}", flush=True)
                operation()
                print(f"通过：{label}", flush=True)
            if child is None:
                child = spawn(log, preload=False)
            else:
                try:
                    child.stdin.write("SERVE\n")
                    child.stdin.flush()
                    child.stdin.close()
                except (OSError, AttributeError) as exc:
                    raise LaunchError(
                        "应用预加载", "工作进程未接收启动信号", "查看 application.log"
                    ) from exc
            wait_ready(child, args.port, args.startup_timeout)
            url = f"http://127.0.0.1:{args.port}/gradio/"
            print(f"应用已就绪：{url}；所属 PID={child.pid}；Ctrl+C 退出", flush=True)
            if not args.no_browser and not webbrowser.open(url):
                print(f"浏览器未打开，请手动访问 {url}", flush=True)
            while child.poll() is None:
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            return child.returncode or 0
    except KeyboardInterrupt:
        print("正在停止所属应用进程…", flush=True)
        return 0
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must redact credentials
        # Keep the actual child failure location without logging exception values/credentials.
        if args.child:
            traceback.print_tb(exc.__traceback__, file=sys.stderr)
        if isinstance(exc, OSError) and exc.filename:
            print(f"失败资源：{exc.filename}", file=sys.stderr, flush=True)
        print(
            f"启动失败：[启动器] {type(exc).__name__}；检查外置配置、持久目录与应用日志。",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        if child is not None:
            if child.stdin is not None and not child.stdin.closed:
                try:
                    child.stdin.close()
                except OSError:
                    pass  # Preserve the original preflight/signal error during cleanup.
            stop_owned(child)
