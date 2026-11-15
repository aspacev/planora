"""A project's scorecard: a weekly panel of the plan's health.

How it works, in brief. The metrics are computed from the live plan (the task
tables; proposals not yet carried across live separately and do not reach this)
and from the revision journal. A week is an ISO week in the project's timezone
(inherited from the organization), and days are working days everywhere —
through the project's calendar.

The architecture deliberately has no scheduler, so committing weeks is lazy: the
first GET after a week boundary appends the missing weekly snapshots and
recomputes the current week. Snapshots of past weeks are immutable — they are a
chronicle, and streaks and the sparkline are computed from them; only the current
week's row is overwritten, and it doubles as a five-minute cache of the live
computation. Journal-based metrics (`date_shifts`, `close_rate`, `scope_growth`)
are reconstructed exactly for missed weeks; state cross-sections cannot be
reconstructed — they are computed from the current state as of the end of that
week and marked `backfilled: true` in details.

`finish_drift` — the shift of the projected finish over a week — is computed as
the difference between two neighbouring snapshots, so missed weeks have no point
of reference: they are written as `no_data`+`backfilled` rather than
reconstructed by replaying the journal (that is a separate task, out of
proportion to the benefit). The very first week with no previous snapshot is
`no_data` too: drift with no base is unmeasurable rather than equal to zero.

The "red two weeks running" rule fires when the current week's snapshot is
written: a "Investigate: {metric}" task is created through the mutation layer,
and a `rule_triggered` event remains as a trace. Repetition is suppressed until
the streak breaks — by the events already recorded rather than by a separate
flag: the event is itself the memory that a task has been created for this streak.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.calendar import Calendar, CalendarError, count_working_days, end_date
from app.models import (
    Category,
    Membership,
    Organization,
    Project,
    Revision,
    RiskFlag,
    ScheduleMode,
    ScorecardAlert,
    ScorecardAlertKind,
    ScorecardDirection,
    ScorecardMetric,
    ScorecardSnapshot,
    ScorecardStatus,
    Task,
    TaskAssignee,
    TaskStatus,
    User,
)
from app.mutations import AssignUser, CreateTask, InvalidOperation, MutationError, apply_op
from app.projects import lock_project
from app.settings_resolution import (
    project_calendar,
    resolve_shift_threshold,
    resolve_timezone,
)

logger = logging.getLogger(__name__)

#: MVP constants. The "red" threshold: target x 2 for lte (with a target of 0 — a
#: value greater than 2), target x 0.75 for gte; between the target and the
#: threshold lies "yellow".
RISK_LTE_FACTOR = Decimal("2")
RISK_LTE_ZERO_LIMIT = Decimal("2")
RISK_GTE_FACTOR = Decimal("0.75")
#: "Stalled in progress" — more than this many working days in in_progress.
STALE_WORKDAYS = 5
#: "An unrealistic deadline" — overdue by more than this many working days with
#: not a single date edit in the journal.
UNREAL_OVERDUE_WORKDAYS = 10
#: The default history window and the cache ceiling for the current week.
DEFAULT_WEEKS = 13
CURRENT_WEEK_TTL_SECONDS = 300
#: How many weeks a streak may stretch back on a read. A ceiling rather than a
#: matter of precision: a streak longer than two years adds nothing to the badge.
MAX_STREAK_WEEKS = 104
#: The window of the per-person pace trend: eight bars read at a glance, and two
#: months is enough for a person's habit to show.
TREND_WEEKS = 8
#: "Blocked" in the header: one or two tasks is attention, three or more is risk.
#: Not a metric config but constants: this number has no owner and no target, it
#: simply says how much work is stuck.
BLOCKED_WARN_FROM = 1
BLOCKED_RISK_FROM = 3
#: The metrics for which no events and no "red two weeks" rule are created: a
#: "Investigate: team pace" task with no recipient would investigate nothing, and
#: the per-person signal is on the screen anyway.
_NO_ALERT_METRICS = frozenset({"team_pace"})

_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class MetricDef:
    key: str
    direction: str
    default_target: Decimal


METRICS: tuple[MetricDef, ...] = (
    MetricDef("overdue_tasks", ScorecardDirection.LTE, Decimal("2")),
    MetricDef("finish_drift", ScorecardDirection.LTE, Decimal("0")),
    MetricDef("scope_growth", ScorecardDirection.LTE, Decimal("3")),
    MetricDef("date_shifts", ScorecardDirection.LTE, Decimal("5")),
    MetricDef("close_rate", ScorecardDirection.GTE, Decimal("1.0")),
    MetricDef("stale_in_progress", ScorecardDirection.LTE, Decimal("3")),
    MetricDef("data_quality", ScorecardDirection.GTE, Decimal("90")),
    MetricDef("team_pace", ScorecardDirection.GTE, Decimal("0.8")),
)

METRIC_KEYS: tuple[str, ...] = tuple(m.key for m in METRICS)
_DEFS = {m.key: m for m in METRICS}

#: The headings for the task the rule creates. The dictionary lives here rather
#: than on the client: a task's name lands in the database, and whoever writes it
#: translates it — into the organization's language. The same principle as with
#: mail (app/mail).
_METRIC_LABELS = {
    "overdue_tasks": {"ru": "Просроченные задачи", "en": "Overdue tasks", "az": "Gecikmiş tapşırıqlar"},
    "finish_drift": {"ru": "Сдвиг финиша", "en": "Finish drift", "az": "Finiş sürüşməsi"},
    "scope_growth": {"ru": "Рост объёма", "en": "Scope growth", "az": "Həcm artımı"},
    "date_shifts": {"ru": "Сдвиги дат", "en": "Date shifts", "az": "Tarix sürüşmələri"},
    "close_rate": {"ru": "Закрываемость", "en": "Close rate", "az": "Bağlanma nisbəti"},
    "stale_in_progress": {"ru": "Зависшие в работе", "en": "Stale in progress", "az": "İşdə ilişib qalanlar"},
    "data_quality": {"ru": "Качество данных", "en": "Data quality", "az": "Məlumat keyfiyyəti"},
    "team_pace": {"ru": "Темп команды", "en": "Team pace", "az": "Komandanın tempi"},
}
_RULE_TASK_TITLE = {"ru": "Разобрать: {label}", "en": "Investigate: {label}", "az": "Araşdır: {label}"}


class ScorecardError(Exception):
    """A scorecard refusal — with a machine code, by the mutation refusal rules."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- the week and time --------------------------------------------------------


def project_tz(project: Project, org: Organization) -> ZoneInfo:
    """The project's timezone (inherited from the organization). An unusable name
    yields UTC rather than a crash: the name is validated on write, but the
    database could have been edited by hand, and the scorecard is not the place
    where a project should stop being readable."""
    name = resolve_timezone(project, org)
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        logger.warning("непригодная таймзона %r, скоркард считает в UTC", name)
        return ZoneInfo("UTC")


def week_start_of(d: date) -> date:
    """The Monday of the ISO week the date falls into."""
    return d - timedelta(days=d.weekday())


