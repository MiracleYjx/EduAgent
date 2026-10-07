"""Build-only Gradio adjustment: frozen distributions do not generate IDE stubs.

The pinned upstream function only reads source and writes .pyi files. Keep its
signature and every other AST node, including ComponentMeta event validation.
Installed/source-mode Gradio is never modified.
"""

from __future__ import annotations

import ast
from pathlib import Path


def freeze_type_hint_writer(source: Path, destination: Path) -> Path:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    targets = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_or_modify_pyi"
    ]
    if len(targets) != 1:
        raise ValueError("Review Gradio type-hint writer before freezing this version")
    writer = targets[0]
    if [arg.arg for arg in writer.args.args] != [
        "component_class",
        "class_name",
        "events",
    ]:
        raise ValueError("Review changed Gradio type-hint writer signature")
    writer.body = [ast.Return(value=ast.Constant(value=None))]
    ast.fix_missing_locations(tree)
    contents = ast.unparse(tree) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_text(encoding="utf-8") != contents:
        destination.write_text(contents, encoding="utf-8")
    return destination
