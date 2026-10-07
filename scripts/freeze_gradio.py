"""Build-only copies of two profiled Gradio hot spots.

Skip IDE stub generation and index API component lookup in frozen builds.
Installed Gradio and source-mode application behavior remain unchanged.
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


def freeze_api_component_lookup(source: Path, destination: Path) -> Path:
    """Replace two quadratic component searches with a per-call first-ID index."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    blocks = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Blocks"
    )
    method = next(
        n
        for n in blocks.body
        if isinstance(n, ast.FunctionDef) and n.name == "get_api_info"
    )
    config_assignments = [
        i
        for i, n in enumerate(method.body)
        if isinstance(n, ast.Assign)
        and ast.dump(n) == ast.dump(ast.parse("config = self.config").body[0])
    ]
    if len(config_assignments) != 1:
        raise ValueError("Review changed Gradio API configuration lookup")
    index_name = "_frozen_components_by_id"
    if any(isinstance(n, ast.Name) and n.id == index_name for n in ast.walk(method)):
        raise ValueError("Review Gradio API lookup local names")
    initializer = ast.parse("""_frozen_components_by_id = {}
for _frozen_component in config["components"]:
    _frozen_components_by_id.setdefault(_frozen_component["id"], _frozen_component)
""").body

    class Lookup(ast.NodeTransformer):
        count = 0

        def visit_For(self, node):
            if (
                not isinstance(node.target, ast.Name)
                or node.target.id != "component"
                or ast.dump(node.iter)
                != ast.dump(ast.parse('config["components"]', mode="eval").body)
            ):
                return self.generic_visit(node)
            if len(node.body) != 1 or not isinstance(node.body[0], ast.If):
                raise ValueError("Review changed Gradio component search")
            condition = node.body[0]
            if (
                not isinstance(condition.test, ast.Compare)
                or len(condition.test.ops) != 1
                or not isinstance(condition.test.ops[0], ast.Eq)
                or ast.dump(condition.test.left)
                != ast.dump(ast.parse('component["id"]', mode="eval").body)
                or len(condition.body) != 1
                or not isinstance(condition.body[0], ast.Break)
                or condition.orelse
                or ast.dump(ast.Module(body=node.orelse, type_ignores=[]))
                != ast.dump(ast.parse("skip_endpoint = True\nbreak"))
            ):
                raise ValueError("Review changed Gradio missing-component behavior")
            self.count += 1
            return [
                ast.Assign(
                    targets=[ast.Name(id="component", ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id=index_name, ctx=ast.Load()),
                            attr="get",
                            ctx=ast.Load(),
                        ),
                        args=[condition.test.comparators[0]],
                        keywords=[],
                    ),
                ),
                ast.If(
                    test=ast.Compare(
                        left=ast.Name(id="component", ctx=ast.Load()),
                        ops=[ast.Is()],
                        comparators=[ast.Constant(value=None)],
                    ),
                    body=node.orelse,
                    orelse=[],
                ),
            ]

    lookup = Lookup()
    lookup.visit(method)
    if lookup.count != 2:
        raise ValueError("Review changed Gradio API component search count")
    insert_at = config_assignments[0] + 1
    method.body[insert_at:insert_at] = initializer
    ast.fix_missing_locations(tree)
    contents = ast.unparse(tree) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_text(encoding="utf-8") != contents:
        destination.write_text(contents, encoding="utf-8")
    return destination