def _week_bounds_utc(week_start: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The bounds of a week [Monday 00:00; next Monday 00:00) in the project's
    timezone — as absolute time, for comparison with journal timestamps."""
    start = datetime.combine(week_start, time.min, tzinfo=tz)
    return start, start + timedelta(days=7)


def _in_week(stamp: datetime | None, week_start: date, tz: ZoneInfo) -> bool:
    if stamp is None:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return week_start <= stamp.astimezone(tz).date() <= week_start + timedelta(days=6)


# --- metric configs -----------------------------------------------------------


def _metric_keys(db: DbSession, project: Project) -> set[str]:
    return set(
        db.scalars(
            select(ScorecardMetric.metric_key).where(
                ScorecardMetric.project_id == project.id
            )
        ).all()
    )


def ensure_metrics(db: DbSession, project: Project) -> list[ScorecardMetric]:
    """The project's metric configs; missing ones are seeded with defaults.

    The first opening of the scorecard creates them all; a metric that appears in
    a new version of the code is seeded the same way. Owners are empty in the
    seed: a default is a target and a direction, not an assignment of people.
    """
    existing = _metric_keys(db, project)
    if any(definition.key not in existing for definition in METRICS):
        # Seeding writes, and two first readers would both insert the same keys —
        # one of them a unique violation and a 500. The lock is taken only when
        # something is missing, so an ordinary read stays lock-free; the keys are
        # re-read under it, since the other reader may have seeded them meanwhile.
        lock_project(db, project)
        existing = _metric_keys(db, project)
    created = False
    for position, definition in enumerate(METRICS):
        if definition.key in existing:
            continue
        db.add(
            ScorecardMetric(
                project_id=project.id,
                metric_key=definition.key,
                target_value=definition.default_target,
                direction=definition.direction,
                enabled=True,
                position=position,
            )
        )
        created = True
    if created:
        db.flush()
    configs = db.scalars(
        select(ScorecardMetric)
        .where(ScorecardMetric.project_id == project.id)
        .order_by(ScorecardMetric.position, ScorecardMetric.metric_key)
    ).all()
    return [c for c in configs if c.metric_key in _DEFS]


# --- status -------------------------------------------------------------------


def metric_status(value: Decimal | None, target: Decimal, direction: str) -> str:
    """ok — the fact satisfies the target; risk — worse than the threshold; warn — in between."""
    if value is None:
        return ScorecardStatus.NO_DATA.value
    if direction == ScorecardDirection.LTE:
        if value <= target:
            return ScorecardStatus.OK.value
        limit = RISK_LTE_ZERO_LIMIT if target == 0 else target * RISK_LTE_FACTOR
        return (
            ScorecardStatus.RISK.value if value > limit else ScorecardStatus.WARN.value
        )
    if value >= target:
        return ScorecardStatus.OK.value
    return (
        ScorecardStatus.RISK.value
        if value < target * RISK_GTE_FACTOR
        else ScorecardStatus.WARN.value
    )


# --- the computation context --------------------------------------------------


@dataclass(frozen=True)
class _PlanView:
    """The live plan, read once per computation: tasks, finish dates, assignees
    and the calendar. There are no finish dates in the database — they are
    computed here, by the same end_date as on the Gantt chart."""

    tasks: list[Task]
    #: The finish date per the project's calendar; empty for a relative plan and
    #: for tasks with a degenerate calendar.
    ends: dict[uuid.UUID, date]
    assignees: dict[uuid.UUID, list[uuid.UUID]]
    names: dict[uuid.UUID, str]
    calendar: Calendar
    #: Whether the mode is calendar-based. A relative axis is not compared with
    #: "today", and metrics tied to real dates answer no_data.
    dated: bool


def _plan_view(db: DbSession, project: Project, org: Organization) -> _PlanView:
    tasks = list(
        db.scalars(
            select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
        ).all()
    )
    calendar = project_calendar(project, org)
    dated = project.schedule_mode == ScheduleMode.CALENDAR
    ends: dict[uuid.UUID, date] = {}
    if dated:
        for task in tasks:
            try:
                ends[task.id] = end_date(task.start_date, task.duration_days, calendar)
            except CalendarError:
                # A degenerate calendar has already refused on the Gantt chart;
                # the scorecard simply does not judge such a task rather than
                # bringing the whole computation down.
                continue
    rows = db.execute(
        select(TaskAssignee.task_id, TaskAssignee.user_id)
        .join(Task, Task.id == TaskAssignee.task_id)
        .where(Task.project_id == project.id)
    ).all()
    assignees: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task_id, user_id in rows:
        assignees.setdefault(task_id, []).append(user_id)
    user_ids = {user_id for _, user_id in rows}
    names = (
        {
            user.id: user.name
            for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
        }
        if user_ids
        else {}
    )
    return _PlanView(
        tasks=tasks, ends=ends, assignees=assignees, names=names,
        calendar=calendar, dated=dated,
    )


def _entry(plan: _PlanView, task: Task, **extra) -> dict:
    end = plan.ends.get(task.id)
    entry = {
        "id": str(task.id),
        "name": task.name,
        "status": task.status,
        "end_date": end.isoformat() if end else None,
        "assignees": [
            plan.names.get(user_id, "")
            for user_id in plan.assignees.get(task.id, [])
        ],
        # The identifiers next to the names: the per-person breakdown groups by
        # them, and a name may change while the snapshot lies in the chronicle.
        "assignee_ids": [str(user_id) for user_id in plan.assignees.get(task.id, [])],
    }
    entry.update(extra)
    return entry


def _overdue_workdays(plan: _PlanView, end: date, ref: date) -> int:
    """Overdue in working days: the working days after the finish date up to and
    including the computation date."""
    if end >= ref:
        return 0
    return count_working_days(end + timedelta(days=1), ref, plan.calendar)


# --- the metrics --------------------------------------------------------------


def _overdue_list(plan: _PlanView, ref: date) -> list[tuple[Task, int]]:
    found: list[tuple[Task, int]] = []
    for task in plan.tasks:
        end = plan.ends.get(task.id)
        if task.status == TaskStatus.DONE or end is None or end >= ref:
            continue
        found.append((task, _overdue_workdays(plan, end, ref)))
    found.sort(key=lambda pair: pair[1], reverse=True)
    return found


def _compute_overdue(plan: _PlanView, ref: date) -> tuple[Decimal | None, dict]:
    """The number of overdue tasks; the average depth of overdue goes into details.

    The average is rolled in here rather than being a separate metric: it is a
    second view of one fact, and keeping a separate row under it would duplicate
    the signal.
    """
    if not plan.dated:
        return None, {}
    overdue = _overdue_list(plan, ref)
    avg_days = (
        (Decimal(sum(days for _, days in overdue)) / Decimal(len(overdue)))
        .quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        if overdue
        else Decimal("0")
    )
    details = {
        "tasks": [_entry(plan, task, days_overdue=days) for task, days in overdue],
        "avg_days": float(avg_days),
    }
    return Decimal(len(overdue)), details


def _signed_working_days(frm: date, to: date, calendar: Calendar) -> int:
    """Working days between two dates, signed: plus means `to` is later than `frm`.

    The quantity is computed like overdue — working days strictly after the
    earlier date up to and including the later one; the sign is attached by the
    direction.
    """
    if frm == to:
        return 0
    lo, hi = (frm, to) if frm < to else (to, frm)
    magnitude = count_working_days(lo + timedelta(days=1), hi, calendar)
    return magnitude if to > frm else -magnitude


def _projected_finish(plan: _PlanView) -> date | None:
    """The plan's projected finish — the latest finish date among unclosed tasks.
    Empty if there is nothing to compute from (a relative plan, no dates)."""
    ends = [
        plan.ends[task.id]
        for task in plan.tasks
        if task.status != TaskStatus.DONE and task.id in plan.ends
    ]
    return max(ends) if ends else None


def _compute_finish_drift(
    db: DbSession, project: Project, plan: _PlanView, week_start: date
) -> tuple[Decimal | None, dict]:
    """The shift of the projected finish over a week, in signed working days.

    The value is how far the finish has moved against the one recorded in the
    previous week's snapshot: plus means the deadline slid right (bad), minus
    means it was pulled in. With no previous point (the first week, a break in the
    streak) it is no_data: drift with no base is unmeasurable. The projection is
    recorded in details regardless, so that the next week has something to compute
    from. A relative plan has no real dates — no_data.
    """
    if not plan.dated:
        return None, {}
    projected = _projected_finish(plan)
    prev_details = db.scalar(
        select(ScorecardSnapshot.details).where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.metric_key == "finish_drift",
            ScorecardSnapshot.week_start == week_start - timedelta(days=7),
        )
    )
    previous: date | None = None
    if prev_details and prev_details.get("projected_finish"):
        try:
            previous = date.fromisoformat(prev_details["projected_finish"])
        except (ValueError, TypeError):
            previous = None
    details = {
        "projected_finish": projected.isoformat() if projected else None,
        "previous_finish": previous.isoformat() if previous else None,
        "shift_days": None,
    }
    if projected is None or previous is None:
        return None, details
    try:
        shift = _signed_working_days(previous, projected, plan.calendar)
    except CalendarError:
        # A degenerate calendar has already refused on the Gantt chart; drift is simply not judged.
        return None, details
    details["shift_days"] = shift
    return Decimal(shift), details


def _compute_scope(
    db: DbSession, project: Project, plan: _PlanView, week_start: date, tz: ZoneInfo
) -> tuple[Decimal, dict]:
    """The net growth of scope over a week: created minus closed.

    Creation is taken from the journal (`create_task`, excluding restorations by
    undo), closing is the same done as in close_rate. Restoring something deleted
    is not counted as new scope: a deliberate asymmetry with date_shifts, where an
    undo is as much a shift as a direct edit. Tasks inside `create_category` do
    not count here: a fresh category is created empty, and it arrives carrying
    tasks only on an undo of a cascading deletion — which is exactly what we cut
    off by undoes_seq.
    """
    begin, end = _week_bounds_utc(week_start, tz)
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.created_at >= begin,
            Revision.created_at < end,
            Revision.undoes_seq.is_(None),
            Revision.op["type"].astext == "create_task",
        )
        .order_by(Revision.seq)
    ).all()
    tasks_by_id = {task.id: task for task in plan.tasks}
    added: list[dict] = []
    for revision in revisions:
        op = revision.op
        try:
            task_id = uuid.UUID(op["task_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("непригодная запись журнала при счёте объёма")
            continue
        task = tasks_by_id.get(task_id)
        if task is not None:
            added.append(_entry(plan, task, added_in_week=True))
        else:
            # The task was deleted later on — we take the name from the journal so
            # that it stays visible in the drill-down.
            added.append(
                {
                    "id": str(task_id),
                    "name": op.get("name", ""),
                    "status": None,
                    "end_date": None,
                    "assignees": [],
                    "added_in_week": True,
                }
            )
    closed = [
        _entry(plan, task, closed_in_week=True)
        for task in plan.tasks
        if _in_week(task.done_at, week_start, tz)
    ]
    details = {
        "tasks": added + closed,
        "added": added,
        "closed": closed,
        "added_count": len(added),
        "closed_count": len(closed),
    }
    return Decimal(len(added) - len(closed)), details


def _shift_delta(op: dict) -> int:
    """By how many days an operation changed start_date/duration_days."""
    kind = op.get("type")
    try:
        if kind == "move_task":
            return abs(
                (date.fromisoformat(op["to"]) - date.fromisoformat(op["from"])).days
            )
        if kind == "set_duration":
            return abs(int(op["to"]) - int(op["from"]))
        if kind == "resize_task":
            start_delta = abs(
                (
                    date.fromisoformat(op["to"]["start_date"])
                    - date.fromisoformat(op["from"]["start_date"])
                ).days
            )
            duration_delta = abs(
                int(op["to"]["duration_days"]) - int(op["from"]["duration_days"])
            )
            return max(start_delta, duration_delta)
        if kind == "move_category":
            return abs(int(op["days"]))
    except (KeyError, TypeError, ValueError):
        # The journal of old versions may have written the fields differently; an
        # unintelligible entry is not a shift rather than a crash of the scorecard read.
        logger.warning("непригодная запись журнала при счёте сдвигов: %r", kind)
    return 0


def _compute_date_shifts(
    db: DbSession, project: Project, org: Organization, plan: _PlanView,
    week_start: date, tz: ZoneInfo,
) -> tuple[Decimal, dict]:
    """The week's journal operations that moved dates by at least the project's threshold.

    Operations are counted, not tasks: three moves of one task are three shifts.
    An undo is a move too: it changes dates just as a direct operation does.
    """
    threshold = max(resolve_shift_threshold(project, org), 1)
    begin, end = _week_bounds_utc(week_start, tz)
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.created_at >= begin,
            Revision.created_at < end,
            Revision.op["type"].astext.in_(
                ("move_task", "resize_task", "set_duration", "move_category")
            ),
        )
        .order_by(Revision.seq)
    ).all()
    tasks_by_id = {task.id: task for task in plan.tasks}
    count = 0
    entries: list[dict] = []
    for revision in revisions:
        delta = _shift_delta(revision.op)
        if delta < threshold:
            continue
        count += 1
        if revision.op["type"] == "move_category":
            category_id = uuid.UUID(revision.op["category_id"])
            for task in plan.tasks:
                if task.category_id == category_id:
                    entries.append(_entry(plan, task, delta_days=delta))
            continue
        task = tasks_by_id.get(uuid.UUID(revision.op["task_id"]))
        if task is None:
            # The task has been deleted since; the operation counts, but it is not in the list.
            continue
        entries.append(_entry(plan, task, delta_days=delta))
    return Decimal(count), {"tasks": entries, "threshold": threshold}


def _compute_close_rate(
    plan: _PlanView, week_start: date, tz: ZoneInfo
) -> tuple[Decimal | None, dict]:
    """done over the week divided by the tasks with a finish date in that week.

    Nothing was due and nothing was closed — no_data rather than 1.0: a dead week
    without a single movement is not "within norm", there is simply nothing to say
    about it. Closures with an empty denominator give 1.0: a week with no promises
    but with work in it does not fail.
    """
    if not plan.dated:
        return None, {}
    week_end = week_start + timedelta(days=6)
    due = [
        task
        for task in plan.tasks
        if (end := plan.ends.get(task.id)) is not None and week_start <= end <= week_end
    ]
    done = [
        task for task in plan.tasks if _in_week(task.done_at, week_start, tz)
    ]
    details = {
        "tasks": [_entry(plan, task, closed_in_week=True) for task in done]
        + [
            _entry(plan, task, closed_in_week=False)
            for task in due
            if not _in_week(task.done_at, week_start, tz)
        ],
        "done": len(done),
        "due": len(due),
    }
    if not due:
        if not done:
            return None, details
        return Decimal("1.00"), details
    rate = (Decimal(len(done)) / Decimal(len(due))).quantize(
        _TWO_PLACES, rounding=ROUND_HALF_UP
    )
    return rate, details


def _compute_stale(plan: _PlanView, ref: date, tz: ZoneInfo) -> tuple[Decimal, dict]:
    """Tasks in in_progress for longer than STALE_WORKDAYS working days.

    Computed from in_progress_since — the real moment the work was taken up rather
    than from the plan's dates, so it works for a relative project too.
    """
    entries: list[dict] = []
    for task in plan.tasks:
        if task.status != TaskStatus.IN_PROGRESS or task.in_progress_since is None:
            continue
        stamp = task.in_progress_since
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        since = stamp.astimezone(tz).date()
        if since > ref:
            continue
        # Whole working days since it was taken up: the day it was taken up does not count.
        in_progress_days = count_working_days(since, ref, plan.calendar) - 1
        if in_progress_days > STALE_WORKDAYS:
            entries.append(_entry(plan, task, in_progress_days=in_progress_days))
    entries.sort(key=lambda e: e["in_progress_days"], reverse=True)
    return Decimal(len(entries)), {"tasks": entries}


def _date_edited_ids(
    db: DbSession, project: Project
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Tasks and categories whose dates were edited by hand at least once (over
    the project's whole life). For "an unrealistic deadline": a plan nobody has
    touched is not a plan but a stub."""
    ops = db.scalars(
        select(Revision.op).where(
            Revision.project_id == project.id,
            Revision.op["type"].astext.in_(
                ("move_task", "resize_task", "set_duration", "move_category")
            ),
        )
    ).all()
    task_ids: set[uuid.UUID] = set()
    category_ids: set[uuid.UUID] = set()
    for op in ops:
        try:
            if op["type"] == "move_category":
                category_ids.add(uuid.UUID(op["category_id"]))
            else:
                task_ids.add(uuid.UUID(op["task_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return task_ids, category_ids


def _compute_data_quality(
    db: DbSession, project: Project, plan: _PlanView, ref: date
) -> tuple[Decimal, dict]:
    """(1 - unusable/total) x 100%.

    A task is unusable if it has no assignee (milestones do not count — a
    milestone is not performed), or if it has "an unrealistic deadline": overdue
    by more than UNREAL_OVERDUE_WORKDAYS working days with its dates never edited,
    or created by AI with its dates never touched.
    """
    edited_tasks, edited_categories = _date_edited_ids(db, project)

    def dates_edited(task: Task) -> bool:
        return task.id in edited_tasks or task.category_id in edited_categories

    unassigned: list[dict] = []
    unreal: list[dict] = []
    for task in plan.tasks:
        reasons = []
        if not task.milestone and not plan.assignees.get(task.id):
            reasons.append("unassigned")
        end = plan.ends.get(task.id)
        badly_overdue = (
            plan.dated
            and end is not None
            and task.status != TaskStatus.DONE
            and _overdue_workdays(plan, end, ref) > UNREAL_OVERDUE_WORKDAYS
        )
        if (badly_overdue and not dates_edited(task)) or (
            task.created_by_ai_session_id is not None and not dates_edited(task)
        ):
            reasons.append("unreal_deadline")
        if "unassigned" in reasons:
            unassigned.append(_entry(plan, task, reasons=reasons))
        if "unreal_deadline" in reasons:
            unreal.append(_entry(plan, task, reasons=reasons))
    total = len(plan.tasks)
    unassigned_ids = {e["id"] for e in unassigned}
    unreal_ids = {e["id"] for e in unreal}
    both = len(unassigned_ids & unreal_ids)
    # bad is the size of the union: a task with both troubles is bad once.
    bad = len(unassigned_ids | unreal_ids)
    value = (
        Decimal("100.00")
        if total == 0
        else (Decimal(total - bad) * Decimal("100") / Decimal(total)).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )
    )
    # affected and both are returned outward so that the checklist of reasons adds
    # up with the percentage: len(unassigned) + len(unreal) - both == affected.
    details = {
        "total": total,
        "affected": bad,
        "both": both,
        "unassigned": unassigned,
        "unreal_deadline": unreal,
    }
    return value, details



# --- the team's pace ----------------------------------------------------------


def _tz_date(stamp: datetime | None, tz: ZoneInfo) -> date | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(tz).date()


def _end_of_day(day: date, tz: ZoneInfo) -> datetime:
    """The end of the day in the project's timezone — the "before the deadline" boundary for warnings."""
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)


def _status_to(op: dict) -> str | None:
    """Which status an operation moved things to: set_status carries `to`,
    set_progress carries `status_to`, and only when the coupling fired."""
    kind = op.get("type")
    if kind == "set_status":
        return op.get("to")
    if kind == "set_progress":
        return op.get("status_to")
    return None


def _status_from(op: dict) -> str | None:
    kind = op.get("type")
    if kind == "set_status":
        return op.get("from")
    if kind == "set_progress":
        return op.get("status_from")
    return None


def _op_task_id(op: dict) -> uuid.UUID | None:
    try:
        return uuid.UUID(op["task_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _is_warning(op: dict) -> str | None:
    """What an operation used to warn of a risk: a flag or a block. Empty if
    nothing did: a green flag and other transitions are not a warning."""
    if op.get("type") == "set_risk":
        to = op.get("to") or {}
        if isinstance(to, dict) and to.get("risk") in (RiskFlag.YELLOW, RiskFlag.RED):
            return "risk"
        return None
    if _status_to(op) == TaskStatus.BLOCKED:
        return "blocked"
    return None


def _without_undone(revisions: list[Revision]) -> list[Revision]:
    """Entries with no undos and nothing undone: a "did it — undid it" pair
    reports nothing in total, and counting either half of it would mean crediting
    a person with something they themselves took back."""
    undone = {r.undoes_seq for r in revisions if r.undoes_seq is not None}
    return [r for r in revisions if r.undoes_seq is None and r.seq not in undone]


def _warnings_before_deadline(
    db: DbSession, project: Project, ends: dict[uuid.UUID, date], tz: ZoneInfo
) -> dict[uuid.UUID, tuple[datetime, str]]:
    """The first warning about a task before the end of its deadline day: (when, by what).

    A retracted warning is not a warning: neither the undo itself nor the entry it
    undid counts (see _without_undone). One query for all the tasks: the journal is
    indexed by op, and per task this would be an N+1.
    """
    if not ends:
        return {}
    ids = [str(task_id) for task_id in ends]
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.op["type"].astext.in_(("set_risk", "set_status", "set_progress")),
            Revision.op["task_id"].astext.in_(ids),
        )
        .order_by(Revision.seq)
    ).all()
    found: dict[uuid.UUID, tuple[datetime, str]] = {}
    for revision in _without_undone(revisions):
        task_id = _op_task_id(revision.op)
        if task_id is None or task_id in found:
            continue
        kind = _is_warning(revision.op)
        if kind is None:
            continue
        stamp = revision.created_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < _end_of_day(ends[task_id], tz):
            found[task_id] = (stamp, kind)
    return found


def _blocked_since(
    db: DbSession, project: Project, blocked_ids: set[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """When a task last entered `blocked` — per the journal. An undo counts here:
    it changes the status just as a direct operation does."""
    if not blocked_ids:
        return {}
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.op["type"].astext.in_(("set_status", "set_progress")),
            Revision.op["task_id"].astext.in_([str(i) for i in blocked_ids]),
        )
        .order_by(Revision.seq)
    ).all()
    since: dict[uuid.UUID, datetime] = {}
    for revision in revisions:
        if _status_to(revision.op) != TaskStatus.BLOCKED:
            continue
        task_id = _op_task_id(revision.op)
        if task_id is not None:
            since[task_id] = revision.created_at
    return since


def _reopened_in_week(
    db: DbSession, project: Project, week_start: date, tz: ZoneInfo
) -> dict[uuid.UUID, datetime]:
    """Tasks that came back out of "done" during the week: when that last
    happened. Undoing a mistaken "done" is a correction of the record rather than
    work coming back; an undone comeback is not a comeback either (see
    _without_undone). Undos are read beyond the week: an entry from the previous
    week can be undone too."""
    begin, end = _week_bounds_utc(week_start, tz)
    revisions = db.scalars(
        select(Revision)
        .where(
            Revision.project_id == project.id,
            Revision.created_at >= begin,
            Revision.op["type"].astext.in_(("set_status", "set_progress")),
        )
        .order_by(Revision.seq)
    ).all()
    reopened: dict[uuid.UUID, datetime] = {}
    for revision in _without_undone(revisions):
        if revision.created_at >= end:
            continue
        if _status_from(revision.op) != TaskStatus.DONE:
            continue
        if _status_to(revision.op) in (None, TaskStatus.DONE):
            continue
        task_id = _op_task_id(revision.op)
        if task_id is not None:
            reopened[task_id] = revision.created_at
    return reopened


def _person_signal(person: dict) -> tuple[str, dict]:
    """One dot per person and its reason — as a code; the client composes the text.

    Red means a deadline missed with no warning: that is the one thing the PM
    model calls unacceptable. Yellow covers everything known in advance: a block,
    a flag, a warned-about overdue, a comeback from "done". The reasons are ordered
    from the one the conversation will start with.
    """
    if person["overdue_silent"]:
        return "red", {"kind": "overdue_silent", "count": person["overdue_silent"]}
    if person["overdue"]:
        # A missed but warned-about deadline comes before a block and a flag: the
        # conversation about it is already due, while about those it is only ahead.
        return "yellow", {"kind": "overdue_warned", "count": person["overdue"]}
    blocked = person["_blocked"]
    if blocked:
        longest = max(blocked, key=lambda entry: entry["blocked_days"])
        return "yellow", {
            "kind": "blocked",
            "task_id": longest["id"],
            "task": longest["name"],
            "days": longest["blocked_days"],
        }
    flagged = person["_flagged"]
    if flagged:
        worst = flagged[0]
        return "yellow", {"kind": "risk_flag", "task_id": worst["id"], "task": worst["name"], "risk": worst["risk"]}
    reopened = person["_reopened"]
    if reopened:
        return "yellow", {"kind": "reopened", "task_id": reopened[0]["id"], "task": reopened[0]["name"]}
    return "green", {"kind": "in_pace"}


def _compute_team_pace(
    db: DbSession, project: Project, plan: _PlanView, week_start: date, ref: date,
    tz: ZoneInfo,
) -> tuple[Decimal | None, dict]:
    """The week's pace by person: done out of planned, beyond the plan, on time,
    and the signal with its reason.

    "Planned" means tasks with a deadline in this week: that is the week's
    promise, taken from the live dates (an explicit confirmation by the assignee is
    the next step). "Beyond" means closed during the week even though the deadline
    stood outside it. A deadline counts as missed once it has passed by the
    computation date; as warned about if the journal knows of a flag or a block
    before the end of the deadline day.

    A task with several assignees counts for each of them — otherwise one of them
    would report work two people were responsible for. With no assignee it goes
    into a separate bucket: it is visible as a counter rather than as a row.
    """
    if not plan.dated:
        return None, {}
    week_end = week_start + timedelta(days=6)
    tasks_by_id = {task.id: task for task in plan.tasks}

    planned = {
        task.id for task in plan.tasks
        if (end := plan.ends.get(task.id)) is not None and week_start <= end <= week_end
    }
    # Missed: the deadline has passed by the computation date and the task is not
    # done. Done late is not missed but done; its lateness is in late_days.
    overdue_ids = {
        task_id for task_id in planned
        if plan.ends[task_id] <= ref and tasks_by_id[task_id].status != TaskStatus.DONE
    }
    warnings = _warnings_before_deadline(
        db, project, {task_id: plan.ends[task_id] for task_id in overdue_ids}, tz
    )
    blocked_ids = {task.id for task in plan.tasks if task.status == TaskStatus.BLOCKED}
    blocked_since = _blocked_since(db, project, blocked_ids)
    reopened = _reopened_in_week(db, project, week_start, tz)

    def blocked_days(task_id: uuid.UUID) -> int:
        since = _tz_date(blocked_since.get(task_id), tz)
        if since is None or since > ref:
            return 0
        # Whole working days: the day the block started does not count, as with stale.
        return max(count_working_days(since, ref, plan.calendar) - 1, 0)

    def describe(task: Task) -> dict:
        end = plan.ends.get(task.id)
        done_day = _tz_date(task.done_at, tz)
        entry = _entry(plan, task, risk=task.risk, due=end.isoformat() if end else None)
        if task.status == TaskStatus.DONE and done_day is not None:
            late = _overdue_workdays(plan, end, done_day) if end is not None else 0
            entry.update(state="done", late_days=late)
        elif task.id in overdue_ids:
            warning = warnings.get(task.id)
            entry.update(
                state="late",
                late_days=_overdue_workdays(plan, end, ref),
                warned=warning is not None,
                warned_kind=warning[1] if warning else None,
                warned_at=warning[0].isoformat() if warning else None,
            )
        elif task.status == TaskStatus.BLOCKED:
            entry.update(state="blocked", blocked_days=blocked_days(task.id))
        elif task.risk in (RiskFlag.YELLOW, RiskFlag.RED):
            entry.update(state="risk")
        elif task.status == TaskStatus.IN_PROGRESS:
            entry.update(state="progress")
        else:
            entry.update(state="planned")
        if task.id in reopened:
            entry["reopened"] = True
        return entry

    def fresh_person(user_id: uuid.UUID | None) -> dict:
        return {
            "user_id": str(user_id) if user_id else None,
            "name": plan.names.get(user_id, "") if user_id else "",
            "planned": 0, "done": 0, "extra": 0, "on_time": 0,
            "overdue": 0, "overdue_silent": 0,
            "blocked": 0, "flagged": 0, "reopened": 0,
            "tasks": [],
            "_blocked": [], "_flagged": [], "_reopened": [],
        }

    people: dict[uuid.UUID | None, dict] = {}
    for user_ids in plan.assignees.values():
        for user_id in user_ids:
            people.setdefault(user_id, fresh_person(user_id))

    # The week's tasks: planned, beyond the plan, blocked, flagged, returned. The
    # rest live outside the week and do not reach a person's row.
    relevant: set[uuid.UUID] = set(planned)
    for task in plan.tasks:
        done_day = _tz_date(task.done_at, tz)
        if (
            task.status == TaskStatus.DONE and done_day is not None
            and week_start <= done_day <= week_end and task.id not in planned
        ):
            relevant.add(task.id)
        if task.status == TaskStatus.BLOCKED or task.id in reopened:
            relevant.add(task.id)
        if task.status != TaskStatus.DONE and task.risk in (RiskFlag.YELLOW, RiskFlag.RED):
            relevant.add(task.id)

    total_planned = total_done = total_extra = total_on_time = 0
    for task in plan.tasks:
        if task.id not in relevant:
            continue
        entry = describe(task)
        owners = plan.assignees.get(task.id) or [None]
        is_planned = task.id in planned
        done_day = _tz_date(task.done_at, tz)
        is_done = task.status == TaskStatus.DONE and done_day is not None and done_day <= week_end
        is_extra = is_done and not is_planned and week_start <= done_day
        on_time = is_done and is_planned and done_day <= plan.ends[task.id]
        if is_planned:
            total_planned += 1
            if is_done:
                total_done += 1
                if on_time:
                    total_on_time += 1
        if is_extra:
            total_extra += 1
        for user_id in owners:
            person = people.setdefault(user_id, fresh_person(user_id))
            person["tasks"].append(entry)
            if is_planned:
                person["planned"] += 1
                if is_done:
                    person["done"] += 1
                    if on_time:
                        person["on_time"] += 1
            if is_extra:
                person["extra"] += 1
            if entry["state"] == "late":
                person["overdue"] += 1
                if not entry["warned"]:
                    person["overdue_silent"] += 1
            if entry["state"] == "blocked":
                person["blocked"] += 1
                person["_blocked"].append(entry)
            if task.status != TaskStatus.DONE and task.risk in (RiskFlag.YELLOW, RiskFlag.RED):
                person["flagged"] += 1
                person["_flagged"].append(entry)
            if task.id in reopened:
                person["reopened"] += 1
                person["_reopened"].append(entry)

    by_person: list[dict] = []
    unassigned: dict | None = None
    for user_id, person in people.items():
        # A warned-about overdue is not "silent": the signal tells them apart.
        person["overdue"] -= person["overdue_silent"]
        person["signal"], person["reason"] = _person_signal(person)
        person["tasks"].sort(key=lambda e: (e.get("due") or "", e["name"]))
        for key in ("_blocked", "_flagged", "_reopened"):
            person.pop(key)
        if user_id is None:
            unassigned = person
        else:
            by_person.append(person)
    by_person.sort(key=lambda person: person["name"])

    blocked_entries = [
        {"id": str(task.id), "name": task.name, "days": blocked_days(task.id)}
        for task in plan.tasks if task.id in blocked_ids
    ]
    longest = max(blocked_entries, key=lambda e: e["days"], default=None)
    details = {
        "planned": total_planned,
        "done": total_done,
        "extra": total_extra,
        "on_time": total_on_time,
        "by_person": by_person,
        "unassigned": unassigned,
        "blocked_count": len(blocked_entries),
        "blocked_longest": longest,
    }
    if total_planned == 0:
        return None, details
    rate = (Decimal(total_done) / Decimal(total_planned)).quantize(
        _TWO_PLACES, rounding=ROUND_HALF_UP
    )
    return rate, details


def _compute_metric(
    db: DbSession, project: Project, org: Organization, plan: _PlanView,
    key: str, week_start: date, ref: date, tz: ZoneInfo,
) -> tuple[Decimal | None, dict]:
    """A metric's value and drill-down for a week. `ref` is the computation date:
    today for the current week, the week's Sunday when appending a missed one."""
    if key == "overdue_tasks":
        return _compute_overdue(plan, ref)
    if key == "finish_drift":
        return _compute_finish_drift(db, project, plan, week_start)
    if key == "scope_growth":
        return _compute_scope(db, project, plan, week_start, tz)
    if key == "date_shifts":
        return _compute_date_shifts(db, project, org, plan, week_start, tz)
    if key == "close_rate":
        return _compute_close_rate(plan, week_start, tz)
    if key == "stale_in_progress":
        return _compute_stale(plan, ref, tz)
    if key == "data_quality":
        return _compute_data_quality(db, project, plan, ref)
    if key == "team_pace":
        return _compute_team_pace(db, project, plan, week_start, ref, tz)
    # An unknown metric: there is no source.
    return None, {}


# --- snapshots and committing -------------------------------------------------


def _week_rows(
    db: DbSession, project: Project, week_start: date
) -> dict[str, ScorecardSnapshot]:
    rows = db.scalars(
        select(ScorecardSnapshot).where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.week_start == week_start,
        )
    ).all()
    return {row.metric_key: row for row in rows}


def _lock_project(db: DbSession, project: Project) -> None:
    """Serializes the writing of snapshots: two concurrent GETs at a week boundary
    would otherwise collide on the unique constraint with a 500. The same technique
    and the same lock as in the mutation layer."""
    lock_project(db, project)


def _compute_week_values(
    db: DbSession, project: Project, org: Organization, plan: _PlanView,
    configs: list[ScorecardMetric], week_start: date, ref: date, tz: ZoneInfo,
) -> dict[str, tuple[Decimal | None, str, dict]]:
    """(value, status, details) for each of the week's metrics. A disabled metric
    is not computed at all: its answer is no_data, and there is no reason to spend
    the journal on it."""
    computed: dict[str, tuple[Decimal | None, str, dict]] = {}
    for config in configs:
        if not config.enabled:
            computed[config.metric_key] = (None, ScorecardStatus.NO_DATA.value, {})
            continue
        value, details = _compute_metric(
            db, project, org, plan, config.metric_key, week_start, ref, tz
        )
        status = metric_status(value, config.target_value, config.direction)
        computed[config.metric_key] = (value, status, details)
    return computed


def _backfill_missing_weeks(
    db: DbSession, project: Project, org: Organization,
    configs: list[ScorecardMetric], plan: _PlanView,
    current_week: date, tz: ZoneInfo,
) -> None:
    """Appends the missing weekly snapshots between the last one recorded and the
    current week.

    Journal metrics (date_shifts, close_rate, scope_growth) are computed exactly
    from that week's journal; state cross-sections cannot be reconstructed — they
    are computed from the current state as of that week's Sunday and marked
    `backfilled: true`. finish_drift is the difference between neighbouring
    snapshots, and a missed week has no point of reference, so it is written as
    no_data+backfilled rather than reconstructed. Weeks recorded earlier are left
    alone: snapshots of past weeks are immutable.
    """
    last = db.scalar(
        select(ScorecardSnapshot.week_start)
        .where(ScorecardSnapshot.project_id == project.id)
        .order_by(ScorecardSnapshot.week_start.desc())
        .limit(1)
    )
    if last is None or last >= current_week - timedelta(days=7):
        return
    week = last + timedelta(days=7)
    while week < current_week:
        existing = _week_rows(db, project, week)
        ref = week + timedelta(days=6)
        computed = _compute_week_values(db, project, org, plan, configs, week, ref, tz)
        for config in configs:
            if config.metric_key in existing:
                continue
            value, status, details = computed[config.metric_key]
            if config.metric_key == "finish_drift":
                # A missed week has no base for drift — no_data, and with no
                # projected_finish, so that the next week does not latch onto a
                # projection taken from today's plan.
                value, status, details = (
                    None, ScorecardStatus.NO_DATA.value, {"backfilled": True},
                )
            elif config.metric_key not in ("date_shifts", "close_rate", "scope_growth"):
                details = details | {"backfilled": True}
            db.add(
                ScorecardSnapshot(
                    project_id=project.id,
                    metric_key=config.metric_key,
                    week_start=week,
                    value=value,
                    target_value=config.target_value,
                    direction=config.direction,
                    status=status,
                    computed_at=datetime.now(timezone.utc),
                    computed_by=None,
                    details=details,
                )
            )
        # A flush on every week rather than at the end: week N's snapshot must be
        # visible when week N+1 is computed (finish_drift reads the previous one).
        db.flush()
        week += timedelta(days=7)


def _upsert_current_week(
    db: DbSession, project: Project, configs: list[ScorecardMetric],
    computed: dict[str, tuple[Decimal | None, str, dict]],
    current_week: date, actor_id: uuid.UUID | None,
) -> dict[str, ScorecardSnapshot]:
    rows = _week_rows(db, project, current_week)
    now = datetime.now(timezone.utc)
    for config in configs:
        value, status, details = computed[config.metric_key]
        row = rows.get(config.metric_key)
        if row is None:
            row = ScorecardSnapshot(
                project_id=project.id,
                metric_key=config.metric_key,
                week_start=current_week,
            )
            db.add(row)
            rows[config.metric_key] = row
        row.value = value
        row.target_value = config.target_value
        row.direction = config.direction
        row.status = status
        row.details = details
        row.computed_at = now
        row.computed_by = actor_id
    db.flush()
    return rows


# --- events and the rule ------------------------------------------------------


def _org_locale(org: Organization) -> str:
    return org.default_locale if org.default_locale in ("ru", "en", "az") else "az"


def _last_working_day(week_start: date, calendar: Calendar) -> date:
    """The last working day of the week — the deadline for the "Investigate" task.
    A week with no working days yields Sunday: a worse deadline, but the task matters more."""
    day = week_start + timedelta(days=6)
    while day > week_start and not calendar.is_working(day):
        day -= timedelta(days=1)
    return day


def _rule_task_start(plan: _PlanView, current_week: date) -> date:
    if plan.dated:
        return _last_working_day(current_week, plan.calendar)
    # A relative plan has no real dates — the task is placed at the end of the
    # relative axis, where it will be seen first.
    from app.schedule import RELATIVE_EPOCH

    return max((task.start_date for task in plan.tasks), default=RELATIVE_EPOCH)


def _create_rule_task(
    db: DbSession, project: Project, org: Organization, plan: _PlanView,
    config: ScorecardMetric, current_week: date,
) -> Task | None:
    """An "Investigate: {metric}" task through the mutation layer — with a journal entry and an undo.

    The assignee is the metric's owner, the deadline is the end of the week, the
    category is the project's first. A project with no categories has nowhere to
    put the task — the rule is silently skipped until the first category appears
    (there is no event either: an empty event with a link to nowhere would say
    nothing).
    """
    category_id = db.scalar(
        select(Category.id)
        .where(Category.project_id == project.id)
        .order_by(Category.position, Category.id)
        .limit(1)
    )
    if category_id is None:
        logger.warning(
            "правило скоркарда: в проекте %s нет категорий, задача не создана",
            project.id,
        )
        return None
    locale = _org_locale(org)
    label = _METRIC_LABELS.get(config.metric_key, {}).get(locale, config.metric_key)
    title = _RULE_TASK_TITLE[locale].format(label=label)
    start = _rule_task_start(plan, current_week)
    batch_id = uuid.uuid4()
    try:
        revision = apply_op(
            db,
            project,
            CreateTask(
                category_id=category_id, name=title, start_date=start, duration_days=1
            ),
            actor_id=None,
            batch_id=batch_id,
        )
    except MutationError as error:
        # A ceiling on tasks, a degenerate calendar — the rule is not entitled to
        # bring down a scorecard read; the trace stays in the application log.
        logger.warning("правило скоркарда: задача не создана (%s)", error.code)
        return None
    task_id = uuid.UUID(revision.op["task_id"])
    if config.owner_user_id is not None:
        try:
            apply_op(
                db,
                project,
                AssignUser(task_id=task_id, user_id=config.owner_user_id),
                actor_id=None,
                batch_id=batch_id,
            )
        except InvalidOperation:
            # The owner has left the organization in the meantime — the task stays
            # unassigned, just as the metric stays without an owner.
            logger.warning("правило скоркарда: владелец метрики вне организации")
    return db.get(Task, task_id)


def _risk_series_start(
    statuses: dict[date, str], current_week: date
) -> date:
    """The Monday of the first week of an unbroken red streak, the current one included."""
    week = current_week
    while statuses.get(week - timedelta(days=7)) == ScorecardStatus.RISK.value:
        week -= timedelta(days=7)
        if current_week - week > timedelta(weeks=MAX_STREAK_WEEKS):
            break
    return week


def _alert_task_source(metric_key: str, details: dict) -> list[dict]:
    """The list of tasks under an alert, depending on the metric: quality hits two
    sets (we merge them without duplicates), scope hits the added ones, and the
    rest use the common drill-down list."""
    if metric_key == "data_quality":
        merged: dict[str, dict] = {}
        for entry in details.get("unassigned", []) + details.get("unreal_deadline", []):
            merged.setdefault(entry["id"], entry)
        return list(merged.values())
    if metric_key == "scope_growth":
        return details.get("added", [])
    return details.get("tasks") or []


def _alert_payload(
    config: ScorecardMetric,
    value: Decimal | None,
    details: dict,
    previous_value: Decimal | None,
) -> dict:
    """The event's payload: the value, the delta against the previous week, who is
    dragging it down and the top 3 tasks. The delta and the addressee turn "X is at
    risk" from a statement into a hint about what exactly to investigate and with whom."""
    payload: dict = {"value": float(value) if value is not None else None}
    if value is not None and previous_value is not None:
        payload["delta"] = float(value - previous_value)
    else:
        payload["delta"] = None

    tasks = _alert_task_source(config.metric_key, details)
    payload["total"] = len(tasks)
    payload["tasks"] = [
        {
            "id": entry["id"],
            "name": entry["name"],
            "assignee": (entry.get("assignees") or [None])[0],
            **({"days_overdue": entry["days_overdue"]} if "days_overdue" in entry else {}),
        }
        for entry in tasks[:3]
    ]

    # The most frequent first assignee across the whole list — "who has the most of it".
    counts: dict[str, int] = {}
    for entry in tasks:
        name = (entry.get("assignees") or [None])[0]
        if name:
            counts[name] = counts.get(name, 0) + 1
    if counts:
        top = max(counts.items(), key=lambda pair: pair[1])
        payload["top_assignee"] = {"name": top[0], "count": top[1]}
    else:
        payload["top_assignee"] = None
    return payload


def _update_alerts(
    db: DbSession, project: Project, org: Organization, plan: _PlanView,
    configs: list[ScorecardMetric],
    computed: dict[str, tuple[Decimal | None, str, dict]],
    current_week: date,
) -> None:
    """The lifecycle of events when the current week's snapshot is written.

    metric_risk lives while the metric is red; the "red two weeks running" rule
    creates a task once per streak — repetition is suppressed by the
    rule_triggered already recorded for that streak, and a broken streak closes it.
    """
    alerts = db.scalars(
        select(ScorecardAlert).where(ScorecardAlert.project_id == project.id)
    ).all()
    by_metric: dict[str, list[ScorecardAlert]] = {}
    for alert in alerts:
        by_metric.setdefault(alert.metric_key, []).append(alert)

    history = db.scalars(
        select(ScorecardSnapshot).where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.week_start < current_week,
        )
    ).all()
    statuses_by_metric: dict[str, dict[date, str]] = {}
    values_by_metric: dict[str, dict[date, Decimal | None]] = {}
    for row in history:
        statuses_by_metric.setdefault(row.metric_key, {})[row.week_start] = row.status
        values_by_metric.setdefault(row.metric_key, {})[row.week_start] = row.value

    now = datetime.now(timezone.utc)
    previous_week = current_week - timedelta(days=7)
    for config in configs:
        if config.metric_key in _NO_ALERT_METRICS:
            continue
        value, status, details = computed[config.metric_key]
        previous_value = values_by_metric.get(config.metric_key, {}).get(previous_week)
        own = by_metric.get(config.metric_key, [])
        active_risk = [
            a
            for a in own
            if a.kind == ScorecardAlertKind.METRIC_RISK and a.resolved_at is None
        ]
        active_rule = [
            a
            for a in own
            if a.kind == ScorecardAlertKind.RULE_TRIGGERED and a.resolved_at is None
        ]
        if status != ScorecardStatus.RISK.value:
            for alert in active_risk + active_rule:
                alert.resolved_at = now
            continue

        payload = _alert_payload(config, value, details, previous_value)
        if active_risk:
            # The event is already open — only the payload is refreshed: the week
            # the risk began and the creation time stay as they were.
            active_risk[0].payload = payload
        else:
            db.add(
                ScorecardAlert(
                    project_id=project.id,
                    metric_key=config.metric_key,
                    week_start=current_week,
                    kind=ScorecardAlertKind.METRIC_RISK.value,
                    payload=payload,
                )
            )

        statuses = statuses_by_metric.get(config.metric_key, {})
        previous_risk = (
            statuses.get(current_week - timedelta(days=7))
            == ScorecardStatus.RISK.value
        )
        if not previous_risk:
            continue
        series_start = _risk_series_start(statuses, current_week)
        already_fired = any(
            a.kind == ScorecardAlertKind.RULE_TRIGGERED
            and a.week_start >= series_start
            for a in own
        )
        if already_fired:
            continue
        task = _create_rule_task(db, project, org, plan, config, current_week)
        if task is None:
            continue
        db.add(
            ScorecardAlert(
                project_id=project.id,
                metric_key=config.metric_key,
                week_start=current_week,
                kind=ScorecardAlertKind.RULE_TRIGGERED.value,
                payload={"task_id": str(task.id), "task_name": task.name},
            )
        )
    db.flush()


