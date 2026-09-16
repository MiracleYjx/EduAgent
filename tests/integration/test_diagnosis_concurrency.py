"""TCR（B02）：诊断保存的检查与写入必须受同一事务锁保护。

在真实 PostgreSQL 隔离 schema 中交错两个请求：旧请求通过来源检查后暂停，
新请求更新成绩并保存诊断，再继续旧请求；最终必须保留新报告。使用数据库阻塞状态
协调线程，不用固定睡眠推断锁是否生效，不调用模型或修改开发库业务表。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.models import DiagnosisReport, ExamResult
from backend.app.schemas.grading import DiagnosisStatus
from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.grading.test_diagnosis_report_store import (
    _final_exam_result,
    _ready_report,
)
from tests.unit.models.sqlite_support import seed_submission


def test_old_save_cannot_overwrite_new_report_during_interleaved_update() -> None:
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            fixture = seed_submission(session)
        exam = _final_exam_result(engine, fixture)
        store = DiagnosisReportStore(session_factory=lambda: Session(engine))
        initial = store.save(_ready_report(exam))
        old_report = _ready_report(exam).model_copy(update={"learning_suggestions": ["旧建议"]})
        new_time = exam.aggregated_at + timedelta(minutes=1)
        new_exam = exam.model_copy(update={"aggregated_at": new_time})
        new_report = _ready_report(new_exam).model_copy(update={"learning_suggestions": ["新建议"]})
        checked, release_old, new_done = Event(), Event(), Event()
        writer_pid: Queue[int] = Queue()

        class PausedStore(DiagnosisReportStore):
            def _latest_row(self, session: Session, submission_id: UUID) -> DiagnosisReport | None:
                checked.set()
                assert release_old.wait(15), "旧请求未收到继续信号"
                return super()._latest_row(session, submission_id)

        def save_new() -> None:
            try:
                with Session(engine) as session, session.begin():
                    session.execute(text("SET LOCAL statement_timeout = '15000ms'"))
                    writer_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
                    row = session.scalars(select(ExamResult)).one()
                    row.aggregated_at = new_time
                store.save(new_report)
            finally:
                new_done.set()

        old_store = PausedStore(session_factory=lambda: Session(engine))
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(old_store.save, old_report)
            try:
                assert checked.wait(10), "旧请求未到达来源检查后的暂停点"
                new = pool.submit(save_new)
                pid = writer_pid.get(timeout=10)
                blocked = False
                deadline = monotonic() + 10
                with engine.connect() as observer:
                    while not new_done.is_set() and monotonic() < deadline:
                        blocked = bool(observer.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                            {"pid": pid},
                        ))
                        if blocked:
                            break
                        sleep(0.01)
                assert blocked or new_done.is_set(), "新请求既未完成也未产生可观察的锁等待"
            finally:
                release_old.set()
            old.result(timeout=20)
            new.result(timeout=20)

        latest = store.read(str(fixture.submission_id), new_exam)
        assert latest.status is DiagnosisStatus.READY
        assert latest.learning_suggestions == ["新建议"]
        assert latest.source_exam_result_updated_at == new_time
        assert latest.exam_result_id == initial.exam_result_id
        assert blocked, "来源检查与写入期间没有锁住成绩行"
        # 再次到达的过期请求同样不能覆盖已经提交的新报告。
        store.save(old_report)
        assert store.read(str(fixture.submission_id), new_exam) == latest
