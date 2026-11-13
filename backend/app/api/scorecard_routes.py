"""Routes of a project's scorecard.

Its own file by the same rule as the proposal: the scorecard has its own domain
layer (app/scorecard) and its own character of writing — snapshots and metric
configs live outside the revision journal; the only thing that goes through the
mutation layer is the task created by the "red two weeks running" rule, and the
domain does that itself.

Reading is for anyone who can see the project, and it has a side effect: the
first GET after a week boundary appends the weekly snapshots (lazy commitment —
see app.scorecard). Writing (recalculation, metric configuration) requires
write access to the project.
"""

import uuid
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from app.access import Action
from app.api.deps import ProjectContext, project_context
from app.db import get_db
from app.live import hub
from app.scorecard import (
    DEFAULT_WEEKS,
    ScorecardError,
    metric_tasks,
    patch_metric,
    scorecard_state,
)
from app.throttle import hit

router = APIRouter(prefix="/api/projects", tags=["scorecard"])

#: Recalculating more often than once a minute is pointless — the current
#: week's cache lasts five minutes anyway, and the button must not turn into a
#: way of loading the database.
RECALC_LIMIT_PER_MINUTE = 1


class ScorecardMetricPatch(BaseModel):
    """Editing a metric: the fields sent are changed, the rest are untouched.

    owner_user_id accepts an explicit null — "clear the owner"; telling "not
    sent" from "sent as null" is done by exclude_unset in the route.
    """

    owner_user_id: uuid.UUID | None = None
    target_value: float | None = Field(default=None, ge=0, le=99_999_999)
    enabled: bool | None = None


def _refuse(error: ScorecardError) -> HTTPException:
    """A domain refusal becomes an HTTP refusal by the same logic as mutations:
    a foreign or nonexistent entity is a 404, everything else is a 422."""
    if error.code == "metric_not_found":
        return HTTPException(status_code=404, detail=error.code)
    return HTTPException(status_code=422, detail=error.code)


def _publish(
    background: BackgroundTasks, db: DbSession, project_id: uuid.UUID, event: dict
) -> None:
    # The same order as in mutations: commit first, then broadcast.
    db.commit()
    background.add_task(hub.publish, project_id, event)


#: An event for neighbouring tabs: "the scorecard changed, re-read it".
_CHANGED = {"type": "scorecard"}


def _visibility(context: ProjectContext) -> dict:
    """What part of the per-person breakdown this reader is entitled to. Decided
    here, by the permission matrix — the client receives an already trimmed answer."""
    return {
        "include_team": context.can(Action.TEAM_PACE_READ),
        "include_assessment": context.can(Action.TEAM_ASSESSMENT_READ),
    }


@router.get("/{project_id}/scorecard")
def get_project_scorecard(
    weeks: int = Query(default=DEFAULT_WEEKS, ge=1, le=26),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """The whole scorecard: metrics with history, events, data quality.

    The side effect is the lazy commitment of weeks: the architecture has no
    scheduler, and the first reader after a week boundary appends the snapshots.
    """
    return scorecard_state(
        db, context.project, context.org, weeks=weeks, **_visibility(context)
    )


@router.post("/{project_id}/scorecard/recalculate")
def recalculate_project_scorecard(
    background: BackgroundTasks,
    weeks: int = Query(default=DEFAULT_WEEKS, ge=1, le=26),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Recalculating the current week past the cache. The current one only: past
    snapshots are immutable, and no button touches them."""
    context.require(Action.PROJECT_WRITE)
    if not hit(
        db,
        f"scorecard_recalc:{context.project.id}",
        limit=RECALC_LIMIT_PER_MINUTE,
        window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    state = scorecard_state(
        db, context.project, context.org, weeks=weeks,
        actor_id=context.user.id, force=True, **_visibility(context),
    )
    _publish(background, db, context.project.id, _CHANGED)
    return state


@router.patch("/{project_id}/scorecard/metrics/{metric_key}")
def update_scorecard_metric(
    metric_key: str,
    payload: ScorecardMetricPatch,
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """This project's configuration of a metric: owner, target, enabled state.

    The direction is not editable — it follows rigidly from the key. After an
    edit the current week is recalculated right away: the status depends on the
    target, and the screen must not show the old colour under a new target.
    """
    context.require(Action.PROJECT_WRITE)
    changes = payload.model_dump(exclude_unset=True)
    try:
        patch_metric(db, context.project, context.org, metric_key, changes)
    except ScorecardError as error:
        raise _refuse(error)
    state = scorecard_state(
        db, context.project, context.org, actor_id=context.user.id, force=True,
        **_visibility(context),
    )
    _publish(background, db, context.project.id, _CHANGED)
    return state


@router.get("/{project_id}/scorecard/metrics/{metric_key}/tasks")
def get_scorecard_metric_tasks(
    metric_key: str,
    week: date | None = Query(default=None),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """A metric drill-down: the week's tasks. Past weeks come from the snapshot,
    the current one is computed live without being written."""
    try:
        return metric_tasks(
            db, context.project, context.org, metric_key, week, **_visibility(context)
        )
    except ScorecardError as error:
        raise _refuse(error)