# --- reading the state --------------------------------------------------------


def _streak(statuses: dict[date, str], current_week: date, status: str) -> int:
    """Weeks in a row (the current one included) in the current status. Computed
    from the snapshots on a read rather than stored: a stored streak would diverge
    from the chronicle on the very first recomputation of the current week."""
    if status == ScorecardStatus.NO_DATA.value:
        return 0
    streak = 1
    week = current_week - timedelta(days=7)
    while statuses.get(week) == status and streak < MAX_STREAK_WEEKS:
        streak += 1
        week -= timedelta(days=7)
    return streak


def _owner_names(db: DbSession, configs: list[ScorecardMetric]) -> dict[uuid.UUID, str]:
    ids = {c.owner_user_id for c in configs if c.owner_user_id is not None}
    if not ids:
        return {}
    return {
        user.id: user.name
        for user in db.scalars(select(User).where(User.id.in_(ids))).all()
    }


def _build_outlook(
    db: DbSession, project: Project,
    current_rows: dict[str, ScorecardSnapshot], today: date,
) -> dict:
    """The finish projection and the nearest milestone for the scorecard's header.

    Cheap on a cache hit: the finish is taken from the finish_drift snapshot, the
    milestone with one query. The nearest one is the first unclosed milestone from
    today forward; if there is nothing ahead, we show the last overdue one. A
    relative plan has no real dates — the outlook is empty.
    """
    if project.schedule_mode != ScheduleMode.CALENDAR:
        return {"projected_finish": None, "milestone": None}
    finish_row = current_rows.get("finish_drift")
    projected = None
    if finish_row is not None and finish_row.details:
        projected = finish_row.details.get("projected_finish")

    milestones = db.scalars(
        select(Task).where(
            Task.project_id == project.id,
            Task.milestone.is_(True),
            Task.status != TaskStatus.DONE,
        )
    ).all()
    chosen: Task | None = None
    status = None
    upcoming = sorted(
        (t for t in milestones if t.start_date >= today), key=lambda t: t.start_date
    )
    if upcoming:
        chosen, status = upcoming[0], "upcoming"
    else:
        overdue = sorted(
            (t for t in milestones if t.start_date < today),
            key=lambda t: t.start_date,
            reverse=True,
        )
        if overdue:
            chosen, status = overdue[0], "overdue"
    milestone = (
        {
            "id": str(chosen.id),
            "name": chosen.name,
            "date": chosen.start_date.isoformat(),
            "status": status,
        }
        if chosen is not None
        else None
    )
    return {"projected_finish": projected, "milestone": milestone}


