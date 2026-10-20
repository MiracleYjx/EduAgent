"""T004：本地质量协议 Spike；最小 TCR 见 docs/test-change-record-personal-learning.md。

执行生产函数，不实现 test-local 准入/评分 validator。脚本 Provider 仅返回预置数据；
内存持久层只替代身份读取、数据库和报告展示，保留生产 run_validation、_report、
_transaction、finish_validation（包括终局 gate）原方法。它不验证真实来源、权限、
持久化、can_review 或练习准入，也不证明真实模型准确率。

current_gap / characterization 保留当前能力边界；多选漏选的新要求已在 T051 落实为 0 分。
空白主观题的旧拒绝，不冒充新“提示后允许提交、本地零分且零 LLM”闭环。
图表样例只有固定真值与 Schema 缺口观察，没有自造图表 validator。
单次语义调用计数不等于完整生成操作/HTTP 零重试；后者由 T005 单独验证。

只使用内存合成数据，不调用持久化 question_validation_fixtures 的 seed/pass 函数，
但沿用其 source_data 字段形状及同组 support 中的 StubSemanticProvider。
不构造 AppSettings、不读取 dotenv/凭据/业务 storage、不连接数据库或外部网络。
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.ai.agents.question_agent import QuestionGenerationPayload
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.models import QuestionValidationResult
from backend.app.schemas.ai import QuestionCandidate
from backend.app.schemas.content_validation import (
    SemanticMachineOutput,
    SemanticQuestionFields,
    SemanticValidationInput,
    ValidationEvidence,
    ValidationInputRefs,
)
from backend.app.schemas.grading import (
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
    ScoringInput,
    same_fixed_scoring_input,
)
from backend.app.schemas.question_assets import QuestionAssetView
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.grading_context import (
    GradingInputError,
    SubjectiveGradingSource,
)
from backend.app.services.grading.objective_grader import (
    InvalidMaxScoreError,
    ObjectiveGrader,
    is_blank_answer,
)
from backend.app.services.grading.scoring_images import (
    GradingImageError,
    prepare_scoring_images,
)
from backend.app.services.grading.subjective_grader import (
    ConfidenceOutOfRangeError,
    InvalidLLMResponseError,
    ScoreOutOfRangeError,
    parse_subjective_payload,
)
from backend.app.services.question_validator import (
    QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
    QUESTION_SCORE_NOT_STORABLE,
    QUESTION_UNKNOWN_COURSE_EVIDENCE,
    QuestionValidator,
    validate_semantic_fields,
)
from tests.support.semantic_validation_doubles import StubSemanticProvider

QUESTION_ID = UUID(int=1)
COURSE_ID = UUID(int=2)
DOCUMENT_ID = UUID(int=3)
CHUNK_ID = UUID(int=4)
EVIDENCE_ID = UUID(int=5)
ACTOR_ID = UUID(int=6)
CHECK_KINDS = (
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
)


def _fields(**changes: Any) -> SemanticQuestionFields:
    data = {
        "type": QuestionType.SINGLE_CHOICE,
        "content": "3 + 4 等于多少？",
        "options": {"A": "7", "B": "8"},
        "reference_answer": "A",
        "scoring_rubric": "选择完全正确得满分10分，其余不得分。",
        "analysis": "3 + 4 = 7。",
        "score": Decimal("10.00"),
    }
    data.update(changes)
    return SemanticQuestionFields.model_validate(data)


def _candidate(**changes: Any) -> QuestionCandidate:
    data = {
        "question_type": QuestionType.SINGLE_CHOICE,
        "content": "3 + 4 等于多少？",
        "options": ["7", "8"],
        "reference_answer": "7",
        "scoring_rubric": "选择完全正确得满分10分，其余不得分。",
        "difficulty": "受控样例",
        "knowledge_points": ["加法"],
        "score": 10.0,
        "source_context_ids": [str(CHUNK_ID)],
    }
    data.update(changes)
    return QuestionCandidate.model_validate(data)


def _semantic_input(**changes: Any) -> SemanticValidationInput:
    # 字段形状沿用 question_validation_fixtures；没有写入或确认任何真实教材。
    evidence = ValidationEvidence(
        evidence_id=EVIDENCE_ID,
        kind="chunk",
        source_id=CHUNK_ID,
        source_data={
            "chunk_id": str(CHUNK_ID),
            "document_id": str(DOCUMENT_ID),
            "course_id": str(COURSE_ID),
            "source_file": "synthetic-quality-spike.txt",
            "location": "合成段落1",
            "content_snapshot": "受控协议数据：3 + 4 = 7；不是独立教师真值。",
        },
    )
    return SemanticValidationInput(
        question_id=QUESTION_ID,
        input_revision=1,
        run_no=1,
        fields=_fields(**changes),
        evidence=[evidence],
    )


def _script(
    *,
    changed_kind: str | None = None,
    verdict: str = "pass",
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """仅装配预置响应，不读题、不推理、不裁决准入。"""
    refs = [str(EVIDENCE_ID)] if evidence_refs is None else evidence_refs
    return {
        "checks": [
            {
                "kind": kind,
                "verdict": verdict if kind == changed_kind else "pass",
                "reason": "受控脚本结论；只测协议，不代表模型质量或独立证明。",
                "evidence_refs": list(refs),
            }
            for kind in CHECK_KINDS
        ],
        "issues": [],
    }


class _RawScriptOutput(BaseModel):
    # 故意不在 Fake 中使用生产输出 Schema；由生产语义边界执行真正的校验。
    model_config = ConfigDict(extra="allow")


class _ScriptedProvider(StubSemanticProvider):
    provider_name = "scripted-quality-spike"
    model_name = "scripted-not-a-real-model"

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__()
        self.script = deepcopy(script)

    async def generate_structured(self, messages, schema, model=None) -> BaseModel:
        self.calls.append(json.loads(str(messages[1]["content"])))
        return _RawScriptOutput.model_validate(deepcopy(self.script))


class _MemoryReportSession:
    """只有报告行存取/计数；不解释四项结果，也不创建 SQLAlchemy Session。"""

    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def get(self, model, identity):
        assert model is QuestionValidationResult
        return self.row if identity == self.row.id else None

    def scalars(self, statement):
        # 生产 _report 构造查询；本 Fake 不执行 SQL，只提供本轮报告存取。
        return SimpleNamespace(one_or_none=lambda: self.row)

    def scalar(self, statement):
        return self.row.run_no

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _MemoryContentValidationService(ContentValidationService):
    """不覆盖生产 run_validation/finish_validation 或其状态判定。

    prepare_validation 只提供合成快照，故这些测试不覆盖生产准备阶段的来源/权限
    核对。_report_view 只回显存储行，不制造可用性、can_review 或审批结论。
    """

    def __init__(self, value: SemanticValidationInput) -> None:
        self.value = value
        self.question = SimpleNamespace(
            id=value.question_id,
            status=QuestionStatus.PENDING_REVIEW,
            validation_revision=value.input_revision,
            frozen_at=None,
        )
        refs = ValidationInputRefs(
            fields=list(value.fields.model_dump()), evidence=value.evidence
        )
        self.row = SimpleNamespace(
            id=UUID(int=10),
            question_id=value.question_id,
            input_revision=value.input_revision,
            run_no=value.run_no,
            input_refs=refs.model_dump(mode="json"),
            outcome="running",
            checks=None,
            issues=None,
            error=None,
            provenance={},
            completed_at=None,
        )
        self.session = _MemoryReportSession(self.row)
        self._runtime = None
        self._semantic_provider_factory = None

    def prepare_validation(self, question_id, *, actor_id, **kwargs):
        assert question_id == self.question.id and actor_id == ACTOR_ID
        return SimpleNamespace(report=self.row, input=self.value)

    def _owner(self, kind, identity, actor_id, *, writing=False):
        assert (kind, identity, actor_id) == ("question", self.question.id, ACTOR_ID)
        return self.question

    def _report_view(self, question, report, actor_id):
        assert question is self.question and actor_id == ACTOR_ID
        return report


def _run_script(value: SemanticValidationInput, script: dict[str, Any]):
    service = _MemoryContentValidationService(value)
    provider = _ScriptedProvider(script)
    # 防止本地 harness 无意间接管真正需要测试的生产入口/终局 gate。
    assert type(service).run_validation is ContentValidationService.run_validation
    assert type(service).finish_validation is ContentValidationService.finish_validation
    returned = asyncio.run(
        service.run_validation(QUESTION_ID, actor_id=ACTOR_ID, provider=provider)
    )
    assert returned is service.row
    assert len(provider.calls) == 1
    assert provider.calls[0]["fields"] == value.fields.model_dump(mode="json")
    assert provider.calls[0]["evidence"] == [
        item.model_dump(mode="json") for item in value.evidence
    ]
    assert service.row.completed_at is not None
    assert service.session.commits == 1 and service.session.rollbacks == 0
    assert not provider.closed  # 调用方拥有注入的 Provider；不是资源泄漏断言。
    return service, provider


@pytest.mark.parametrize(
    "changes,kind,verdict,outcome",
    [
        pytest.param({}, None, "pass", "passed", id="correct-answer-script"),
        pytest.param(
            {"reference_answer": "B"},
            "answer_correctness", "fail", "failed", id="wrong-answer-script",
        ),
        pytest.param(
            {"content": "矩形长为3，求面积。", "analysis": None},
            "condition_sufficiency", "insufficient_evidence", "failed",
            id="missing-condition-script",
        ),
        pytest.param(
            {"content": "选择等于二分之一的数。", "options": {"A": "0.5", "B": "1/2"},
             "analysis": None},
            "option_ambiguity", "needs_review", "failed", id="ambiguous-options-script",
        ),
    ],
)
def test_real_terminal_gate_preserves_independent_scripted_checks_without_second_call(
    changes, kind, verdict, outcome
):
    value = _semantic_input(**changes)
    # 错答案/缺条件/语义等价选项都可编码合法；这里不能把结构通过当内容正确。
    assert validate_semantic_fields(value.fields) == ()
    service, provider = _run_script(
        value, _script(changed_kind=kind, verdict=verdict)
    )
    assert service.row.outcome == outcome
    assert service.question.status is (
        QuestionStatus.PENDING_REVIEW if outcome == "passed" else QuestionStatus.NEEDS_REVISION
    )
    assert service.question.status not in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}
    assert {item["kind"]: item["verdict"] for item in service.row.checks} == {
        check: verdict if check == kind else "pass" for check in CHECK_KINDS
    }
    assert service.row.provenance["provider_name"] == provider.provider_name
    assert service.row.provenance["model"] == "scripted-not-a-real-model"


def test_real_terminal_gate_holds_all_pass_with_warning_without_second_call():
    script = _script()
    script["issues"] = [{
        "code": "SYNTHETIC_UNRESOLVED_ISSUE", "field": "analysis",
        "severity": "warning", "message": "受控未解决疑点，不得被四项pass掩盖。",
        "evidence_refs": [str(EVIDENCE_ID)],
    }]
    service, _provider = _run_script(_semantic_input(), script)
    assert service.row.outcome == "failed"
    assert service.row.issues[0]["severity"] == "warning"
    assert service.question.status is QuestionStatus.NEEDS_REVISION


def test_unknown_model_evidence_is_technical_error_not_pass_or_zero():
    script = _script()
    script["checks"][0]["evidence_refs"] = [str(UUID(int=999))]
    service, _provider = _run_script(_semantic_input(), script)
    assert service.row.outcome == "technical_error"
    assert service.row.error["code"] == "CONTENT_OUTPUT_INVALID"
    assert service.row.error["retryable"] is False
    assert service.row.checks is None and service.row.issues is None
    assert service.question.status is QuestionStatus.PENDING_REVIEW


@pytest.mark.parametrize("malformation", ["missing_check", "duplicate_check", "forged_approval"])
def test_production_semantic_schema_rejects_malformed_scripted_output(malformation):
    script = _script()
    if malformation == "missing_check":
        script["checks"].pop()
    elif malformation == "duplicate_check":
        script["checks"][-1] = deepcopy(script["checks"][0])
    else:
        script["can_review"] = True
    service, _provider = _run_script(_semantic_input(), script)
    assert service.row.outcome == "technical_error"
    assert service.row.error["code"] == "CONTENT_OUTPUT_INVALID"
    assert service.question.status is QuestionStatus.PENDING_REVIEW


def test_current_gap_empty_model_evidence_refs_can_pass_terminal_gate():
    """Characterization：空引用满足当前子集检查，不是充分依据已实现。"""
    service, _provider = _run_script(_semantic_input(), _script(evidence_refs=[]))
    assert service.row.outcome == "passed"
    assert all(item["evidence_refs"] == [] for item in service.row.checks)


@pytest.mark.parametrize(
    "changes,code",
    [
        pytest.param({"source_context_ids": [str(UUID(int=999))]},
                     QUESTION_UNKNOWN_COURSE_EVIDENCE, id="unknown-chunk"),
        pytest.param({"reference_answer": "C"},
                     QUESTION_ANSWER_ENCODING_INCOMPATIBLE, id="illegal-answer-encoding"),
        pytest.param({"score": 10.001},
                     QUESTION_SCORE_NOT_STORABLE, id="unstoreable-score-precision"),
    ],
)
def test_real_candidate_validator_rejects_deterministic_failures(changes, code):
    result = QuestionValidator().validate_candidate(
        _candidate(**changes), retrieved_context_ids=[str(CHUNK_ID)]
    )
    assert not result.is_valid
    assert result.status is QuestionStatus.NEEDS_REVISION
    assert code in {issue.code for issue in result.issues}


def test_real_semantic_field_validator_rejects_normalized_duplicate_options():
    issues = validate_semantic_fields(_fields(options={"A": "７", "B": "7"}))
    assert QUESTION_ANSWER_ENCODING_INCOMPATIBLE in {issue.code for issue in issues}
    assert any(issue.field == "options" for issue in issues)


def test_current_gap_chart_conflict_is_not_checked_by_existing_encoding_schema():
    """固定真值+真实 Schema 观察；不实现图表准入判定或执行模型代码。"""
    chart = {"kind": "bar", "labels": ["甲", "乙"], "values": [2, 4],
             "x_axis": "组别", "y_axis": "数量", "unit": "个"}
    fields = _fields(
        content="柱状图甲组2个、乙组4个，求合计。",
        options={"A": "6", "B": "8"}, reference_answer="B", analysis="合计为8。",
    )
    # 只固定样例 oracle；sum 不参与生产 validator，也不产生本地准入结论。
    assert sum(chart["values"]) == 6
    assert fields.options[fields.reference_answer] == "8"
    assert validate_semantic_fields(fields) == ()
    with pytest.raises(ValidationError) as error:
        # chart 是演示用未批准扩展名，不是本 Spike 定义的新产品 DTO。
        SemanticQuestionFields.model_validate({**fields.model_dump(), "chart": chart})
    assert any(item["loc"] == ("chart",) and item["type"] == "extra_forbidden"
               for item in error.value.errors())


def test_current_gap_generation_payload_has_no_same_response_quality_envelope():
    candidate = _candidate()
    assert set(QuestionGenerationPayload.model_fields) == {"candidates"}
    with pytest.raises(ValidationError) as error:
        QuestionGenerationPayload.model_validate({
            "candidates": [candidate.model_dump()], "quality_assessment": _script(),
        })
    assert any(item["loc"] == ("quality_assessment",) and item["type"] == "extra_forbidden"
               for item in error.value.errors())


def _asset(number: int, order: int) -> QuestionAssetView:
    return QuestionAssetView(
        id=UUID(int=100 + number), question_id=QUESTION_ID,
        file_id=f"synthetic-unread-image-{number}", asset_type="diagram",
        width=32, height=32, caption=None, source_page_id=None, region=None,
        order_index=order, student_visible=True,
    )


def _fixed_input(**changes: Any) -> ScoringInput:
    data = {
        "exam_id": str(UUID(int=20)), "exam_question_id": str(UUID(int=21)),
        "question_id": str(QUESTION_ID), "submission_id": str(UUID(int=22)),
        "answer_id": str(UUID(int=23)), "student_id": str(UUID(int=24)),
        "course_id": str(COURSE_ID), "order": 1, "question_validation_revision": 1,
        "question_type": QuestionType.SHORT_ANSWER,
        "question_content": "根据所给条件说明结论。", "options": None,
        "order_preserved": True, "reference_answer": "7",
        "source_rubric": "回答正确要点得满分10分，其余不得分。",
        "effective_score": Decimal("10.00"), "base_score": Decimal("10.00"),
        "scoring_basis": {"kind": "subjective", "points": [], "additive": False,
                          "rounding_delta": None},
        "published_knowledge_points": ["受控知识点"], "student_answer": "7",
    }
    data.update(changes)
    return ScoringInput.model_validate(data)


def test_missing_key_image_evidence_fails_before_any_file_or_database_read():
    fixed = _fixed_input(assets=[_asset(1, 1)])
    source = SubjectiveGradingSource(
        question_type=fixed.question_type, course_id=fixed.course_id,
        question_content=fixed.question_content, reference_answer=fixed.reference_answer,
        scoring_rubric=fixed.source_rubric, student_answer=fixed.student_answer,
        question_id=fixed.question_id, answer_id=fixed.answer_id,
        submission_id=fixed.submission_id,
        knowledge_points=tuple(fixed.published_knowledge_points), scoring_input=fixed,
        teacher_id=None,
    )
    # 裸 object 没有任何数据库/文件接口；越过前置拒绝将导致此测试失败。
    with pytest.raises(GradingImageError) as error:
        prepare_scoring_images(object(), source)
    assert error.value.error_code == "VISION_REVIEW_REQUIRED"
    assert fixed.verified_image_conditions is None


def test_real_objective_grader_scores_local_cases_and_rejects_invalid_maxima(monkeypatch):
    def forbidden_model_factory(*args, **kwargs):
        pytest.fail("客观评分不得构建 LLM Provider。")

    monkeypatch.setattr(
        "backend.app.ai.llm.factory.create_llm_provider", forbidden_model_factory
    )
    grader = ObjectiveGrader()
    cases = [
        (QuestionType.SINGLE_CHOICE, "A", "A", Decimal("10")),
        (QuestionType.SINGLE_CHOICE, "A", "B", Decimal("0")),
        (QuestionType.SINGLE_CHOICE, "A", None, Decimal("0")),
        (QuestionType.MULTIPLE_CHOICE, "A,C", ["C", "A"], Decimal("10")),
        (QuestionType.MULTIPLE_CHOICE, "A,C", ["A", "B"], Decimal("0")),
        (QuestionType.MULTIPLE_CHOICE, "A,C", [], Decimal("0")),
    ]
    for kind, reference, answer, expected in cases:
        result = grader.grade(
            question_type=kind, reference_answer=reference, student_answer=answer,
            max_score=Decimal("10.00"), option_keys=["A", "B", "C"],
        )
        assert result.score == expected, (kind, answer)
        assert result.retrieved_context_ids == []
    for maximum in (Decimal("0"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(InvalidMaxScoreError):
            grader.grade(question_type=QuestionType.SINGLE_CHOICE, reference_answer="A",
                         student_answer="A", max_score=maximum)
    assert is_blank_answer(None) and not is_blank_answer("B")
    # 上面二者可同得0分，但该旧评分 DTO 不提供新个人独立作答状态/诊断资格。


def test_new_contract_multiple_choice_omission_must_score_zero():
    result = ObjectiveGrader().grade(
        question_type=QuestionType.MULTIPLE_CHOICE, reference_answer="A,C",
        student_answer=["A"], max_score=Decimal("10.00"), option_keys=["A", "B", "C"],
    )
    assert result.score == Decimal("0.00")


def _subjective_payload(**changes: Any) -> dict[str, Any]:
    data = {"score": "4.50", "confidence": 0.9, "reason": "受控评分理由。",
            "correct_points": ["要点一"], "missing_knowledge_points": ["要点二"],
            "suggestions": ["核对缺失要点。"]}
    data.update(changes)
    return data


@pytest.mark.parametrize("score", ["4.50", "0.00"], ids=["partial-score", "answered-zero"])
def test_real_subjective_parser_retains_partial_or_valid_zero_score(score):
    points = [] if Decimal(score) == 0 else ["要点一"]
    result = parse_subjective_payload(
        json.dumps(_subjective_payload(score=score, correct_points=points), ensure_ascii=False),
        question_type=QuestionType.SHORT_ANSWER, max_score=Decimal("10.00"),
    )
    assert result.score == Decimal(score) and result.max_score == Decimal("10.00")
    assert result.correct_points == points
    assert result.reason and result.missing_knowledge_points
    assert result.validation_status == "Validated"
    # 只证明解析，不将模型自报 confidence 或 Not Required 冒充已接受的准确评分。


@pytest.mark.parametrize(
    "raw,error_type",
    [
        pytest.param("not JSON", InvalidLLMResponseError, id="invalid-json"),
        pytest.param({"score": "0", "confidence": 0.9},
                     InvalidLLMResponseError, id="missing-required-fields"),
        pytest.param(_subjective_payload(score="10.01"),
                     ScoreOutOfRangeError, id="score-out-of-range"),
        pytest.param(_subjective_payload(confidence=1.01),
                     ConfidenceOutOfRangeError, id="confidence-out-of-range"),
        pytest.param(_subjective_payload(score="NaN"),
                     InvalidLLMResponseError, id="nonfinite-score"),
    ],
)
def test_real_subjective_parser_fails_explicitly_instead_of_fabricating_zero(raw, error_type):
    with pytest.raises(error_type):
        parse_subjective_payload(
            raw, question_type=QuestionType.SHORT_ANSWER, max_score=Decimal("10.00")
        )


def test_fixed_scoring_input_allows_only_retrieval_enrichment_not_content_changes():
    fixed = _fixed_input(options={"B": "8", "A": "7"}, assets=[_asset(1, 1), _asset(2, 2)])
    original = fixed.model_dump(mode="python")
    enriched = ScoringInput.model_validate({
        **original, "course_context": "合成实际检索上下文。",
        "source_references": [{"chunk_id": str(CHUNK_ID), "course_id": str(COURSE_ID),
                               "document_id": str(DOCUMENT_ID), "metadata": {}}],
    })
    assert same_fixed_scoring_input(fixed, enriched)
    changes = [
        {"question_content": "已改变关键条件。"},
        {"reference_answer": "8"},
        {"source_rubric": "另一评分依据。"},
        {"effective_score": Decimal("9.00")},
        {"options": {"A": "7", "B": "8"}},
        {"assets": [_asset(2, 1), _asset(1, 2)]},
    ]
    for change in changes:
        altered = ScoringInput.model_validate({**original, **change})
        assert not same_fixed_scoring_input(fixed, altered), tuple(change)
    assert fixed.model_dump(mode="python") == original


def test_current_blank_subjective_rejection_is_not_new_local_zero_submission(monkeypatch):
    def forbidden_model_factory(*args, **kwargs):
        pytest.fail("空白输入前置拒绝不得构建 LLM Provider。")

    monkeypatch.setattr(
        "backend.app.ai.llm.factory.create_llm_provider", forbidden_model_factory
    )
    with pytest.raises(GradingInputError) as error:
        SubjectiveGradingSource(
            question_type=QuestionType.SHORT_ANSWER, course_id=COURSE_ID,
            question_content="说明结论。", reference_answer="7",
            scoring_rubric="正确要点得满分10分。", student_answer=" \n\t ",
        )
    assert error.value.error_code == "GRADING_INVALID_INPUT"
    assert is_blank_answer(" \n\t ") and not is_blank_answer("0")
    # FR-023.03/.04：提示、允许继续、持久未答状态及本地零分尚未由此路径实现。
    # 不把异常转零分，不写一个 test-local 提交器绕过旧拒绝并伪称新闭环通过。


def test_current_diagnosis_counted_filter_is_not_personal_dispute_or_partial_group_contract():
    accepted_zero = QuestionResultDTO(
        order=1, answer_id="answered-valid-zero", question_id="question-1",
        question_type=QuestionType.SHORT_ANSWER, max_score=Decimal("10"),
        score=Decimal("0"), effective_score=Decimal("0"), counted=True,
        grading_status="Accepted", knowledge_points=["受控知识点"],
    )
    excluded = QuestionResultDTO(
        order=2, answer_id="excluded-unconfirmed", question_id="question-2",
        question_type=QuestionType.SHORT_ANSWER, max_score=Decimal("10"),
        score=Decimal("10"), effective_score=None, counted=False, requires_review=True,
        grading_status="Pending Review", knowledge_points=["受控知识点"],
    )
    group = ExamResultDTO(
        submission_id="synthetic-submission", exam_id="synthetic-exam",
        student_id="synthetic-student", result_status=ExamResultStatus.PENDING_REVIEW,
        is_final=False, confirmed_subtotal=Decimal("0"),
        confirmed_subtotal_label="仅已确定部分", total_max_score=Decimal("20"),
        expected_answer_count=2, graded_answer_count=2, counted_answer_count=1,
        pending_review_answer_count=1, items=[accepted_zero, excluded],
        aggregated_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    service = DiagnosisService(weak_mastery_threshold=Decimal("0.60"))
    entries, _weak, _reasons, _insufficient = service._compute_platform_fields(group)
    assert len(entries) == 1 and entries[0].answered_count == 1
    assert entries[0].awarded_score == Decimal("0")
    assert entries[0].max_score == Decimal("10")
    assert service.final_learning_observations(group, group.items) == ([], [], [])
    # 真实旧聚合会过滤 counted=False、保留有效0分；但公开投影仍被整卷final阻塞。
    # counted 不是新个人争议状态机：标争议即排除、接受当前有效重评才恢复仍未验证。


class _ProposedSameResponseEnvelope(BaseModel):
    """Spike-only wire proposal; these fields are not a production API."""

    model_config = ConfigDict(extra="forbid")
    fields: SemanticQuestionFields
    assessment: SemanticMachineOutput


@pytest.mark.parametrize(
    "changes,kind,verdict,expected",
    [
        pytest.param({}, None, "pass", "passed", id="same-response-correct"),
        pytest.param({"reference_answer": "B"}, "answer_correctness", "fail",
                     "failed", id="same-response-wrong-answer"),
        pytest.param({"content": "Rectangle length is 3; find its area."},
                     "condition_sufficiency", "insufficient_evidence", "failed",
                     id="same-response-missing-condition"),
        pytest.param({"options": {"A": "0.5", "B": "1/2"}},
                     "option_ambiguity", "needs_review", "failed",
                     id="same-response-ambiguous-options"),
    ],
)
def test_proposed_same_response_reuses_real_checks_without_second_call(
    changes, kind, verdict, expected
):
    from backend.app.schemas.content_validation import ValidationOutput

    value = _semantic_input(**changes)
    provider = _ScriptedProvider({
        "fields": value.fields.model_dump(mode="json"),
        "assessment": _script(changed_kind=kind, verdict=verdict),
    })
    raw = asyncio.run(provider.generate_structured(
        [{"role": "system", "content": "Synthetic single-response protocol Spike."},
         {"role": "user", "content": value.model_dump_json()}],
        _ProposedSameResponseEnvelope,
    ))
    # The Fake returns raw data. Real Pydantic and service checks run here.
    envelope = _ProposedSameResponseEnvelope.model_validate(raw.model_dump())
    assert validate_semantic_fields(envelope.fields) == ()
    service = _MemoryContentValidationService(
        value.model_copy(update={"fields": envelope.fields})
    )
    assert type(service).finish_validation is ContentValidationService.finish_validation
    returned = service.finish_validation(
        service.row.id, actor_id=ACTOR_ID,
        output=ValidationOutput(checks=envelope.assessment.checks, issues=[]),
    )
    assert returned.outcome == expected
    assert service.question.status is (
        QuestionStatus.PENDING_REVIEW if expected == "passed"
        else QuestionStatus.NEEDS_REVISION
    )
    assert len(provider.calls) == 1
    assert service.session.commits == 1 and service.session.rollbacks == 0
    # This is protocol composition, not generation wiring, source verification,
    # persistent admission, general-knowledge support or real model accuracy.
