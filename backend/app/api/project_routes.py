import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.access import Action, can, needs_project_grant, parse_role, visible_op
from app.api.deps import ProjectContext, project_context
from app.api.serialization import comments_out, project_state
from app.auth import current_user
from app.calendar import CalendarError
from app.comments import CommentRejected, add_comment, comment_counts, list_comments
from app.db import get_db
from app.live import hub
from app.models import (
    Category,
    IdempotencyRecord,
    Membership,
    Project,
    ProjectAccess,
    Revision,
    Task,
    User,
)
from app.mutations import (
    MutationError,
    NotFoundInProject,
    PublicOp,
    ReasonRequired,
    UndoConflict,
    apply_op,
    last_undoable,
    to_internal,
    undo_batch,
)

# An alias, because the route handler below has the same name — and a handler's
# name must not be changed without need: FastAPI builds the operationId from it,
# and the contract snapshot depends on that.
from app.mutations import MAX_WIRE_DATE, undo_last as undo_last_revision
from app.orgs import current_membership
from app.plans import PlanVersionNotFound, approve_plan, plan_versions, restore_plan_version
from app.projects import create_project as create_project_entity, lock_project
from app.schedule import apply_schedule, planned_schedule
from app.settings_input import (
    MAX_WORKING_DAYS,
    MIN_WORKING_DAYS,
    NULLABLE_PROJECT_FIELDS,
    ProjectSettingsIn,
    changes,
)
from app.slugs import slug_check

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str


class CommentIn(BaseModel):
    body: str = Field(min_length=1)
    task_id: uuid.UUID | None = None
    #: An "aside" remark: visible to members, not to a public-link guest.
    internal: bool = False


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectIn,
    membership: Membership = Depends(current_membership),
    db: DbSession = Depends(get_db),
):
    # scoped=... here too, not only on reads: a new project has no pre-granted
    # access and cannot have any, while a narrowed role needs it for any action —
    # so can() always refuses them, and that is correct. A different answer would
    # be a hole in the narrowing itself: an editor locked inside a handful of
    # projects would create new ones for themselves around the list — and then
    # would not see them in /api/projects, because there the same check asks for
    # granted access rather than for the creator.
    if not can(parse_role(membership.role), Action.PROJECT_WRITE, scoped=membership.project_scoped):
        raise HTTPException(status_code=403, detail="forbidden")

    project = create_project_entity(db, org_id=membership.org_id, name=payload.name)
    return ProjectOut(id=str(project.id), name=project.name, slug=project.slug)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    user: User = Depends(current_user),
    membership: Membership = Depends(current_membership),
    db: DbSession = Depends(get_db),
):
    role = parse_role(membership.role)
    if not can(role, Action.PROJECT_READ, project_granted=True):
        # A role that reads no projects under any grant (that is, a value outside
        # the matrix) does not get a list either. The same 404 as for a single
        # project: an empty list would say "there are no projects", which is not
        # true.
        raise HTTPException(status_code=404, detail="project_not_found")

    query = select(Project).where(Project.org_id == membership.org_id)
    if needs_project_grant(role, scoped=membership.project_scoped):
        # A role invited into projects individually (or whose own membership is
        # narrowed, see Membership.project_scoped) sees exactly those. Filtered by
        # the query rather than sifted in Python: the organization's project list
        # is precisely what is being hidden from them.
        query = query.join(ProjectAccess, ProjectAccess.project_id == Project.id).where(
            ProjectAccess.user_id == user.id
        )
    projects = db.scalars(query).all()
    return [ProjectOut(id=str(p.id), name=p.name, slug=p.slug) for p in projects]


def _undoable(db: DbSession, context: ProjectContext) -> dict | None:
    """The entry the "Undo" button will remove — or None.

    It passes through visible_op: the snapshot of a deleted task carries an
    internal note, and the button's label must not give it away.
    """
    if not context.can(Action.PROJECT_WRITE):
        return None
    revision = last_undoable(db, context.project)
    if revision is None:
        return None
    return {
        "seq": revision.seq,
        "op": visible_op(revision.op, context.role, project_granted=context.granted),
        "batch_id": str(revision.batch_id) if revision.batch_id else None,
    }


