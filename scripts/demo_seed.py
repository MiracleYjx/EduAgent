"""T087 演示种子：DEV_MODE=true 后运行 python -m scripts.demo_seed。

所有写入均调用业务服务；服务各自提交，失败后保留真实状态，重跑复用已有记录。
不重置演示过程中的学生答卷或成绩，不输出随机密码。首次摄取会调用配置的 Embedding。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.embedding.factory import create_embedding_provider
from backend.app.ai.ingestion.service import IngestionService
from backend.app.core.config import AppSettings, ConfigurationError, get_settings
from backend.app.core.database import create_database_engine, create_session_factory
from backend.app.domain.enums import (
    DocumentStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.services.auth_service import ensure_dev_mode_accounts
from backend.app.services.course_service import CourseService
from backend.app.services.exam_service import ExamService
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from backend.app.services.question_service import QuestionService

COURSE_NAME = "[Demo v1] Python 基础"
KNOWLEDGE_BASE_NAME = "[Demo v1] Python 教材"
EXAM_TITLE = "[Demo v1] Python 混合题型练习"
SOURCE_PATH = Path(__file__).resolve().parent / "demo_materials" / "python_basics.md"
QUESTION_SPECS: tuple[dict[str, Any], ...] = (
    {
        "question_type": QuestionType.SINGLE_CHOICE,
        "content": "[Demo v1-1] Python 中用于定义函数的关键字是？",
        "options": {"A": "def", "B": "class", "C": "if", "D": "for"},
        "reference_answer": "A",
        "score": 5,
        "difficulty": "简单",
        "knowledge_points": ["函数"],
    },
    {
        "question_type": QuestionType.SHORT_ANSWER,
        "content": "[Demo v1-2] 请解释 Python 函数的作用，以及参数和返回值各自的作用。",
        "reference_answer": "函数封装可重复调用的逻辑，参数传入数据，return 将结果返回调用者。",
        "scoring_rubric": "说明封装可复用逻辑得 2 分；说明参数传入数据得 2 分；说明返回结果得 1 分。",
        "score": 5,
        "difficulty": "简单",
        "knowledge_points": ["函数", "参数与返回值"],
    },
)


class DemoSeedError(RuntimeError):
    """可直接展示的种子前置条件或状态错误。"""


def _unique[T](items: list[T], label: str) -> T | None:
    if len(items) > 1:
        raise DemoSeedError(f"DEMO_AMBIGUOUS：存在多个同标识的{label}，请先人工核对。")
    return items[0] if items else None


def seed_demo(
    session: Session,
    *,
    settings: AppSettings,
    embedding_provider: BaseEmbeddingProvider | None = None,
) -> dict[str, Any]:
    """幂等准备演示数据；注入 Embedding 仅用于验证，不替代领域服务。"""
    if not settings.DEV_MODE:
        raise DemoSeedError("DEMO_DEV_MODE_REQUIRED：请在本地演示配置中显式设置 DEV_MODE=true。")
    # 先检查随镜像打包的教材，避免缺文件时创建半份演示数据。
    content = SOURCE_PATH.read_bytes()
    accounts = ensure_dev_mode_accounts(session, settings=settings)
    teacher_id = accounts[UserRole.TEACHER].id
    courses = CourseService(session)
    course = _unique([
        item for item in courses.list_courses(teacher_id=teacher_id) if item.name == COURSE_NAME
    ], "演示课程")
    if course is None:
        course = courses.create_course(
            COURSE_NAME, "T087 演示数据；仅供本地教学演示。", created_by=teacher_id,
        )

    knowledge = KnowledgeBaseService(session)
    knowledge_base = _unique([
        item for item in knowledge.list_knowledge_bases(course.id, teacher_id=teacher_id)
        if item.name == KNOWLEDGE_BASE_NAME
    ], "演示知识库")
    if knowledge_base is None:
        knowledge_base = knowledge.create_knowledge_base(
            course.id, KNOWLEDGE_BASE_NAME, teacher_id=teacher_id,
        )
    document = _unique([
        item for item in knowledge.list_documents(knowledge_base.id, teacher_id=teacher_id)
        if item.original_filename == SOURCE_PATH.name
    ], "演示资料")
    if document is None:
        document = knowledge.upload_document(
            course_id=course.id, knowledge_base_id=knowledge_base.id, uploaded_by=teacher_id,
            original_filename=SOURCE_PATH.name, storage_path=str(SOURCE_PATH),
        )
    if document.status is not DocumentStatus.READY:
        if document.status not in {DocumentStatus.UPLOADED, DocumentStatus.FAILED}:
            raise DemoSeedError("DEMO_INGESTION_IN_PROGRESS：资料仍在处理中，请先核对处理状态。")
        if document.status is DocumentStatus.FAILED and not document.retryable:
            raise DemoSeedError("DEMO_INGESTION_NOT_RETRYABLE：请先修正资料或 Provider 配置。")
        provider = embedding_provider or create_embedding_provider(settings)
        outcome = knowledge.ingest_document(
            document.id, content=content, teacher_id=teacher_id,
            ingestion_service_factory=lambda listener: IngestionService(
                embedding_provider=provider, on_transition=listener,
            ),
        )
        if outcome.status is not DocumentStatus.READY:
            raise DemoSeedError(
                f"DEMO_INGESTION_FAILED：{outcome.error_code}；修正后重跑，不会重复登记资料。"
            )

    questions = QuestionService(session)
    existing = questions.list_questions(course.id, teacher_id=teacher_id)
    question_ids: list[str] = []
    question_states: list[dict[str, str]] = []
    awaiting_question_ids: list[str] = []
    for spec in QUESTION_SPECS:
        question = _unique([
            item for item in existing if item.content == spec["content"]
        ], "演示题目")
        if question is None:
            question = questions.create_question(course.id, created_by=teacher_id, **spec)
        if question.status is QuestionStatus.DRAFT:
            question = questions.update_question_status(
                question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher_id,
            )
        question_ids.append(question.id)
        question_states.append({"id": question.id, "status": question.status.value})
        if question.status is not QuestionStatus.APPROVED:
            awaiting_question_ids.append(question.id)

    exams = ExamService(session)
    exam = _unique([
        item for item in exams.list_exams(course.id, teacher_id=teacher_id)
        if item.title == EXAM_TITLE
    ], "演示考试")
    common_result = {
        "course_id": course.id, "knowledge_base_id": knowledge_base.id,
        "document_id": document.id, "question_ids": question_ids,
        "question_states": question_states, "awaiting_question_ids": awaiting_question_ids,
        "accounts": {role.value: account.username for role, account in accounts.items()},
        "login": "使用 /gradio 登录页的开发模式快速登录按钮；不提供密码。",
    }
    if awaiting_question_ids:
        return {
            **common_result,
            "status": "awaiting_teacher_review",
            "exam_id": exam.id if exam is not None else None,
            "exam_status": exam.status.value if exam is not None else None,
            "next_step": (
                "教师在题库补全缺失评分标准，选择本课程真实教学片段，"
                "执行当前语义核验并逐题审核通过后，重跑本命令准备演示考试。"
            ),
        }
    if exam is None:
        exam = exams.create_exam(
            course.id, EXAM_TITLE, description="客观题 + 主观题；无截止日期，供开发账号体验。",
            duration_minutes=30, question_ids=question_ids, created_by=teacher_id,
        )
    if set(exam.question_ids) != set(question_ids):
        raise DemoSeedError("DEMO_EXAM_CHANGED：演示考试题目已变更，未覆盖现有组卷。")
    if exam.status is ExamStatus.DRAFT:
        exam = exams.publish_exam(exam.id, teacher_id=teacher_id)
    if exam.status is not ExamStatus.PUBLISHED:
        raise DemoSeedError("DEMO_EXAM_NOT_OPEN：演示考试已关闭或归档，未重置生命周期。")
    return {
        **common_result,
        "status": "ready", "exam_id": exam.id, "exam_status": exam.status.value,
    }


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    engine = None
    try:
        settings = get_settings()
        engine = create_database_engine(settings)
        with create_session_factory(engine)() as session:
            result = seed_demo(session, settings=settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (DemoSeedError, ConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 失败时不泄露密钥、连接串或 Provider 响应
        print(f"DEMO_SEED_FAILED：{type(exc).__name__}；检查迁移、数据库和 Embedding 配置。",
              file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
