"""Personal practice preparation and answering; grading is a later task."""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import LearningApiError
from backend.app.schemas.personal_practice import (
    PersonalPracticeView,
    PracticeAnswerInput,
    PracticeAnswerView,
    PracticeBlankConfirmation,
    PracticeConditions,
    PracticePreviewChanged,
    PracticePreviewView,
    PracticeReport,
    PracticeStartRequest,
    PracticeSubmitRequest,
)
from backend.app.services.personal_grading import BlankAnswersPending
from backend.app.services.practice_service import PracticeService

router = APIRouter(
    prefix="/api/learning",
    tags=["personal-practice"],
    route_class=PersonalRoute,
    responses={s: {"model": LearningApiError} for s in (403, 404, 409, 422, 503)},
)


@router.post(
    "/subjects/{subject_id}/practices/preview", response_model=PracticePreviewView
)
def preview(subject_id: UUID, body: PracticeConditions, space: Subjects):
    return PracticeService(space).preview(subject_id, body)


@router.get("/practice-previews/{identity}", response_model=PracticePreviewView)
def get_preview(identity: UUID, space: Subjects):
    return PracticeService(space).preview_view(identity)


@router.post(
    "/practice-previews/{identity}/start",
    response_model=PersonalPracticeView,
    response_model_exclude_none=True,
    responses={409: {"model": PracticePreviewChanged}},
)
def start(identity: UUID, body: PracticeStartRequest, space: Subjects):
    result = PracticeService(space).start(identity, body)
    if isinstance(result, PracticePreviewChanged):
        return JSONResponse(status_code=409, content=result.model_dump(mode="json"))
    return result


@router.get(
    "/practices/{identity}",
    response_model=PersonalPracticeView,
    response_model_exclude_none=True,
)
def get_practice(identity: UUID, space: Subjects):
    return PracticeService(space).practice(identity)


@router.put(
    "/practices/{identity}/items/{item_id}/answer", response_model=PracticeAnswerView
)
def save_answer(
    identity: UUID, item_id: UUID, body: PracticeAnswerInput, space: Subjects
):
    return PracticeService(space).save_answer(identity, item_id, body)


@router.post(
    "/practices/{identity}/submit",
    response_model=PracticeReport,
    responses={409: {"model": PracticeBlankConfirmation}},
)
def submit(identity: UUID, body: PracticeSubmitRequest, space: Subjects):
    """整组提交；空白题未确认时返回 409 与待确认题目，不静默评分。"""

    try:
        return PracticeService(space).submit(identity, body)
    except BlankAnswersPending as pending:
        return JSONResponse(
            status_code=409,
            content=PracticeBlankConfirmation(
                item_ids=list(pending.item_ids)
            ).model_dump(mode="json"),
        )


@router.get("/practices/{identity}/results", response_model=PracticeReport)
def results(identity: UUID, space: Subjects):
    """Canonical result read model; ``/report`` remains a compatibility alias."""

    return PracticeService(space).report(identity)


@router.get("/practices/{identity}/report", response_model=PracticeReport)
def report(identity: UUID, space: Subjects):
    return PracticeService(space).report(identity)
