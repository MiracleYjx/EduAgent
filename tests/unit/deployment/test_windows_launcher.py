from unittest.mock import Mock

import pytest

from backend.app.deployment import windows_launcher as launcher


def test_missing_explicit_config_is_reported_without_fallback(tmp_path):
    with pytest.raises(launcher.LaunchError, match="外置配置"):
        launcher.configure(tmp_path / "missing.env")


def test_writable_data_root_does_not_leave_probe(tmp_path):
    launcher.check_storage(tmp_path / "data", frozen=False)
    assert (tmp_path / "data" / "logs").is_dir()
    assert not list((tmp_path / "data").glob(".write-check-*"))


def test_local_model_missing_is_explicit(tmp_path):
    with pytest.raises(launcher.LaunchError, match="模型目录"):
        launcher.check_local_model(str(tmp_path / "missing"), "Embedding")


def test_missing_ocr_weight_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.app.ai.llm.factory.create_llm_provider", lambda _: None
    )
    monkeypatch.setattr(
        "backend.app.ai.embedding.factory.create_embedding_provider", lambda _: None
    )
    settings = Mock(
        ocr_enabled=True,
        ocr_provider="rapidocr",
        ocr_model="PP-OCRv5-mobile",
        ocr_model_dir=tmp_path,
        embedding_provider="openai_compatible",
        rerank_provider="none",
    )
    with pytest.raises(launcher.LaunchError, match="OCR"):
        launcher.check_models(settings)


def test_owned_shutdown_never_touches_another_process():
    owned = Mock()
    owned.poll.return_value = None
    other = Mock()
    launcher.stop_owned(owned)
    owned.terminate.assert_called_once()
    owned.wait.assert_called_once()
    other.terminate.assert_not_called()


def test_ready_timeout_does_not_open_browser(monkeypatch):
    owned = Mock()
    owned.poll.return_value = None
    monkeypatch.setattr(launcher, "http_ready", lambda _: False)
    browser = Mock()
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    with pytest.raises(launcher.LaunchError, match="就绪"):
        launcher.wait_ready(owned, 8000, 0)
    browser.assert_not_called()


def test_failed_migration_never_starts_app(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "configure", lambda _: Mock(storage_root=tmp_path))
    monkeypatch.setattr(launcher, "check_storage", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "check_models", lambda _: None)
    monkeypatch.setattr(launcher, "check_dependencies", lambda _: None)
    monkeypatch.setattr(launcher, "check_network", lambda _: None)

    def fail():
        raise launcher.LaunchError("数据库迁移", "未提交", "检查迁移记录")

    monkeypatch.setattr(launcher, "migrate", fail)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert (
        launcher.main(["--config", str(tmp_path / "config.env"), "--no-browser"]) == 1
    )
    spawn.assert_not_called()


def test_browser_opens_only_after_both_services_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "configure", lambda _: Mock(storage_root=tmp_path))
    (tmp_path / "logs").mkdir()
    for name in (
        "check_storage",
        "check_models",
        "check_dependencies",
        "check_network",
        "migrate",
    ):
        monkeypatch.setattr(launcher, name, lambda *a, **k: None)
    child = Mock()
    child.poll.return_value = 0
    child.returncode = 0
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: child)
    order = []
    monkeypatch.setattr(launcher, "wait_ready", lambda *a: order.append("ready"))
    monkeypatch.setattr(
        launcher.webbrowser, "open", lambda url: order.append("browser") or True
    )
    assert (
        launcher.main(["--config", str(tmp_path / "config.env"), "--port", "19487"])
        == 0
    )
    assert order == ["ready", "browser"]
