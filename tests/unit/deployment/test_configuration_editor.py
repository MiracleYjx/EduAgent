"""File integrity, isolated validation and real Tk form behavior."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from backend.app.core.config import ConfigurationError
from backend.app.deployment import configuration_editor as editor
from backend.app.deployment import windows_launcher as launcher


def valid_values():
    return {
        "DATABASE_URL": "postgresql+psycopg://synthetic@127.0.0.1:5432/test",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "LLM_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "synthetic-key",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-chat",
        "EMBEDDING_PROVIDER": "local",
        "EMBEDDING_MODEL": "D:/synthetic-model",
        "RERANK_PROVIDER": "llm",
        "CONFIDENCE_THRESHOLD": "0.7",
        "JWT_SECRET_KEY": "synthetic-signing-secret-at-least-32-characters",
    }


@pytest.fixture
def document(tmp_path):
    template = tmp_path / "template.env"
    template.write_text(
        "".join(f"{key}={value}\n" for key, value in valid_values().items()),
        encoding="utf-8",
    )
    return editor.ConfigDocument(tmp_path / "config.env", template)


def test_missing_required_file_value_cannot_borrow_environment_or_dotenv(
    document, monkeypatch, tmp_path
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-private-key")
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("DEEPSEEK_API_KEY=other-private-key\n", encoding="utf-8")
    values = document.values | {"DEEPSEEK_API_KEY": ""}
    with pytest.raises(ConfigurationError) as error:
        document.save(values)
    assert "DEEPSEEK_API_KEY" in str(error.value)
    assert "private-key" not in str(error.value)
    assert not document.path.exists()


def test_save_roundtrips_quotes_backslashes_unicode_and_preserves_unmanaged_text(
    document,
):
    document.text += "# keep comment\nCUSTOM_FLAG='${KEEP_UNEXPANDED}'\nDEMO_EMBEDDING_MODEL=existing\n"
    document.raw = editor._values(document.text)
    values = document.values | {"DEEPSEEK_API_KEY": "synthetic's\\folder #$中文"}
    document.save(values)
    parsed = dotenv_values(document.path, interpolate=False)
    assert parsed["DEEPSEEK_API_KEY"] == values["DEEPSEEK_API_KEY"]
    saved = document.path.read_text(encoding="utf-8")
    assert (
        "# keep comment\nCUSTOM_FLAG='${KEEP_UNEXPANDED}'\nDEMO_EMBEDDING_MODEL=existing\n"
        in saved
    )


def test_changed_value_retains_inline_comment_and_hash_in_quoted_value(document):
    document.text = document.text.replace(
        "DEEPSEEK_MODEL=deepseek-chat",
        "export DEEPSEEK_MODEL='old#model'  # retain this note",
    )
    document.raw = editor._values(document.text)
    document.save(document.values | {"DEEPSEEK_MODEL": "new#model"})
    assert dotenv_values(document.path)["DEEPSEEK_MODEL"] == "new#model"
    assert "# retain this note" in document.path.read_text(encoding="utf-8")


def test_duplicate_changed_keys_have_one_effective_value(document):
    document.text += "DEEPSEEK_MODEL=old-last\n"
    document.raw = editor._values(document.text)
    document.save(document.values | {"DEEPSEEK_MODEL": "new-model"})
    text = document.path.read_text(encoding="utf-8")
    assert text.count("DEEPSEEK_MODEL=") == 1
    assert dotenv_values(document.path)["DEEPSEEK_MODEL"] == "new-model"


def test_existing_lowercase_configuration_is_loaded_and_updated(tmp_path):
    path = tmp_path / "existing.env"
    path.write_text(
        "".join(f"{key.lower()}={value}\n" for key, value in valid_values().items()),
        encoding="utf-8",
    )
    document = editor.ConfigDocument(path, tmp_path / "unused-template")
    assert document.values["DEEPSEEK_API_KEY"] == "synthetic-key"
    document.save(document.values | {"DEEPSEEK_MODEL": "new-model"})
    assert (
        editor._values(path.read_text(encoding="utf-8"))["DEEPSEEK_MODEL"]
        == "new-model"
    )


def test_clearing_optional_value_is_explicit_but_blank_storage_uses_default(document):
    document.text += "VISION_MODEL=old-vision\nSTORAGE_ROOT=D:/old-storage\n"
    document.raw = editor._values(document.text)
    document.save(document.values | {"VISION_MODEL": "", "STORAGE_ROOT": ""})
    parsed = dotenv_values(document.path)
    assert parsed["VISION_MODEL"] == ""
    assert "STORAGE_ROOT" not in parsed


@pytest.mark.parametrize(
    "changes",
    [
        {"JWT_SECRET_KEY": "short-sensitive-secret"},
        {"CONFIDENCE_THRESHOLD": "2"},
        {"EMBEDDING_DIMENSION": "768"},
        {"DEEPSEEK_API_KEY": "key\nDEV_MODE=true"},
    ],
)
def test_invalid_settings_never_write_file_or_expose_secret(document, changes):
    with pytest.raises(ConfigurationError) as error:
        document.save(document.values | changes)
    assert "short-sensitive-secret" not in str(error.value)
    assert not document.path.exists()


def test_external_edit_is_not_overwritten(document):
    document.path.write_text("# someone else edited this\n", encoding="utf-8")
    before = document.path.read_bytes()
    with pytest.raises(ConfigurationError, match="其他程序修改"):
        document.save(document.values)
    assert document.path.read_bytes() == before


def test_failed_atomic_replace_preserves_original_and_removes_temporary(
    document, monkeypatch
):
    document.save(document.values)
    before = document.path.read_bytes()
    monkeypatch.setattr(
        editor.os, "replace", Mock(side_effect=OSError("synthetic failure"))
    )
    with pytest.raises(OSError):
        document.save(document.values | {"DEEPSEEK_MODEL": "new-model"})
    assert document.path.read_bytes() == before
    assert not list(document.path.parent.glob(".config-*"))


@pytest.fixture
def tk_root():
    if sys.platform != "win32" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        pytest.skip("Tk 表单需要图形会话，纯文件与启动器测试仍执行")
    tk = pytest.importorskip("tkinter", reason="仅本机表单测试需要可选 Tk 运行库")
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_real_tk_cancel_does_not_create_file_and_secrets_are_masked(document, tk_root):
    window = editor.ConfigurationWindow(tk_root, document)
    assert window.entries["DEEPSEEK_API_KEY"].cget("show") == "*"
    assert window.entries["DATABASE_URL"].cget("show") == "*"
    window.show_secrets.set(True)
    window.toggle_secrets()
    assert window.entries["DEEPSEEK_API_KEY"].cget("show") == ""
    window.cancel()
    assert not window.saved
    assert not document.path.exists()


def test_real_tk_save_button_persists_generated_secret_and_closes(document, tk_root):
    window = editor.ConfigurationWindow(tk_root, document)
    window.generate_secret()
    generated = window.variables["JWT_SECRET_KEY"].get()
    assert len(generated) >= 32
    window.save_button.invoke()
    assert window.saved
    assert dotenv_values(document.path)["JWT_SECRET_KEY"] == generated


def test_real_tk_validation_keeps_window_and_original_file(document, tk_root):
    document.save(document.values)
    before = document.path.read_bytes()
    window = editor.ConfigurationWindow(tk_root, document)
    window.variables["JWT_SECRET_KEY"].set("bad")
    window.save_button.invoke()
    assert not window.saved
    assert "JWT_SECRET_KEY" in window.status.get()
    assert tk_root.winfo_exists()
    assert document.path.read_bytes() == before


@pytest.mark.parametrize("saved", [True, False])
def test_configure_command_never_runs_business_preflight_or_child(
    tmp_path, monkeypatch, saved
):
    opened = Mock(return_value=saved)
    monkeypatch.setattr(editor, "open_configuration", opened)
    for name in ("configure", "check_models", "check_dependencies", "migrate"):
        monkeypatch.setattr(
            launcher,
            name,
            lambda *a: pytest.fail("configuration must not start business checks"),
        )
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.main(["--configure", "--config", str(tmp_path / ".env")]) == 0
    opened.assert_called_once()
    spawn.assert_not_called()


def test_frozen_first_launch_opens_default_editor_and_cancel_stops(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = Mock(return_value=False)
    monkeypatch.setattr(editor, "open_configuration", opened)
    monkeypatch.setattr(
        launcher, "configure", lambda *a: pytest.fail("cancel cannot load settings")
    )
    assert launcher.main([]) == 0
    assert opened.call_args.args[0] == tmp_path / "EduAgent/config.env"
    assert opened.call_args.kwargs["continue_launch"] is True


@pytest.mark.parametrize(
    "arguments", [["--no-browser"], ["--config", "missing-explicit.env"]]
)
def test_unattended_or_explicit_missing_config_never_opens_editor(
    tmp_path, monkeypatch, arguments
):
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened = Mock()
    monkeypatch.setattr(editor, "open_configuration", opened)
    assert launcher.main(arguments) == 1
    opened.assert_not_called()


def test_frozen_editor_refuses_installation_directory(document, monkeypatch):
    monkeypatch.setattr(editor.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        editor.sys, "_MEIPASS", str(document.path.parent / "_internal"), raising=False
    )
    monkeypatch.setattr(
        editor.sys, "executable", str(document.path.parent / "EduAgent.exe")
    )
    with pytest.raises(ConfigurationError, match="安装目录之外"):
        document.save(document.values)
    assert not document.path.exists()


def test_file_validation_does_not_read_the_working_directory_dotenv(
    document, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        "DEEPSEEK_API_KEY=unrelated-private-key\n", encoding="utf-8"
    )

    def forbid(*args, **kwargs):
        pytest.fail("file validation must not read another dotenv")

    monkeypatch.setattr(
        "pydantic_settings.sources.providers.dotenv.dotenv_values", forbid
    )
    document.save(document.values)
    assert document.path.exists()


@pytest.mark.parametrize(
    ("changes", "expected_fields"),
    [
        (
            {
                "DATABASE_URL": "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/eduagent",
                "EMBEDDING_PROVIDER": "openai_compatible",
                "EMBEDDING_MODEL": "",
                "EMBEDDING_API_KEY": "",
            },
            ("DATABASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"),
        ),
        (
            {
                "DATABASE_URL": "postgresql+psycopg://USER:synthetic-private-password@localhost/db"
            },
            ("DATABASE_URL",),
        ),
        (
            {"EMBEDDING_PROVIDER": "openai_compatible", "EMBEDDING_API_KEY": ""},
            ("EMBEDDING_API_KEY",),
        ),
        (
            {
                "EMBEDDING_PROVIDER": "openai_compatible",
                "EMBEDDING_MODEL": "",
                "EMBEDDING_API_KEY": "synthetic-private-embedding-key",
            },
            ("EMBEDDING_MODEL",),
        ),
        ({"EMBEDDING_MODEL": ""}, ("EMBEDDING_MODEL",)),
    ],
)
def test_incomplete_runtime_fields_preserve_existing_configuration(
    document, changes, expected_fields
):
    document.save(document.values)
    before = document.path.read_bytes()
    with pytest.raises(ConfigurationError) as error:
        document.save(document.values | changes)
    assert error.value.fields == expected_fields
    assert all(name in str(error.value) for name in expected_fields)
    assert "synthetic-private" not in str(error.value)
    assert "synthetic-key" not in str(error.value)
    assert document.path.read_bytes() == before


def test_complete_cloud_embedding_configuration_can_be_saved(document):
    values = document.values | {
        "EMBEDDING_PROVIDER": "openai_compatible",
        "EMBEDDING_MODEL": "synthetic-1024-model",
        "EMBEDDING_API_KEY": "synthetic-embedding-key",
    }
    document.save(values)
    saved = dotenv_values(document.path)
    assert saved["EMBEDDING_MODEL"] == values["EMBEDDING_MODEL"]
    assert saved["EMBEDDING_API_KEY"] == values["EMBEDDING_API_KEY"]


def test_real_tk_reports_all_incomplete_fields_without_discarding_key(
    document, tk_root
):
    window = editor.ConfigurationWindow(tk_root, document)
    window.variables["DATABASE_URL"].set(
        "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/eduagent"
    )
    window.variables["EMBEDDING_PROVIDER"].set("openai_compatible")
    window.variables["EMBEDDING_MODEL"].set("")
    window.save_button.invoke()
    assert not window.saved
    assert not document.path.exists()
    assert tk_root.winfo_exists()
    for field in ("DATABASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"):
        assert field in window.status.get()
    assert window.variables["DEEPSEEK_API_KEY"].get() == "synthetic-key"
    assert "synthetic-key" not in window.status.get()
