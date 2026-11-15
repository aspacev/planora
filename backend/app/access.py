from enum import StrEnum

from app.models import Role


class Action(StrEnum):
    PROJECT_READ = "project_read"
    PROJECT_WRITE = "project_write"
    PROJECT_ADMIN = "project_admin"
    ORG_ADMIN = "org_admin"
    COMMENT = "comment"
    READ_INTERNAL_NOTE = "read_internal_note"
    # Approval and re-approval are different permissions rather than one: the
    # first is available to an editor, the second the specification leaves to the
    # owner. Re-approval erases the baseline plan that every explained shift is
    # counted from — that is, it resets the accumulated chronicle of delay, and
    # that is an owner-level decision, not a routine edit of dates.
    PLAN_APPROVE = "plan_approve"
    PLAN_REAPPROVE = "plan_reapprove"
    # Deleting a project is a separate permission rather than part of
    # PROJECT_ADMIN: an editor edits the settings too, but a deletion carries the
    # revision journal away with it — that is, every possibility of an undo. A
    # decision of that weight, like re-approving a plan, the specification leaves
    # to the owner.
    PROJECT_DELETE = "project_delete"
    # Exporting a project as a file. A separate permission, even though today
    # everyone entitled to read the project has it: a snapshot of the plan carried
    # away as a file goes on living a life of its own and cannot be revoked —
    # unlike a public link, which can be closed. Naming the permission gives an
    # installation that cares about this one lever instead of an inquiry across
    # the routes.
    #
    # The difference in the file's contents is not expressed by a permission: it
    # is decided by the already existing READ_INTERNAL_NOTE and the rule for
    # showing assignees, exactly the ones that apply on the public page.
    PROJECT_EXPORT = "project_export"
    # The proposal is the deal's internal kitchen: rates, cost, risks and the
    # team's conversation about the client. A client and a link-holding guest do
    # not read it — by the same rule with which the export already cuts the budget
    # out of the client copy (see export/document.py, INTERNAL_SECTIONS). What is
    # meant for the client is a document assembled from client-facing fields, not
    # a screen — and they receive it from the contractor by email rather than
    # downloading it themselves: while the proposal is being edited, its draft is
    # internal work.
    #
    # A separate permission rather than PROJECT_READ: reading the plan is exactly
    # what a client is entitled to, and the difference between the plan and the
    # budget has to be named.
    PROPOSAL_READ = "proposal_read"
    # Rating people on the scorecard — the "on pace / falling behind / missed it
    # silently" signal with a reason. The pace figures are seen by the whole team,
    # while a judgement about a person is seen only by whoever answers for the
    # team: the owner. A separate permission rather than PROJECT_ADMIN: an editor
    # edits the plan but does not judge colleagues.
    TEAM_ASSESSMENT_READ = "team_assessment_read"
    # The team's pace by person — done/planned per week for each. The work figures
    # are seen by the whole team, the viewer included; a client and a link-holding
    # guest look at the plan, not at how each contributor is coping.
    TEAM_PACE_READ = "team_pace_read"
    # The requirements document a project was created from. Closed to a client
    # and a link-holding guest for the same reason as the proposal: a
    # requirements document carries internal remarks and prices. A separate
    # permission rather than PROPOSAL_READ: the two are different things that
    # happen to be hidden from the same people today.
    DOCUMENT_READ = "document_read"
    # Deleting the document takes the file and its text away for good — there
    # is no journal behind it to undo from. An owner-level decision, like
    # deleting the project.
    DOCUMENT_DELETE = "document_delete"


_MATRIX: dict[Role | None, frozenset[Action]] = {
    Role.OWNER: frozenset(Action),
    Role.EDITOR: frozenset(
        {
            Action.PROJECT_READ,
            Action.PROJECT_WRITE,
            Action.PROJECT_ADMIN,
            Action.COMMENT,
            Action.READ_INTERNAL_NOTE,
            Action.PLAN_APPROVE,
            Action.PROJECT_EXPORT,
            Action.PROPOSAL_READ,
            Action.TEAM_PACE_READ,
            Action.DOCUMENT_READ,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Action.PROJECT_READ,
            Action.COMMENT,
            Action.READ_INTERNAL_NOTE,
            Action.PROJECT_EXPORT,
            Action.PROPOSAL_READ,
            Action.TEAM_PACE_READ,
            Action.DOCUMENT_READ,
        }
    ),
    # A client and a link-holding guest export the client copy: the same trimming
    # that already applies on the public page. Refusing them would be odd — they
    # see the same thing on screen and can capture it with a screenshot.
    Role.CLIENT: frozenset(
        {Action.PROJECT_READ, Action.COMMENT, Action.PROJECT_EXPORT}
    ),
    None: frozenset({Action.PROJECT_READ, Action.COMMENT, Action.PROJECT_EXPORT}),
}

