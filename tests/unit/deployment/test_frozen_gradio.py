from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.freeze_gradio import freeze_type_hint_writer

PROBE = r"""
import importlib.abc, importlib.util, json, sys
from pathlib import Path
if sys.argv[1] != "original":
    frozen = Path(sys.argv[1])
    class Loader(importlib.abc.Loader):
        def create_module(self, spec): return None
        def exec_module(self, module):
            exec(compile(frozen.read_text(encoding="utf-8"), str(frozen), "exec"), module.__dict__)
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "gradio.component_meta":
                return importlib.util.spec_from_file_location(fullname, frozen, loader=Loader())
    sys.meta_path.insert(0, Finder())
import gradio as gr
from gradio.exceptions import ComponentDefinitionError
def echo(value: str) -> dict:
    return {"text": value}
with gr.Blocks(analytics_enabled=False) as demo:
    text = gr.Textbox(label="Input", value="before")
    result = gr.JSON(label="Result")
    button = gr.Button("Run")
    button.click(echo, inputs=text, outputs=result, api_name="echo")
try:
    class Invalid(gr.Textbox):
        EVENTS = [object()]
except ComponentDefinitionError:
    rejected = True
else:
    rejected = False
print(json.dumps({"components": demo.config["components"],
                  "dependencies": demo.config["dependencies"],
                  "api": demo.get_api_info(),
                  "result": demo.fns[0].fn("after"),
                  "invalid_events_rejected": rejected}, sort_keys=True))
"""


def test_frozen_real_gradio_preserves_events_components_and_api(tmp_path):
    origin = importlib.util.find_spec("gradio").origin
    source = Path(origin).with_name("component_meta.py")
    original_bytes = source.read_bytes()
    patched = freeze_type_hint_writer(source, tmp_path / "component_meta.py")
    assert source.read_bytes() == original_bytes
    env = os.environ | {
        "GRADIO_ANALYTICS_ENABLED": "False",
        "PYTHONIOENCODING": "utf-8",
    }
    observed = []
    for variant in ("original", str(patched)):
        run = subprocess.run(
            [sys.executable, "-c", PROBE, variant],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
            timeout=45,
        )
        observed.append(json.loads(run.stdout))
    assert observed[0] == observed[1]
    assert observed[1]["result"] == {"text": "after"}
    assert observed[1]["invalid_events_rejected"] is True
    assert "/echo" in observed[1]["api"]["named_endpoints"]


@pytest.mark.parametrize(
    "source",
    [
        "def different(): pass\n",
        "def create_or_modify_pyi(changed): pass\n",
    ],
)
def test_unrecognized_type_hint_writer_requires_review(tmp_path, source):
    original = tmp_path / "original.py"
    original.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="Review"):
        freeze_type_hint_writer(original, tmp_path / "frozen.py")
    assert not (tmp_path / "frozen.py").exists()


def _indexed_api_method(tmp_path):
    import ast

    import gradio.blocks as blocks_module

    from scripts.freeze_gradio import freeze_api_component_lookup

    source = Path(blocks_module.__file__)
    original = source.read_bytes()
    frozen = freeze_api_component_lookup(source, tmp_path / "blocks.py")
    assert source.read_bytes() == original
    tree = ast.parse(frozen.read_text(encoding="utf-8"))
    blocks = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Blocks"
    )
    method = next(
        n
        for n in blocks.body
        if isinstance(n, ast.FunctionDef) and n.name == "get_api_info"
    )
    namespace = vars(blocks_module).copy()
    code = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(  # noqa: S102 - trusted build copy
        compile(code, str(frozen), "exec"), namespace
    )
    return namespace["get_api_info"]


def test_indexed_api_information_matches_full_production_workbench(
    tmp_path, monkeypatch
):
    import time

    from backend.app.ui.gradio_app import create_gradio_app

    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    method = _indexed_api_method(tmp_path)
    demo = create_gradio_app()
    for all_endpoints in (False, True):
        start = time.perf_counter()
        original = demo.get_api_info(all_endpoints=all_endpoints)
        original_seconds = time.perf_counter() - start
        start = time.perf_counter()
        indexed = method(demo, all_endpoints=all_endpoints)
        indexed_seconds = time.perf_counter() - start
        assert original == indexed
        assert len(indexed["named_endpoints"]) > 100
        print(
            {
                "all_endpoints": all_endpoints,
                "original_seconds": original_seconds,
                "indexed_seconds": indexed_seconds,
            }
        )


@pytest.mark.parametrize("change", ["missing", "duplicate"])
def test_indexed_api_keeps_missing_and_first_duplicate_component_semantics(
    tmp_path, change
):
    import copy

    import gradio as gr

    method = _indexed_api_method(tmp_path)
    with gr.Blocks(analytics_enabled=False) as demo:
        value = gr.Textbox(label="First", value="a")
        output = gr.JSON()
        gr.Button().click(lambda text: {"text": text}, inputs=value, outputs=output)
    if change == "missing":
        demo.config["components"] = [
            c for c in demo.config["components"] if c["id"] != value._id
        ]
    else:
        duplicate = copy.deepcopy(
            next(c for c in demo.config["components"] if c["id"] == value._id)
        )
        duplicate["props"]["label"] = "Shadow"
        demo.config["components"].append(duplicate)
    assert method(demo) == demo.get_api_info()
