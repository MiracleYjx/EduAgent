"""P3.2：真实 PostgreSQL、独立应用/Session 的 M4 启动互斥与锁生命周期。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.app.api.workflow import WORKFLOW_SUBMISSION_NOT_READY
from backend.app.domain.enums import SubmissionStatus, WorkflowStatus
from backend.app.models import Submission, WorkflowRun
from backend.app.services.grading.grading_repository import CHECKPOINT_KIND
from backend.app.services.workflow_checkpoint import run_kind_criteria
from tests.integration.test_langgraph_grading_workflow import (
    ReviewEnv,
    SequenceScoringProvider,
    _api_client,
    _seed_paper,
    _start,
)
from tests.postgres_helpers import isolated_postgres_engine


@pytest.fixture
def solo_env() -> Iterator[ReviewEnv]:
    """沿用 T079 输入事实，在本用例专属 schema 中准备已提交答卷。"""

    with isolated_postgres_engine() as engine, Session(engine) as session:
        yield ReviewEnv(engine=engine, paper=_seed_paper(session, "start", solo=True))


class HeldScoringProvider(SequenceScoringProvider):
    """只暂挂外部模型边界，让主线程验证在途运行和锁释放时机。"""

    def __init__(self) -> None:
        super().__init__([0.3])
        self.entered = Event()
        self.release = Event()

    async def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        assert await asyncio.to_thread(self.release.wait, 20), "模型测试闸门未释放"
        return await super().generate_structured(*args, **kwargs)


@pytest.mark.parametrize("with_m3_run", [False, True], ids=["m4-only", "m3-isolated"])
def test_concurrent_start_reuses_one_run_and_releases_submission_lock(
    solo_env: ReviewEnv, with_m3_run: bool
) -> None:
    """两请求确实并发争锁；模型尚未返回时另一请求已复用，独立 Session 可取得锁。"""

    submission_id = UUID(solo_env.paper.submission_id)
    if with_m3_run:
        with Session(solo_env.engine) as session, session.begin():
            session.add(
                WorkflowRun(
                    workflow_id="m3-running",
                    request_id="m3-request",
                    submission_id=submission_id,
                    status=WorkflowStatus.RUNNING,
                    checkpoint={"kind": CHECKPOINT_KIND, "task": {}},
                    current_node="score",
                )
            )

    scoring = HeldScoringProvider()
    contenders = Event()
    contenders_guard = Lock()
    backend_pids: set[int] = set()

    def observe_lock(
        _connection: Any, cursor: Any, statement: str, *_args: Any
    ) -> None:
        if "FOR NO KEY UPDATE" in statement and "submissions" in statement:
            with contenders_guard:
                backend_pids.add(cursor.connection.info.backend_pid)
                if len(backend_pids) == 2:
                    contenders.set()

    with (
        _api_client(solo_env, scoring_provider=scoring) as first,
        _api_client(solo_env, scoring_provider=scoring) as second,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        # 先持有真实行锁，使两个请求均到达锁竞争点；不是依赖线程调度碰巧重叠。
        gate = Session(solo_env.engine)
        futures = []
        try:
            gate.execute(
                select(Submission.id)
                .where(Submission.id == submission_id)
                .with_for_update(key_share=True)
            ).one()
            event.listen(solo_env.engine, "before_cursor_execute", observe_lock)
            try:
                futures = [executor.submit(_start, app, solo_env) for app in (first, second)]
                assert contenders.wait(10), "两个独立 PostgreSQL 连接必须同时尝试答卷锁"
                assert not any(future.done() for future in futures)
            finally:
                event.remove(solo_env.engine, "before_cursor_execute", observe_lock)
                gate.rollback()
                gate.close()

            assert len(backend_pids) == 2
            assert scoring.entered.wait(10), "受理后必须执行真实图并到达模型边界"
            done, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            assert len(done) == len(pending) == 1, "复用请求不应等待模型完成"
            reused = done.pop().result()
            assert reused.status_code == 200
            assert reused.json()["reused"] is True
            assert reused.json()["status"] == WorkflowStatus.RUNNING.value

            # 第三个独立 Session 在模型仍阻塞时立即取写锁，同时读到已提交的受理状态。
            with Session(solo_env.engine) as observer, observer.begin():
                observer.execute(
                    select(Submission.id)
                    .where(Submission.id == submission_id)
                    .with_for_update(key_share=True, nowait=True)
                ).one()
                row = observer.scalars(
                    select(WorkflowRun).where(run_kind_criteria())
                ).one()
                assert row.status is WorkflowStatus.RUNNING
                assert row.workflow_id == reused.json()["workflow_id"]
            assert not scoring.release.is_set()
        finally:
            gate.close()
            scoring.release.set()

        responses = [future.result(timeout=20) for future in futures]
        assert [response.status_code for response in responses] == [200, 200]
        bodies = [response.json() for response in responses]
        assert sorted(body["reused"] for body in bodies) == [False, True]
        assert len({body["workflow_id"] for body in bodies}) == 1
        assert len({body["thread_id"] for body in bodies}) == 1

        # 保留顺序重复启动语义：新的 Session/请求仍复用原运行，不再次调用模型。
        repeated = _start(second, solo_env)
        assert repeated.status_code == 200
        assert repeated.json()["reused"] is True
        assert repeated.json()["workflow_id"] == bodies[0]["workflow_id"]

    assert len(scoring.calls) == 1
    with Session(solo_env.engine) as session:
        rows = list(session.scalars(select(WorkflowRun)))
        assert len(rows) == 1 + int(with_m3_run)
        m4 = session.scalars(select(WorkflowRun).where(run_kind_criteria())).one()
        assert m4.status is WorkflowStatus.PAUSED
        if with_m3_run:
            m3 = next(row for row in rows if row.workflow_id == "m3-running")
            assert m3.status is WorkflowStatus.RUNNING
            assert m3.checkpoint == {"kind": CHECKPOINT_KIND, "task": {}}


def test_waiting_start_rechecks_submission_lifecycle_after_lock(
    solo_env: ReviewEnv,
) -> None:
    """等锁期间 Submitted 已变为 Graded，不得按锁前旧快照重新受理。"""

    attempting = Event()
    scoring = SequenceScoringProvider([0.3])

    def observe_lock(
        _connection: Any, _cursor: Any, statement: str, *_args: Any
    ) -> None:
        if "FOR NO KEY UPDATE" in statement and "submissions" in statement:
            attempting.set()

    with (
        _api_client(solo_env, scoring_provider=scoring) as app,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        with Session(solo_env.engine) as gate:
            submission = gate.scalars(
                select(Submission)
                .where(Submission.id == UUID(solo_env.paper.submission_id))
                .with_for_update(key_share=True)
            ).one()
            event.listen(solo_env.engine, "before_cursor_execute", observe_lock)
            try:
                future = executor.submit(_start, app, solo_env)
                assert attempting.wait(10)
                assert not future.done()
                submission.status = SubmissionStatus.GRADED
                gate.commit()
            finally:
                event.remove(solo_env.engine, "before_cursor_execute", observe_lock)
                gate.rollback()
        response = future.result(timeout=20)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == WORKFLOW_SUBMISSION_NOT_READY
    assert scoring.calls == []
    with Session(solo_env.engine) as session:
        assert list(session.scalars(select(WorkflowRun))) == []
