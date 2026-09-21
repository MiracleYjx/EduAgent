"""M3 结果、诊断、复核与运行模型迁移链与对象边界测试。

本文件对每个 revision 校验三件事：

1. 线性链条与 head 与 `migrations/versions/` 中的实际文件一致；
2. `upgrade()` 只创建该 revision 负责的表、索引、CHECK/UNIQUE/主键/外键定义，
   并且记录当时的枚举值（不动态导入会变化的应用枚举）；
3. `downgrade()` 只撤销该 revision 的对象，不越界删除其它 revision 的表。

真实 PostgreSQL 上的列精度、约束与外键行为由 `alembic upgrade/check` 与一次性
验证库上的 pg_catalog 断言覆盖，本文件不替代该验证。
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum

from backend.app.domain.enums import (
    GradingStatus,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.schemas.grading import DiagnosisStatus, ExamResultStatus

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

#: 批次迁移链：revision -> down_revision（逐个任务向后追加）。
EXPECTED_CHAIN: dict[str, str] = {
    "0005_grading_results": "0004_document_chunks",
    "0006_diagnosis_reports": "0005_grading_results",
    "0007_review_records": "0006_diagnosis_reports",
    "0008_workflow_runs": "0007_review_records",
    "0009_agent_runs": "0008_workflow_runs",
    "0010_review_round_ids": "0009_agent_runs",
}

#: 当前 head（逐个任务向后移动）。
EXPECTED_HEAD = "0010_review_round_ids"


@dataclass(frozen=True, slots=True)
class TableExpectation:
    """单个新表的期望结构摘要。"""

    columns: tuple[str, ...]
    nullable_columns: frozenset[str]
    check_constraints: frozenset[str]
    unique_constraints: frozenset[str]
    foreign_keys: tuple[tuple[str, str, str | None], ...]
    enum_columns: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class RevisionExpectation:
    """单个 revision 的期望对象边界。"""

    revision: str
    down_revision: str
    indexes: frozenset[str]
    tables: tuple[tuple[str, TableExpectation], ...]


REVISION_EXPECTATIONS: tuple[RevisionExpectation, ...] = (
    RevisionExpectation(
        revision="0005_grading_results",
        down_revision="0004_document_chunks",
        indexes=frozenset(
            {
                "ix_grading_results_submission_id",
                "ix_grading_results_submission_review_status",
                "ix_exam_results_exam_student",
            }
        ),
        tables=(
            (
                "grading_results",
                TableExpectation(
                    columns=(
                        "answer_id",
                        "submission_id",
                        "question_type",
                        "score",
                        "max_score",
                        "reason",
                        "correct_points",
                        "missing_knowledge_points",
                        "knowledge_points",
                        "suggestions",
                        "retrieved_context_ids",
                        "confidence",
                        "validation_status",
                        "review_status",
                        "decision_confidence",
                        "decision_threshold",
                        "decision_requires_review",
                        "decision_review_status",
                        "decision_grading_status",
                        "decision_reason",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset(
                        {
                            "decision_confidence",
                            "decision_threshold",
                            "decision_requires_review",
                            "decision_review_status",
                            "decision_grading_status",
                            "decision_reason",
                        }
                    ),
                    check_constraints=frozenset(
                        {
                            "question_type",
                            "grading_validation_status",
                            "grading_review_status",
                            "grading_decision_review_status",
                            "grading_decision_grading_status",
                            "ck_grading_results_score_non_negative",
                            "ck_grading_results_max_score_positive",
                            "ck_grading_results_score_range",
                            "ck_grading_results_confidence_range",
                            "ck_grading_results_decision_confidence_range",
                            "ck_grading_results_decision_threshold_range",
                            "ck_grading_results_decision_snapshot",
                        }
                    ),
                    unique_constraints=frozenset({"uq_grading_results_answer"}),
                    foreign_keys=(
                        ("answer_id", "answers.id", "CASCADE"),
                        ("submission_id", "submissions.id", "CASCADE"),
                    ),
                    enum_columns=(
                        ("question_type", tuple(item.value for item in QuestionType)),
                        (
                            "validation_status",
                            tuple(item.value for item in ValidationStatus),
                        ),
                        ("review_status", tuple(item.value for item in ReviewStatus)),
                        (
                            "decision_review_status",
                            tuple(item.value for item in ReviewStatus),
                        ),
                        (
                            "decision_grading_status",
                            tuple(item.value for item in GradingStatus),
                        ),
                    ),
                ),
            ),
            (
                "exam_results",
                TableExpectation(
                    columns=(
                        "submission_id",
                        "exam_id",
                        "student_id",
                        "result_status",
                        "is_final",
                        "final_total_score",
                        "confirmed_subtotal",
                        "total_max_score",
                        "aggregated_at",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset({"final_total_score"}),
                    check_constraints=frozenset(
                        {
                            "exam_result_status",
                            "ck_exam_results_total_max_score_positive",
                            "ck_exam_results_confirmed_subtotal_non_negative",
                            "ck_exam_results_final_total_score_non_negative",
                            "ck_exam_results_final_state",
                        }
                    ),
                    unique_constraints=frozenset({"uq_exam_results_submission"}),
                    foreign_keys=(
                        ("submission_id", "submissions.id", "CASCADE"),
                        ("exam_id", "exams.id", "CASCADE"),
                        ("student_id", "users.id", "RESTRICT"),
                    ),
                    enum_columns=(
                        (
                            "result_status",
                            tuple(item.value for item in ExamResultStatus),
                        ),
                    ),
                ),
            ),
        ),
    ),
    RevisionExpectation(
        revision="0006_diagnosis_reports",
        down_revision="0005_grading_results",
        indexes=frozenset(
            {
                "ix_diagnosis_reports_exam_result_id",
                "ix_diagnosis_reports_submission_id",
                "ix_diagnosis_reports_student_id",
                "ix_diagnosis_reports_student_generated",
            }
        ),
        tables=(
            (
                "diagnosis_reports",
                TableExpectation(
                    columns=(
                        "exam_result_id",
                        "submission_id",
                        "student_id",
                        "status",
                        "mastery_by_knowledge_point",
                        "weak_knowledge_points",
                        "error_reasons",
                        "learning_suggestions",
                        "insufficient_evidence_answer_ids",
                        "error_code",
                        "retryable",
                        "source_code",
                        "attempt_count",
                        "generated_at",
                        "source_exam_result_updated_at",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset(
                        {
                            "error_code",
                            "retryable",
                            "source_code",
                            "attempt_count",
                            "generated_at",
                            "source_exam_result_updated_at",
                        }
                    ),
                    check_constraints=frozenset(
                        {
                            "diagnosis_status",
                            "ck_diagnosis_reports_attempt_count_non_negative",
                            "ck_diagnosis_reports_status_consistency",
                        }
                    ),
                    unique_constraints=frozenset(),
                    foreign_keys=(
                        ("exam_result_id", "exam_results.id", "CASCADE"),
                        ("submission_id", "submissions.id", "CASCADE"),
                        ("student_id", "users.id", "RESTRICT"),
                    ),
                    enum_columns=(
                        ("status", tuple(item.value for item in DiagnosisStatus)),
                    ),
                ),
            ),
        ),
    ),
    RevisionExpectation(
        revision="0007_review_records",
        down_revision="0006_diagnosis_reports",
        indexes=frozenset(
            {
                "ix_review_records_grading_result_id",
                "ix_review_records_reviewer_id",
            }
        ),
        tables=(
            (
                "review_records",
                TableExpectation(
                    columns=(
                        "grading_result_id",
                        "reviewer_id",
                        "decision",
                        "original_score",
                        "original_reason",
                        "original_knowledge_points",
                        "final_score",
                        "final_reason",
                        "final_knowledge_points",
                        "comment",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset(
                        {
                            "final_score",
                            "final_reason",
                            "final_knowledge_points",
                            "comment",
                        }
                    ),
                    check_constraints=frozenset(
                        {
                            "review_decision",
                            "ck_review_records_decision_operable",
                            "ck_review_records_original_score_non_negative",
                            "ck_review_records_final_score_non_negative",
                        }
                    ),
                    unique_constraints=frozenset(),
                    foreign_keys=(
                        ("grading_result_id", "grading_results.id", "CASCADE"),
                        ("reviewer_id", "users.id", "RESTRICT"),
                    ),
                    enum_columns=(
                        ("decision", tuple(item.value for item in ReviewStatus)),
                    ),
                ),
            ),
        ),
    ),
    RevisionExpectation(
        revision="0008_workflow_runs",
        down_revision="0007_review_records",
        indexes=frozenset(
            {
                "ix_workflow_runs_request_id",
                "ix_workflow_runs_submission_id",
            }
        ),
        tables=(
            (
                "workflow_runs",
                TableExpectation(
                    columns=(
                        "workflow_id",
                        "request_id",
                        "submission_id",
                        "current_node",
                        "current_answer_id",
                        "status",
                        "checkpoint",
                        "pause_reason",
                        "retry_count",
                        "resumable",
                        "exam_result_id",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset(
                        {
                            "current_node",
                            "current_answer_id",
                            "checkpoint",
                            "pause_reason",
                            "exam_result_id",
                        }
                    ),
                    check_constraints=frozenset(
                        {
                            "workflow_status",
                            "ck_workflow_runs_retry_count_non_negative",
                        }
                    ),
                    unique_constraints=frozenset({"uq_workflow_runs_workflow_id"}),
                    foreign_keys=(
                        ("submission_id", "submissions.id", "CASCADE"),
                        ("current_answer_id", "answers.id", "SET NULL"),
                        ("exam_result_id", "exam_results.id", "SET NULL"),
                    ),
                    enum_columns=(
                        ("status", tuple(item.value for item in WorkflowStatus)),
                    ),
                ),
            ),
        ),
    ),
    RevisionExpectation(
        revision="0009_agent_runs",
        down_revision="0008_workflow_runs",
        indexes=frozenset(
            {
                "ix_agent_runs_request_id",
                "ix_agent_runs_user_id",
                "ix_agent_runs_workflow_id",
            }
        ),
        tables=(
            (
                "agent_runs",
                TableExpectation(
                    columns=(
                        "agent_type",
                        "workflow_id",
                        "request_id",
                        "user_id",
                        "input_summary",
                        "output_summary",
                        "validation_status",
                        "status",
                        "latency_ms",
                        "model",
                        "prompt_version",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "error_code",
                        "error_message",
                        "error_retryable",
                        "id",
                        "created_at",
                        "updated_at",
                    ),
                    nullable_columns=frozenset(
                        {
                            "workflow_id",
                            "user_id",
                            "input_summary",
                            "output_summary",
                            "validation_status",
                            "latency_ms",
                            "model",
                            "prompt_version",
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                            "error_code",
                            "error_message",
                            "error_retryable",
                        }
                    ),
                    check_constraints=frozenset(
                        {
                            "agent_validation_status",
                            "ck_agent_runs_status_trace_value",
                            "ck_agent_runs_latency_non_negative",
                            "ck_agent_runs_token_counts_non_negative",
                        }
                    ),
                    unique_constraints=frozenset(),
                    foreign_keys=(
                        (
                            "workflow_id",
                            "workflow_runs.workflow_id",
                            "SET NULL",
                        ),
                        ("user_id", "users.id", "RESTRICT"),
                    ),
                    enum_columns=(
                        (
                            "validation_status",
                            tuple(item.value for item in ValidationStatus),
                        ),
                    ),
                ),
            ),
        ),
    ),
)

#: 期望迁移中不得出现的应用枚举类（S03：迁移固定当时的字面值）。
FORBIDDEN_ENUM_CLASSES = (
    GradingStatus,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
    ExamResultStatus,
    DiagnosisStatus,
)


@dataclass(frozen=True, slots=True)
class RecordedTable:
    """从 `create_table` 调用中提取的结构摘要。"""

    columns: tuple[str, ...]
    nullable_columns: frozenset[str]
    check_constraints: frozenset[str]
    unique_constraints: frozenset[str]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[tuple[str, str, str | None], ...]
    enum_columns: tuple[tuple[str, tuple[str, ...]], ...]


class RecordingOp:
    """记录迁移 DDL 调用的 `op` 替身，不连接数据库。"""

    def __init__(self) -> None:
        self.created_tables: dict[str, RecordedTable] = {}
        self.dropped_tables: list[str] = []
        self.created_indexes: list[str] = []
        self.dropped_indexes: list[str] = []

    @staticmethod
    def f(name: str) -> str:
        """保持与 `op.f()` 相同的命名行为。"""

        return name

    def create_table(self, table_name: str, *items: Any, **_kwargs: Any) -> None:
        self.created_tables[table_name] = _record_table(items)

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.dropped_tables.append(table_name)

    def create_index(
        self,
        index_name: str,
        table_name: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        self.created_indexes.append(index_name)

    def drop_index(
        self,
        index_name: str,
        table_name: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self.dropped_indexes.append(index_name)


def _record_table(items: Sequence[Any]) -> RecordedTable:
    """把 `create_table` 的位置参数转换为可比较的结构摘要。"""

    # 位置参数挂到临时 Table，ForeignKey 的本地列与目标列才会被解析（替身 op 不会真正建表）。
    scratch = Table("recorded_scratch_table", MetaData(), *items)

    columns: list[str] = []
    nullable: list[str] = []
    checks: list[str] = []
    uniques: list[str] = []
    primary_key: list[str] = []
    enum_columns: list[tuple[str, tuple[str, ...]]] = []

    for item in items:
        if isinstance(item, Column):
            columns.append(item.name)
            if item.nullable:
                nullable.append(item.name)
            if isinstance(item.type, SAEnum):
                enum_columns.append((item.name, tuple(item.type.enums)))
        elif isinstance(item, CheckConstraint):
            checks.append(str(item.name))
        elif isinstance(item, UniqueConstraint):
            uniques.append(str(item.name))
        elif isinstance(item, PrimaryKeyConstraint):
            primary_key.extend(column.name for column in item.columns)
        elif isinstance(item, ForeignKeyConstraint):
            continue

    # 未挂载的 ForeignKey 没有 parent；统一从临时表读取本地列、目标列与删除行为，
    # 并按该表的列序排列，避免集合迭代顺序不稳定。
    column_positions = {
        column.name: index for index, column in enumerate(scratch.columns)
    }
    foreign_keys = sorted(
        (
            (element.parent.name, element.target_fullname, element.ondelete)
            for element in scratch.foreign_keys
        ),
        key=lambda item: column_positions[item[0]],
    )

    return RecordedTable(
        columns=tuple(columns),
        nullable_columns=frozenset(nullable),
        check_constraints=frozenset(checks),
        unique_constraints=frozenset(uniques),
        primary_key=tuple(primary_key),
        foreign_keys=tuple(foreign_keys),
        enum_columns=tuple(enum_columns),
    )


def _load_revision(revision: str) -> Any:
    """按 revision 名导入迁移模块。"""

    return importlib.import_module(f"migrations.versions.{revision}")


def _run_revision(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    upgrade: bool,
) -> RecordingOp:
    """在记录器替身下执行 upgrade 或 downgrade。"""

    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)
    if upgrade:
        module.upgrade()
    else:
        module.downgrade()
    return recorder


def test_revision_chain_is_linear_and_head_matches_expected() -> None:
    """迁移链必须与期望的线性链条一致，head 必须是本批最后一个 revision。"""

    script = ScriptDirectory(str(MIGRATIONS_DIR))

    assert script.get_heads() == [EXPECTED_HEAD]
    for revision, down_revision in EXPECTED_CHAIN.items():
        script_revision = script.get_revision(revision)
        assert script_revision is not None
        assert script_revision.down_revision == down_revision
    assert EXPECTED_CHAIN[EXPECTED_HEAD] is not None


@pytest.mark.parametrize(
    "expectation",
    REVISION_EXPECTATIONS,
    ids=lambda expectation: expectation.revision,
)
def test_revision_upgrade_creates_only_its_own_objects(
    expectation: RevisionExpectation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个 revision 的 upgrade 只创建自己负责的表、索引与约束。"""

    module = _load_revision(expectation.revision)
    recorder = _run_revision(module, monkeypatch, upgrade=True)

    assert set(recorder.created_tables) == {
        table_name for table_name, _ in expectation.tables
    }
    assert set(recorder.created_indexes) == set(expectation.indexes)

    for table_name, table_expectation in expectation.tables:
        recorded = recorder.created_tables[table_name]
        assert recorded.columns == table_expectation.columns
        assert recorded.nullable_columns == table_expectation.nullable_columns
        assert recorded.check_constraints == table_expectation.check_constraints
        assert recorded.unique_constraints == table_expectation.unique_constraints
        assert recorded.primary_key == ("id",)
        assert recorded.foreign_keys == table_expectation.foreign_keys
        assert recorded.enum_columns == table_expectation.enum_columns