def _row_value(row: ScorecardSnapshot | None) -> float | None:
    return float(row.value) if row is not None and row.value is not None else None


def _row_status(row: ScorecardSnapshot | None) -> str:
    return row.status if row is not None else ScorecardStatus.NO_DATA.value


def _blocked_status(count: int) -> str:
    if count >= BLOCKED_RISK_FROM:
        return ScorecardStatus.RISK.value
    if count >= BLOCKED_WARN_FROM:
        return ScorecardStatus.WARN.value
    return ScorecardStatus.OK.value


def _build_summary(current_rows: dict[str, ScorecardSnapshot]) -> dict:
    """The three numbers of the header and the week's counter — from the already
    recorded snapshots of the current week, with no second computation."""
    pace = current_rows.get("team_pace")
    pace_details = (pace.details or {}) if pace is not None else {}
    overdue = current_rows.get("overdue_tasks")
    overdue_details = (overdue.details or {}) if overdue is not None else {}
    drift = current_rows.get("finish_drift")
    drift_details = (drift.details or {}) if drift is not None else {}
    blocked_count = int(pace_details.get("blocked_count", 0))
    return {
        "planned": int(pace_details.get("planned", 0)),
        "done": int(pace_details.get("done", 0)),
        "overdue": {
            "value": _row_value(overdue),
            "status": _row_status(overdue),
            "avg_days": overdue_details.get("avg_days"),
        },
        "blocked": {
            "value": blocked_count,
            "status": _blocked_status(blocked_count),
            "longest": pace_details.get("blocked_longest"),
        },
        "finish_drift": {
            "value": _row_value(drift),
            "status": _row_status(drift),
            "projected_finish": drift_details.get("projected_finish"),
        },
    }


