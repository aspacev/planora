"""The relative plan and its anchoring to a calendar start date.

A relative plan is not tied to real dates: tasks have an offset from the
project's beginning in working days, a duration and dependencies, while the
scale reads as "Month 1 / Week 1 / Day 1". The offset, however, is stored not in
a column of its own but in that same `Task.start_date` — as a coordinate on the
relative axis:

    day N of the project  =  RELATIVE_EPOCH + (N - 1) calendar days.

RELATIVE_EPOCH is a Monday, so "Week 1" starts on a Monday and the working-week
mask lands on the relative axis without corrections. Holidays are deliberately
not applied in relative mode: a holiday is a property of a real date, which the
plan does not have yet, and a relative project's calendar consists of a single
weekly mask (see relative_calendar).

Why a coordinate rather than a column with an offset: an offset and a coordinate
are in one-to-one correspondence for a fixed mask, but a coordinate is already
what the revision journal, undo, the shift threshold, the plan snapshots and the
entire client speak in. A second column with the same content would be a second
representation of one and the same thing — and would one day diverge from the
first.

Anchoring to a start date moves the plan into calendar mode with no drift: every
task's coordinate is read as an offset in working days from the epoch, and that
same offset is laid off from the assigned start along the real calendar — with
weekends and holidays. The source of truth, both before and after, is the start
plus durations plus dependencies plus the working calendar, not the finish date.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.calendar import Calendar, count_working_days, end_date, first_working_on_or_after
from app.models import Organization, PlanVersion, Project, Revision, ScheduleMode, Task
from app.settings_resolution import project_calendar, resolve_working_days

#: The beginning of the relative axis. A Monday — so that "Day 1" opens "Week 1"
#: and the working-week mask lines up with the grid's columns. The year is taken
#: knowingly earlier than any real plan: a relative coordinate must not be
#: confused with a real date even by eye.
RELATIVE_EPOCH = date(2001, 1, 1)


def relative_calendar(project: Project, org: Organization) -> Calendar:
    """The calendar of the relative axis: a single weekly mask, no holidays.

    Holidays and declared working days are properties of real dates; on an axis
    where there are no dates yet, there is nothing for them to land on (dates of
    real years configured in the organization would not intersect coordinates in
    2001 anyway — but the rule here is by meaning, not by a coincidence of
    ranges).
    """
    return Calendar(working_days=resolve_working_days(project, org))


def offset_of(coordinate: date, anchor: date, cal: Calendar) -> int:
    """A task's offset from the anchor in working days, starting from zero.

    A start on a non-working day reads as the nearest working day after it —
    exactly as the finish-date computation reads it (see calendar.end_date): the
    offset must name the day on which the work will actually begin.

    For a coordinate earlier than the anchor there is no offset in working days —
    the caller decides for itself (see _shifted_dates); here it is a ValueError,
    as in count_working_days.
    """
    started = first_working_on_or_after(coordinate, cal)
    return count_working_days(anchor, started, cal) - 1


def date_at_offset(anchor: date, offset: int, cal: Calendar) -> date:
    """The date standing `offset` working days from the anchor (zero being the
    first working day on or after the anchor).

    The same counting as for duration: the `offset + 1`-th working day of a range
    started at the anchor. A separate name, because the question is different —
    "where does the task stand" rather than "when will it finish".
    """
    return end_date(anchor, offset + 1, cal)


@dataclass(frozen=True)
class SchedulePlan:
    """The result of recomputing a plan for a new start date — before it is applied.

    It holds the new starts by task and the resulting bounds: the preview shows
    them to a person before confirmation, and applying writes them.
    """

    start_date: date
    #: The project's end under the new dates; None for a project with no tasks.
    end_date: date | None
    #: The new task starts, by their identifiers.
    starts: dict[uuid.UUID, date]
    #: The new baseline starts — only for tasks that have a baseline plan.
    baseline_starts: dict[uuid.UUID, date]
    #: How any date of the old axis lands on the new one; None when the tasks
    #: stay where they are. Applying needs it for the dates that live outside
    #: the task rows — the snapshots of earlier plan versions.
    carry: Callable[[date], date] | None = None


def _project_tasks(db: DbSession, project: Project) -> list[Task]:
    return list(
        db.scalars(
            select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
        ).all()
    )


def _shifted_dates(
    tasks: list[Task],
    *,
    old_anchor: date,
    old_cal: Calendar,
    new_anchor: date,
    new_cal: Calendar,
) -> SchedulePlan:
    """Moves every task from the old axis to the new one, preserving its offset in
    working days from the anchor.

    A task starting before the anchor (in a calendar project that happens after
    manual moves) is shifted by the calendar difference between the anchors: it
    has no offset in working days, and its relative position to the project's
    beginning must not be lost.
    """
    calendar_delta = new_anchor - old_anchor

    def carried(coordinate: date) -> date:
        if coordinate < old_anchor:
            return coordinate + calendar_delta
        return date_at_offset(new_anchor, offset_of(coordinate, old_anchor, old_cal), new_cal)

    starts: dict[uuid.UUID, date] = {}
    baseline_starts: dict[uuid.UUID, date] = {}
    ends: list[date] = []
    for task in tasks:
        starts[task.id] = carried(task.start_date)
        ends.append(end_date(starts[task.id], task.duration_days, new_cal))
        if task.baseline_start is not None and task.baseline_duration is not None:
            baseline_starts[task.id] = carried(task.baseline_start)

    return SchedulePlan(
        start_date=new_anchor,
        end_date=max(ends) if ends else None,
        starts=starts,
        baseline_starts=baseline_starts,
        carry=carried,
    )


def _current_anchor(project: Project, tasks: list[Task]) -> date:
    """The point the current plan's offsets are read from.

    In relative mode it is the epoch. In calendar mode it is the assigned start,
    and for a project that has been calendar-based since birth (created before the
    modes existed) it is the earliest task start: it has no other beginning.
    """
    if project.schedule_mode == ScheduleMode.RELATIVE:
        return RELATIVE_EPOCH
    if project.start_date is not None:
        return project.start_date
    return min((task.start_date for task in tasks), default=RELATIVE_EPOCH)


def _source_calendar(project: Project, org: Organization) -> Calendar:
    if project.schedule_mode == ScheduleMode.RELATIVE:
        return relative_calendar(project, org)
    return project_calendar(project, org)


def planned_schedule(
    db: DbSession,
    project: Project,
    org: Organization,
    *,
    start: date,
    working_days: int | None = None,
    shift_tasks: bool = True,
) -> SchedulePlan:
    """Computes where the tasks will stand after anchoring to a start date, without changing them.

    `working_days` is the mask of the new working week, if a person chose one in
    the same dialog; None means keep the one in force. The offsets are read
    against the old calendar (the plan was drawn under it) and laid out against
    the new one.

    `shift_tasks=False` is the "leave the dates as they are" option when the start
    of a calendar project is changed again: the tasks stay put and only the anchor
    changes (and possibly the calendar — finish dates are recomputed from it). For
    a relative project the "as they are" option does not exist: without a
    recomputation its coordinates would stay on the relative axis.
    """
    tasks = _project_tasks(db, project)
    current = project_calendar(project, org)
    target = Calendar(
        working_days=working_days if working_days is not None else current.working_days,
        holidays=current.holidays,
        extra_workdays=current.extra_workdays,
    )

    if project.schedule_mode == ScheduleMode.CALENDAR and not shift_tasks:
        ends = [end_date(task.start_date, task.duration_days, target) for task in tasks]
        return SchedulePlan(
            start_date=start,
            end_date=max(ends) if ends else None,
            starts={task.id: task.start_date for task in tasks},
            baseline_starts={},
        )

    return _shifted_dates(
        tasks,
        old_anchor=_current_anchor(project, tasks),
        old_cal=_source_calendar(project, org),
        new_anchor=start,
        new_cal=target,
    )


def _carry_plan_versions(
    db: DbSession, project: Project, carry: Callable[[date], date]
) -> None:
    """Writes every earlier promise's start on the new axis next to the original.

    `current_start` is carried from its own previous value when there is one: a
    project anchored twice has left two axes behind, and the original
    `start_date` belongs to the first of them.
    """
    versions = db.scalars(select(PlanVersion).where(PlanVersion.project_id == project.id))
    for version in versions:
        # A new dict rather than an edit in place: SQLAlchemy does not see
        # changes inside a JSONB value and would not write them.
        version.snapshot = {
            task_id: {
                **promised,
                "current_start": carry(
                    date.fromisoformat(promised.get("current_start") or promised["start_date"])
                ).isoformat(),
            }
            for task_id, promised in version.snapshot.items()
        }


def apply_schedule(
    db: DbSession,
    project: Project,
    org: Organization,
    *,
    start: date,
    working_days: int | None = None,
    shift_tasks: bool = True,
) -> SchedulePlan:
    """Assigns the start date: writes it down, moves the project into calendar
    mode and lays the tasks out along the real calendar.

    Offsets, durations and dependencies are preserved — only the axis they are
    laid off on changes. The baseline plans move across together with the starts:
    a promise made in relative days must, after anchoring, stand on the same
    working days from the start, otherwise the whole plan would suddenly be "off
    schedule" without a single edit.

    `shift_tasks=False` moves the start date without recomputing the tasks
    ("leave the dates as they are"): a person's choice when changing the start of
    an already calendar-based project again. For a relative project there is no
    choice — without a recomputation its coordinates would stay in 2001.

    Snapshots of earlier plan versions (PlanVersion.snapshot) keep their
    `start_date` as it was: they are a chronicle of promises in the axis they were
    made in. Next to it each row gets `current_start` — the same promise on the new
    axis: restoring a version reads that one (see plans.restore_plan_version),
    otherwise a promise made in relative days would become a baseline in 2001.

    It does not go through the revision journal — like edits to the settings: this
    is a change of the frame of reference rather than an edit of the plan, and an
    entry saying "every task was moved" is not something the journal could undo
    with one button. For the same reason the revisions made so far stop being
    undoable (Project.undo_floor_seq): their inverse operations hold dates of the
    axis that has just been left.
    """
    plan = planned_schedule(
        db, project, org, start=start, working_days=working_days, shift_tasks=shift_tasks
    )

    if working_days is not None:
        project.working_days = working_days

    if project.schedule_mode == ScheduleMode.RELATIVE or shift_tasks:
        moved = False
        for task in _project_tasks(db, project):
            moved = moved or task.start_date != plan.starts[task.id]
            task.start_date = plan.starts[task.id]
            moved_baseline = plan.baseline_starts.get(task.id)
            if moved_baseline is not None:
                task.baseline_start = moved_baseline
        if plan.carry is not None:
            _carry_plan_versions(db, project, plan.carry)
        # Only when a date really changed: re-saving the same start moves
        # nothing, and sealing the undo history for it would be a loss for nothing.
        if moved or project.schedule_mode == ScheduleMode.RELATIVE:
            project.undo_floor_seq = (
                db.scalar(
                    select(func.max(Revision.seq)).where(Revision.project_id == project.id)
                )
                or 0
            )

    project.start_date = start
    project.schedule_mode = ScheduleMode.CALENDAR
    db.flush()
    return plan