@pytest.mark.parametrize(
    "expectation",
    REVISION_EXPECTATIONS,
    ids=lambda expectation: expectation.revision,
)
def test_revision_downgrade_removes_only_its_own_objects(
    expectation: RevisionExpectation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个 revision 的 downgrade 只撤销自己创建的表与索引。"""

    module = _load_revision(expectation.revision)
    upgrade_recorder = _run_revision(module, monkeypatch, upgrade=True)
    downgrade_recorder = _run_revision(module, monkeypatch, upgrade=False)

    assert sorted(downgrade_recorder.dropped_tables) == sorted(
        upgrade_recorder.created_tables
    )
    assert sorted(downgrade_recorder.dropped_indexes) == sorted(
        upgrade_recorder.created_indexes
    )


@pytest.mark.parametrize(
    "expectation",
    REVISION_EXPECTATIONS,
    ids=lambda expectation: expectation.revision,
)
def test_revision_does_not_reference_application_enum_classes(
    expectation: RevisionExpectation,
) -> None:
    """迁移必须固定当时的枚举字面值，不导入会变化的应用枚举类。"""

    module = _load_revision(expectation.revision)
    module_values = tuple(vars(module).values())

    for enum_class in FORBIDDEN_ENUM_CLASSES:
        assert enum_class not in module_values