def _build_team(
    rows: dict[date, ScorecardSnapshot], current_week: date, *, assessment: bool
) -> dict:
    """The per-person rows with a trend from the chronicle. The signal and its
    reason stay in the answer only under the assessment permission — they are cut
    out here, on the server, rather than hidden by the client."""
    current = rows.get(current_week)
    details = (current.details or {}) if current is not None else {}
    weeks = [current_week - timedelta(days=7 * i) for i in range(TREND_WEEKS - 1, -1, -1)]
    closed_by_week: dict[date, dict[str | None, int]] = {}
    for week in weeks:
        row = rows.get(week)
        if row is None or not row.details:
            continue
        per_user: dict[str | None, int] = {}
        for person in row.details.get("by_person", []):
            per_user[person.get("user_id")] = int(person.get("done", 0)) + int(person.get("extra", 0))
        closed_by_week[week] = per_user

    members: list[dict] = []
    for person in details.get("by_person", []):
        member = {
            "user": {"id": person["user_id"], "name": person.get("name", "")},
            "planned": person.get("planned", 0),
            "done": person.get("done", 0),
            "extra": person.get("extra", 0),
            "on_time": person.get("on_time", 0),
            "trend": [
                {
                    "week_start": week.isoformat(),
                    "closed": closed_by_week[week].get(person["user_id"]) if week in closed_by_week else None,
                }
                for week in weeks
            ],
            "tasks": person.get("tasks", []),
        }
        if assessment:
            member["signal"] = person.get("signal")
            member["reason"] = person.get("reason")
        members.append(member)
    unassigned = details.get("unassigned") or {}
    return {
        "assessment": assessment,
        "members": members,
        "unassigned_planned": int(unassigned.get("planned", 0)),
    }


