import uuid
from datetime import date, datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.models import (
    CRITICALITY_LEVELS,
    RISK_FLAGS,
    TASK_STATUSES,
    Category,
    Comment,
    Criticality,
    RiskFlag,
    Dependency,
    Membership,
    Organization,
    Project,
    Revision,
    Task,
    TaskAssignee,
    TaskStatus,
    User,
)
from app.cascade import apply_dates, push_successors
from app.plans import deviation_days
from app.projects import lock_project
from app.settings_resolution import project_calendar, resolve_shift_threshold


class MutationError(Exception):
    """A refusal to apply an operation.

    It carries a stable machine code for the answer and human text for the log. The
    text does not reach the response body: the interface's default language is
    Azerbaijani, the server deliberately keeps no message dictionaries (the client
    composes them), so prose in `detail` is untranslatable.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NotFoundInProject(MutationError):
    """The named entity does not exist or belongs to another project.

    A separate class from InvalidOperation: reaching for a task of another
    organization is not a request-format error, and the route answers it with a 404
    rather than a 422.
    """


class InvalidOperation(MutationError):
    """The operation is composed in a way that makes it impossible to apply."""


class UndoConflict(MutationError):
    """The revision asked to be undone is not the one at the top of the journal.

    The client names the number of the revision whose undo it is offering a person:
    the "Undo" button in a toast appears after a particular move and promises to
    bring back exactly that one. Seconds pass between showing the button and
    pressing it, and in those seconds a colleague on the project applies a change of
    their own — the head of the journal moves on, and a numberless undo would remove
    someone else's edit silently.

    A separate class, because this is not an error of composing the request: it is
    correct, and repeated after the state has been re-read it will pass.
    """


class ReasonRequired(MutationError):
    """The edit takes the task further from the baseline plan than the threshold, and no reason was named.

    A separate class, because this is not an error of composing the operation: the
    operation is correct, and the moment the person explains the shift the very same
    operation will pass. It carries the deviation and threshold figures with it: the
    interface computes them itself too, but the last word on them belongs to the
    server, and a divergence must be visible rather than silently resolved in the
    client's favour.
    """

    def __init__(self, deviation_days: int, threshold_days: int):
        super().__init__(
            "reason_required",
            f"отклонение {deviation_days} дн. при пороге {threshold_days} дн. требует причины",
        )
        self.deviation_days = deviation_days
        self.threshold_days = threshold_days


# --- the internal representation ---------------------------------------------
#
# These models describe an operation as the domain sees it and as it lies in the
# revision journal. Creating operations have restore fields — category_id,
# task_id, position — because undoing a deletion must bring a row back under its
# previous identifier and to its previous place. Such fields must not be accepted
# over the wire: they hand the assignment of identifiers to the client and allow a
# row to be placed at an arbitrary index. The public contract is below, as
# separate models; to_internal() translates one into the other.


class CreateCategory(BaseModel):
    type: Literal["create_category"] = "create_category"
    name: str
    color: str
    category_id: uuid.UUID | None = None
    position: int | None = None
    # The tasks that lay in the category at the moment it was deleted. A restore
    # field, mirroring the cascade: delete_category carries the category's contents
    # away with it, and the inverse operation must bring back not an empty heading
    # row but the whole stage — with the dependencies, assignments and conversation
    # of every task (see CreateTask below). Like the other restore fields, it is not
    # accepted over the wire: it is absent from the public model.
    tasks: list["CreateTask"] = Field(default_factory=list)


class CreateTask(BaseModel):
    type: Literal["create_task"] = "create_task"
    category_id: uuid.UUID
    name: str
    start_date: date
    duration_days: int
    description: str = ""
    internal_note: str = ""
    criticality: str = "normal"
    progress_pct: int = 0
    status: str = "planned"
    milestone: bool = False
    baseline_start: date | None = None
    baseline_duration: int | None = None
    task_id: uuid.UUID | None = None
    position: int | None = None
    # The restore fields of a deleted task. A deletion carries away the
    # dependencies, assignments and comments by cascade; without their snapshot in
    # inverse, an undo would bring back a bare task row while the conversation with
    # the client and the arrows on the chart would be lost for good. Like the other
    # restore fields, they are not accepted over the wire — they are absent from the
    # public model.
    assignees: list[uuid.UUID] = Field(default_factory=list)
    dependencies: list[dict] = Field(default_factory=list)
    comments: list[dict] = Field(default_factory=list)


# CreateCategory refers to CreateTask before the latter is declared: a category
# with its tasks is one operation, and the pair cannot be broken by swapping the
# classes around (CreateTask would then refer to CreateCategory). The reference is
# resolved here, right after the second of them is declared.
CreateCategory.model_rebuild()


# --- the automatic-shift restore field ----------------------------------------
#
# With automatic shifting on, operations that change dates move not only their own
# task but its successors as well (see app/cascade.py). An undo must bring all of
# them back, not one, and bring them back exactly — so the forward operation writes
# a map of what was shifted into the journal, and the inverse gets it back through
# this field and lays the dates out verbatim, computing nothing.
#
# The same technique as SetProgress.status: the coupling runs in the forward
# operation while the inverse takes the finished result. Recomputing the automatic
# shift on an undo is impossible in principle — "move to the earliest possible" is
# irreversible: a forward move remembers where it came from, a computation of it
# does not.
#
# The field is not accepted over the wire: a client that sent a date map would lay
# the plan out around every check.

class MoveTask(BaseModel):
    type: Literal["move_task"] = "move_task"
    task_id: uuid.UUID
    start_date: date
    cascade: dict[uuid.UUID, date] | None = None


class SetDuration(BaseModel):
    type: Literal["set_duration"] = "set_duration"
    task_id: uuid.UUID
    duration_days: int
    cascade: dict[uuid.UUID, date] | None = None


class ResizeTask(BaseModel):
    """The start and the duration at once — the left edge of a bar.

    A separate operation rather than a move_task + set_duration pair, for the same
    reason set_task_fields saves three fields at once: the left edge is dragged with
    one movement, the task's end stays where it is, and in the history this must
    read as one change. A pair of operations would give two entries, two undos and
    an intermediate state a person never created — a task already moved but not yet
    shortened.

    The right edge does not require this operation: there only the duration
    changes, and set_duration already exists for it.
    """

    type: Literal["resize_task"] = "resize_task"
    task_id: uuid.UUID
    start_date: date
    duration_days: int
    cascade: dict[uuid.UUID, date] | None = None


class SetMilestone(BaseModel):
    """A task becomes a milestone or stops being one.

    A separate operation rather than a field of set_task_fields: turning a segment
    into a point collapses the duration, that is, changes dates — and in the history
    this must read as a change of dates rather than as an edit of text.
    """

    type: Literal["set_milestone"] = "set_milestone"
    task_id: uuid.UUID
    milestone: bool
    # A restore field — mirroring set_progress.status: a milestone collapses the
    # duration into one day, and an undo must bring back the one that stood before
    # the operation rather than an invented single day. It is not accepted over the
    # wire: a duration from the wire is assigned by set_duration.
    duration_days: int | None = None


class MoveCategory(BaseModel):
    """A shift of a whole category by N calendar days.

    One operation rather than a batch of move_tasks numbering as many as there are
    tasks, for the same reason set_task_fields saves three fields at once: the
    person made one movement — the category's summary bar was dragged to the right —
    and the history must show one entry while an undo brings everything back with
    one press.

    Days rather than a target date: a category has no bounds of its own (the summary
    bar is drawn from the extreme dates of its tasks), and "move the category to 3
    March" would mean inventing a beginning for it that does not exist in the model.
    """

    type: Literal["move_category"] = "move_category"
    category_id: uuid.UUID
    days: int
    cascade: dict[uuid.UUID, date] | None = None


class DeleteTask(BaseModel):
    type: Literal["delete_task"] = "delete_task"
    task_id: uuid.UUID


class DeleteCategory(BaseModel):
    type: Literal["delete_category"] = "delete_category"
    category_id: uuid.UUID


class SetTaskFields(BaseModel):
    """Three text fields at once.

    The task card saves them with one action; splitting that into three revisions
    would mean littering the history with three entries where a person made one
    change.
    """

    type: Literal["set_task_fields"] = "set_task_fields"
    task_id: uuid.UUID
    name: str
    description: str
    internal_note: str


class SetCriticality(BaseModel):
    type: Literal["set_criticality"] = "set_criticality"
    task_id: uuid.UUID
    criticality: str


class SetRisk(BaseModel):
    """The risk flag and the reason in one operation.

    The card changes them with one gesture ("there is a risk — waiting for access"),
    and two entries in the history about it is the same argument as with
    SetTaskFields. Both bounds go into the journal as a dict (see _MAPPED_BOUNDS).
    """

    type: Literal["set_risk"] = "set_risk"
    task_id: uuid.UUID
    risk: str
    note: str = ""


# --- the coupling of progress and status --------------------------------------
#
# The rule has exactly three clauses and no more:
#   set_progress to 100         -> the status becomes 'done';
#   set_progress below 100 from
#   the status 'done'           -> the status becomes 'in_progress';
#   set_status to 'done'        -> the progress becomes 100.
# Leaving 'done' for another status does not touch the progress, even when it
# stands at 100: an invented "almost ready" (99?) would be a value the person never
# entered, and the system has no honest candidate.


class SetProgress(BaseModel):
    type: Literal["set_progress"] = "set_progress"
    task_id: uuid.UUID
    progress_pct: int
    # A restore field: an undo must bring back the status that stood before the
    # operation rather than the one the coupling would derive (from 'blocked' the
    # coupling would never guess it). It is not accepted over the wire — a status
    # from the wire is governed by set_status.
    status: str | None = None


class SetStatus(BaseModel):
    type: Literal["set_status"] = "set_status"
    task_id: uuid.UUID
    status: str
    # A restore field — mirroring set_progress.status: undoing a move into 'done'
    # must bring the previous progress back rather than leave 100.
    progress_pct: int | None = None


class RenameCategory(BaseModel):
    type: Literal["rename_category"] = "rename_category"
    category_id: uuid.UUID
    name: str


class SetCategoryColor(BaseModel):
    type: Literal["set_category_color"] = "set_category_color"
    category_id: uuid.UUID
    color: str


class ReorderTask(BaseModel):
    type: Literal["reorder_task"] = "reorder_task"
    task_id: uuid.UUID
    category_id: uuid.UUID
    position: int


class ReorderCategory(BaseModel):
    """A category takes another place in the list of stages.

    A separate operation from reorder_task rather than a shared "move a row": a
    task's place is described by a pair (category, number) — and it is moved from
    one stage into another — while a category has no parent at all and only a
    number. A shared operation would carry an empty category_id on half of its calls,
    that is, would describe something other than what happened.
    """

    type: Literal["reorder_category"] = "reorder_category"
    category_id: uuid.UUID
    position: int


class AddDependency(BaseModel):
    type: Literal["add_dependency"] = "add_dependency"
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID
    # A new dependency moves the successor exactly as moving the predecessor does:
    # it is what "this work waits on that one" means.
    cascade: dict[uuid.UUID, date] | None = None


class RemoveDependency(BaseModel):
    type: Literal["remove_dependency"] = "remove_dependency"
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID
    # The inverse of add_dependency: a removed dependency returns the successors to
    # where its appearance moved them from. A removed dependency by itself moves
    # nothing — the automatic shift does not pull backwards (see app/cascade.py).
    cascade: dict[uuid.UUID, date] | None = None


class AssignUser(BaseModel):
    type: Literal["assign_user"] = "assign_user"
    task_id: uuid.UUID
    user_id: uuid.UUID


class UnassignUser(BaseModel):
    type: Literal["unassign_user"] = "unassign_user"
    task_id: uuid.UUID
    user_id: uuid.UUID


class ApplyPositions(BaseModel):
    """An internal operation: lay positions out from a ready map.

    It exists only as the inverse of reorder_task. It is not accepted over the wire
    — it is absent from the public registry: otherwise a client would send an
    arbitrary map and lay the rows out around every check of the ordering.
    """

    type: Literal["apply_positions"] = "apply_positions"
    positions: dict[uuid.UUID, int]
    categories: dict[uuid.UUID, uuid.UUID]


class ApplyCategoryPositions(BaseModel):
    """An internal operation: lay the category order out from a ready map.

    It exists only as the inverse of reorder_category — for the same reason
    apply_positions exists, and with the same ban on the wire: a map sent by a
    client would lay the stages out around every check of the ordering.
    """

    type: Literal["apply_category_positions"] = "apply_category_positions"
    positions: dict[uuid.UUID, int]


Op = Annotated[
    CreateCategory
    | CreateTask
    | MoveTask
    | MoveCategory
    | ResizeTask
    | SetDuration
    | SetMilestone
    | DeleteTask
    | DeleteCategory
    | SetTaskFields
    | SetCriticality
    | SetRisk
    | SetProgress
    | SetStatus
    | RenameCategory
    | SetCategoryColor
    | ReorderTask
    | ReorderCategory
    | ApplyPositions
    | ApplyCategoryPositions
    | AddDependency
    | RemoveDependency
    | AssignUser
    | UnassignUser,
    Field(discriminator="type"),
]

_MODELS = {
    "create_category": CreateCategory,
    "create_task": CreateTask,
    "move_task": MoveTask,
    "move_category": MoveCategory,
    "resize_task": ResizeTask,
    "set_duration": SetDuration,
    "set_milestone": SetMilestone,
    "delete_task": DeleteTask,
    "delete_category": DeleteCategory,
    "set_task_fields": SetTaskFields,
    "set_criticality": SetCriticality,
    "set_risk": SetRisk,
    "set_progress": SetProgress,
    "set_status": SetStatus,
    "rename_category": RenameCategory,
    "set_category_color": SetCategoryColor,
    "reorder_task": ReorderTask,
    "reorder_category": ReorderCategory,
    "apply_positions": ApplyPositions,
    "apply_category_positions": ApplyCategoryPositions,
    "add_dependency": AddDependency,
    "remove_dependency": RemoveDependency,
    "assign_user": AssignUser,
    "unassign_user": UnassignUser,
}


# --- the contract over the wire ----------------------------------------------
#
# The length bounds repeat the width of the columns: without them a string longer
# than the varchar reaches the database and comes back as a 500 on a truncation
# error rather than as an honest refusal to the client. extra="forbid" was chosen
# deliberately over silent ignoring: a client that sent a task_id expecting the
# server to honour it must learn about that right away rather than wonder later why
# the row ended up somewhere else.
#
# A task's position is the exception, and a deliberate one: a row's place in a list
# is chosen by a person, not by the server. They choose it by dragging anyway
# (reorder_task has accepted a position over the wire from the start), and the
# "plus" on a row boundary creates a task right where it was pointed. Without this
# field, inserting in the middle would be a pair of operations — create at the end
# and move — that is, two entries in the history and two presses of "Undo" for one
# action by a person. The assignment of identifiers stays with the server: task_id
# is still not accepted over the wire.

#: The far edge of the dates the wire accepts. Not date.max: the calendar searches
#: for working days years ahead of the start (moving the end past holidays), and a
#: date at the very edge would tip the date arithmetic beyond what is supported.
#: The year 2200 is knowingly further than any real plan and knowingly nearer than
#: the edge.
MAX_WIRE_DATE = date(2200, 12, 31)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    @field_validator("description", "internal_note", mode="after", check_fields=False)
    @classmethod
    def _within_max_text_len(cls, value: str) -> str:
        # The ceiling is read at call time rather than baked into a Field at module
        # import: MAX_TEXT_LEN is an installation setting, and the value set in .env
        # must be the one in force rather than whichever happened to be there at
        # the first import (in tests that also rendered monkeypatch powerless).
        limit = get_settings().max_text_len
        if len(value) > limit:
            raise ValueError(f"длиннее потолка в {limit} символов")
        return value


class PublicCreateCategory(_Wire):
    type: Literal["create_category"] = "create_category"
    name: str = Field(min_length=1, max_length=200)
    color: str = Field(min_length=1, max_length=9)


class PublicCreateTask(_Wire):
    type: Literal["create_task"] = "create_task"
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=300)
    start_date: date = Field(le=MAX_WIRE_DATE)
    duration_days: int = Field(ge=1)
    #: The row's place in the category's list. Not sent — the task goes to the end,
    #: as before; named — to that number, and the rows that held it move down (see
    #: _create_task).
    position: int | None = Field(default=None, ge=0)
    description: str = ""
    internal_note: str = ""
    criticality: Criticality = Criticality.NORMAL
    progress_pct: int = Field(default=0, ge=0, le=100)
    status: TaskStatus = TaskStatus.PLANNED
    milestone: bool = False
    baseline_start: date | None = Field(default=None, le=MAX_WIRE_DATE)
    baseline_duration: int | None = Field(default=None, ge=1)


class PublicMoveTask(_Wire):
    type: Literal["move_task"] = "move_task"
    task_id: uuid.UUID
    start_date: date = Field(le=MAX_WIRE_DATE)


#: The far edge of a category shift. Ten years in each direction is knowingly more
#: than any real move and knowingly less than what could tip the date arithmetic.
#: The real boundary is held by MAX_WIRE_DATE anyway: a shift taking a task past it
#: is rejected on application.
MAX_WIRE_SHIFT_DAYS = 3650


class PublicMoveCategory(_Wire):
    type: Literal["move_category"] = "move_category"
    category_id: uuid.UUID
    days: int = Field(ge=-MAX_WIRE_SHIFT_DAYS, le=MAX_WIRE_SHIFT_DAYS)


class PublicResizeTask(_Wire):
    type: Literal["resize_task"] = "resize_task"
    task_id: uuid.UUID
    start_date: date = Field(le=MAX_WIRE_DATE)
    duration_days: int = Field(ge=1)


class PublicSetDuration(_Wire):
    type: Literal["set_duration"] = "set_duration"
    task_id: uuid.UUID
    duration_days: int = Field(ge=1)


class PublicSetMilestone(_Wire):
    type: Literal["set_milestone"] = "set_milestone"
    task_id: uuid.UUID
    milestone: bool


class PublicDeleteTask(_Wire):
    type: Literal["delete_task"] = "delete_task"
    task_id: uuid.UUID


class PublicDeleteCategory(_Wire):
    type: Literal["delete_category"] = "delete_category"
    category_id: uuid.UUID


class PublicSetTaskFields(_Wire):
    type: Literal["set_task_fields"] = "set_task_fields"
    task_id: uuid.UUID
    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    internal_note: str = ""


class PublicSetCriticality(_Wire):
    type: Literal["set_criticality"] = "set_criticality"
    task_id: uuid.UUID
    criticality: Criticality


class PublicSetRisk(_Wire):
    type: Literal["set_risk"] = "set_risk"
    task_id: uuid.UUID
    risk: RiskFlag
    note: str = Field(default="", max_length=300)


class PublicSetProgress(_Wire):
    type: Literal["set_progress"] = "set_progress"
    task_id: uuid.UUID
    progress_pct: int = Field(ge=0, le=100)


class PublicSetStatus(_Wire):
    type: Literal["set_status"] = "set_status"
    task_id: uuid.UUID
    status: TaskStatus


class PublicRenameCategory(_Wire):
    type: Literal["rename_category"] = "rename_category"
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class PublicSetCategoryColor(_Wire):
    type: Literal["set_category_color"] = "set_category_color"
    category_id: uuid.UUID
    color: str = Field(min_length=1, max_length=9)


class PublicReorderTask(_Wire):
    type: Literal["reorder_task"] = "reorder_task"
    task_id: uuid.UUID
    category_id: uuid.UUID
    position: int = Field(ge=0)


class PublicReorderCategory(_Wire):
    type: Literal["reorder_category"] = "reorder_category"
    category_id: uuid.UUID
    position: int = Field(ge=0)


class PublicAddDependency(_Wire):
    type: Literal["add_dependency"] = "add_dependency"
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID


class PublicRemoveDependency(_Wire):
    type: Literal["remove_dependency"] = "remove_dependency"
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID


class PublicAssignUser(_Wire):
    type: Literal["assign_user"] = "assign_user"
    task_id: uuid.UUID
    user_id: uuid.UUID


class PublicUnassignUser(_Wire):
    type: Literal["unassign_user"] = "unassign_user"
    task_id: uuid.UUID
    user_id: uuid.UUID


# ApplyPositions and ApplyCategoryPositions have no public mirror and must not have
# one: both are internal.


PublicOp = Annotated[
    PublicCreateCategory
    | PublicCreateTask
    | PublicMoveTask
    | PublicMoveCategory
    | PublicResizeTask
    | PublicSetDuration
    | PublicSetMilestone
    | PublicDeleteTask
    | PublicDeleteCategory
    | PublicSetTaskFields
    | PublicSetCriticality
    | PublicSetRisk
    | PublicSetProgress
    | PublicSetStatus
    | PublicRenameCategory
    | PublicSetCategoryColor
    | PublicReorderTask
    | PublicReorderCategory
    | PublicAddDependency
    | PublicRemoveDependency
    | PublicAssignUser
    | PublicUnassignUser,
    Field(discriminator="type"),
]


def to_internal(op) -> Op:
    """An operation from the wire -> the internal representation.

    The restore fields are not carried over simply because they are absent from the
    public model: the assignment of identifiers and positions stays with the server.
    """
    return _MODELS[op.type].model_validate(op.model_dump())


def _next_seq(db: DbSession, project: Project) -> int:
    current = db.scalar(
        select(func.coalesce(func.max(Revision.seq), 0)).where(Revision.project_id == project.id)
    )
    return current + 1


def _require_task(db: DbSession, project: Project, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.project_id != project.id:
        raise NotFoundInProject("task_not_found", "задача не найдена в этом проекте")
    return task


def _require_category(db: DbSession, project: Project, category_id: uuid.UUID) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.project_id != project.id:
        raise NotFoundInProject("category_not_found", "категория не найдена в этом проекте")
    return category


def _make_room(db: DbSession, category_id: uuid.UUID, position: int) -> None:
    """Frees the named slot in a category, moving the rows that held it down.

    Two paths need this at once. The first is inserting a row in the middle: the
    "plus" on a row boundary creates a task where it was pointed, and the neighbour
    that stood at that number, along with everyone below, slides down by one. The
    second is undoing a deletion: a task comes back to its previous number, and that
    number may have gone to another row since (a reorder renumbers the list
    consecutively and closes the hole left by the deleted one). Without this shift
    an undo would fail on the uniqueness of (category_id, position) — that is, with
    a 500 instead of the task coming back.

    A free slot is not touched at all: moving neighbours for the sake of a number
    that is nobody's anyway would mean changing the order where nobody changed it.

    A shift by one preserves the order and violates no uniqueness: the mapping is
    monotone, and the intermediate states are held by the deferred check of the
    constraint (DEFERRABLE INITIALLY DEFERRED, see models.Task).
    """
    occupied = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.category_id == category_id, Task.position == position)
    )
    if not occupied:
        return
    # The rows are loaded as objects rather than shifted with one UPDATE: those same
    # tasks may already be in the session, and a bulk update around the ORM would
    # leave them with their previous numbers until the end of the request.
    for row in db.scalars(
        select(Task).where(Task.category_id == category_id, Task.position >= position)
    ).all():
        row.position += 1
    db.flush()


def _find_dependency(
    db: DbSession, project: Project, from_task_id: uuid.UUID, to_task_id: uuid.UUID
) -> Dependency | None:
    """A dependency by both ends, restricted to this project.

    The project_id in the condition is redundant as long as both tasks have already
    been checked by _require_task — but it is also what makes the query correct on
    its own, without relying on the caller not having forgotten the check.
    """
    return db.scalar(
        select(Dependency).where(
            Dependency.project_id == project.id,
            Dependency.from_task_id == from_task_id,
            Dependency.to_task_id == to_task_id,
        )
    )


def _find_assignment(
    db: DbSession, task_id: uuid.UUID, user_id: uuid.UUID
) -> TaskAssignee | None:
    return db.scalar(
        select(TaskAssignee).where(
            TaskAssignee.task_id == task_id, TaskAssignee.user_id == user_id
        )
    )


# The fields the task card saves with one action.
_TASK_FIELDS = ("name", "description", "internal_note")


def _snapshot_task_links(db: DbSession, task: Task) -> dict:
    """A task's dependencies, assignments and comments — in the form for the journal.

    Taken before a deletion: the cascade will carry these rows away together with
    the task, and there is no other source for restoring them — the revision journal
    stores operations on tasks, not on their environment.
    """
    assignees = [
        str(row.user_id)
        for row in db.scalars(
            select(TaskAssignee).where(TaskAssignee.task_id == task.id).order_by(TaskAssignee.id)
        )
    ]
    dependencies = [
        {"from_task_id": str(row.from_task_id), "to_task_id": str(row.to_task_id)}
        for row in db.scalars(
            select(Dependency)
            .where((Dependency.from_task_id == task.id) | (Dependency.to_task_id == task.id))
            .order_by(Dependency.id)
        )
    ]
    comments = [
        {
            "id": str(row.id),
            "author_user_id": str(row.author_user_id) if row.author_user_id else None,
            "guest_name": row.guest_name,
            "body": row.body,
            # Without the flag a restored remark comes back public: the column's
            # default is "visible to the guest", and the team's aside would
            # surface on the shared page after a delete and an undo.
            "internal": row.internal,
            "created_at": row.created_at.isoformat(),
        }
        for row in db.scalars(
            select(Comment).where(Comment.task_id == task.id).order_by(Comment.created_at)
        )
    ]
    snapshot = {}
    if assignees:
        snapshot["assignees"] = assignees
    if dependencies:
        snapshot["dependencies"] = dependencies
    if comments:
        snapshot["comments"] = comments
    return snapshot


def _stamp_status_change(task: Task, previous_status: str) -> None:
    """The status timestamps: done_at and in_progress_since.

    Set on entering a status, cleared on leaving it — the column stores the last
    boundary rather than a history (the chronicle of transitions stays with the
    revision journal). Here rather than in the set_status/set_progress branches
    separately: the status is changed by two operations plus the creation of a task,
    and three copies of this rule would diverge on the first edit. An undo takes the
    same path and sets the current time rather than the previous one — "when the
    task became done again" is precisely the answer to the question these
    timestamps serve (see app.scorecard).
    """
    if task.status == previous_status:
        return
    now = datetime.now(timezone.utc)
    if task.status == TaskStatus.DONE:
        task.done_at = now
    elif previous_status == TaskStatus.DONE:
        task.done_at = None
    if task.status == TaskStatus.IN_PROGRESS:
        task.in_progress_since = now
    elif previous_status == TaskStatus.IN_PROGRESS:
        task.in_progress_since = None


def _add_task(db: DbSession, project: Project, op: CreateTask) -> Task:
    """A task row — with every check and the choice of a place in the list.

    Separate from the `create_task` branch in `_apply`, because two things create a
    task: the operation itself and restoring a category together with its contents
    (see CreateCategory.tasks). A second set of these checks would diverge from the
    first on the very first edit — and would diverge silently, since the only way to
    err here is in the database's favour: a row a CHECK will reject comes back as a
    500 instead of an honest refusal code.

    A task's environment — dependencies, assignments, the conversation — is not
    restored here: for a category that is done in a separate pass, once all of its
    rows have been created (see _restore_task_links).
    """
    if op.duration_days < 1:
        raise InvalidOperation("duration_too_short", "длительность должна быть не меньше одного дня")
    # The same check as in set_criticality/set_progress: the internal model arrives
    # not only from the wire (where the public one holds the bounds) but also from
    # the journal — and must not be able to store a row the CHECK in the database
    # would reject with a 500 anyway.
    if op.criticality not in CRITICALITY_LEVELS:
        raise InvalidOperation("unknown_criticality", f"неизвестный уровень: {op.criticality}")
    if not 0 <= op.progress_pct <= 100:
        raise InvalidOperation(
            "progress_out_of_range", f"процент вне 0..100: {op.progress_pct}"
        )
    if op.status not in TASK_STATUSES:
        raise InvalidOperation("unknown_status", f"неизвестный статус: {op.status}")
    if op.milestone and op.duration_days != 1:
        # The same constraint the database holds: a milestone is a point on the
        # scale. The check is here rather than only in the CHECK so that the refusal
        # is an honest operation code rather than a 500 on a constraint violation.
        raise InvalidOperation(
            "milestone_has_duration", "у вехи длительность ровно один день"
        )
    # A foreign key guarantees only that the category exists somewhere — not that it
    # belongs to this project. Without an explicit check a task can quietly end up
    # under a category of another project.
    _require_category(db, project, op.category_id)
    # The ceiling from the settings (MAX_TASKS_PER_PROJECT): checked here rather
    # than in the route, because this is a rule of the domain, not of the request's shape.
    limit = get_settings().max_tasks_per_project
    existing = db.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == project.id)
    )
    if existing >= limit:
        raise InvalidOperation(
            "task_limit_reached", f"в проекте уже {existing} задач при потолке {limit}"
        )
    # The same principle as for categories: the greatest taken position + 1 rather
    # than COUNT(*) — otherwise the number freed by a deletion goes to the very next
    # task created, a second time. It is computed within the category: the position
    # is the row's number in its list — the number used to be taken across the whole
    # project, and categories ended up with holey numberings that depended on the
    # order of creation and that the unique constraint (category_id, position) cannot
    # hold.
    if op.position is None:
        position = db.scalar(
            select(func.coalesce(func.max(Task.position), -1) + 1).where(
                Task.category_id == op.category_id
            )
        )
    else:
        position = op.position
        _make_room(db, op.category_id, position)
    task = Task(
        id=op.task_id or uuid.uuid4(),
        project_id=project.id,
        category_id=op.category_id,
        name=op.name,
        description=op.description,
        internal_note=op.internal_note,
        start_date=op.start_date,
        duration_days=op.duration_days,
        criticality=op.criticality,
        progress_pct=op.progress_pct,
        status=op.status,
        milestone=op.milestone,
        position=position,
        baseline_start=op.baseline_start,
        baseline_duration=op.baseline_duration,
    )
    # A task born straight into done or in_progress (a carry-across from a budget, a
    # restore from the journal) gets its status-entry timestamp here: it will have
    # no other transition.
    _stamp_status_change(task, TaskStatus.PLANNED.value)
    db.add(task)
    db.flush()
    return task


def _task_payload(task: Task) -> dict:
    """A task in the form it lands in the journal — as create_task's fields.

    One shape for three places: the creation entry, the snapshot for undoing a
    deletion, and the snapshot of a task inside a deleted category. Three lists of
    the same fields would diverge on the very first one added — and an undo would
    bring the task back without it.
    """
    return {
        "type": "create_task",
        "task_id": str(task.id),
        "category_id": str(task.category_id),
        "name": task.name,
        "start_date": task.start_date.isoformat(),
        "duration_days": task.duration_days,
        "description": task.description,
        "internal_note": task.internal_note,
        "criticality": task.criticality,
        "progress_pct": task.progress_pct,
        "status": task.status,
        "milestone": task.milestone,
        "position": task.position,
        "baseline_start": task.baseline_start.isoformat() if task.baseline_start else None,
        "baseline_duration": task.baseline_duration,
    }


def _task_snapshot(db: DbSession, task: Task) -> dict:
    """The whole task — with its dependencies, assignments and conversation.

    Taken before a deletion: the cascade carries the environment away along with the
    row, and an undo without it would bring back a bare name.
    """
    return _task_payload(task) | _snapshot_task_links(db, task)


def _restore_task_links(db: DbSession, project: Project, task: Task, op: CreateTask) -> None:
    """Returns to a restored task what the deletion cascade carried away.

    The world may have moved on since the deletion, so every row is restored where
    possible rather than where required: a dependency on a second task that no
    longer exists, an assignment to a user whose account has been deleted — are
    silently skipped. Skipping is exactly what the cascade would have done with such
    a row itself; refusing the whole undo because of it would leave the person with
    no task at all.
    """
    if op.assignees:
        existing_users = set(db.scalars(select(User.id).where(User.id.in_(op.assignees))))
        for user_id in op.assignees:
            if user_id in existing_users and _find_assignment(db, task.id, user_id) is None:
                db.add(TaskAssignee(task_id=task.id, user_id=user_id))

    for link in op.dependencies:
        from_id = uuid.UUID(link["from_task_id"])
        to_id = uuid.UUID(link["to_task_id"])
        other_id = to_id if from_id == task.id else from_id
        other = db.get(Task, other_id)
        if other is None or other.project_id != project.id:
            continue
        if _find_dependency(db, project, from_id, to_id) is None:
            db.add(Dependency(project_id=project.id, from_task_id=from_id, to_task_id=to_id))

    for row in op.comments:
        author_id = uuid.UUID(row["author_user_id"]) if row.get("author_user_id") else None
        if author_id is not None and db.get(User, author_id) is None:
            # The "exactly one author" constraint will not let a remark be
            # re-signed with a guest name, and a remark with no author is
            # forbidden — so it is skipped.
            continue
        db.add(
            Comment(
                id=uuid.UUID(row["id"]),
                project_id=project.id,
                task_id=task.id,
                author_user_id=author_id,
                guest_name=row.get("guest_name"),
                body=row["body"],
                # Snapshots written before the flag was saved do not say which
                # remarks were internal. A member's remark from such a snapshot
                # is restored as internal: hiding a public reply from the guest
                # is an inconvenience, publishing an aside is a leak. A guest's
                # remark cannot be internal at all.
                internal=row.get("internal", author_id is not None),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    db.flush()


def _cascade(
    db: DbSession,
    project: Project,
    seeds: set[uuid.UUID],
    given: dict[uuid.UUID, date] | None,
) -> tuple[dict, dict]:
    """Automatic shifting along dependencies — and two date maps for the journal.

    It returns a pair of "for the forward operation, for the inverse one": where
    the successors ended up and where they came from. Both are already reduced to
    the form they lie in inside the journal — strings, not objects.

    `given` is a map from the journal: that means an undo is under way and the
    dates must be laid out verbatim. Recomputing the automatic shift here is
    impossible in principle: "move to the earliest possible" is irreversible — a
    forward move remembers where it came from, a computation of it does not.
    """
    if given is not None:
        was = apply_dates(db, project, given)
        return _dates_out(given), _dates_out(was)

    if not project.auto_schedule:
        return {}, {}

    org = db.get(Organization, project.org_id)
    cal = project_calendar(project, org)
    was = push_successors(db, project, cal, seeds)
    # The forward operation carries the new dates, the inverse one the previous
    # ones. The new ones are taken from the tasks themselves rather than recomputed
    # in a second pass: the shift has only just laid them out.
    moved = {
        task.id: task.start_date
        for task in db.scalars(select(Task).where(Task.id.in_(was))).all()
    } if was else {}
    return _dates_out(moved), _dates_out(was)


def _dates_out(dates: dict[uuid.UUID, date]) -> dict[str, str]:
    """A date map in the form it lies in inside the journal."""
    return {str(task_id): moment.isoformat() for task_id, moment in dates.items()}


def _with_cascade(payload: dict, cascade: dict) -> dict:
    """The map of what was shifted — into the journal entry, and only if anything was shifted.

    An empty field is not written: a `"cascade": {}` on every other operation would
    litter the journal with a trace of something that did not happen.
    """
    return payload | {"cascade": cascade} if cascade else payload


def _swap(payload: dict) -> dict:
    """The inverse operation differs from the forward one only in swapping from and to.

    One helper instead of an inversion branch in every operation: a pair of bounds
    is already a complete description both of the forward action and of the reverse.
    """
    return {**payload, "from": payload["to"], "to": payload["from"]}


def _apply(db: DbSession, project: Project, op) -> tuple[dict, dict]:
    """Applies the operation and returns the pair (what to write into op, what to write into inverse)."""

    if isinstance(op, CreateCategory):
        # max(position) + 1 rather than COUNT(*): a deletion punches a hole in the
        # numbering, and COUNT(*) after a deletion hands out an already taken number
        # again. coalesce(..., -1) yields 0 for an empty collection without a
        # separate branch.
        position = (
            op.position
            if op.position is not None
            else db.scalar(
                select(func.coalesce(func.max(Category.position), -1) + 1).where(
                    Category.project_id == project.id
                )
            )
        )
        category = Category(
            id=op.category_id or uuid.uuid4(),
            project_id=project.id,
            name=op.name,
            color=op.color,
            position=position,
        )
        db.add(category)
        db.flush()
        forward = {
            "type": "create_category", "category_id": str(category.id), "name": op.name,
            "color": op.color, "position": category.position,
        }
        # The tasks from the snapshot (undoing a cascading deletion) are created
        # every last one first, and only then is each one's environment restored:
        # tasks of one category refer to each other, and a dependency on a
        # neighbour that has not been created yet would be silently skipped — see
        # _restore_task_links.
        if op.tasks:
            restored = [_add_task(db, project, task_op) for task_op in op.tasks]
            for task, task_op in zip(restored, op.tasks):
                _restore_task_links(db, project, task, task_op)
            forward["tasks"] = [_task_payload(task) for task in restored]
        return (forward, {"type": "delete_category", "category_id": str(category.id)})

    if isinstance(op, CreateTask):
        task = _add_task(db, project, op)
        _restore_task_links(db, project, task, op)
        return (_task_payload(task), {"type": "delete_task", "task_id": str(task.id)})

    if isinstance(op, MoveTask):
        task = _require_task(db, project, op.task_id)
        previous = task.start_date
        task.start_date = op.start_date
        db.flush()
        moved, back = _cascade(db, project, {task.id}, op.cascade)
        return (
            _with_cascade(
                {"type": "move_task", "task_id": str(task.id), "from": previous.isoformat(),
                 "to": op.start_date.isoformat()},
                moved,
            ),
            _with_cascade(
                {"type": "move_task", "task_id": str(task.id), "from": op.start_date.isoformat(),
                 "to": previous.isoformat()},
                back,
            ),
        )

    if isinstance(op, ResizeTask):
        if op.duration_days < 1:
            raise InvalidOperation("duration_too_short", "длительность должна быть не меньше одного дня")
        task = _require_task(db, project, op.task_id)
        if task.milestone:
            # A milestone has no edges to drag: it is a point. The reason is the
            # same as for set_duration — the flag was set by a person, and clearing
            # it is theirs to do as well.
            raise InvalidOperation(
                "task_is_milestone", "у вехи длительность не меняется: сначала снимите признак"
            )
        # Both bounds as a pair of dicts, as with set_task_fields: the operation
        # changes two fields at once and must read as one change.
        forward = {
            "type": "resize_task",
            "task_id": str(task.id),
            "from": {
                "start_date": task.start_date.isoformat(),
                "duration_days": task.duration_days,
            },
            "to": {
                "start_date": op.start_date.isoformat(),
                "duration_days": op.duration_days,
            },
        }
        task.start_date = op.start_date
        task.duration_days = op.duration_days
        db.flush()
        moved, back = _cascade(db, project, {task.id}, op.cascade)
        return _with_cascade(forward, moved), _with_cascade(_swap(forward), back)

    if isinstance(op, MoveCategory):
        if op.days == 0:
            # Zero days is not a change, while an entry in the history would promise
            # that something happened. move_task deliberately has no such rejection:
            # there zero means "put it back on the previous date", and it is still a
            # gesture on a single task. Here zero shifts a whole category — that is,
            # shifts nothing.
            raise InvalidOperation("empty_shift", "сдвиг на ноль дней ничего не меняет")
        category = _require_category(db, project, op.category_id)
        tasks = db.scalars(
            select(Task).where(Task.category_id == category.id).order_by(Task.position, Task.id)
        ).all()
        if not tasks:
            raise InvalidOperation("category_empty", "в категории нет задач: двигать нечего")
        shift = timedelta(days=op.days)
        for task in tasks:
            moved = task.start_date + shift
            if moved > MAX_WIRE_DATE or moved < date.min + timedelta(days=1):
                # The same far edge as for dates from the wire: a shift must not be
                # able to store a date in the database that the wire would not have
                # accepted.
                raise InvalidOperation(
                    "date_out_of_range", f"сдвиг уводит «{task.name}» за границу дат"
                )
            task.start_date = moved
        db.flush()
        # Successors outside the category are successors too: the stage moved, and
        # the work waiting on its tasks must move with it. The category's own tasks
        # have already been moved by then, and the automatic shift does not touch
        # them — they are the walk's starting points.
        pushed, back = _cascade(db, project, {task.id for task in tasks}, op.cascade)
        return (
            _with_cascade(
                {
                    "type": "move_category",
                    "category_id": str(category.id),
                    "days": op.days,
                    # The tasks are named individually rather than only by a number
                    # of days: the history must show what exactly moved, and the
                    # undo card must say how many rows will come back.
                    "task_ids": [str(task.id) for task in tasks],
                },
                pushed,
            ),
            _with_cascade(
                {
                    "type": "move_category",
                    "category_id": str(category.id),
                    "days": -op.days,
                    "task_ids": [str(task.id) for task in tasks],
                },
                back,
            ),
        )

    if isinstance(op, SetMilestone):
        task = _require_task(db, project, op.task_id)
        previous_duration = task.duration_days
        forward = {
            "type": "set_milestone",
            "task_id": str(task.id),
            "from": task.milestone,
            "to": op.milestone,
        }
        inverse = _swap(forward)
        task.milestone = op.milestone
        if op.duration_days is not None:
            # A restore field: the journal dictates the exact duration, and the
            # collapse is not recomputed — it already ran in the forward operation.
            task.duration_days = op.duration_days
        elif op.milestone:
            # A milestone is a point on the scale: the duration collapses into a
            # day. That is exactly what an undo must be able to bring back, so both
            # bounds go into the journal below.
            task.duration_days = 1
        db.flush()
        if task.duration_days != previous_duration:
            forward |= {"duration_from": previous_duration, "duration_to": task.duration_days}
            inverse |= {"duration_from": task.duration_days, "duration_to": previous_duration}
        return forward, inverse

    if isinstance(op, SetDuration):
        if op.duration_days < 1:
            raise InvalidOperation("duration_too_short", "длительность должна быть не меньше одного дня")
        task = _require_task(db, project, op.task_id)
        if task.milestone and op.duration_days != 1:
            # A milestone has no duration. This operation is not entitled to turn it
            # silently back into a segment: the milestone flag was set by a person,
            # and clearing it is their decision too (set_milestone).
            raise InvalidOperation(
                "task_is_milestone", "у вехи длительность не меняется: сначала снимите признак"
            )
        previous = task.duration_days
        task.duration_days = op.duration_days
        db.flush()
        moved, back = _cascade(db, project, {task.id}, op.cascade)
        return (
            _with_cascade(
                {"type": "set_duration", "task_id": str(task.id), "from": previous,
                 "to": op.duration_days},
                moved,
            ),
            _with_cascade(
                {"type": "set_duration", "task_id": str(task.id), "from": op.duration_days,
                 "to": previous},
                back,
            ),
        )

    if isinstance(op, SetTaskFields):
        task = _require_task(db, project, op.task_id)
        before = {field: getattr(task, field) for field in _TASK_FIELDS}
        after = {
            "name": op.name,
            "description": op.description,
            "internal_note": op.internal_note,
        }
        for field, value in after.items():
            setattr(task, field, value)
        db.flush()
        forward = {
            "type": "set_task_fields",
            "task_id": str(task.id),
            "from": before,
            "to": after,
        }
        return forward, _swap(forward)

    if isinstance(op, SetCriticality):
        if op.criticality not in CRITICALITY_LEVELS:
            raise InvalidOperation(
                "unknown_criticality", f"неизвестный уровень: {op.criticality}"
            )
        task = _require_task(db, project, op.task_id)
        forward = {
            "type": "set_criticality",
            "task_id": str(task.id),
            "from": task.criticality,
            "to": op.criticality,
        }
        task.criticality = op.criticality
        db.flush()
        return forward, _swap(forward)

    if isinstance(op, SetRisk):
        if op.risk not in RISK_FLAGS:
            raise InvalidOperation("unknown_risk", f"неизвестный флаг риска: {op.risk}")
        task = _require_task(db, project, op.task_id)
        forward = {
            "type": "set_risk",
            "task_id": str(task.id),
            "from": {"risk": task.risk, "note": task.risk_note},
            "to": {"risk": op.risk, "note": op.note},
        }
        task.risk = op.risk
        task.risk_note = op.note
        db.flush()
        return forward, _swap(forward)

    if isinstance(op, SetProgress):
        if not 0 <= op.progress_pct <= 100:
            raise InvalidOperation(
                "progress_out_of_range", f"процент вне 0..100: {op.progress_pct}"
            )
        if op.status is not None and op.status not in TASK_STATUSES:
            raise InvalidOperation("unknown_status", f"неизвестный статус: {op.status}")
        task = _require_task(db, project, op.task_id)
        previous_status = task.status
        forward = {
            "type": "set_progress",
            "task_id": str(task.id),
            "from": task.progress_pct,
            "to": op.progress_pct,
        }
        inverse = _swap(forward)
        if op.status is not None:
            # A restore field: the status is stored as the journal dictates and the
            # coupling is not recomputed — it already ran in the forward operation.
            task.status = op.status
        elif op.progress_pct >= 100:
            # The coupling (see the comment at the model): progress carried through
            # to the end is exactly what "done" means.
            task.status = TaskStatus.DONE.value
        elif task.status == TaskStatus.DONE:
            # Progress has retreated from 100 on a done task — it is back in
            # progress. The other statuses ('planned', 'blocked') are left alone: a
            # movement of progress says nothing about them.
            task.status = TaskStatus.IN_PROGRESS.value
        task.progress_pct = op.progress_pct
        _stamp_status_change(task, previous_status)
        db.flush()
        if task.status != previous_status:
            # Both status bounds go into the journal: an undo must bring the
            # previous status back verbatim rather than derive it through the
            # coupling anew (from 'blocked' the coupling would never guess it).
            forward |= {"status_from": previous_status, "status_to": task.status}
            inverse |= {"status_from": task.status, "status_to": previous_status}
        return forward, inverse

    if isinstance(op, SetStatus):
        if op.status not in TASK_STATUSES:
            raise InvalidOperation("unknown_status", f"неизвестный статус: {op.status}")
        if op.progress_pct is not None and not 0 <= op.progress_pct <= 100:
            raise InvalidOperation(
                "progress_out_of_range", f"процент вне 0..100: {op.progress_pct}"
            )
        task = _require_task(db, project, op.task_id)
        previous_progress = task.progress_pct
        forward = {
            "type": "set_status",
            "task_id": str(task.id),
            "from": task.status,
            "to": op.status,
        }
        inverse = _swap(forward)
        previous_status = task.status
        task.status = op.status
        if op.progress_pct is not None:
            # A restore field — like status in set_progress: the journal dictates
            # the exact value and the coupling is not recomputed.
            task.progress_pct = op.progress_pct
        elif op.status == TaskStatus.DONE:
            # The coupling (see the comment at the model): "done" is the whole
            # scope. There is no reverse rule: leaving 'done' does not touch progress.
            task.progress_pct = 100
        _stamp_status_change(task, previous_status)
        db.flush()
        if task.progress_pct != previous_progress:
            forward |= {"progress_from": previous_progress, "progress_to": task.progress_pct}
            inverse |= {"progress_from": task.progress_pct, "progress_to": previous_progress}
        return forward, inverse

    if isinstance(op, RenameCategory):
        category = _require_category(db, project, op.category_id)
        forward = {
            "type": "rename_category",
            "category_id": str(category.id),
            "from": category.name,
            "to": op.name,
        }
        category.name = op.name
        db.flush()
        return forward, _swap(forward)

    if isinstance(op, SetCategoryColor):
        category = _require_category(db, project, op.category_id)
        forward = {
            "type": "set_category_color",
            "category_id": str(category.id),
            "from": category.color,
            "to": op.color,
        }
        category.color = op.color
        db.flush()
        return forward, _swap(forward)

    if isinstance(op, AssignUser):
        task = _require_task(db, project, op.task_id)
        # Without this check a task could be pinned on an outsider, and the very
        # fact of their existence would leak outward: appearing in a project's
        # answer is already an observable difference between "there is no such
        # address" and "there is".
        member = db.scalar(
            select(Membership.id).where(
                Membership.org_id == project.org_id, Membership.user_id == op.user_id
            )
        )
        if member is None:
            raise InvalidOperation("user_not_in_organization", "пользователь не в этой организации")
        if _find_assignment(db, task.id, op.user_id) is not None:
            raise InvalidOperation("already_assigned", "этот человек уже назначен")
        db.add(TaskAssignee(task_id=task.id, user_id=op.user_id))
        db.flush()
        pair = {"task_id": str(task.id), "user_id": str(op.user_id)}
        return {"type": "assign_user", **pair}, {"type": "unassign_user", **pair}

    if isinstance(op, UnassignUser):
        # There is deliberately no membership check here: clearing an assignment
        # from someone who has already left the organization is a lawful action
        # rather than a refusal.
        task = _require_task(db, project, op.task_id)
        assignment = _find_assignment(db, task.id, op.user_id)
        if assignment is None:
            raise NotFoundInProject("assignment_not_found", "назначение не найдено")
        db.delete(assignment)
        db.flush()
        pair = {"task_id": str(task.id), "user_id": str(op.user_id)}
        return {"type": "unassign_user", **pair}, {"type": "assign_user", **pair}

    if isinstance(op, AddDependency):
        # Per the specification, dependencies are arrows in a picture rather than a
        # rule of computation: dates are not recomputed from them. But garbage must
        # not be allowed into them.
        if op.from_task_id == op.to_task_id:
            raise InvalidOperation("self_dependency", "задача не может зависеть от себя")
        # Both sides through _require_task: a dependency on a task from another
        # project is cut off by the same mechanism as everything else.
        _require_task(db, project, op.from_task_id)
        _require_task(db, project, op.to_task_id)
        if _find_dependency(db, project, op.from_task_id, op.to_task_id) is not None:
            raise InvalidOperation("dependency_exists", "такая связь уже есть")
        # A cycle is garbage too, even for "just arrows": the chart draws them as
        # edges, and a ring A->B->A reads as a plan that will never begin. The check
        # is a walk from to towards from over the existing edges: if from is
        # reachable from to, the new edge closes a ring.
        edges: dict[uuid.UUID, list[uuid.UUID]] = {}
        for row in db.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        ):
            edges.setdefault(row.from_task_id, []).append(row.to_task_id)
        frontier = [op.to_task_id]
        seen: set[uuid.UUID] = set()
        while frontier:
            node = frontier.pop()
            if node == op.from_task_id:
                raise InvalidOperation("dependency_cycle", "связь замыкает кольцо")
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(edges.get(node, ()))
        db.add(
            Dependency(
                project_id=project.id,
                from_task_id=op.from_task_id,
                to_task_id=op.to_task_id,
            )
        )
        db.flush()
        ends = {"from_task_id": str(op.from_task_id), "to_task_id": str(op.to_task_id)}
        # A new dependency moves the successor exactly as moving the predecessor
        # does: it is what "this work waits on that one" means. The walk's starting
        # point is the predecessor: it does not move itself, but everything that now
        # depends on it must stand after it.
        moved, back = _cascade(db, project, {op.from_task_id}, op.cascade)
        return (
            _with_cascade({"type": "add_dependency", **ends}, moved),
            _with_cascade({"type": "remove_dependency", **ends}, back),
        )

    if isinstance(op, RemoveDependency):
        _require_task(db, project, op.from_task_id)
        _require_task(db, project, op.to_task_id)
        dependency = _find_dependency(db, project, op.from_task_id, op.to_task_id)
        if dependency is None:
            raise NotFoundInProject("dependency_not_found", "связь не найдена в этом проекте")
        db.delete(dependency)
        db.flush()
        ends = {"from_task_id": str(op.from_task_id), "to_task_id": str(op.to_task_id)}
        # Removing a dependency moves nothing by itself: the automatic shift does
        # not pull backwards (see app/cascade.py). A date map arrives here only as a
        # restore field — when this operation is the inverse of add_dependency and
        # must return the successors to where the new dependency moved them from.
        moved, back = _cascade(db, project, set(), op.cascade)
        return (
            _with_cascade({"type": "remove_dependency", **ends}, moved),
            _with_cascade({"type": "add_dependency", **ends}, back),
        )

    if isinstance(op, ReorderTask):
        if op.position < 0:
            raise InvalidOperation("negative_position", "позиция не может быть отрицательной")
        task = _require_task(db, project, op.task_id)
        _require_category(db, project, op.category_id)

        # A snapshot over the whole project rather than over one category: a move
        # between categories changes both, and a half map would be undone by halves.
        rows = db.scalars(
            select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
        ).all()
        before_pos = {str(row.id): row.position for row in rows}
        before_cat = {str(row.id): str(row.category_id) for row in rows}

        siblings = [
            row for row in rows if row.category_id == op.category_id and row.id != task.id
        ]
        # A position past the end of the list is not a refusal: dragging to the very
        # bottom sends an index equal to the length, and that is a normal gesture.
        index = min(op.position, len(siblings))
        ordered = siblings[:index] + [task] + siblings[index:]

        task.category_id = op.category_id
        for slot, row in enumerate(ordered):
            row.position = slot
        db.flush()

        # A diff goes into the journal rather than a snapshot of the whole project:
        # a reorder touches one or two categories, while a snapshot of a thousand
        # tasks would write kilobytes into jsonb on every drag — and would drag
        # exactly as much into the mutation's answer and into the history. An undo
        # needs only the rows whose position or category changed: the rest stand
        # where they stood.
        changed = [
            row
            for row in rows
            if before_pos[str(row.id)] != row.position
            or before_cat[str(row.id)] != str(row.category_id)
        ]
        forward = {
            "type": "reorder_task",
            "task_id": str(task.id),
            "from": {str(row.id): before_pos[str(row.id)] for row in changed},
            "to": {str(row.id): row.position for row in changed},
            "categories_from": {str(row.id): before_cat[str(row.id)] for row in changed},
            "categories_to": {str(row.id): str(row.category_id) for row in changed},
        }
        inverse = {
            "type": "apply_positions",
            "positions": {str(row.id): before_pos[str(row.id)] for row in changed},
            "categories": {str(row.id): before_cat[str(row.id)] for row in changed},
        }
        return forward, inverse

    if isinstance(op, ReorderCategory):
        if op.position < 0:
            raise InvalidOperation("negative_position", "позиция не может быть отрицательной")
        category = _require_category(db, project, op.category_id)

        # The order is read by the same key the chart reads it by: the position,
        # and on a tie the identifier. Ties do happen for real: categories are
        # created with max(position) + 1, but undoing a deletion returns a stage to
        # its previous number, which may have gone to a neighbour since.
        rows = db.scalars(
            select(Category)
            .where(Category.project_id == project.id)
            .order_by(Category.position, Category.id)
        ).all()
        before_pos = {str(row.id): row.position for row in rows}

        others = [row for row in rows if row.id != category.id]
        # A position past the end of the list is not a refusal: a throw to the very
        # bottom sends an index equal to the length.
        index = min(op.position, len(others))
        ordered = others[:index] + [category] + others[index:]
        for slot, row in enumerate(ordered):
            row.position = slot
        db.flush()

        # A diff goes into the journal rather than a snapshot: a reorder touches the
        # stretch between the old and the new place, not the whole list (see
        # reorder_task above).
        changed = [row for row in rows if before_pos[str(row.id)] != row.position]
        forward = {
            "type": "reorder_category",
            "category_id": str(category.id),
            "from": {str(row.id): before_pos[str(row.id)] for row in changed},
            "to": {str(row.id): row.position for row in changed},
        }
        inverse = {
            "type": "apply_category_positions",
            "positions": {str(row.id): before_pos[str(row.id)] for row in changed},
        }
        return forward, inverse

    if isinstance(op, ApplyCategoryPositions):
        before_categories: dict[str, int] = {}
        for raw_id, position in op.positions.items():
            row = _require_category(db, project, raw_id)
            before_categories[str(row.id)] = row.position
            row.position = position
        db.flush()
        # The keys are converted to strings: in the model they are uuid.UUID, and
        # json.dumps on the way into jsonb fails on such a key — the journal entry
        # would not be stored at all.
        forward = {
            "type": "apply_category_positions",
            "positions": {str(key): value for key, value in op.positions.items()},
        }
        inverse = {"type": "apply_category_positions", "positions": before_categories}
        return forward, inverse

    if isinstance(op, ApplyPositions):
        before_pos: dict[str, int] = {}
        before_cat: dict[str, str] = {}
        for raw_id, position in op.positions.items():
            row = _require_task(db, project, raw_id)
            # Both maps are written by one hand and must agree on their keys, but a
            # journal entry is data, not code: a desync must be a coded refusal
            # rather than a KeyError, which would go outward as a 500.
            target_category = op.categories.get(raw_id)
            if target_category is None:
                raise InvalidOperation(
                    "positions_categories_mismatch",
                    f"в карте категорий нет задачи {raw_id}",
                )
            # The category may have been deleted since: an insert into it would fail
            # on a foreign key — a 500 as well, even though this is an ordinary
            # "the category is gone" refusal.
            _require_category(db, project, target_category)
            before_pos[str(row.id)] = row.position
            before_cat[str(row.id)] = str(row.category_id)
            row.position = position
            row.category_id = target_category
        db.flush()
        # The keys are converted to strings: in the model they are uuid.UUID, and
        # json.dumps on the way into jsonb fails on such a key — the journal entry
        # would not be stored at all.
        forward = {
            "type": "apply_positions",
            "positions": {str(key): value for key, value in op.positions.items()},
            "categories": {str(key): str(value) for key, value in op.categories.items()},
        }
        inverse = {
            "type": "apply_positions",
            "positions": before_pos,
            "categories": before_cat,
        }
        return forward, inverse

    if isinstance(op, DeleteCategory):
        category = _require_category(db, project, op.category_id)
        # A category goes away together with its contents: the stage was cancelled
        # as a whole — and taking it apart row by row to get rid of the heading
        # means as many deletions as it has tasks, and as many history entries, for
        # one decision by a person.
        #
        # Deleting a non-empty category used to be forbidden (`category_not_empty`),
        # and the ban was not a whim: the cascade would carry the tasks away while
        # the inverse operation would bring back an empty heading. What answers this
        # is not a ban but a snapshot — the same one that undoes a task deletion,
        # only for every row of the stage. Warning a person about what will go away
        # with the category is the interface's job: the server is no place for such
        # a warning.
        tasks = db.scalars(
            select(Task).where(Task.category_id == category.id).order_by(Task.position, Task.id)
        ).all()
        snapshot = {
            "type": "create_category",
            "category_id": str(category.id),
            "name": category.name,
            "color": category.color,
            "position": category.position,
        }
        if tasks:
            snapshot["tasks"] = [_task_snapshot(db, task) for task in tasks]
        for task in tasks:
            db.delete(task)
        # A flush before the category itself is deleted rather than one shared flush
        # afterwards: the relationship between these two tables is described only by
        # a foreign key, SQLAlchemy does not derive the order of deletions from it —
        # and a category removed first would carry the tasks away by the database's
        # cascade, leaving the ORM to delete rows that no longer exist (and to
        # complain about it with a warning).
        db.flush()
        db.delete(category)
        db.flush()
        forward = {"type": "delete_category", "category_id": str(op.category_id)}
        # The number of tasks goes into the entry itself: "deleted a category" says
        # nothing about the stage deleted along with it, and there is nothing to
        # count them by in the history feed — the restore snapshot is not visible to
        # every role.
        if tasks:
            forward["tasks"] = len(tasks)
        return (forward, snapshot)

    if isinstance(op, DeleteTask):
        task = _require_task(db, project, op.task_id)
        # The snapshot carries the task's environment too — dependencies,
        # assignments, the conversation: the cascade carries them away with the row,
        # and an undo without them would bring back a bare name.
        snapshot = _task_snapshot(db, task)
        db.delete(task)
        db.flush()
        return ({"type": "delete_task", "task_id": str(op.task_id)}, snapshot)

    raise InvalidOperation("unknown_operation", f"неизвестная операция: {op!r}")


def _guard_shift_threshold(db: DbSession, project: Project, op, reason: str | None) -> None:
    """The check "a deviation from the baseline plan greater than the threshold requires a reason".

    It lives in the mutation layer rather than in the route for exactly the reason
    the specification calls the rule one and the same regardless of the means of
    input: dragging with the mouse, editing a field on a card and applying a batch
    from AI all arrive here by one road, while in the route there would be three of
    them.

    The check runs before application: there is no intermediate "moved but not
    explained" state in the system, and none must come about even for the duration
    of one transaction.

    An undo deliberately goes through the same check. It is not a privileged
    action: if returning to a previous value takes a task further from the baseline
    plan than the threshold, an explanation is needed exactly as it is for any other
    way of getting there.

    Tasks moved by the automatic shift are not counted here, and that is a decision
    rather than an omission. The threshold asks "explain what you are doing", while
    an automatic shift is not what a person does but a consequence of dependencies
    they set up earlier: they move one task, and the chain follows it by a rule they
    themselves turned on. Asking for a reason once more for every link of the chain
    would mean asking one and the same question as many times as there are
    dependencies. The links' own deviation is not hidden meanwhile: the badge on the
    bar and the "the plan has diverged from what was agreed" flag are computed from
    the dates and show them, whoever moved those dates.

    The check runs before application while the forward computation of the shift
    runs during it, so there is nothing here to compute the future chain with short
    of repeating the whole computation in a second pass. That is a second argument
    for the same decision, but not the first: were the computation free, the answer
    would be the same.
    """
    if reason:
        return

    if isinstance(op, MoveTask):
        task = _require_task(db, project, op.task_id)
        deviation = deviation_days(task, start_date=op.start_date)
    elif isinstance(op, SetDuration):
        task = _require_task(db, project, op.task_id)
        deviation = deviation_days(task, duration_days=op.duration_days)
    elif isinstance(op, ResizeTask):
        # The only operation in which both dimensions change with one movement — and
        # the only one it is honest to ask for the greater of the two deviations.
        # The rule "the dimension named is the one measured" does not apply to it:
        # it names both.
        task = _require_task(db, project, op.task_id)
        deviation = deviation_days(
            task, start_date=op.start_date, duration_days=op.duration_days
        )
    elif isinstance(op, MoveCategory):
        # A category shift is the same move of dates, only across many tasks at
        # once, and the threshold must be computed against it the same way. The
        # greatest deviation is taken: the category was shifted with one movement,
        # and it has one explanation — for the task of it that drifted furthest.
        # Computing the threshold for each one separately would mean asking for a
        # reason as many times as the category has rows, for one movement of a hand.
        shift = timedelta(days=op.days)
        deviations = [
            deviation_days(task, start_date=task.start_date + shift)
            for task in db.scalars(
                select(Task).where(Task.category_id == op.category_id)
            ).all()
        ]
        measured = [value for value in deviations if value is not None]
        deviation = max(measured) if measured else None
    elif isinstance(op, SetMilestone):
        # A milestone collapses the duration — that is, it changes dates, and the
        # threshold here is the same as for set_duration. The duration after the
        # operation is known: either dictated by the journal (an undo) or one day.
        task = _require_task(db, project, op.task_id)
        after = op.duration_days if op.duration_days is not None else (1 if op.milestone else None)
        deviation = None if after is None else deviation_days(task, duration_days=after)
    else:
        # The other operations do not touch the baseline plan. Creating a task does
        # not either: a task created after approval has no baseline plan, is marked
        # "beyond the original plan" and is exempt from explanations.
        return

    if deviation is None:
        return

    org = db.get(Organization, project.org_id)
    threshold = resolve_shift_threshold(project, org)
    if deviation > threshold:
        raise ReasonRequired(deviation, threshold)


def apply_op(
    db: DbSession,
    project: Project,
    op,
    *,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
    batch_id: uuid.UUID | None = None,
    undoes_seq: int | None = None,
) -> Revision:
    # A lock on the project's row for the whole time the operation is applied.
    # Without it, two requests editing one project compute max(seq)+1 from one and
    # the same snapshot: the loser violates the unique constraint (project_id, seq)
    # and the caller gets a bare 500. The same race duplicates position, where there
    # is no constraint at all and the divergence goes unnoticed. Collaborative
    # editing of one project is this product's normal mode rather than a rare case;
    # when there is no contention, the lock costs nothing.
    lock_project(db, project)
    # A reason made of nothing but spaces is the absence of a reason. It is
    # normalized here rather than in the route: the threshold rule lives in this
    # layer, and checking one thing here while storing another would mean two
    # different truths about one value.
    reason = reason.strip() or None if reason else None
    _guard_shift_threshold(db, project, op, reason)
    forward, inverse = _apply(db, project, op)
    revision = Revision(
        project_id=project.id,
        seq=_next_seq(db, project),
        actor_user_id=actor_id,
        op=forward,
        inverse=inverse,
        reason=reason,
        batch_id=batch_id,
        undoes_seq=undoes_seq,
    )
    db.add(revision)
    db.flush()
    return revision


# Operations that store both bounds in the journal: the name of the model field
# the scalar `to` lands in. A mapping rather than a chain of elifs: a chain grows
# with the number of operations and stops being readable, whereas here a new
# operation is one line.
_SCALAR_BOUNDS_FIELD = {
    "move_task": "start_date",
    "set_duration": "duration_days",
    "set_criticality": "criticality",
    "set_milestone": "milestone",
    "set_progress": "progress_pct",
    "set_status": "status",
    "rename_category": "name",
    "set_category_color": "color",
}

# Operations whose `to` is not a scalar but a dict of fields: they are unfolded
# into the model as a whole.
_MAPPED_BOUNDS = frozenset({"set_task_fields", "resize_task", "set_risk"})

# A coupled field whose bounds an operation carries beyond its own: the model
# field's name and a pair of journal keys. They are present in an entry only when
# the coupling fired — otherwise a restore would read values the operation did not
# change.
_COUPLED_BOUNDS_FIELD = {
    "set_progress": ("status", "status_from", "status_to"),
    "set_status": ("progress_pct", "progress_from", "progress_to"),
    "set_milestone": ("duration_days", "duration_from", "duration_to"),
}


def _op_from_dict(payload: dict):
    """Restores an operation from a journal entry.

    Operations with a pair of bounds store both from and to, so the value is taken
    from to — that way one and the same entry reads both as the forward operation
    and as the inverse one (its inverse differs only in the order of those two
    fields).
    """
    kind = payload["type"]
    model = _MODELS[kind]
    data = dict(payload)
    if kind in _SCALAR_BOUNDS_FIELD:
        data[_SCALAR_BOUNDS_FIELD[kind]] = data.pop("to")
        data.pop("from", None)
    elif kind in _MAPPED_BOUNDS:
        data.update(data.pop("to"))
        data.pop("from", None)
    if kind in _COUPLED_BOUNDS_FIELD:
        # A coupled field follows the same rule as the main one: the value comes
        # from *_to, so the entry reads both as the forward operation and as the
        # inverse. Without it an undo would entrust the restore to the coupling, and
        # the coupling does not know the previous value (see SetProgress.status).
        field, bound_from, bound_to = _COUPLED_BOUNDS_FIELD[kind]
        if bound_to in data:
            data[field] = data.pop(bound_to)
        data.pop(bound_from, None)
    return model.model_validate(data)


def undo(
    db: DbSession,
    project: Project,
    revision: Revision,
    *,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
    batch_id: uuid.UUID | None = None,
) -> Revision:
    # Every neighbouring helper re-checks project_id, while undo took the revision
    # on trust. The route takes the revision number from the address, and without
    # this check that is a cross-tenant write exactly: someone else's revision would
    # be applied to one's own project.
    if revision.project_id != project.id:
        raise NotFoundInProject("revision_not_found", "ревизия не найдена в этом проекте")
    return apply_op(
        db,
        project,
        _op_from_dict(revision.inverse),
        actor_id=actor_id,
        reason=reason,
        batch_id=batch_id,
        undoes_seq=revision.seq,
    )


def undo_last(
    db: DbSession,
    project: Project,
    *,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
    expected_seq: int | None = None,
) -> tuple[Revision, Revision]:
    """Undoing the last change: choosing the revision and applying it under one lock.

    Choosing it here rather than in the route closes the double-undo race: two
    simultaneous presses of "Undo" read last_undoable from one snapshot, both found
    the same revision — and the one that lost the lock inside apply_op applied its
    undo a second time, returning the project to where the first one had just left.
    Under the project lock the second request waits for the first and picks the next
    revision — or learns that there is nothing to undo.

    `expected_seq` is the number of the revision whose undo the client promised to a
    person. The check happens here, under the same lock: a comparison on the client
    is separated from the undo by a network journey, and into that gap slips exactly
    the change the comparison was protecting against. With no number, simply the
    head of the journal is undone — that is how the history feed calls it, and it
    has a number too, and that keeps a blind undo (a script, a console) possible.

    Returns the pair (the undo entry, the revision undone): the route needs both.
    """
    lock_project(db, project)
    revision = last_undoable(db, project)
    if revision is None:
        raise NotFoundInProject("nothing_to_undo", "отменять нечего")
    if expected_seq is not None and revision.seq != expected_seq:
        raise UndoConflict(
            "undo_conflict",
            f"наверху журнала ревизия {revision.seq}, а отменить просят {expected_seq}",
        )
    applied = undo(db, project, revision, actor_id=actor_id, reason=reason)
    return applied, revision


def last_undoable(db: DbSession, project: Project) -> Revision | None:
    """The revision the "Undo" button will undo.

    The newest of those not yet undone by anyone and not themselves an undo.
    Without the second condition, pressing again would bring back what was undone —
    and a person pressing "Undo" twice would end up where they started instead of
    stepping back twice.
    """
    undone = select(Revision.undoes_seq).where(
        Revision.project_id == project.id, Revision.undoes_seq.is_not(None)
    )
    return db.scalar(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.undoes_seq.is_(None),
            Revision.seq.not_in(undone),
            # Revisions made before the start date was last assigned hold dates
            # of the previous axis (see Project.undo_floor_seq).
            Revision.seq > project.undo_floor_seq,
        )
        .order_by(Revision.seq.desc())
        .limit(1)
    )


def _refuse_outgrown_batch(db: DbSession, revisions: Sequence[Revision]) -> None:
    """A batch whose categories have since received other people's work is not
    rolled back.

    Undoing `create_category` deletes the category together with its contents.
    For the batch's own tasks that is the point; for a task a person added to
    the category afterwards it is a loss with no way back — an undo is never
    itself undoable — and the journal is left with a `create_task` on top whose
    task no longer exists, so "Undo" answers `task_not_found` from then on. The
    refusal leaves both the task and the batch in place: the person moves or
    deletes their rows deliberately and repeats the rollback.
    """
    own_tasks = {
        revision.op["task_id"] for revision in revisions if revision.op.get("type") == "create_task"
    }
    for revision in revisions:
        if revision.op.get("type") != "create_category":
            continue
        residents = db.scalars(
            select(Task.id).where(Task.category_id == uuid.UUID(revision.op["category_id"]))
        )
        if any(str(task_id) not in own_tasks for task_id in residents):
            raise UndoConflict(
                "batch_has_later_tasks",
                "в категории пачки есть задачи, созданные не этой пачкой",
            )


def undo_batch(
    db: DbSession,
    project: Project,
    batch_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
) -> list[Revision]:
    """Rolling a whole batch back — the one AI applied with a single button.

    The order is reversed: a batch creates a category and then the tasks in it, and
    a rollback from the beginning would run into a non-empty category.

    Revisions already undone are skipped: pressing the same button again is not an
    order to apply the inverse operation a second time. Undos themselves are not
    undone either — for the same reason `last_undoable` goes around them; there is
    no un-rollback ("bring the batch back") in the first version.

    The undos get a shared batch_id of their own: in the journal a batch rollback
    reads as one action rather than as a scattering of unrelated entries.

    A reason is accepted and handed to every undo: a rollback goes through the same
    threshold check as any change of dates, and without a reason a batch that moved
    dates further than the threshold would be un-rollbackable altogether.
    """
    # The same lock and for the same reason as in undo_last: without it two
    # simultaneous rollbacks read the list of revisions from one snapshot and each
    # applies every undo — two per revision.
    lock_project(db, project)
    undone = select(Revision.undoes_seq).where(
        Revision.project_id == project.id, Revision.undoes_seq.is_not(None)
    )
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.batch_id == batch_id,
            Revision.undoes_seq.is_(None),
            Revision.seq.not_in(undone),
        )
        .order_by(Revision.seq.desc())
    ).all()

    if not revisions:
        raise NotFoundInProject("batch_not_found", "пачка не найдена в этом проекте")
    # A batch is rolled back whole or not at all: one whose revisions predate the
    # last assignment of the start date holds dates of the previous axis.
    if any(revision.seq <= project.undo_floor_seq for revision in revisions):
        raise NotFoundInProject("nothing_to_undo", "пачка применена до смены даты старта")
    _refuse_outgrown_batch(db, revisions)

    undo_batch_id = uuid.uuid4()
    return [
        undo(db, project, revision, actor_id=actor_id, reason=reason, batch_id=undo_batch_id)
        for revision in revisions
    ]
