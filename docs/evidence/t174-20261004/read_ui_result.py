import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from uuid import UUID
from backend.app.core.config import get_settings
from backend.app.core.database import create_database_engine,create_session_factory
from backend.app.models import Exam
from backend.app.services.exam_scoring_service import ExamScoringService
engine=create_database_engine(get_settings());assert engine.url.database.startswith("eduagent_e4_batch_")
ids=json.loads(Path(".cache/t170-174-20261004/ui_fixture.json").read_text(encoding="utf-8"))
with create_session_factory(engine)() as session:
    exam=session.get(Exam,UUID(ids["exam_id"]))
    view=ExamScoringService(session).get_scoring_basis(exam.id,UUID(ids["question_id"]),teacher_id=UUID(ids["teacher_id"]))
    assert exam.status.value=="Published"
    assert view.basis.rounding_delta==__import__("decimal").Decimal("0.01")
    assert [str(p.default_points) for p in view.basis.points]==["3.33"]*3
    assert [str(p.confirmed_points) for p in view.basis.points]==["3.34","3.33","3.33"]
    assert not view.editable
    print(json.dumps({"status":exam.status.value,"published_knowledge_points":exam.exam_question_links[0].published_knowledge_points,"view":view.model_dump(mode="json")},ensure_ascii=False,indent=2))
engine.dispose()