#: The details fields that are hoisted straight into the metric's row (rather
#: than into the drill-down): a second view without which the value reads by halves.
_ROW_DETAIL_FIELDS = {
    "overdue_tasks": ("avg_days",),
    "scope_growth": ("added_count", "closed_count"),
}


def _build_state(
    db: DbSession, project: Project, configs: list[ScorecardMetric],
    current_rows: dict[str, ScorecardSnapshot], current_week: date, weeks: int,
    today: date, *, include_team: bool = False, include_assessment: bool = False,
) -> dict:
    weeks = max(1, weeks)
    horizon = current_week - timedelta(days=7 * (weeks - 1))
    # Read deeper than the window: a streak is counted beyond the sparkline.
    floor = current_week - timedelta(days=7 * MAX_STREAK_WEEKS)
    rows = db.scalars(
        select(ScorecardSnapshot)
        .where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.week_start >= floor,
        )
        .order_by(ScorecardSnapshot.week_start)
    ).all()
    by_metric: dict[str, dict[date, ScorecardSnapshot]] = {}
    for row in rows:
        by_metric.setdefault(row.metric_key, {})[row.week_start] = row

    owners = _owner_names(db, configs)
    metrics: list[dict] = []
    for config in configs:
        own = by_metric.get(config.metric_key, {})
        current = own.get(current_week)
        status = current.status if current else ScorecardStatus.NO_DATA.value
        statuses = {week: row.status for week, row in own.items()}
        history = [
            {
                "week_start": week.isoformat(),
                "value": float(row.value) if row.value is not None else None,
                "status": row.status,
            }
            for week, row in sorted(own.items())
            if horizon <= week <= current_week
        ]
        owner = None
        if config.owner_user_id is not None:
            owner = {
                "id": str(config.owner_user_id),
                "name": owners.get(config.owner_user_id, ""),
            }
        entry = {
            "key": config.metric_key,
            "direction": config.direction,
            "target": float(config.target_value),
            "enabled": config.enabled,
            "owner": owner,
            "value": (
                float(current.value)
                if current is not None and current.value is not None
                else None
            ),
            "status": status,
            "streak": _streak(statuses, current_week, status),
            "history": history,
        }
        # The metric's second view goes straight into the row: the average overdue,
        # the breakdown of scope. `.get` rather than an index: a snapshot from an
        # old TTL window after a deploy may not carry the new fields yet.
        current_details = current.details or {} if current is not None else {}
        for field in _ROW_DETAIL_FIELDS.get(config.metric_key, ()):
            entry[field] = current_details.get(field)
        metrics.append(entry)

    alerts = db.scalars(
        select(ScorecardAlert)
        .where(
            ScorecardAlert.project_id == project.id,
            ScorecardAlert.resolved_at.is_(None),
            # Otherwise the event of a removed metric would hang forever:
            # _update_alerts walks only the current configs and would not close it.
            ScorecardAlert.metric_key.in_(METRIC_KEYS),
        )
        .order_by(ScorecardAlert.created_at.desc())
    ).all()
    alerts_out = [
        {
            "id": str(alert.id),
            "metric_key": alert.metric_key,
            "kind": alert.kind,
            "week_start": alert.week_start.isoformat(),
            "created_at": alert.created_at.isoformat(),
            "payload": alert.payload,
        }
        for alert in alerts
    ]

    quality_row = current_rows.get("data_quality")
    data_quality = None
    if quality_row is not None and quality_row.value is not None:
        details = quality_row.details or {}
        unassigned = len(details.get("unassigned", []))
        unreal = len(details.get("unreal_deadline", []))
        # affected/both come from details; if a snapshot from an old TTL window does
        # not carry them, we reconstruct them from the two lists (both is not there
        # — we estimate it as 0).
        data_quality = {
            "value": float(quality_row.value),
            "total": details.get("total", 0),
            "affected": details.get("affected", unassigned + unreal),
            "both": details.get("both", 0),
            "unassigned": unassigned,
            "unreal_deadline": unreal,
        }

    computed_stamps = [r.computed_at for r in current_rows.values() if r.computed_at]
    return {
        "week": {
            "number": current_week.isocalendar()[1],
            "start": current_week.isoformat(),
            "end": (current_week + timedelta(days=6)).isoformat(),
        },
        "computed_at": max(computed_stamps).isoformat() if computed_stamps else None,
        "metrics": metrics,
        "alerts": alerts_out,
        "outlook": _build_outlook(db, project, current_rows, today),
        "data_quality": data_quality,
        "summary": _build_summary(current_rows),
        "team": (
            _build_team(
                by_metric.get("team_pace", {}), current_week,
                assessment=include_assessment,
            )
            if include_team
            else None
        ),
    }


