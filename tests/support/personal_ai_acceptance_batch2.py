"""Frozen T083 batch-two samples and request plan (P1/V1/R1/T1).

This module is intentionally data-only.  It reads the frozen acceptance
fixtures, verifies their hashes, and exposes the exact request/token plan used
by the offline batch-two tests.  It never reads credentials or sends HTTP.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "personal_ai_acceptance"
SAMPLES_PATH = FIXTURES / "samples.json"
P1_PDF = FIXTURES / "cross-page-paper.pdf"
V1_PNG = FIXTURES / "linear-function.png"

SAMPLES: dict[str, Any] = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
SCENARIOS = {item["id"]: item for item in SAMPLES["scenarios"]}

# Frozen scenario values that are not repeated verbatim in samples.json.
T1_MESSAGE = "一次函数 斜率 错误 1/2"
R1_QUESTION = "长方形长20米、宽5米，求面积，写出计算式、结果和单位。"
R1_OLD_ANSWER = "50平方米"
R1_NEW_ANSWER = "20×5=100平方米"
R1_STUDENT_ANSWER = "20×5=100平方米"
R1_RUBRIC = (
    "选择长乘宽并写出20×5得4分；计算结果100得4分；单位为平方米得2分。"
)
V1_QUESTION = SAMPLES["vision_oracle"]["question"]
V1_EXPECTED = SAMPLES["vision_oracle"]["expected"]
V1_IMAGE_SHA256 = SAMPLES["vision_oracle"]["image_sha256"]
V1_IMAGE_BYTES = SAMPLES["vision_oracle"]["image_bytes"]
V1_IMAGE_SIZE = tuple(SAMPLES["vision_oracle"]["image_size"])
V1_RETRIEVED_MATERIALS = tuple(
    SAMPLES["vision_oracle"]["input_boundary"]["retrieved_material_ids"]
)

# V1 method-style fixture (user decision 2026-10-21, isolation option A).
# The reference answer states only the method and the student answer is
# deliberately wrong, so no sent text carries the graph points, the numeric
# slope/intercept or the function answer.  The evaluation-only ``expected``
# value stays in samples.json and is never used as a sent field.
V1_REFERENCE_ANSWER = (
    "由图读出直线经过的两点，用 k=(y₂−y₁)/(x₂−x₁) 求斜率，"
    "再由 x=0 时的 y 值确定截距，写出 y=kx+b 的表达式。"
)
V1_RUBRIC = (
    "正确读出图中两点坐标得4分；正确计算斜率得3分；"
    "正确确定截距并写出解析式得3分。"
)
V1_STUDENT_ANSWER = "y=3x+2，斜率为3，截距为2。"
#: Content classes the frozen isolation clause forbids in any V1 sent text.
V1_FORBIDDEN_TEXT = ("(0,1)", "(2,5)", "斜率2", "截距1", "y=2x+1")

# Offline request plan.  The real-call plan is the same five primary requests:
# P1 1 + V1 2 + R1 1 + T1 1.
SCENARIO_PLAN: dict[str, dict[str, Any]] = {
    "P1": {
        "tasks": {"paper_extraction": 1},
        "requests": 1,
        "input_tokens": 8_000,
        "output_tokens": 2_000,
        "source": "cross-page-paper.pdf",
    },
    "V1": {
        "tasks": {"vision": 1, "embedding": 1},
        "requests": 2,
        "input_tokens": 8_000,
        "output_tokens": 1_500,
        "embedding_dimension": 1_024,
        "source": "linear-function.png",
    },
    "R1": {
        "tasks": {"subjective_grading": 1},
        "requests": 1,
        "input_tokens": 4_000,
        "output_tokens": 1_200,
        "source": "50平方米 -> 20×5=100平方米",
    },
    "T1": {
        "tasks": {"default_llm": 1},
        "requests": 1,
        "input_tokens": 6_000,
        "output_tokens": 1_500,
        "source": "斜率错误",
    },
}
TOTAL_REQUESTS = sum(item["requests"] for item in SCENARIO_PLAN.values())

# Strict T1 needs a chunk containing the word "错误".  The already indexed G2
# linear-function chunks contain 斜率/截距 but not 错误, so a strict
# keyword-only T1 requires one local T1-material index request (an Embedding
# request) before the one teacher text request.  This is a conditional
# requirement, not part of the base five-request plan.
T1_NEW_MATERIAL_INDEX_REQUESTS = 1
TOTAL_REQUESTS_WITH_T1_MATERIAL = TOTAL_REQUESTS + T1_NEW_MATERIAL_INDEX_REQUESTS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedding_vector(dimension: int = 1_024) -> list[float]:
    """Deterministic vector used only to make the offline index retrievable."""

    if dimension < 1:
        raise ValueError("dimension must be positive")
    return [1.0] + [0.0] * (dimension - 1)


def assert_frozen_inputs() -> dict[str, Any]:
    """Verify the frozen acceptance inputs and return a compact fact sheet.

    The literal assertions below intentionally mirror samples.json.  Changing a
    fixture without updating the oracle must fail here, not silently pass.
    """

    p1 = SCENARIOS["P1"]
    v1 = SCENARIOS["V1"]
    r1 = SCENARIOS["R1"]
    t1 = SCENARIOS["T1"]

    assert P1_PDF.is_file() and V1_PNG.is_file()
    assert P1_PDF.stat().st_size == p1["paper_bytes"] == 147_632
    assert sha256_file(P1_PDF) == p1["paper_sha256"] == (
        "d647897413daf3e142b5be479e42eb9c62e57a149b5def3553bff075e13acdb1"
    )
    assert p1["page_count"] == 2 and p1["pdf_text_layer"] is False
    assert list(p1["scan_size_per_page"]) == [1240, 1754]
    assert p1["render_dpi"] == 150

    assert V1_PNG.stat().st_size == V1_IMAGE_BYTES == 32_642
    assert sha256_file(V1_PNG) == V1_IMAGE_SHA256 == (
        "1c04d5699cb3dc1ad83e4907c8058f42522cd4f7ffd24a921355e13a0145221e"
    )
    assert list(V1_IMAGE_SIZE) == [1200, 1000]
    assert V1_RETRIEVED_MATERIALS == ("linear-function",)
    boundary = SAMPLES["vision_oracle"]["input_boundary"]
    # The frozen oracle names the positive material only.  It does not require
    # an area-distractor to be indexed for V1, so no extra indexing request is
    # part of this plan.
    assert "distractor_material_ids" not in boundary
    assert "area-distractor" not in json.dumps(boundary, ensure_ascii=False)

    assert r1["action"] == "explicit_revision_regrade"
    assert "50平方米" in r1["oracle"] and "100平方米" in r1["oracle"]
    assert t1["action"] == "teacher_first_message"
    assert "斜率错误" in t1["oracle"]

    assert SCENARIO_PLAN["P1"]["tasks"] == {"paper_extraction": 1}
    assert SCENARIO_PLAN["V1"]["tasks"] == {"vision": 1, "embedding": 1}
    assert SCENARIO_PLAN["R1"]["tasks"] == {"subjective_grading": 1}
    assert SCENARIO_PLAN["T1"]["tasks"] == {"default_llm": 1}
    assert TOTAL_REQUESTS == 5
    assert T1_NEW_MATERIAL_INDEX_REQUESTS == 1
    assert TOTAL_REQUESTS_WITH_T1_MATERIAL == 6

    return {
        "sample_version": SAMPLES["sample_version"],
        "p1": {
            "file": P1_PDF.name,
            "bytes": P1_PDF.stat().st_size,
            "sha256": sha256_file(P1_PDF),
            "pages": p1["page_count"],
        },
        "v1": {
            "file": V1_PNG.name,
            "bytes": V1_PNG.stat().st_size,
            "sha256": sha256_file(V1_PNG),
            "size": list(V1_IMAGE_SIZE),
            "retrieved_materials": list(V1_RETRIEVED_MATERIALS),
            "area_distractor_required": False,
        },
        "r1": {"old": R1_OLD_ANSWER, "new": R1_NEW_ANSWER},
        "t1": {"message": T1_MESSAGE},
        "plan": {
            scenario: dict(plan) for scenario, plan in SCENARIO_PLAN.items()
        },
        "total_requests": TOTAL_REQUESTS,
        "t1_new_material_index_requests": T1_NEW_MATERIAL_INDEX_REQUESTS,
        "total_requests_with_t1_material": TOTAL_REQUESTS_WITH_T1_MATERIAL,
    }


def linear_function_docx() -> bytes:
    """Build the offline V1 study material containing the frozen PNG.

    The image bytes are embedded unchanged; the resulting material asset SHA
    must equal the frozen PNG SHA.  The text is the frozen linear-function
    material from samples.json.
    """

    from docx import Document as Docx
    from docx.shared import Inches

    document = Docx()
    document.add_paragraph(SAMPLES["materials"][0]["text"])
    document.add_picture(str(V1_PNG), width=Inches(4))
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


__all__ = [
    "FIXTURES",
    "P1_PDF",
    "R1_NEW_ANSWER",
    "R1_OLD_ANSWER",
    "R1_QUESTION",
    "R1_RUBRIC",
    "R1_STUDENT_ANSWER",
    "SAMPLES",
    "SAMPLES_PATH",
    "SCENARIOS",
    "SCENARIO_PLAN",
    "T1_MESSAGE",
    "T1_NEW_MATERIAL_INDEX_REQUESTS",
    "TOTAL_REQUESTS",
    "TOTAL_REQUESTS_WITH_T1_MATERIAL",
    "V1_EXPECTED",
    "V1_IMAGE_BYTES",
    "V1_IMAGE_SHA256",
    "V1_FORBIDDEN_TEXT",
    "V1_IMAGE_SIZE",
    "V1_PNG",
    "V1_QUESTION",
    "V1_REFERENCE_ANSWER",
    "V1_RETRIEVED_MATERIALS",
    "V1_RUBRIC",
    "V1_STUDENT_ANSWER",
    "assert_frozen_inputs",
    "embedding_vector",
    "linear_function_docx",
    "sha256_file",
]