# The roles that see only the projects they were explicitly invited to —
# regardless of whether a particular membership is narrowed (see needs_project_grant).
_NEEDS_GRANT: frozenset[Role | None] = frozenset({Role.CLIENT, None})


# A role value absent from Role. Not None: None is a link-holding guest, who does
# have permissions. An unknown role must get nothing at all.
UNKNOWN_ROLE = "__unknown__"


def parse_role(raw: str | None) -> Role | str | None:
    """A role from a membership row, in the form can() understands.

    Role(raw) on a corrupted value raises ValueError — that is, a 500 before can()
    is even asked, and the permission matrix, locked by default, turns out to be
    unreachable. An unknown value is a refusal, not a crash.
    """
    if raw is None:
        return None
    try:
        return Role(raw)
    except ValueError:
        return UNKNOWN_ROLE


def needs_project_grant(role: Role | str | None, *, scoped: bool = False) -> bool:
    """Whether this role (or this particular membership) sees only the projects it
    was individually invited to.

    `scoped` is not a property of the role but of the membership
    (Membership.project_scoped): an inviter may narrow both an editor and a viewer
    down to particular projects without touching the role itself or its permission
    matrix. `client` and a link-holding guest are always narrowed, regardless of
    the value of `scoped` — there is nothing to ask them about, hence `or` rather
    than a replacement.

    An owner is never narrowed, even if their membership row somehow carries
    `project_scoped=True` (for instance, a narrowed editor was promoted): they
    alone govern the whole organization, and an owner locked inside a handful of
    projects is an organization without an administrator.

    It is asked from outside — by the project list and by loading a single
    project: they need to know not only "is this allowed" but also "by which rule
    to select". The knowledge of which roles work this way stays here, in the one
    place where access is decided.
    """
    if role is Role.OWNER:
        return False
    return role in _NEEDS_GRANT or scoped


def can(
    role: Role | None, action: Action, *, project_granted: bool = False, scoped: bool = False
) -> bool:
    if needs_project_grant(role, scoped=scoped) and not project_granted:
        return False
    # .get with an empty set as the default: an unknown role does not find itself
    # in the matrix and can do nothing — the matrix is locked by default.
    return action in _MATRIX.get(role, frozenset())


def require(
    role: Role | None, action: Action, *, project_granted: bool = False, scoped: bool = False
) -> None:
    if not can(role, action, project_granted=project_granted, scoped=scoped):
        raise PermissionError(f"{role or 'guest'} не может выполнить {action}")


# Journal fields that not everyone is entitled to see. Today there is exactly one
# — the specification promises there will be no complex per-field visibility.
_NOTE_FIELD = "internal_note"


def _carries_note(payload: dict) -> bool:
    """Whether there is a note anywhere in the entry, nested dicts and lists included."""
    return any(key == _NOTE_FIELD or _nested_note(value) for key, value in payload.items())


def _nested_note(value: object) -> bool:
    """A note inside a value — a dict or a list of dicts."""
    if isinstance(value, dict):
        return _carries_note(value)
    if isinstance(value, list):
        return any(_nested_note(item) for item in value)
    return False


def _without_note(payload: dict) -> dict:
    """A copy of the entry without the note — at any depth of nesting.

    A flat "is there such a key at the root" check is not enough: set_task_fields
    puts the note not at the root but inside from and to, and on it such a check
    silently fails — the note rides out in the answer. A walk over nested dicts
    makes the rule insensitive to the shape of an entry, and therefore to the
    shape of operations that do not exist yet.

    Lists are walked alongside dicts, and that is not provision for the future:
    the snapshot of a deleted category carries its tasks as a list specifically,
    and each of their notes sits two levels deep — in a dict inside a list inside
    the entry.
    """
    return {
        key: _prune_note(value)
        for key, value in payload.items()
        if key != _NOTE_FIELD
    }


def _prune_note(value):
    """A value without the note: a dict by key, a list element by element."""
    if isinstance(value, dict):
        return _without_note(value)
    if isinstance(value, list):
        return [_prune_note(item) for item in value]
    return value


def visible_op(payload: dict, role: Role | None, *, project_granted: bool = False) -> dict:
    """A journal entry in the form this role is entitled to see it.

    The visibility decision lives here rather than in the route: the same thing
    will be needed by the change history on a task card, and its author must not
    have to work out anew which operations carry a note. create_task puts
    internal_note into op alongside the other fields, delete_task puts it into
    inverse (the snapshot for undoing), and set_task_fields puts it inside both
    sides.

    Returns a new dict: revision.op / revision.inverse on the entry itself are
    left untouched, otherwise a future undo would restore the task without its
    note.
    """
    if not _carries_note(payload):
        return payload
    if can(role, Action.READ_INTERNAL_NOTE, project_granted=project_granted):
        return payload
    return _without_note(payload)