def scorecard_state(
    db: DbSession,
    project: Project,
    org: Organization,
    *,
    weeks: int = DEFAULT_WEEKS,
    actor_id: uuid.UUID | None = None,
    force: bool = False,
    include_team: bool = False,
    include_assessment: bool = False,
) -> dict:
    """The scorecard's state — with the side effect of lazy committing.

    `include_team` / `include_assessment` are the reader's permissions (see
    access.py): the per-person breakdown and the signal about them are returned
    only to those entitled to them, and the route makes that decision, not the
    client.

    The current week is computed live with a five-minute cache: the cache is that
    same week's snapshot, so it survives a restart and is shared by all replicas,
    unlike process memory. `force` (the "Recalculate" button) skips the cache; past
    weeks are never recomputed.
    """
    tz = project_tz(project, org)
    today = datetime.now(tz).date()
    current_week = week_start_of(today)
    configs = ensure_metrics(db, project)

    current_rows = _week_rows(db, project, current_week)
    now = datetime.now(timezone.utc)
    fresh = (
        not force
        and all(c.metric_key in current_rows for c in configs)
        and all(
            row.computed_at is not None
            and row.computed_at >= now - timedelta(seconds=CURRENT_WEEK_TTL_SECONDS)
            for row in current_rows.values()
        )
    )
    last_before = db.scalar(
        select(ScorecardSnapshot.week_start)
        .where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.week_start < current_week,
        )
        .order_by(ScorecardSnapshot.week_start.desc())
        .limit(1)
    )
    gap = last_before is not None and last_before < current_week - timedelta(days=7)

    if force or gap or not fresh:
        # A lock on the project's row: concurrent GETs at a week boundary would
        # otherwise collide on the snapshots' unique constraint.
        _lock_project(db, project)
        plan = _plan_view(db, project, org)
        _backfill_missing_weeks(db, project, org, configs, plan, current_week, tz)
        computed = _compute_week_values(
            db, project, org, plan, configs, current_week, today, tz
        )
        current_rows = _upsert_current_week(
            db, project, configs, computed, current_week, actor_id
        )
        _update_alerts(db, project, org, plan, configs, computed, current_week)

    return _build_state(
        db, project, configs, current_rows, current_week, weeks, today,
        include_team=include_team, include_assessment=include_assessment,
    )


