import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import PlanVersion, Project, Task
from app.projects import lock_project


def deviation_days(
    task: Task, *, start_date: date | None = None, duration_days: int | None = None
) -> int | None:
    """A task's deviation from its baseline plan, in days.

    ``None`` means the task has no baseline: it was created after approval and
    is exempt from explanations — adding work is normal, hiding a schedule shift
    is not.

    It is measured against the baseline plan, not against the previous value.
    That is not an implementation detail but the rule itself: measured against
    the previous value, a task is moved five times by one day, ends up a week
    away in total, and not a single explanation is left in its history.

    A shift of the start is measured in calendar days while duration is measured
    in working days, because that is the unit duration is given in. Mixing units
    is deliberate: "we pushed it by a week" is something a person says about the
    calendar, while "it got two days longer" is about the work, and neither of
    the two questions becomes clearer when answered in the other's units.

    The dimensions are not mixed: the dimension named is the one measured. Pass
    ``start_date`` and only the shift of the start is computed; pass
    ``duration_days`` and only the duration is. Otherwise a task whose dates
    have already moved past the threshold with an explanation would demand a
    reason for every one-day edit of its duration — and vice versa; the
    X-Shift-Deviation-Days header would then name a number from the other
    dimension. With no arguments, the greater of the two current deviations is
    returned — the answer to "how far has the task drifted from what was
    promised".
    """
    if task.baseline_start is None or task.baseline_duration is None:
        return None

    start_shift = abs(
        ((task.start_date if start_date is None else start_date) - task.baseline_start).days
    )
    duration_shift = abs(
        (task.duration_days if duration_days is None else duration_days)
        - task.baseline_duration
    )
    if start_date is not None and duration_days is None:
        return start_shift
    if duration_days is not None and start_date is None:
        return duration_shift
    return max(start_shift, duration_shift)


def approve_plan(db: DbSession, project: Project, *, actor_id: uuid.UUID | None) -> PlanVersion:
    """Approving (or re-approving) a plan.

    The snapshot is written in two places at once: into ``PlanVersion.snapshot``
    as a chronicle, and into every task's ``baseline_start`` /
    ``baseline_duration`` as the thing every subsequent edit is compared
    against. The second is not a cache of the first: the threshold check happens
    on every drag, and parsing the latest version's JSON for it would mean
    paying twice for the same thing.

    The project's row is locked for the whole time: the version number is
    computed as ``plan_version + 1``, and two simultaneous approvals without a
    lock would get the same number — which a unique constraint holds, meaning
    the loser would get a bare 500.
    """
    lock_project(db, project)

    tasks = db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
    ).all()

    snapshot = {
        str(task.id): {
            # The name sits in the snapshot next to the dates even though the
            # specification requires only dates: an old version reads as a
            # promise, and a promise made of bare identifiers does not read at
            # all — all the more so since the task may have been renamed or
            # deleted since.
            "name": task.name,
            "start_date": task.start_date.isoformat(),
            "duration_days": task.duration_days,
        }
        for task in tasks
    }

    project.plan_version += 1
    project.plan_approved_at = datetime.now(timezone.utc)

    for task in tasks:
        task.baseline_start = task.start_date
        task.baseline_duration = task.duration_days

    version = PlanVersion(
        project_id=project.id,
        version=project.plan_version,
        approved_by=actor_id,
        snapshot=snapshot,
    )
    db.add(version)
    db.flush()
    return version


class PlanVersionNotFound(Exception):
    """The project has no version by that name."""


def restore_plan_version(
    db: DbSession, project: Project, version: int, *, actor_id: uuid.UUID | None
) -> PlanVersion:
    """Returns the baseline plan to the promises of version `version`.

    Approval stops being irreversible: an "Approve" pressed by mistake used to
    rewrite the baseline of every task, and there was nothing to bring the
    previous promise back with — the snapshot lay in the chronicle as dead
    weight. Restoring touches only baseline_*: a task's current dates are
    reality, not a promise, and rolling a promise back must not move reality.

    The result is a new version with the same snapshot rather than a replacement
    of the current one: the chronicle stays a chronicle, and it shows that the
    promise was returned to version N.

    Tasks created after version N are absent from the snapshot — their baseline
    is cleared: relative to the restored promise they are "beyond the plan".
    """
    lock_project(db, project)

    source = db.scalar(
        select(PlanVersion).where(
            PlanVersion.project_id == project.id, PlanVersion.version == version
        )
    )
    if source is None:
        raise PlanVersionNotFound(str(version))

    tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    for task in tasks:
        promised = source.snapshot.get(str(task.id))
        if promised is None:
            task.baseline_start = None
            task.baseline_duration = None
        else:
            # `current_start` is the promise on today's axis: it appears when the
            # start date is assigned or moved after the version was approved (see
            # schedule.apply_schedule). `start_date` stays in the axis the promise
            # was made in and is the answer only while that axis is still in force.
            task.baseline_start = date.fromisoformat(
                promised.get("current_start") or promised["start_date"]
            )
            task.baseline_duration = promised["duration_days"]

    project.plan_version += 1
    project.plan_approved_at = datetime.now(timezone.utc)
    restored = PlanVersion(
        project_id=project.id,
        version=project.plan_version,
        approved_by=actor_id,
        snapshot=source.snapshot,
    )
    db.add(restored)
    db.flush()
    return restored


def plan_versions(db: DbSession, project: Project) -> list[PlanVersion]:
    """Every version of the plan, newest first."""
    return list(
        db.scalars(
            select(PlanVersion)
            .where(PlanVersion.project_id == project.id)
            .order_by(PlanVersion.version.desc())
        ).all()
    )
