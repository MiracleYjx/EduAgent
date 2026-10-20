"""Strict-pool, no-replacement selection with observable coverage and shortages."""

import random
from dataclasses import dataclass, field
from uuid import UUID

from backend.app.schemas.personal_practice import PracticeConditions


@dataclass(frozen=True)
class SelectionCandidate:
    question_id: UUID
    canonical_id: UUID
    revision_id: UUID
    knowledge_point_ids: frozenset[UUID] = frozenset()
    attempted: bool = False
    original_wrong: bool = False
    source_kind: str = "uploaded"


@dataclass
class SelectionResult:
    selected: list[SelectionCandidate]
    eligible_count: int
    unseen_count: int
    candidate_shortage: int
    unseen_shortage: int
    coverage: dict[UUID, int] = field(default_factory=dict)
    original_wrong_count: int = 0
    other_count: int = 0


def select_candidates(
    candidates: list[SelectionCandidate],
    conditions: PracticeConditions,
    *,
    weak_points: set[UUID] | None = None,
    allowed_ai_ids: set[UUID] | None = None,
    rng: random.Random | None = None,
) -> SelectionResult:
    randomizer = rng or random.SystemRandom()
    allowed_ai = allowed_ai_ids or set()
    pool = [
        row
        for row in candidates
        if (conditions.source == "ai" and row.source_kind == "ai")
        or (conditions.source != "ai" and row.source_kind == "uploaded")
        or (conditions.source == "mixed" and row.question_id in allowed_ai)
    ]
    pool = [
        row
        for row in pool
        if not conditions.knowledge_point_ids
        or row.knowledge_point_ids & set(conditions.knowledge_point_ids)
    ]
    # Prefer a genuine uploaded representative when the same family has two sources.
    randomizer.shuffle(pool)
    pool.sort(key=lambda row: row.source_kind != "uploaded")
    families: dict[UUID, SelectionCandidate] = {}
    for row in pool:
        families.setdefault(row.canonical_id, row)
    pool = list(families.values())
    unseen = sum(not row.attempted for row in pool)
    total = len(pool)
    available = [
        row
        for row in pool
        if not conditions.prefer_unseen
        or conditions.allow_practiced
        or not row.attempted
    ]
    targets = set(conditions.knowledge_point_ids) or {
        point for row in pool for point in row.knowledge_point_ids
    }
    coverage = {point: 0 for point in targets}
    weak = weak_points or set()
    selected: list[SelectionCandidate] = []
    while available and len(selected) < conditions.count:

        def rank(row):
            source = (
                int(row.source_kind != "uploaded")
                if conditions.source == "mixed"
                else 0
            )
            done = int(row.attempted) if conditions.prefer_unseen else 0
            points = (
                row.knowledge_point_ids & set(coverage)
                if coverage
                else row.knowledge_point_ids
            )
            if conditions.mode == "weak":
                on_weak = bool(row.knowledge_point_ids & weak)
                # Other questions come first; thereafter interleave available original
                # errors instead of reserving a fixed percentage of the set.
                want_original = bool(selected and not selected[-1].original_wrong)
                return (
                    source,
                    int(not on_weak),
                    int(row.original_wrong != want_original) if on_weak else 0,
                    done,
                )
            if conditions.mode == "balanced":
                values = [coverage.get(point, 0) for point in points]
                return (
                    source,
                    done,
                    min(values, default=len(selected) + 1),
                    -sum(value == 0 for value in values),
                )
            return source, done

        winner = min(available, key=rank)
        available.remove(winner)
        selected.append(winner)
        for point in winner.knowledge_point_ids:
            coverage[point] = coverage.get(point, 0) + 1
    wrong = sum(row.original_wrong for row in selected)
    return SelectionResult(
        selected=selected,
        eligible_count=total,
        unseen_count=unseen,
        candidate_shortage=max(0, conditions.count - total),
        unseen_shortage=max(0, conditions.count - unseen)
        if conditions.prefer_unseen
        else 0,
        coverage=coverage,
        original_wrong_count=wrong,
        other_count=len(selected) - wrong,
    )
