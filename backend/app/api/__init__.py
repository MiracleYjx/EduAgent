"""API boundary package."""

from typing import Any

from backend.app.schemas.personal_learning import LearningApiError


def learning_errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Stable personal error docs across supported Python HTTPStatus versions."""
    descriptions = {
        403: "Forbidden", 404: "Not Found", 409: "Conflict",
        413: "Content Too Large", 415: "Unsupported Media Type",
        422: "Unprocessable Content", 503: "Service Unavailable",
    }
    return {status: {"model": LearningApiError, "description": descriptions[status]}
            for status in statuses}
