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
