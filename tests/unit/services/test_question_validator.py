"""T068 Question Validator 候选校验与状态转换的失败优先测试。

任务编号：T068（M4；实现见 `backend/app/services/question_validator.py`）。

必要性：`plan.md` §4.1 要求 Question Validator 检查候选题是否有课程依据、答案是否可用、评分
标准是否可用，并把候选状态设为 `Pending Review` 或 `Needs Revision`；FR-026/FR-027 要求 AI 生成
结果永远不能绕过教师审核直接发布。此外 B03 已确认作答编码事实：列表选项提交的是选项全文，
判断题默认提交 `True`/`False`，客观题评分器做整体文本等值比较，因此“Schema 合法但答案编码
不可兼容”的候选题必须进入待修订，否则教师批准后学生选对也会被判 0 分。

覆盖内容：
1. 校验通过的候选只能是 `Pending Review`，绝不出现 `Approved`/`Published`；
2. 答案编码兼容性：字典选项用选项键、列表选项用选项全文、无选项判断题用 `True`/`False`，
   多选复用 M3 `parse_multiple_choice_keys` 口径；
3. 课程依据：缺失引用或引用不在本次检索白名单内都进入 `Needs Revision` 并保留原因；
4. 评分标准可用性下限与分值可落库性（有限正数、最多两位小数、不超过 M1 上限）；
5. 整批条件：非空难度逐题匹配、请求知识点必须被整批候选覆盖；
6. 状态转换：AGENT/VALIDATOR 对 `Approved`/`Published`（含同状态幂等情形）一律拒绝
   `QUESTION_AUTOMATIC_PUBLISH_BLOCKED`，只有 TEACHER 可批准；`Needs Revision` 修订后必须
   重新校验才能回到 `Pending Review`；
7. 模块间用例：QuestionAgent（mock Provider）生成的候选直接进入 QuestionValidator 得到
   `Pending Review`；
8. 本模块是纯业务校验：不访问数据库、不调用 LLM。

执行方法（先红后绿）：``python -m pytest tests/unit/services/test_question_validator.py -q``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.app.ai.agents.question_agent import QuestionAgent
from backend.app.ai.agents.state import (
    AgentInput,
    AgentType,
    QuestionGenerationRequest,
)
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.schemas.ai import QuestionCandidate
from backend.app.services.question_validator import (
    CANDIDATE_STATUS_TRANSITIONS,
    QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
    QUESTION_AUTOMATIC_PUBLISH_BLOCKED,
    QUESTION_DIFFICULTY_MISMATCH,
    QUESTION_EMPTY_CANDIDATE_BATCH,
    QUESTION_KNOWLEDGE_POINT_UNCOVERED,
    QUESTION_MISSING_COURSE_EVIDENCE,
    QUESTION_NOT_CANDIDATE,
    QUESTION_RUBRIC_UNUSABLE,
    QUESTION_SCORE_NOT_STORABLE,
    QUESTION_STATUS_TRANSITION_BLOCKED,
    QUESTION_UNKNOWN_COURSE_EVIDENCE,
    VALIDATOR_RESULT_STATUSES,
    AutomaticPublishingBlockedError,
    CandidateBatchValidation,
    CandidateValidationResult,
    QuestionValidationIssue,
    QuestionValidator,
    StatusTransitionBlockedError,
    ValidationActor,
    plan_transition,
)
from tests.support.question_generation_doubles import (
    COURSE_UUID,
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings

#: 本次检索写入 Prompt 的白名单片段。
_WHITELIST = ("chunk-1",)


def _candidate(**overrides: Any) -> QuestionCandidate:
    """构造默认合法候选；默认引用白名单片段。"""

    return make_candidate(**overrides)


def _validate(
    candidate: QuestionCandidate,
    *,
    whitelist: tuple[str, ...] = _WHITELIST,
) -> CandidateValidationResult:
    """用默认校验器校验单个候选。"""

    return QuestionValidator().validate_candidate(
        candidate,
        retrieved_context_ids=whitelist,
    )


def _issue_codes(result: CandidateValidationResult) -> set[str]:
    """返回校验结果中的原因码集合。"""

    return {issue.code for issue in result.issues}


def test_valid_candidate_only_reaches_pending_review() -> None:
    """校验通过的候选只能进入 Pending Review，绝不等于 Approved/Published。"""

    result = _validate(_candidate())

    assert result.status is QuestionStatus.PENDING_REVIEW
    assert result.status not in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}
    assert result.is_valid is True
    assert result.issues == ()
    assert VALIDATOR_RESULT_STATUSES == frozenset(
        {QuestionStatus.PENDING_REVIEW, QuestionStatus.NEEDS_REVISION}
    )


def test_list_option_answer_must_be_option_text() -> None:
    """列表选项提交的是选项全文，字母参考答案必须进入 Needs Revision。"""

    result = _validate(_candidate(reference_answer="A"))

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_ANSWER_ENCODING_INCOMPATIBLE in _issue_codes(result)
    assert result.is_valid is False


def test_candidate_options_are_list_form_only() -> None:
    """T010 候选题只支持列表选项：可提交取值就是选项全文。"""

    with pytest.raises(ValidationError):
        _candidate(options={"A": "变量用于保存数据。", "B": "变量不能被引用。"})

    options = ["变量用于保存数据。", "变量不能被引用。"]
    assert _validate(_candidate(options=options)).status is QuestionStatus.PENDING_REVIEW
    assert _validate(
        _candidate(options=options, reference_answer="A")
    ).status is QuestionStatus.NEEDS_REVISION


def test_true_false_without_options_uses_boolean_values() -> None:
    """无选项判断题的可提交取值为 True/False，中文“对/错”不可兼容。"""

    valid = _validate(
        _candidate(
            question_type=QuestionType.TRUE_FALSE,
            options=None,
            reference_answer="True",
        )
    )
    assert valid.status is QuestionStatus.PENDING_REVIEW

    invalid = _validate(
        _candidate(
            question_type=QuestionType.TRUE_FALSE,
            options=None,
            reference_answer="对",
        )
    )
    assert invalid.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_ANSWER_ENCODING_INCOMPATIBLE in _issue_codes(invalid)


def test_missing_course_evidence_needs_revision() -> None:
    """没有任何课程依据的候选必须进入待修订，并保留失败原因。"""

    result = _validate(_candidate(source_context_ids=[]))

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_MISSING_COURSE_EVIDENCE in _issue_codes(result)
    assert all(issue.message.strip() for issue in result.issues)


def test_unknown_course_evidence_needs_revision() -> None:
    """引用不在本次检索白名单内的片段同样进入待修订。"""

    result = _validate(_candidate(source_context_ids=["chunk-not-retrieved"]))

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_UNKNOWN_COURSE_EVIDENCE in _issue_codes(result)


def test_unusable_rubric_needs_revision() -> None:
    """评分标准必须给出可分配的分值或得分依据，否则不可用。"""

    result = _validate(_candidate(scoring_rubric="请按标准评分。"))

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_RUBRIC_UNUSABLE in _issue_codes(result)


def test_rubric_with_explicit_allocation_but_no_digits_is_usable() -> None:
    """M02：明确的分值分配规则（无论是否含数字）都是可用的。"""

    for rubric in (
        "正确选项得满分，其余不得分。",
        "答对全部要点得满分。",
        "要点齐全得分，缺一要点扣一半。",
        "参考答案正确即给分，部分正确按要点给分。",
    ):
        result = _validate(_candidate(scoring_rubric=rubric))
        assert result.status is QuestionStatus.PENDING_REVIEW, rubric
        assert result.issues == ()


def test_digit_only_or_placeholder_rubric_needs_revision() -> None:
    """M02：仅数字、数字加单位与空洞占位表达都不可用。"""

    for rubric in ("1", "2 分", "100％", "（略）", "见参考答案。"):
        result = _validate(_candidate(scoring_rubric=rubric))
        assert result.status is QuestionStatus.NEEDS_REVISION, rubric
        assert QUESTION_RUBRIC_UNUSABLE in _issue_codes(result)


@pytest.mark.parametrize(
    "score",
    [2.005, 1000000.0, float("inf")],
)
def test_unstorable_score_needs_revision(score: float) -> None:
    """分值必须可落库（有限、最多两位小数、不超过 M1 上限），否则待修订。"""

    result = _validate(_candidate(score=score))

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_SCORE_NOT_STORABLE in _issue_codes(result)


def test_candidate_must_be_in_candidate_generation_status() -> None:
    """被改写为其它审核状态的候选不再作为候选接受。"""

    tampered = _candidate().model_copy(update={"status": QuestionStatus.APPROVED.value})
    result = _validate(tampered)  # type: ignore[arg-type]

    assert result.status is QuestionStatus.NEEDS_REVISION
    assert QUESTION_NOT_CANDIDATE in _issue_codes(result)


def test_batch_checks_difficulty_and_knowledge_coverage() -> None:
    """整批条件：非空难度逐题匹配，请求知识点必须被整批候选覆盖。"""

    request = QuestionGenerationRequest(
        course_id=COURSE_UUID,
        knowledge_points=["变量", "作用域"],
        difficulty="困难",
        question_type=QuestionType.SINGLE_CHOICE,
        count=1,
    )
    candidate = _candidate(knowledge_points=["变量"], difficulty="中等")
    batch = QuestionValidator().validate_candidates(
        [candidate],
        retrieved_context_ids=_WHITELIST,
        generation_request=request,
    )

    assert isinstance(batch, CandidateBatchValidation)
    assert batch.status is QuestionStatus.NEEDS_REVISION
    assert {issue.code for issue in batch.issues} == {
        QUESTION_DIFFICULTY_MISMATCH,
        QUESTION_KNOWLEDGE_POINT_UNCOVERED,
    }

    matching = QuestionValidator().validate_candidates(
        [_candidate(knowledge_points=["变量", "作用域"], difficulty="困难")],
        retrieved_context_ids=_WHITELIST,
        generation_request=request,
    )
    assert matching.status is QuestionStatus.PENDING_REVIEW
    assert matching.issues == ()


def test_empty_batch_cannot_be_pending_review() -> None:
    """M03：空批次不得被视为成功，必须给出明确原因。"""

    batch = QuestionValidator().validate_candidates(
        [],
        retrieved_context_ids=_WHITELIST,
        generation_request=None,
    )

    assert isinstance(batch, CandidateBatchValidation)
    assert batch.status is QuestionStatus.NEEDS_REVISION
    assert batch.is_valid is False
    assert QUESTION_EMPTY_CANDIDATE_BATCH in {issue.code for issue in batch.issues}
    assert batch.results == ()


def test_batch_model_rejects_empty_success() -> None:
    """M03：批次模型本身不得把“空集合且无原因”表示为成功。"""

    with pytest.raises(ValidationError):
        CandidateBatchValidation()

    valid_result = _validate(_candidate())
    batch = CandidateBatchValidation(results=(valid_result,))
    assert batch.is_valid is True
    assert batch.status is QuestionStatus.PENDING_REVIEW


def test_result_model_rejects_publishable_status() -> None:
    """M03：单题结果状态只能为 Pending Review 或 Needs Revision。"""

    for status in (QuestionStatus.APPROVED, QuestionStatus.PUBLISHED):
        with pytest.raises(ValidationError):
            CandidateValidationResult(status=status)


def test_result_model_enforces_status_and_issues_consistency() -> None:
    """M03：Pending Review 不得携带原因，Needs Revision 必须保留原因。"""

    issue = QuestionValidationIssue(code="QUESTION_TEST_ISSUE", message="需要修订。")
    with pytest.raises(ValidationError):
        CandidateValidationResult(status=QuestionStatus.PENDING_REVIEW, issues=(issue,))
    with pytest.raises(ValidationError):
        CandidateValidationResult(status=QuestionStatus.NEEDS_REVISION)
    assert (
        CandidateValidationResult(
            status=QuestionStatus.NEEDS_REVISION,
            issues=(issue,),
        ).is_valid
        is False
    )


def test_batch_is_invalid_when_any_candidate_needs_revision() -> None:
    """任一批内候选待修订时整批不得进入 Pending Review，但有效候选结果保持可见。"""

    batch = QuestionValidator().validate_candidates(
        [_candidate(), _candidate(reference_answer="A")],
        retrieved_context_ids=_WHITELIST,
        generation_request=None,
    )

    assert batch.status is QuestionStatus.NEEDS_REVISION
    assert [result.status for result in batch.results] == [
        QuestionStatus.PENDING_REVIEW,
        QuestionStatus.NEEDS_REVISION,
    ]


def test_transition_table_covers_every_question_status() -> None:
    """状态表必须覆盖全部题库状态，避免出现未定义转换。"""

    assert set(CANDIDATE_STATUS_TRANSITIONS) == set(QuestionStatus)
    assert CANDIDATE_STATUS_TRANSITIONS[QuestionStatus.CANDIDATE_GENERATION] == frozenset(
        {QuestionStatus.PENDING_REVIEW, QuestionStatus.NEEDS_REVISION}
    )
    assert QuestionStatus.APPROVED not in CANDIDATE_STATUS_TRANSITIONS[
        QuestionStatus.CANDIDATE_GENERATION
    ]
    assert CANDIDATE_STATUS_TRANSITIONS[QuestionStatus.NEEDS_REVISION] == frozenset(
        {QuestionStatus.PENDING_REVIEW}
    )


@pytest.mark.parametrize("actor", [ValidationActor.AGENT, ValidationActor.VALIDATOR])
def test_agent_path_cannot_publish(actor: ValidationActor) -> None:
    """Agent/校验器路径对 Approved、Published（含同状态幂等）一律拒绝。"""

    for current in (
        QuestionStatus.CANDIDATE_GENERATION,
        QuestionStatus.PENDING_REVIEW,
    ):
        with pytest.raises(AutomaticPublishingBlockedError) as error:
            plan_transition(current, QuestionStatus.APPROVED, actor=actor)
        assert error.value.error_code == QUESTION_AUTOMATIC_PUBLISH_BLOCKED

    with pytest.raises(AutomaticPublishingBlockedError):
        plan_transition(QuestionStatus.APPROVED, QuestionStatus.APPROVED, actor=actor)
    with pytest.raises(AutomaticPublishingBlockedError):
        plan_transition(
            QuestionStatus.PENDING_REVIEW,
            QuestionStatus.PUBLISHED,
            actor=actor,
        )


def test_teacher_can_confirm_but_not_skip_states() -> None:
    """只有 TEACHER 可以批准；教师同样不得跳过 Needs Revision 直接批准。"""

    assert (
        plan_transition(
            QuestionStatus.PENDING_REVIEW,
            QuestionStatus.APPROVED,
            actor=ValidationActor.TEACHER,
        )
        is QuestionStatus.APPROVED
    )
    with pytest.raises(StatusTransitionBlockedError) as error:
        plan_transition(
            QuestionStatus.NEEDS_REVISION,
            QuestionStatus.APPROVED,
            actor=ValidationActor.TEACHER,
        )
    assert error.value.error_code == QUESTION_STATUS_TRANSITION_BLOCKED


def test_revision_cycle_requires_revalidation() -> None:
    """Needs Revision 修订后必须先重新校验才能回到 Pending Review。"""

    revised = _candidate(
        scoring_rubric="选择正确选项得 2 分。",
        reference_answer="变量用于保存数据。",
    )
    assert _validate(revised).status is QuestionStatus.PENDING_REVIEW

    assert (
        plan_transition(
            QuestionStatus.NEEDS_REVISION,
            QuestionStatus.PENDING_REVIEW,
            actor=ValidationActor.VALIDATOR,
        )
        is QuestionStatus.PENDING_REVIEW
    )


def test_question_agent_output_flows_into_validator() -> None:
    """模块间：QuestionAgent 生成的候选必须由 QuestionValidator 判为 Pending Review。

    缺少引用时保留空来源，因此该用例同时证明“空引用进入 Needs Revision”。
    """

    import asyncio

    provider = StubQuestionProvider(candidates=[_candidate()])
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type]
            AgentInput(
                agent_type=AgentType.QUESTION,
                request_id="request-cross-module",
                generation_request=QuestionGenerationRequest(
                    course_id=COURSE_UUID,
                    knowledge_points=["变量"],
                    difficulty="中等",
                    question_type=QuestionType.SINGLE_CHOICE,
                    count=1,
                ),
            ),
            retriever=StubRetriever([make_chunk("chunk-1")]),
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )

    assert output.question_candidates
    result = QuestionValidator().validate_candidate(
        output.question_candidates[0],
        retrieved_context_ids=output.retrieved_context_ids,
    )
    assert result.status is QuestionStatus.PENDING_REVIEW


def test_validator_module_has_no_database_or_llm_dependency() -> None:
    """Question Validator 是纯业务校验：不引入数据库、ORM 与 LLM Provider。"""

    import backend.app.services.question_validator as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "backend.app.models" not in source
    assert "ai.llm" not in source
