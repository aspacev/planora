import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    LargeBinary,
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.calendar import WEEKDAYS_MON_FRI
from app.db import Base


class Role(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"
    CLIENT = "client"


class Criticality(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# Derived from Criticality rather than spelled out as a second list: two lists of
# the same values would one day diverge when a level is added — exactly as the
# CHECK on roles would diverge if it were spelled out by hand.
CRITICALITY_LEVELS: tuple[str, ...] = tuple(level.value for level in Criticality)


class ProjectType(StrEnum):
    """What kind of work a project is — it decides what the intake asks about.

    A requirements document for software and a brief for an event leave different
    things unsaid: the first rarely names the acceptance procedure, the second
    rarely names who approves the spending. The list of topics per type lives in
    config/intake_topics.yml; here is only the set of types itself.
    """

    SOFTWARE = "software"
    EVENT = "event"
    GENERAL = "general"


PROJECT_TYPES: tuple[str, ...] = tuple(kind.value for kind in ProjectType)


class TaskStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


# By the same technique as CRITICALITY_LEVELS: the list for the CHECK and for the
# mutation layer's validation is derived from the enum rather than written out a
# second time by hand.
TASK_STATUSES: tuple[str, ...] = tuple(status.value for status in TaskStatus)


class RiskFlag(StrEnum):
    """The assignee's own assessment: am I going to make the deadline.

    Not a computation but a person's word: green means on plan, yellow means there
    is a risk, red means the deadline is under threat. The scorecard compares this
    word with the fact ("warned in advance" or "missed it silently"), and that is
    exactly why the flag lives on the task rather than in a comment: the journal
    shows when it was set.
    """

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


# By the same technique as CRITICALITY_LEVELS and TASK_STATUSES.
RISK_FLAGS: tuple[str, ...] = tuple(flag.value for flag in RiskFlag)


class ScheduleMode(StrEnum):
    """Which kind of time the project's plan lives in.

    `relative` is a preliminary plan with no dates: the scale is "Month 1 / Week 1
    / Day 1" and the project's start has not been assigned yet. `calendar` means
    the start is assigned and the tasks have real dates. The default value is
    `relative`: creating a project does not ask for a start date, which is assigned
    once the plan is approved.
    """

    RELATIVE = "relative"
    CALENDAR = "calendar"


SCHEDULE_MODES: tuple[str, ...] = tuple(mode.value for mode in ScheduleMode)


class EffortUnit(StrEnum):
    """What a proposal's effort is measured in: days or hours.

    A property of the proposal as a whole rather than of each row: a budget where
    one row is in days and the next is in hours does not add up into one total
    without the question "what does this say" on every row.
    """

    DAYS = "days"
    HOURS = "hours"


# By the same technique as CRITICALITY_LEVELS: the list for the CHECK is derived
# from the enum rather than written out a second time by hand.
EFFORT_UNITS: tuple[str, ...] = tuple(unit.value for unit in EffortUnit)


class ProposalStatus(StrEnum):
    """The stage of a proposal that a person marks: draft, sent, agreed.

    "In the plan" is deliberately absent here: that stage is not marked but derived
    from the rows' references to tasks (ProposalTask.plan_task_id). A stored flag
    would diverge from the truth on the very first undo of a carry-across — an undo
    deletes the tasks, the database nulls the references, and the flag would stay
    raised.
    """

    DRAFT = "draft"
    SENT = "sent"
    AGREED = "agreed"


PROPOSAL_STATUSES: tuple[str, ...] = tuple(status.value for status in ProposalStatus)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)

    default_locale: Mapped[str] = mapped_column(String(5), default="az")
    default_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Baku")
    working_days: Mapped[int] = mapped_column(Integer, default=WEEKDAYS_MON_FRI)
    week_start: Mapped[int] = mapped_column(Integer, default=0)
    holiday_calendar: Mapped[list] = mapped_column(JSON, default=list)
    default_shift_threshold_days: Mapped[int] = mapped_column(Integer, default=2)
    public_sharing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_comments_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    locale: Mapped[str] = mapped_column(String(5), default="az")
    # The reader's timezone — level 4 of the settings. Nullable, and `null` here is
    # not "empty" but "ask the browser": a person's timezone travels with them, and
    # one written down once when the account was created would lie after the first
    # trip. The organization's timezone is not copied here for the same reason a
    # project does not copy its settings: a copy drifts from the original.
    timezone: Mapped[str | None] = mapped_column(String(64))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # This person's last activity — the moment of the last request with a valid
    # session of theirs, rather than something derived from Session.last_used_at.
    # Session rows are swept away (sign-out, expiry, a week of idleness with no
    # requests — see app.auth), and an aggregate over them goes blind precisely
    # when activity is asked about after a long break: there are no fresh rows left
    # while the person keeps using the product. A separate field survives that
    # cleanup. It is refreshed by the same step as last_used_at — not on every
    # request. It feeds the director's panel (see admin_routes).
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id"),
        # A free String(16) let any value into the column, and Role(...) on it
        # raised ValueError right inside the query. The list is derived from Role so
        # as not to diverge from it when a role is added.
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role.value}'" for role in Role) + ")",
            name="ck_memberships_role",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    # Asked on every request with a session; the composite (org_id, user_id) does
    # not lead with the right column and is useless for this lookup.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    # Whether this membership sees only the projects it was individually invited to
    # — regardless of the role. For the `client` role and for a link-holding guest
    # the role itself already decides this (see `_NEEDS_GRANT` in app.access); the
    # column exists for the sake of the other roles — an inviter is entitled to
    # invite an editor or a viewer into particular projects rather than into all of
    # the organization's projects at once. False by default: an invitation with no
    # projects selected does not narrow a role that sees the whole organization by
    # default — otherwise enabling this capability would itself trim the rights of
    # those already invited.
    project_scoped: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # "Sign out on all devices" and the cleanup of expired sessions at sign-in both
    # look sessions up by their owner.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The organization chosen by the switcher. It lives on the session rather than
    # on the user: one tab looks at one's own company while another looks at
    # someone else's that one was invited to, and a shared field on the user would
    # throw both tabs across at once. SET NULL rather than CASCADE: a deleted
    # organization must not carry the session away with it — the person simply
    # returns to the first available one.
    active_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL")
    )

    # The last request made with this session — for the idle timeout: a stolen
    # cookie from an abandoned device must not live all thirty days of its expiry
    # merely because it was issued once. Refreshed by a step (see app.auth) rather
    # than on every request: otherwise every read of a project is also a write into
    # the sessions table.
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ThrottleEvent(Base):
    """An event for the rate counters: one row is one counted attempt.

    In the database rather than in the process's memory, because sign-in and
    registration — unlike guest comments — are guarded not against flooding but
    against password guessing: a fuse that a process restart resets and that a
    neighbouring replica cannot see is no fuse there. The database is the only
    shared storage of this architecture (the product has no external services).

    The key is an arbitrary string of the form "login:ip:...", and whoever composes
    it is responsible for its uniqueness across uses. Old rows are swept away along
    the way on every access to their own key.
    """

    __tablename__ = "throttle_events"
    __table_args__ = (Index("ix_throttle_events_bucket_at", "bucket", "at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    bucket: Mapped[str] = mapped_column(String(120))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiUsage(Base):
    """An organization's LLM token spend over a calendar day.

    Separate from AiSession.tokens_used: a session counts its own conversation,
    while the budget counts everything the organization spent in a day, including
    targeted actions such as splitting a task, which have no session at all.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("org_id", "day"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    day: Mapped[date] = mapped_column(Date)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class IdempotencyRecord(Base):
    """The answer once issued to a writing request carrying an idempotency key.

    A repeated request with the same key (a network retry, a double click) gets the
    stored answer instead of a second application: the mutation "move by a day",
    applied twice, is a move by two days, and a client whose first answer got lost
    in the network must not be able to arrange that.

    The rows live for a day and are swept away along the way: retries arrive within
    seconds, and a key older than a day is no longer a retry.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("project_id", "key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(120))
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailVerification(Base):
    """A single-use address confirmation link.

    Arranged like a session: the plain token goes outward while its hash lies in
    the database — a leaked dump does not let anyone confirm someone else's
    address. A row lives until it expires, so the table does not grow: the next
    issue removes the owner's previous links, and confirmation sweeps away the
    expired ones.

    A redeemed row is not deleted but marked `used_at`: a link from an email gets
    opened again — from the browser's history, from the same email on another
    device — and without that mark the second visit would answer "this link does
    not fit" to a person for whom everything is fine. Nothing can be confirmed with
    it a second time: the mark is checked before the expiry date.
    """

    __tablename__ = "email_verifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Looked up by owner on every resend and on confirmation.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: When the message actually went out. Empty means it did not: the token is
    #: issued before sending and survives an unreachable mail server, while the
    #: pause between resends is counted from the message rather than from the row.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: When the link was used. Empty means the link is still valid.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PasswordReset(Base):
    """A single-use password recovery link.

    The same discipline as EmailVerification: the plain token goes outward while
    its hash lies in the database. A separate table rather than one shared with
    confirmation: a recovery link is entry into an account, and it must live
    noticeably shorter, while a mistake in shared code must not turn a "confirm
    your address" message into the key to someone else's password.
    """

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Looked up by owner on every repeated request and on redemption.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("org_id", "slug"),
        # The same principle as the task's CHECK constraints: the invariant is held
        # by the database rather than by the application layer alone — a second
        # write path must not be able to store a mode that does not exist.
        CheckConstraint(
            "schedule_mode IN (" + ", ".join(f"'{mode}'" for mode in SCHEDULE_MODES) + ")",
            name="ck_projects_schedule_mode",
        ),
        CheckConstraint(
            "project_type IN (" + ", ".join(f"'{kind}'" for kind in PROJECT_TYPES) + ")",
            name="ck_projects_project_type",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    deadline: Mapped[date | None] = mapped_column(Date)
    plan_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_version: Mapped[int] = mapped_column(Integer, default=0)

    # A relative plan or a calendar one (see ScheduleMode). In relative mode
    # `start_date` is empty and the task dates are coordinates on the relative
    # axis: day N of the project is stored as RELATIVE_EPOCH + (N-1) (see
    # app.schedule). The source of truth is the start plus durations plus
    # dependencies plus the working calendar; there are no finish dates in the
    # database in this mode either.
    #
    # server_default for the same reason as Task.status: a NOT NULL with no value
    # would break the second write path.
    schedule_mode: Mapped[str] = mapped_column(
        Text, default=ScheduleMode.RELATIVE, server_default=text("'relative'")
    )
    # The assigned start date. It appears when the plan is anchored to the calendar
    # and stays as the anchor: the relative view of an already calendar-based
    # project is counted from it, as is the shift of every task when the start moves.
    start_date: Mapped[date | None] = mapped_column(Date)

    # nullable = "inherit from the organization"
    timezone: Mapped[str | None] = mapped_column(String(64))
    working_days: Mapped[int | None] = mapped_column(Integer)
    shift_threshold_days: Mapped[int | None] = mapped_column(Integer)

    holidays_extra: Mapped[list] = mapped_column(JSON, default=list)
    workdays_extra: Mapped[list] = mapped_column(JSON, default=list)
    # Automatic shifting along dependencies: a successor does not start before its
    # predecessor has finished. Off by default — before it, a dependency moved
    # nothing at all, and turning it on changes the meaning of every dependency in
    # the project at once (see app/cascade.py). A property of the project rather
    # than of the organization: in one project the plan is driven by the chain and
    # in the next one by hand, and a shared setting would force one choice on
    # everyone.
    auto_schedule: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # The last revision "Undo" no longer reaches. Raised to the head of the
    # journal when the start date is assigned or moved (app.schedule): that lays
    # the tasks out on a new axis outside the journal, and the inverse operations
    # of earlier revisions still hold dates of the old one — undoing them would
    # throw a task back to the relative epoch, the year 2001.
    undo_floor_seq: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # The kind of work, as chosen in the intake that created the project. Empty
    # for a project created by hand: nobody was asked. Not shown anywhere yet —
    # kept so that what is built on it later (a quote from the document,
    # organization templates) has something to start from.
    project_type: Mapped[str | None] = mapped_column(String(16))


class ProjectAccess(Base):
    """Access to one project, granted to a person individually.

    Always needed by the `client` role and by a link-holding guest (see
    `_NEEDS_GRANT` in app.access), and by the other roles when their own membership
    is narrowed (`Membership.project_scoped`, see the same place). For an
    un-narrowed membership, rows here mean nothing — its right to read a project
    follows from the role alone.
    """

    __tablename__ = "project_access"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Asked on every read of a project by a role that needs explicit access, and
    # when assembling such a person's project list — that is, leading with this column.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class ShareLink(Base):
    """A project's public link.

    The token lies in plain text — deliberately, unlike an invitation, where a hash
    is stored. An invitation is shown once and goes to its recipient; a public link
    is copied by the owner again and again, from the project's settings, and a
    server that had forgotten it would leave "show the link again" with only one
    meaning — issue a new one and kill the one in force. The price of the trade-off
    is stated plainly: a leaked database dump hands over read access to published
    projects, not access to organizations.

    A revoked link is not deleted but marked `revoked_at`: the old address must
    answer "this link is no longer valid" rather than "there is no such project".
    """

    __tablename__ = "share_links"
    __table_args__ = (
        # A project has one link in force. A partial index rather than an ordinary
        # unique constraint: a project may have any number of revoked links — that
        # is the record of which address died when.
        Index(
            "uq_share_links_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True)
    comments_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Comment(Base):
    """A remark on a project or on one of its tasks.

    The author is either a member with an account or a link-holding guest who gave
    a name. Exactly one of the two: a comment with no author is signed by nobody,
    while a comment with both is a member pretending to be a guest. That constraint
    is held by the database rather than by a check in a route: there are already
    two routes that create a comment.
    """

    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(author_user_id, guest_name) = 1",
            name="ck_comments_single_author",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # null means a comment on the whole project rather than on a task. A task's feed
    # is looked up by exactly this column.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    # CASCADE rather than SET NULL: a nulled author would leave the entry with no
    # signature at all — neither an account nor a guest name — that is, it would
    # violate the constraint below at the very moment the person was deleted.
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    guest_name: Mapped[str | None] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text)
    # An internal remark: visible to members, not to a public-link guest. False by
    # default — the conversation with the client stays shared, as it was; the flag
    # is set by an author who decided to speak "aside". server_default covers the
    # old rows too: before the flag existed, every remark was public in fact.
    internal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # clock_timestamp(), not now(): now() returns the transaction's start time, and
    # two remarks inserted in one get the same timestamp — while the order of the
    # conversation rests on exactly that. The revision journal has seq for this;
    # comments do not, and there is no reason to introduce one.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class Invitation(Base):
    """An invitation into an organization: single-use, with a lifetime and a role inside.

    It lives in the database after acceptance too — it is the record of who brought
    whom in, and `accepted_at` doubles as the flag for "the token no longer works".
    """

    __tablename__ = "invitations"
    __table_args__ = (
        # The same way as with membership: the list is derived from Role so that a
        # role in an invitation cannot be created around the permission matrix.
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role.value}'" for role in Role) + ")",
            name="ck_invitations_role",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # null means an invitation by link only: it goes to whoever presents it, and
    # that is a deliberate trade-off rather than an oversight.
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    # The projects an invitation grants access to right away. Needed by the
    # `client` role; for other roles the list is empty. Stored as a list of ids
    # rather than as a link table: it is read and rewritten whole, and nothing
    # searches inside it.
    project_ids: Mapped[list] = mapped_column(JSON, default=list)
    # A hash is stored, as with a password and a session: a database dump must not
    # hand out access to organizations. The direct consequence is that we show the
    # plain link once, at the moment it is issued.
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    # SET NULL: a person who has left the organization does not carry away the
    # record of whom they brought in.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Filled in only by sending a message. Issuing a link to copy does not touch
    # it: there was no message, and such an issue does not count towards the
    # mailing ceiling.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    color: Mapped[str] = mapped_column(String(9))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # The position is unique within a category — that is the model of ordering
        # itself. DEFERRABLE INITIALLY DEFERRED: a reorder renumbers several rows in
        # one transaction, and a check at every UPDATE would catch intermediate
        # duplicates that do not exist in the end.
        UniqueConstraint(
            "category_id",
            "position",
            name="uq_tasks_category_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        # The domain's invariants are held by the database rather than by the
        # mutation layer alone: a second write path (a restore from the journal,
        # hand-written SQL) must not be able to store a row the mutation layer
        # would not have accepted.
        CheckConstraint("progress_pct BETWEEN 0 AND 100", name="ck_tasks_progress_pct"),
        CheckConstraint("duration_days >= 1", name="ck_tasks_duration_days"),
        # A milestone is a point on the scale, and one day is exactly what that
        # means. The invariant is held by the database rather than by the mutation
        # layer alone: a milestone with a duration of a week is drawn as a diamond
        # but counted as a segment, and a divergence between what is seen and what
        # is computed is the worst kind of error in a chart.
        CheckConstraint(
            "NOT milestone OR duration_days = 1", name="ck_tasks_milestone_duration"
        ),
        CheckConstraint(
            "criticality IN (" + ", ".join(f"'{level}'" for level in CRITICALITY_LEVELS) + ")",
            name="ck_tasks_criticality",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in TASK_STATUSES) + ")",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "risk IN (" + ", ".join(f"'{flag}'" for flag in RISK_FLAGS) + ")",
            name="ck_tasks_risk",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Gantt rows are grouped by category — the selection leads with this column.
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    internal_note: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[date] = mapped_column(Date)
    duration_days: Mapped[int] = mapped_column(Integer)
    # A milestone: a stage handover, a sign-off, a contractor's deadline — something
    # that happens on a day rather than lasting. A flag on the task rather than a
    # separate table: a milestone has the same name, category, status, assignees,
    # comments and dependencies, and a second entity would mean a second set of
    # operations, a second journal and a second undo for the sake of one difference
    # in rendering.
    #
    # A milestone still stores a duration (equal to one day — see the constraint
    # above): the finish-date computation, the plan snapshots and the shift
    # threshold all ask every task for it alike, and a nullable column would add an
    # "and what if it is a milestone" branch to each of them.
    milestone: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    criticality: Mapped[str] = mapped_column(String(16), default="normal")
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    # server_default is not only for a migration over a live table: without it a
    # second write path (hand-written SQL) would get a NOT NULL with no value.
    status: Mapped[str] = mapped_column(Text, default="planned", server_default=text("'planned'"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    baseline_start: Mapped[date | None] = mapped_column(Date)
    baseline_duration: Mapped[int | None] = mapped_column(Integer)
    # Where the task came from. The history keeps "created by an AI session of
    # 10 August", and without this field there would be nowhere for such an entry to
    # come from: the revision journal stores the operation, not its origin.
    created_by_ai_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="SET NULL")
    )
    # When the task became "done" and when it was taken up. They are set by the
    # mutation layer on a status transition and cleared on leaving it (see
    # _stamp_status_change in app.mutations): the scorecard needs "closed this week"
    # and "hanging in progress for N days", and the revision journal answers those
    # questions only by walking every entry of the task. The column is the last
    # boundary, not a history: the full chronicle of transitions is still kept by
    # the journal.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    in_progress_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The assignee's risk flag and a one-line reason (see RiskFlag). The flag is not
    # cleared on a closed task: clearing it around the journal would break the undo,
    # while clearing it through the status coupling would be an extra history entry
    # about something the person did not do. The screen simply does not show a flag
    # on "done".
    risk: Mapped[str] = mapped_column(
        String(8), default="green", server_default=text("'green'")
    )
    risk_note: Mapped[str] = mapped_column(String(300), default="", server_default=text("''"))


class PlanVersion(Base):
    """An approved plan: a snapshot of dates and durations at the moment of approval.

    A separate table rather than only the task's baseline_*: a task's baseline
    fields hold the latest version, while a chronicle of "what was promised in
    January, what in March" requires all the previous ones. Versions are numbered
    within a project, and a unique constraint holds that numbering: two
    simultaneous approvals would otherwise get the same number.
    """

    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("project_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    # SET NULL: deleting an account must neither fail on a foreign key nor carry
    # away the chronicle of approvals — the entry stays, the author is forgotten.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # jsonb for the same reason as the revision journal: the snapshot is read whole,
    # but a task is also looked up inside it when versions are compared.
    snapshot: Mapped[dict] = mapped_column(JSONB)


class TaskAssignee(Base):
    __tablename__ = "task_assignees"
    __table_args__ = (UniqueConstraint("task_id", "user_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    # "This person's tasks" are looked up leading with the user_id column; the
    # composite unique (task_id, user_id) does not lead with it and is useless here.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (UniqueConstraint("from_task_id", "to_task_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # A project's dependencies are read in full on every GET of the state.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    from_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    # The reverse end: "who is waiting on this task" and the snapshot of
    # dependencies when it is deleted. The forward end is covered by the prefix of
    # the unique (from_task_id, to_task_id).
    to_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )


class OrgLlmCredential(Base):
    """An LLM connection: one per organization.

    `base_url` and `model` are mandatory settings rather than constants: without
    them BYOK works with one cloud on one hard-coded model, and the promise "you
    can plug in a local model" stays words.

    The key is encrypted symmetrically with the application's secret and is never
    handed outward — only the flag "a key is configured".
    """

    __tablename__ = "org_llm_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="openai")
    base_url: Mapped[str] = mapped_column(String(300))
    model: Mapped[str] = mapped_column(String(100))
    encrypted_key: Mapped[str] = mapped_column(Text)


class AiSession(Base):
    """The interview, the summary and the draft — before being applied to a project.

    It lives separately from the project, because before application the project
    does not exist: AI writes nothing into a project without a person's explicit
    confirmation.
    """

    __tablename__ = "ai_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # Filled in after application: before it there is no project.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    # SET NULL — for the same reason as plan_versions.approved_by.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # The interview's language is fixed on the session rather than taken from the
    # profile each time: otherwise someone who switched the interface mid-interview
    # would get a draft half in one language and half in another.
    locale: Mapped[str] = mapped_column(String(5), default="az")
    status: Mapped[str] = mapped_column(String(16), default="interview")
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[list] = mapped_column(JSONB, default=list)
    draft: Mapped[dict] = mapped_column(JSONB, default=dict)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    applied_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The kind of project: it selects the interview's topics (ProjectType).
    project_type: Mapped[str] = mapped_column(
        String(16), default=ProjectType.GENERAL, server_default=text("'general'")
    )
    # The document the session started from, if it started from one. SET NULL:
    # deleting the document must not take the session — and with it the link
    # from the created tasks to their origin — away.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_documents.id", ondelete="SET NULL")
    )
    # How many parts of the document have been read. A long document is read in
    # parts, one request per part: a single request reading all of it would not
    # fit into a serverless function's time limit.
    document_cursor: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # What the document asks for: [{"id": 1, "section": "3.2", "text": "..."}].
    # From here on the model works with this list rather than with the document's
    # full text — otherwise fifty pages would be paid for again on every question.
    requirements: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # One or two sentences on what the document is about — shown next to the
    # suggested project type so that a person sees the document was understood.
    brief: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    # The interview topics the document already answers; they are not asked about.
    document_topics: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class ProjectDocument(Base):
    """The document a project was created from — the file itself and its text.

    The file lives in the database rather than on a disk or in an object store:
    the product has no external services, and a disk does not exist at all in the
    serverless layout. The price is known and accepted — database backups grow by
    the size of the uploads — and is bounded by MAX_DOCUMENT_BYTES.

    `project_id` is empty until the draft is applied: before that there is no
    project. A document whose session was abandoned is swept on a later upload
    (see app.documents.sweep_abandoned).
    """

    __tablename__ = "project_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # Unique: one document per project in this version. The index also serves the
    # only query by this column — "the document of this project".
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    # Deferred: the two heavy columns are read only by the routes that serve the
    # file and by the reading step — never by a query that merely asks whether a
    # project has a document.
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    extracted_text: Mapped[str] = mapped_column(Text, deferred=True)
    # SET NULL — for the same reason as plan_versions.approved_by.
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "seq"),
        # A GIN index over the payload: a task's history is found by the occurrence
        # of task_id in op (see serialization), and without the index that is a
        # sequential read of the project's whole journal every time a card is opened.
        Index("ix_revisions_op_gin", "op", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    # SET NULL: the revision journal outlives the deletion of an author — a
    # project's history is not an account's property.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # jsonb rather than json: all three features the journal is kept for search by
    # the payload's content. json stores raw text, knows no containment operators
    # and is not GIN-indexable. The product is Postgres-only; changing this after
    # production entries appear means rewriting the table, whereas today it is free.
    op: Mapped[dict] = mapped_column(JSONB)
    inverse: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    # A batch of revisions is read in full when a group operation is undone.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    # The number of the revision this one undid. Without it, "undo the last thing"
    # would mean undoing one's own undo: the journal is linear, and the second
    # revision from the top after an undo is the undo itself. There is deliberately
    # no foreign key: it would have to refer to the composite (project_id, seq), and
    # the benefit of such a reference is nil — revisions are not deleted.
    undoes_seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Proposal(Base):
    """A project's commercial proposal: the budget's settings.

    One row per project, and it is created lazily — by the first change rather than
    by the creation of the project: most projects have no proposal, and an empty
    row for each of them would be a record for the record's sake. A read with no row
    returns the default values (see app.proposals).

    The rate and the tax live here rather than in the organization: a proposal is
    drawn up for a particular client, and neighbouring projects have their own
    currency and their own tax.
    """

    __tablename__ = "proposals"
    __table_args__ = (
        # The same principle as with schedule_mode: the invariant is held by the
        # database rather than by the application layer alone.
        CheckConstraint(
            "effort_unit IN (" + ", ".join(f"'{unit}'" for unit in EFFORT_UNITS) + ")",
            name="ck_proposals_effort_unit",
        ),
        CheckConstraint("hours_per_day >= 1", name="ck_proposals_hours_per_day"),
        CheckConstraint("tax_rate_pct >= 0", name="ck_proposals_tax_rate_pct"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in PROPOSAL_STATUSES) + ")",
            name="ck_proposals_status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # unique: a project has one proposal. A second one is a different project, not
    # a second row here.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    effort_unit: Mapped[str] = mapped_column(
        Text, default=EffortUnit.DAYS, server_default=text("'days'")
    )
    # How many hours to count as a working day when carrying an hourly budget into
    # the plan: the plan's durations are in days, and without this number there is
    # nothing to derive them from.
    hours_per_day: Mapped[int] = mapped_column(Integer, default=8, server_default=text("8"))
    # Numeric rather than Float: tax is money, and 18% must stay exactly eighteen
    # rather than 17.999999.
    tax_rate_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), server_default=text("0")
    )
    # An ISO 4217 code. Stored rather than derived from the language: the
    # interface's language and the deal's currency are independent things.
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default=text("'USD'"))
    # The proposal's assumptions and notes as a whole — "estimates for the current
    # scope", "rates exclude licence costs". Free text rather than a list: the items
    # are written as lines, and a list's structure would add nothing to them.
    notes: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    # The stage of the deal marked by hand: draft, sent to the client, agreed. The
    # timestamps next to it are the captions under the stage bar ("sent 27 Aug"); a
    # step back clears them so that the bar does not name the date of a stage that no
    # longer exists.
    status: Mapped[str] = mapped_column(
        Text, default=ProposalStatus.DRAFT, server_default=text("'draft'")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProposalCategory(Base):
    """A proposal's section: a group of works with its own rows.

    Its own table rather than the plan's Category: a budget section lives before the
    plan and without the plan, while a chart category carries a colour and takes
    part in the chart's ordering — mixing two lives in one table would mean a budget
    draft showing up on the chart.
    """

    __tablename__ = "proposal_categories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # One line about the section as a whole — "understand the goals, the people and
    # the requirements": in the budget table it stands on the section's row, next to
    # the sum of its works.
    description: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class ProposalTask(Base):
    """A budget row: the work, the role, the effort and the rate.

    The price is deliberately not stored: it equals effort x rate, and a stored copy
    would diverge from its factors on the very first edit. Both ends compute it
    anew — the client for the screen, and the server restates it nowhere.
    """

    __tablename__ = "proposal_tasks"
    __table_args__ = (
        CheckConstraint("effort >= 0", name="ck_proposal_tasks_effort"),
        CheckConstraint("rate >= 0", name="ck_proposal_tasks_rate"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Both references at once: the section for the order on screen, the proposal so
    # that a project's rows can be read with one query, without walking the sections.
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposal_categories.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    # The short description is a table column; the detailed one is the row's card.
    description: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[str] = mapped_column(Text, default="")
    # The performer's role in words ("designer", "senior backend") rather than a
    # reference to a member: a budget is written before it is known who exactly will
    # do the work.
    role: Mapped[str] = mapped_column(String(120), default="")
    # The effort in the proposal's units (see Proposal.effort_unit). Numeric: half a
    # day is 0.5, not 0.5000000000000001.
    effort: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), default=Decimal("0"), server_default=text("0")
    )
    # The rate per unit of effort, in the proposal's currency.
    rate: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default=text("0")
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    risks: Mapped[str] = mapped_column(Text, default="")
    assumptions: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # The plan task the row was assembled from ("Assemble from the plan") or carried
    # across into ("Add to the plan", see app.proposals). While the reference is
    # alive, the carry-across skips the row: without it a second carry-across would
    # double the plan. SET NULL rather than CASCADE: deleting a task — including by
    # undoing a carry-across batch — returns the row to the carryable ones rather
    # than carrying it out of the proposal. The index exists for the sake of the SET
    # NULL itself: without it every task deletion would scan every row of every
    # budget looking for referrers.
    plan_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    # For the role suggestions: "this role's latest rate" is the rate of the
    # freshest row, and freshness cannot be derived from anything without a
    # timestamp. clock_timestamp rather than now(): rows created in one transaction
    # (assembling from the plan) must differ in time, while now() is the same for
    # all of them.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class ProposalComment(Base):
    """A remark on a budget row.

    Its own table rather than the project's Comment: that one is rigidly tied to
    plan tasks (task_id leads into tasks) and to the public page, whereas discussing
    the budget is an internal conversation among members and is not handed to guests
    at all. That is also why the author is mandatory here: there is no such thing as
    a guest signed by a name.
    """

    __tablename__ = "proposal_comments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    proposal_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposal_tasks.id", ondelete="CASCADE"), index=True
    )
    # CASCADE, as with Comment: a remark with no author is signed by nobody.
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    body: Mapped[str] = mapped_column(Text)
    # clock_timestamp for the same reason as with Comment: the order of the
    # conversation rests on the timestamp, and two remarks from one transaction are
    # indistinguishable by now().
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class ScorecardDirection(StrEnum):
    """Which way a scorecard metric should look: "no more than the target" or "no less"."""

    LTE = "lte"
    GTE = "gte"


SCORECARD_DIRECTIONS: tuple[str, ...] = tuple(d.value for d in ScorecardDirection)


class ScorecardStatus(StrEnum):
    """The week's assessment for a metric. `no_data` means there is no source or the
    metric is disabled: a grey dash, taking part in no streaks and no rules."""

    OK = "ok"
    WARN = "warn"
    RISK = "risk"
    NO_DATA = "no_data"


SCORECARD_STATUSES: tuple[str, ...] = tuple(s.value for s in ScorecardStatus)


class ScorecardAlertKind(StrEnum):
    RULE_TRIGGERED = "rule_triggered"
    METRIC_RISK = "metric_risk"


SCORECARD_ALERT_KINDS: tuple[str, ...] = tuple(k.value for k in ScorecardAlertKind)


class ScorecardMetric(Base):
    """A scorecard metric's configuration — per project, not per organization.

    The rows are created lazily, by the first opening of a project's scorecard (see
    app.scorecard.ensure_metrics): the defaults are only a seed, after which the
    owner, the target and the enabled state are edited by PATCH and apply within
    their project. The direction is nonetheless stored in the table even though it
    follows rigidly from the key: a snapshot copies the config as of the moment it
    is written, and without a column here there would be nowhere to copy it from
    when the constants in the code change.
    """

    __tablename__ = "scorecard_metrics"
    __table_args__ = (
        UniqueConstraint("project_id", "metric_key"),
        # The same principle as with schedule_mode: the invariant is held by the
        # database rather than by the application layer alone.
        CheckConstraint(
            "direction IN (" + ", ".join(f"'{d}'" for d in SCORECARD_DIRECTIONS) + ")",
            name="ck_scorecard_metrics_direction",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    metric_key: Mapped[str] = mapped_column(String(40))
    # SET NULL: deleting an account leaves the metric without an owner rather than
    # carrying its configuration away.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Numeric rather than Float: a target of "90%" must stay exactly ninety.
    target_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    direction: Mapped[str] = mapped_column(String(3))
    # The "yellow" threshold is groundwork for the future and is unused in the MVP:
    # the statuses are computed from the target by constant multipliers (see
    # app.scorecard).
    warn_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class ScorecardSnapshot(Base):
    """A metric's weekly snapshot: the value, the status and a copy of the config as
    of the moment it was written.

    Snapshots of past weeks are immutable — they are a chronicle from which streaks
    and the sparkline are computed. Only the current week's row is overwritten: it
    also serves as the cache of the live computation (see app.scorecard). The target
    and the direction are copied into the snapshot deliberately: editing a target
    today must not repaint past weeks.
    """

    __tablename__ = "scorecard_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "metric_key", "week_start"),
        # The history is read as "every metric of the project over N weeks" — leading with this pair.
        Index("ix_scorecard_snapshots_project_week", "project_id", "week_start"),
        CheckConstraint(
            "direction IN (" + ", ".join(f"'{d}'" for d in SCORECARD_DIRECTIONS) + ")",
            name="ck_scorecard_snapshots_direction",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in SCORECARD_STATUSES) + ")",
            name="ck_scorecard_snapshots_status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    metric_key: Mapped[str] = mapped_column(String(40))
    # The Monday of the ISO week in the project's timezone.
    week_start: Mapped[date] = mapped_column(Date)
    # NULL means there is no data (the source is absent or the metric was disabled).
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    target_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    direction: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(8))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # NULL means the snapshot was written not by a person but by lazy committing on a GET.
    computed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # The serialized drill-down: the ids and short attributes of the metric's tasks.
    # jsonb by the journal's rule: past weeks are read only from here, and one day
    # something will have to search by the keys inside.
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class ScorecardAlert(Base):
    """An event for the "Needs attention" panel.

    `metric_risk` lives while a metric is red in the current week; `rule_triggered`
    is the trace of the "red two weeks running" rule having fired, with a reference
    to the created task in payload. Closed events are not deleted but marked
    resolved_at: they show when a streak broke, and they are also what suppresses a
    repeat of the rule within one streak.
    """

    __tablename__ = "scorecard_alerts"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in SCORECARD_ALERT_KINDS) + ")",
            name="ck_scorecard_alerts_kind",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # The events are read as a batch on every GET of the scorecard — leading with this column.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    metric_key: Mapped[str] = mapped_column(String(40))
    week_start: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