@router.get("/{project_id}")
def get_project(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    try:
        return project_state(
            db,
            context.project,
            context.org,
            show_notes=context.can(Action.READ_INTERNAL_NOTE),
            undoable=_undoable(db, context),
        )
    except CalendarError as error:
        # The same shape as mutation refusals: a 422 with a machine code. There
        # used to be a bare 500 here — a degenerate mask is set by a person, and
        # the project stopped being readable with no explanation.
        raise HTTPException(status_code=422, detail=error.code)


def _revision_entry(revision: Revision, actor_name: str | None, op: dict) -> dict:
    """A journal entry in the form the client reads it.

    One shape for two paths: the history feed is requested over HTTP, while new
    revisions arrive through the socket. Assembled separately, they would diverge
    on the very first added field, and the client would have to parse two shapes of
    one event.

    `op` arrives as a parameter rather than being taken from the revision: who sees
    what is decided at the recipient (over HTTP by the caller's role, in the socket
    by each subscriber's role separately), and deciding it here would mean deciding
    it twice.
    """
    return {
        "seq": revision.seq,
        "created_at": revision.created_at.isoformat(),
        # There may be no author: AI operations and system entries go without a
        # person, and inventing an author for them is not allowed.
        "actor": (
            {"id": str(revision.actor_user_id), "name": actor_name}
            if actor_name is not None
            else None
        ),
        # The reason is a person's text: returned as is and not translated.
        "reason": revision.reason,
        # The batch and the undo mark. By batch_id the feed folds an AI application
        # into a single line; by undoes_seq it marks undone entries — an undo entry
        # is always newer than the one it undoes, so when reading from the head of
        # the journal the pair meets up on the client without a second request.
        "batch_id": str(revision.batch_id) if revision.batch_id else None,
        "undoes_seq": revision.undoes_seq,
        "op": op,
    }


# Operation keys that carry entity identifiers, by the kind of entity. A
# dictionary rather than a dispatch over operation types: a new operation with a
# task_id gets a name in the feed for free, with no edit to this place.
_OP_ID_KEYS = {
    "task": ("task_id", "from_task_id", "to_task_id"),
    "category": ("category_id",),
    "user": ("user_id",),
}


def _referenced_ids(op: dict) -> set[str]:
    """Every entity identifier an operation mentions."""
    return {
        str(op[key]) for keys in _OP_ID_KEYS.values() for key in keys if op.get(key)
    }


def _names_for(db: DbSession, project: Project, ops: list[dict]) -> dict[str, str]:
    """uuid -> name for everything the page's operations refer to.

    A whole project's feed is unreadable without names: "moved the start from the
    12th to the 19th of March" does not say whose start it is. A task card does not
    need the names — there is one entity there and it is in the heading — but the
    answer's shape is the same for both: two shapes of one journal would drift
    apart on the very first added field.

    The names of live tasks and categories are taken from the tables; those of
    deleted ones from the restore snapshot in their delete revision: the journal is
    the only place where a name outlived a deletion. An identifier whose name is
    nowhere (a user erased their account) does not reach the dictionary — the
    client makes do with a phrase without a name.
    """
    ids: dict[str, set[str]] = {kind: set() for kind in _OP_ID_KEYS}
    for op in ops:
        for kind, keys in _OP_ID_KEYS.items():
            for key in keys:
                if op.get(key):
                    ids[kind].add(str(op[key]))

    names: dict[str, str] = {}
    for kind, model in (("task", Task), ("category", Category), ("user", User)):
        if ids[kind]:
            for row in db.execute(
                select(model.id, model.name).where(model.id.in_(ids[kind]))
            ):
                names[str(row.id)] = row.name

    # Deleted tasks and categories: the name is taken from the inverse of their
    # delete revision. The freshest entry wins — an entity renamed and then deleted
    # is called what it was called at the moment of deletion.
    for op_type, key, missing in (
        ("delete_task", "task_id", ids["task"] - set(names)),
        ("delete_category", "category_id", ids["category"] - set(names)),
    ):
        if not missing:
            continue
        deletions = db.scalars(
            select(Revision)
            .where(
                Revision.project_id == project.id,
                Revision.op["type"].astext == op_type,
                Revision.op[key].astext.in_(missing),
            )
            .order_by(Revision.seq.desc())
        ).all()
        for revision in deletions:
            entity_id = str(revision.op[key])
            name = revision.inverse.get("name")
            if entity_id not in names and name:
                names[entity_id] = name

    # Tasks carried away by the cascade together with their category: they have no
    # deletion entry of their own at all — their names lie in the category's restore
    # snapshot, as a list inside its inverse. Without this pass, entries about such
    # tasks would read in the feed with no subject: "moved the start from the 12th
    # to the 19th of March" — and there would be nothing left to say whose start it is.
    orphaned = ids["task"] - set(names)
    if orphaned:
        cascades = db.scalars(
            select(Revision)
            .where(
                Revision.project_id == project.id,
                Revision.op["type"].astext == "delete_category",
            )
            .order_by(Revision.seq.desc())
        ).all()
        for revision in cascades:
            for snapshot in revision.inverse.get("tasks", []):
                task_id = str(snapshot.get("task_id"))
                if task_id in orphaned and task_id not in names and snapshot.get("name"):
                    names[task_id] = snapshot["name"]
    return names


@router.get("/{project_id}/revisions")
def list_revisions(
    task_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    types: list[str] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    before_seq: int | None = Query(default=None, ge=1),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """The project's change journal, optionally that of a single task.

    An entry is returned as parameters rather than as a ready phrase: the reader's
    language is decided in the browser, and one and the same move must read in
    three languages. The server deliberately keeps no message dictionaries (see
    MutationError).

    The filters — by author and by operation type — are computed by the server
    rather than by the client: the feed is paged by cursor, and client-side sifting
    would turn "Show more" into a lottery — the page exists, but it may hold no
    matching entries.

    The inverse operation does not go outward: it is needed for the undo, and the
    undo is done by the server (`POST /{project_id}/undo`), so the client has no use
    for it. Returning it would mean doubling the feed's weight and doubling the
    surface a note can leak through.
    """
    query = select(Revision).where(Revision.project_id == context.project.id)
    if task_id is not None:
        # A search inside jsonb content is what the column is declared jsonb for:
        # selecting every revision of the project and sifting them in Python would
        # mean dragging the whole journal over for the sake of one card.
        query = query.where(Revision.op["task_id"].astext == str(task_id))
    if actor_id is not None:
        query = query.where(Revision.actor_user_id == actor_id)
    if types:
        query = query.where(Revision.op["type"].astext.in_(types))
    # The cursor for "show earlier": the next page starts from the oldest entry of
    # the previous one. A number rather than an offset: the journal grows from the
    # head, and an OFFSET would shift with every new revision.
    if before_seq is not None:
        query = query.where(Revision.seq < before_seq)
    # Newest first: the feed is read from the last event. seq rather than
    # created_at — two operations in the same second are indistinguishable by time,
    # but always distinguishable by number.
    revisions = db.scalars(query.order_by(Revision.seq.desc()).limit(limit)).all()

    actors = {
        row.id: row.name
        for row in db.scalars(
            select(User).where(
                User.id.in_({r.actor_user_id for r in revisions if r.actor_user_id})
            )
        ).all()
    }

    # The names follow the visible form of the operation rather than the stored
    # one: what a role may not see must not be named either.
    ops = [
        visible_op(revision.op, context.role, project_granted=context.granted)
        for revision in revisions
    ]
    names = _names_for(db, context.project, ops)

    return [
        {
            **_revision_entry(revision, actors.get(revision.actor_user_id), op),
            "names": {
                entity_id: names[entity_id]
                for entity_id in _referenced_ids(op)
                if entity_id in names
            },
        }
        for revision, op in zip(revisions, ops)
    ]


def _refuse(error: MutationError):
    """A mutation refusal becomes an HTTP refusal. One shape for every writing route.

    The three different statuses are not a matter of taste but of meaning: someone
    else's entity is indistinguishable from a nonexistent one (404), an unspoken
    reason is removed by the same request with the field added (409), and a
    malformed operation will never pass (422).

    A stale sequence number for the revision being undone is also a 409: the
    request is correct, the state underneath it has changed, and the client only
    needs to re-read the project.
    """
    if isinstance(error, NotFoundInProject):
        return HTTPException(status_code=404, detail=error.code)
    if isinstance(error, UndoConflict):
        return HTTPException(status_code=409, detail=error.code)
    if isinstance(error, ReasonRequired):
        return HTTPException(
            status_code=409,
            detail=error.code,
            headers={
                "X-Shift-Deviation-Days": str(error.deviation_days),
                "X-Shift-Threshold-Days": str(error.threshold_days),
            },
        )
    return HTTPException(status_code=422, detail=error.code)


def _project_slug_taken(db: DbSession, org_id: uuid.UUID, slug: str, *, except_id) -> bool:
    return (
        db.scalar(
            select(Project.id).where(
                Project.org_id == org_id, Project.slug == slug, Project.id != except_id
            )
        )
        is not None
    )


@router.get("/{project_id}/slug-check")
def check_project_slug(
    slug: str = Query(min_length=1, max_length=100),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Whether the slug is free inside this organization — and what to offer if it is taken."""
    context.require(Action.PROJECT_ADMIN)

    def taken(candidate: str) -> bool:
        return _project_slug_taken(
            db, context.project.org_id, candidate, except_id=context.project.id
        )

    return slug_check(slug, is_taken=taken)


def _replayed_response(db: DbSession, project_id: uuid.UUID, key: str) -> dict | None:
    """The answer already issued for this key — or None if the key is fresh."""
    return db.scalar(
        select(IdempotencyRecord.response).where(
            IdempotencyRecord.project_id == project_id, IdempotencyRecord.key == key
        )
    )


def _remember_response(
    db: DbSession, project_id: uuid.UUID, key: str, response: dict
) -> dict:
    """Records the answer under the key. On a race between two identical requests
    it returns the winner's answer, having rolled its own — the second — attempt
    back entirely."""
    # A cleanup along the way: keys older than a day are no longer retries.
    db.execute(
        IdempotencyRecord.__table__.delete().where(
            IdempotencyRecord.project_id == project_id,
            IdempotencyRecord.created_at
            < datetime.now(timezone.utc) - timedelta(hours=24),
        )
    )
    try:
        with db.begin_nested():
            db.add(IdempotencyRecord(project_id=project_id, key=key, response=response))
            db.flush()
        return response
    except IntegrityError:
        # The winner has already applied the operation and recorded the answer — our
        # attempt is rolled back entirely so that it does not apply a second time.
        db.rollback()
        return _replayed_response(db, project_id, key) or response


def _publish(
    background: BackgroundTasks, db: DbSession, project_id: uuid.UUID, event: dict
) -> None:
    """The commit and the broadcast of an event into the project's room.

    One shape for every writing route — in the same order as in mutations: the
    commit first (a client that has received a signal re-reads the project, and
    before the commit it would re-read the old state), then the broadcast task.
    Until wave 3 only the mutation route published into the hub: undos, batch
    rollbacks, settings, the plan and neighbouring tabs' comments reached people
    only through a reload.
    """
    db.commit()
    background.add_task(hub.publish, project_id, event)


@router.patch("/{project_id}")
def update_project(
    payload: ProjectSettingsIn,
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Level 3 of the settings: the slug, the target date and the organization's overrides.

    A `null` in the timezone, the working days and the threshold means "inherit"
    rather than "empty", and it is told apart from "the field was not sent" by the
    fact that the latter simply does not reach the set of changes. Without that
    difference there would be nothing to clear an override with: any request without
    the field would erase it.

    Edits do not go through the revision journal: the journal is the history of the
    plan, not the history of the settings. Mixing them would mean filling a task's
    history with entries about someone changing the timezone.
    """
    context.require(Action.PROJECT_ADMIN)
    project = context.project

    # The lock on the project's row — the same one mutations take (apply_op): the
    # calendar and threshold settings move the layout and take part in the threshold
    # check, and an edit to the settings interleaved with applying an operation
    # would give the mutation the threshold from before the edit and the layout from
    # after it.
    lock_project(db, project)

    updates = changes(payload, nullable=NULLABLE_PROJECT_FIELDS)
    if "slug" in updates and _project_slug_taken(
        db, project.org_id, updates["slug"], except_id=project.id
    ):
        raise HTTPException(status_code=409, detail="slug_taken")

    for field, value in updates.items():
        setattr(project, field, value)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="slug_taken")

    if updates:
        # An entry about the edit goes into the application log: the revision
        # journal is the history of the plan, not of the settings, but a trace of
        # "who changed what" must remain.
        logger.info(
            "настройки проекта %s изменил %s: %s",
            project.id,
            context.user.id,
            ", ".join(sorted(updates)),
        )

    response = get_project(context, db)
    if updates:
        # The settings move everyone's layout (the calendar, the threshold) but have
        # no entry in the journal — the event carries a synthetic op: all the client
        # needs from it is a reason to re-read the project.
        _publish(
            background,
            db,
            project.id,
            {"type": "revision", "op": {"type": "settings_changed"}},
        )
    return response


class ScheduleIn(BaseModel):
    """Anchoring the plan to a start date — or a preview of it.

    `working_days` is the mask of the new working week, if one was chosen in the
    same dialog; None means keep the one in force. `shift_tasks=False` means, when
    changing the start of a calendar project again, to leave the task dates as they
    are; for a relative project it makes no difference — its tasks are always laid
    out along the calendar.
    """

    start_date: date = Field(le=MAX_WIRE_DATE)
    working_days: int | None = Field(default=None, ge=MIN_WORKING_DAYS, le=MAX_WORKING_DAYS)
    shift_tasks: bool = True


def _schedule_out(plan) -> dict:
    return {
        "start_date": plan.start_date.isoformat(),
        "end_date": plan.end_date.isoformat() if plan.end_date else None,
    }


@router.post("/{project_id}/schedule/preview")
def preview_schedule(
    payload: ScheduleIn,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """What will become of the plan after anchoring to a date: the project's bounds before the write.

    The anchoring dialog must show the computed finish date before confirmation —
    the promise "we will account for weekends and holidays" is unverifiable without
    that figure.
    """
    context.require(Action.PROJECT_ADMIN)
    try:
        plan = planned_schedule(
            db,
            context.project,
            context.org,
            start=payload.start_date,
            working_days=payload.working_days,
            shift_tasks=payload.shift_tasks,
        )
    except CalendarError as error:
        raise HTTPException(status_code=422, detail=error.code)
    return _schedule_out(plan)


@router.post("/{project_id}/schedule")
def assign_schedule(
    payload: ScheduleIn,
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Assigning (or moving) the project's start date.

    The permission is the same as for the settings (PROJECT_ADMIN): the action
    changes the frame of reference of the whole plan and the working week rather
    than a single task. It does not go through the revision journal for the same
    reason as the settings: this is not an edit of the plan, and it has no undo —
    the way back remains the "Relative plan" view, which is not going anywhere.
    """
    context.require(Action.PROJECT_ADMIN)
    project = context.project

    # The lock on the project's row — the same one the settings and mutations take:
    # laying tasks out along the calendar must not be interleaved with someone
    # moving a task.
    lock_project(db, project)

    try:
        plan = apply_schedule(
            db,
            project,
            context.org,
            start=payload.start_date,
            working_days=payload.working_days,
            shift_tasks=payload.shift_tasks,
        )
    except CalendarError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail=error.code)

    # The trace goes into the application log, as with a settings edit: this action
    # has no entry in the revision journal.
    logger.info(
        "дата старта проекта %s назначена %s: %s",
        project.id,
        context.user.id,
        plan.start_date.isoformat(),
    )

    response = get_project(context, db)
    # A synthetic op — as with settings_changed: all the neighbouring tabs need
    # from the event is a reason to re-read the project.
    _publish(
        background,
        db,
        project.id,
        {"type": "revision", "op": {"type": "schedule_changed"}},
    )
    return response


@router.delete("/{project_id}", status_code=204)
def delete_project(
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Deleting the whole project.

    The permission is its own rather than PROJECT_ADMIN: together with the project
    the cascade carries away the revision journal, that is, every possibility of an
    undo. An irreversible action of that weight, like re-approving a plan, stays
    with the owner.

    The deletion deliberately does not go through the revision journal: the journal
    lives inside the project and dies with it — there would be nowhere for a
    "project deleted" entry to lie.
    """
    context.require(Action.PROJECT_DELETE)
    project = context.project

    # A trace in the application log — as with a settings edit: this action has no
    # entry in the revision journal and cannot have one.
    logger.info("проект %s удалил %s", project.id, context.user.id)

    db.delete(project)
    # Neighbouring tabs learn the project's fate the same way they learn about any
    # change: by re-reading on a signal. Having re-read, they will get a 404 — the
    # honest answer "the project is gone".
    _publish(
        background,
        db,
        project.id,
        {"type": "revision", "op": {"type": "project_deleted"}},
    )
    return None


@router.post("/{project_id}/undo", status_code=201)
def undo_last(
    background: BackgroundTasks,
    reason: str | None = Body(default=None, embed=True),
    expected_seq: int | None = Body(default=None, embed=True),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Undoing the last change.

    An arbitrary revision from the middle of the journal is still not undoable:
    that is a different function ("bring back this one thing"), and its inverse
    operation was built for a state that no longer exists. `expected_seq` is not a
    choice of revision but a condition: what is undone is still the same head of
    the journal, but only while it is the very one the client named to the person.
    Diverged — a 409 refusal, rather than a silent undo of someone else's edit that
    slipped into the gap between showing the button and pressing it.

    A reason is accepted, because an undo goes through the same threshold check as
    any change of dates: a revert taking a task further from the baseline plan than
    the threshold is explained in exactly the same way.
    """
    context.require(Action.PROJECT_WRITE)

    # The choice of the revision to undo lives inside undo_last, under the project
    # lock: choosing it here, in the route, would give two simultaneous presses the
    # same revision, and the second would undo what was already undone (see
    # mutations.undo_last). For the same reason expected_seq is checked there rather
    # than here.
    try:
        applied, revision = undo_last_revision(
            db,
            context.project,
            actor_id=context.user.id,
            reason=reason,
            expected_seq=expected_seq,
        )
    except MutationError as error:
        raise _refuse(error)

    # An undo is a revision just like a mutation, and it travels around the room in
    # the same event shape.
    _publish(
        background,
        db,
        context.project.id,
        {"type": "revision", **_revision_entry(applied, context.user.name, applied.op)},
    )

    return {
        "seq": applied.seq,
        "undone_seq": revision.seq,
        "op": visible_op(applied.op, context.role, project_granted=context.granted),
    }


@router.post("/{project_id}/batches/{batch_id}/undo", status_code=201)
def undo_whole_batch(
    batch_id: uuid.UUID,
    background: BackgroundTasks,
    reason: str | None = Body(default=None, embed=True),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Rolling a whole batch back — with one button, as promised about applying AI.

    A reason is accepted by the same logic as for a single undo: the rollback goes
    through the threshold check, and a batch that moved dates further than the
    threshold would be un-rollbackable without a reason.
    """
    context.require(Action.PROJECT_WRITE)

    try:
        applied = undo_batch(
            db, context.project, batch_id, actor_id=context.user.id, reason=reason
        )
    except MutationError as error:
        raise _refuse(error)

    # One event per batch rather than an event per undo: on a signal the client
    # re-reads the whole project, and ten signals in a row are ten identical
    # re-reads.
    _publish(
        background,
        db,
        context.project.id,
        {
            "type": "revision",
            **_revision_entry(applied[-1], context.user.name, applied[-1].op),
        },
    )

    return {"undone": len(applied), "seq": applied[-1].seq}


@router.post("/{project_id}/plan/approvals", status_code=201)
def approve_plan_route(
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Approving a plan, which is also re-approving it.

    One route rather than two: the action is exactly one — take a snapshot and
    update the baseline values — while the difference is in who is allowed to do it.
    A second route would differ from the first only in the permission check, and
    they would drift apart on the very first edit to the snapshot.
    """
    # "First time or again" is decided under the lock, on the reloaded row: read
    # before it, two simultaneous first approvals both see version 0 — and the one
    # that waited would re-approve with PLAN_APPROVE alone, which an editor holds
    # while re-approval is the owner's.
    lock_project(db, context.project)
    first_time = context.project.plan_version == 0
    context.require(Action.PLAN_APPROVE if first_time else Action.PLAN_REAPPROVE)

    version = approve_plan(db, context.project, actor_id=context.user.id)
    payload = {
        "version": version.version,
        "approved_at": version.approved_at.isoformat(),
        "tasks": len(version.snapshot),
    }
    # Approval changes the baseline values of every task — neighbouring tabs must
    # see the new "promises" without waiting for a reload.
    _publish(
        background,
        db,
        context.project.id,
        {"type": "revision", "op": {"type": "plan_approved"}},
    )
    return payload


@router.post("/{project_id}/plan/approvals/{version}/restore", status_code=201)
def restore_plan_version_route(
    version: int,
    background: BackgroundTasks,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """Returning the promise to a version from the chronicle — re-approving its snapshot.

    The permission is the same as for re-approval: restoring changes the baseline of
    every task in exactly the same way.
    """
    context.require(Action.PLAN_REAPPROVE)
    try:
        restored = restore_plan_version(
            db, context.project, version, actor_id=context.user.id
        )
    except PlanVersionNotFound:
        raise HTTPException(status_code=404, detail="plan_version_not_found")
    payload = {
        "version": restored.version,
        "restored_from": version,
        "approved_at": restored.approved_at.isoformat(),
        "tasks": len(restored.snapshot),
    }
    _publish(
        background,
        db,
        context.project.id,
        {"type": "revision", "op": {"type": "plan_approved"}},
    )
    return payload


@router.get("/{project_id}/plan/approvals")
def list_plan_versions(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    """The chronicle of approvals: what was promised in January, what in March.

    The snapshot is returned in full — there are no internal notes in it, only
    dates, durations and names, and names are seen by anyone entitled to read the
    project.
    """
    versions = plan_versions(db, context.project)
    approvers = {
        row.id: row.name
        for row in db.scalars(
            select(User).where(User.id.in_({v.approved_by for v in versions if v.approved_by}))
        ).all()
    }
    return [
        {
            "version": version.version,
            "approved_at": version.approved_at.isoformat(),
            "approved_by": (
                {"id": str(version.approved_by), "name": approvers[version.approved_by]}
                if version.approved_by in approvers
                else None
            ),
            # Without `current_start`: that is bookkeeping for restoring a version
            # across a change of the start date, not part of the promise as made.
            "snapshot": {
                task_id: {k: v for k, v in promised.items() if k != "current_start"}
                for task_id, promised in version.snapshot.items()
            },
        }
        for version in versions
    ]


@router.post("/{project_id}/mutations", status_code=201)
def apply_mutation(
    background: BackgroundTasks,
    op: PublicOp = Body(..., embed=True),
    reason: str | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    context.require(Action.PROJECT_WRITE)

    # The idempotency key: a repeated request (a network retry, a double click)
    # gets the first answer rather than a second application — "move by a day" twice
    # is a move by two days.
    if idempotency_key:
        replayed = _replayed_response(db, context.project.id, idempotency_key)
        if replayed is not None:
            return replayed

    try:
        revision = apply_op(
            db, context.project, to_internal(op), actor_id=context.user.id, reason=reason
        )
    except MutationError as error:
        # An entity from another project is not a request-format error, while an
        # unspoken reason is removed by the same request with the field added: the
        # code dispatch is shared by every writing route.
        raise _refuse(error)

    # The note is put inside the event as is: who sees it is decided by each socket
    # separately — an editor and a client sit in one room.
    event = {"type": "revision", **_revision_entry(revision, context.user.name, revision.op)}

    seen = {"role": context.role, "project_granted": context.granted}
    payload = {
        "seq": revision.seq,
        "op": visible_op(revision.op, **seen),
        "inverse": visible_op(revision.inverse, **seen),
    }

    if idempotency_key:
        remembered = _remember_response(db, context.project.id, idempotency_key, payload)
        if remembered is not payload:
            # A race between two identical requests: our attempt is rolled back, the
            # first one won — its answer is what goes out, and there is nothing to
            # broadcast.
            return remembered

    # The commit is explicit and comes before the broadcast is queued, and that is
    # not over-caution. Only what is already in the database can be broadcast: on a
    # signal the client re-requests the whole project — and, arriving before the
    # commit, would not see the change, while there will be no second signal.
    # Relying on the commit from get_db here is not an option: background tasks run
    # before a dependency with a yield is closed. A repeated commit on the way out is
    # harmless — there is nothing left to commit.
    db.commit()
    background.add_task(hub.publish, context.project.id, event)

    return payload


@router.get("/{project_id}/comments")
def list_project_comments(
    task_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    before: uuid.UUID | None = None,
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    """The project's feed — the very same one visible on the public page.

    A member gets guest remarks along with the rest: the point of a public link is
    that the conversation with the client lives in the project rather than in email.

    The tail of the conversation is returned; "show earlier" uses the before cursor.
    """
    try:
        rows = list_comments(
            db, context.project, task_id=task_id, limit=limit, before=before
        )
    except CommentRejected as error:
        raise HTTPException(status_code=404, detail=error.code)
    return comments_out(db, rows)


@router.get("/{project_id}/comments/counts")
def project_comment_counts(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    """How many remarks each task has — as the number on a chart row.

    A separate route rather than a field in the project's state: a comment does not
    change the state and gives birth to no revision, so the state is not re-requested
    after it, and a counter sewn into it would show yesterday's number until the next
    edit of the plan. Here it is refreshed together with the remark feed itself — by
    the same socket event.
    """
    return {str(task_id): count for task_id, count in comment_counts(db, context.project).items()}


@router.post("/{project_id}/comments", status_code=201)
def create_project_comment(
    payload: CommentIn,
    background: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
    context: ProjectContext = Depends(project_context),
    db: DbSession = Depends(get_db),
):
    context.require(Action.COMMENT)

    # The same mechanism as for mutations: a network retry must not give birth to a
    # duplicate remark.
    if idempotency_key:
        replayed = _replayed_response(db, context.project.id, idempotency_key)
        if replayed is not None:
            return replayed
    try:
        comment = add_comment(
            db,
            context.project,
            body=payload.body,
            task_id=payload.task_id,
            author=context.user,
            internal=payload.internal,
        )
    except CommentRejected as error:
        # task_not_found is not a format error: the same 404 as for mutations
        # referring to someone else's task.
        status = 404 if error.code == "task_not_found" else 422
        raise HTTPException(status_code=status, detail=error.code)
    response = comments_out(db, [comment])[0]
    if idempotency_key:
        remembered = _remember_response(db, context.project.id, idempotency_key, response)
        if remembered is not response:
            return remembered
    # The event carries only the fact that "there is something new in the feed",
    # not the text: an internal remark must not be handed out into a room where a
    # client also sits, and there is nothing here to decide it per subscriber with,
    # as with revisions — the client will fetch the remark's body over HTTP, where
    # the filter already exists.
    _publish(background, db, context.project.id, {"type": "comment"})
    return response