def patch_metric(
    db: DbSession, project: Project, org: Organization, key: str, changes: dict
) -> None:
    """Editing a metric's config: owner, target, enabled state.

    The direction is not editable — it follows rigidly from the metric's key. The
    owner is checked for membership in the organization: someone else's identifier
    must neither land in the config nor confirm an account's existence by its
    refusal.
    """
    if key not in _DEFS:
        raise ScorecardError("metric_not_found", f"неизвестная метрика: {key}")
    ensure_metrics(db, project)
    config = db.scalar(
        select(ScorecardMetric).where(
            ScorecardMetric.project_id == project.id,
            ScorecardMetric.metric_key == key,
        )
    )
    if "owner_user_id" in changes:
        owner_id = changes["owner_user_id"]
        if owner_id is not None:
            member = db.scalar(
                select(Membership.id).where(
                    Membership.org_id == project.org_id,
                    Membership.user_id == owner_id,
                )
            )
            if member is None:
                raise ScorecardError(
                    "user_not_in_organization", "владелец метрики не в этой организации"
                )
        config.owner_user_id = owner_id
    if "target_value" in changes:
        target = Decimal(str(changes["target_value"]))
        if target < 0:
            raise ScorecardError("target_out_of_range", "цель не бывает отрицательной")
        config.target_value = target.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if "enabled" in changes:
        config.enabled = bool(changes["enabled"])
    db.flush()


def _visible_details(
    key: str, details: dict, *, include_team: bool, include_assessment: bool
) -> dict:
    """A metric's details in the form this reader is entitled to.

    Only the team pace carries people: the per-person breakdown needs the pace
    permission, and the signal with its reason — a judgement about a colleague —
    needs the assessment one. The same two permissions trim the scorecard itself
    (see _build_team); without this the drill-down handed the untrimmed details to
    anyone who could open the project, a client included.
    """
    if key != "team_pace" or not details:
        return details
    if not include_team:
        return {k: v for k, v in details.items() if k not in ("by_person", "unassigned")}
    if include_assessment:
        return details
    hidden = ("signal", "reason")
    trimmed = dict(details)
    trimmed["by_person"] = [
        {k: v for k, v in person.items() if k not in hidden}
        for person in details.get("by_person", [])
    ]
    if details.get("unassigned"):
        trimmed["unassigned"] = {
            k: v for k, v in details["unassigned"].items() if k not in hidden
        }
    return trimmed


def metric_tasks(
    db: DbSession, project: Project, org: Organization, key: str, week: date | None,
    *, include_team: bool = False, include_assessment: bool = False,
) -> dict:
    """A metric's drill-down for a week: past weeks come from the snapshot's
    details (the chronicle), the current one is a live computation without a write.

    `include_team` / `include_assessment` are the reader's permissions, the same
    pair scorecard_state takes; both are closed by default.
    """
    seen = {"include_team": include_team, "include_assessment": include_assessment}
    if key not in _DEFS:
        raise ScorecardError("metric_not_found", f"неизвестная метрика: {key}")
    tz = project_tz(project, org)
    today = datetime.now(tz).date()
    current_week = week_start_of(today)
    week_start = week_start_of(week) if week is not None else current_week
    if week_start > current_week:
        raise ScorecardError("week_in_future", "неделя ещё не наступила")

    if week_start == current_week:
        configs = {c.metric_key: c for c in ensure_metrics(db, project)}
        config = configs[key]
        if not config.enabled:
            value, details = None, {}
        else:
            plan = _plan_view(db, project, org)
            value, details = _compute_metric(
                db, project, org, plan, key, week_start, today, tz
            )
        return {
            "metric_key": key,
            "week_start": week_start.isoformat(),
            "value": float(value) if value is not None else None,
            "details": _visible_details(key, details, **seen),
        }

    row = db.scalar(
        select(ScorecardSnapshot).where(
            ScorecardSnapshot.project_id == project.id,
            ScorecardSnapshot.metric_key == key,
            ScorecardSnapshot.week_start == week_start,
        )
    )
    return {
        "metric_key": key,
        "week_start": week_start.isoformat(),
        "value": float(row.value) if row is not None and row.value is not None else None,
        "details": _visible_details(key, (row.details or {}) if row is not None else {}, **seen),
    }
