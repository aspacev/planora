"""HTTP around the AI intake. No business logic here — it is in app/ai/."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as DbSession

from app.access import Action, can, parse_role
from app.ai import intake, usage
from app.ai.credentials import credential, provider_for, save_credential
from app.ai.provider import LlmError, LlmProvider
from app.auth import current_user
from app.config import get_settings
from app.db import get_db
from app.models import AiSession, Membership, Organization, Project, Task, User
from app.orgs import current_membership
from app import throttle

router = APIRouter(prefix="/api/ai", tags=["ai"])


# The membership comes from current_membership — that is, from the organization
# chosen by the switcher on the session — rather than "the first one by id", as
# it used to. First-by-id made AI blind to the switcher: a person working in
# their second organization read and configured the first one's LLM key, spent
# its tokens and filed AI sessions into it — that is, into an organization
# foreign to the current screen.


def _admin(membership: Membership = Depends(current_membership)) -> Membership:
    if not can(parse_role(membership.role), Action.ORG_ADMIN):
        raise HTTPException(status_code=403, detail="forbidden")
    return membership


def _writer(membership: Membership = Depends(current_membership)) -> Membership:
    if not can(parse_role(membership.role), Action.PROJECT_WRITE):
        raise HTTPException(status_code=403, detail="forbidden")
    return membership


def _refuse(error: Exception) -> HTTPException:
    """A model failure is not a 500.

    A timeout, garbage instead of a schema and an exhausted key limit are states
    a person is told about in words, while the conversation and the draft are
    preserved: the session continues from the same place.
    """
    code = getattr(error, "code", "llm_failed")
    if code == "llm_not_configured":
        return HTTPException(status_code=409, detail=code)
    return HTTPException(status_code=502, detail=code)


# --- the LLM connection ------------------------------------------------------


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="openai", max_length=32)
    base_url: str = Field(min_length=1, max_length=300)
    model: str = Field(min_length=1, max_length=100)
    #: Empty means keep the previous key. The key is never handed outward, and
    #: demanding it in order to edit the address would be demanding the impossible.
    api_key: str = ""


class CredentialOut(BaseModel):
    provider: str
    base_url: str
    model: str
    #: A flag only. The key itself is not here and cannot be.
    configured: bool


@router.get("/credential", response_model=CredentialOut)
def read_credential(
    membership: Membership = Depends(_admin), db: DbSession = Depends(get_db)
):
    row = credential(db, db.get(Organization, membership.org_id))
    if row is None:
        return CredentialOut(provider="", base_url="", model="", configured=False)
    return CredentialOut(
        provider=row.provider, base_url=row.base_url, model=row.model, configured=True
    )


@router.put("/credential", response_model=CredentialOut)
def write_credential(
    payload: CredentialIn,
    membership: Membership = Depends(_admin),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    try:
        row = save_credential(
            db,
            org,
            provider=payload.provider,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
        )
    except LlmError as error:
        # An unsafe address (not https, a private network) is a form refusal
        # with a code, not a 500 and not a silent save of a hole.
        raise HTTPException(status_code=422, detail=error.code)
    except ValueError:
        raise HTTPException(status_code=422, detail="api_key_required")
    return CredentialOut(
        provider=row.provider, base_url=row.base_url, model=row.model, configured=True
    )


# --- the session -------------------------------------------------------------


def _session(db: DbSession, org: Organization, session_id: uuid.UUID) -> AiSession:
    session = db.get(AiSession, session_id)
    if session is None or session.org_id != org.id:
        raise HTTPException(status_code=404, detail="ai_session_not_found")
    return session


def _provider(db: DbSession, org: Organization) -> LlmProvider:
    """The organization's provider behind two gates: rate and daily budget.

    The gates stand here because this is where all paths to the model converge —
    the interview, the summary, the draft, splitting a task. A separate check in
    every route is a route where someone will forget it one day.

    The rate is counted in the database by the same mechanism as sign-in and
    registration: a limit that a restart resets is no limit. Consumption is
    recorded by MeteredProvider — after the model's answer, because before the
    answer it is unknown.
    """
    settings = get_settings()
    if not throttle.hit(
        db,
        f"ai:org:{org.id}",
        limit=settings.ai_requests_per_minute,
        window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="ai_rate_limited")
    if not usage.budget_left(db, org):
        raise HTTPException(status_code=429, detail="ai_budget_exhausted")
    try:
        return usage.MeteredProvider(provider_for(db, org), db, org)
    except LlmError as error:
        raise _refuse(error)


def _out(session: AiSession) -> dict:
    gaps = intake.draft_coverage(session)
    return {
        "id": str(session.id),
        "status": session.status,
        "locale": session.locale,
        "transcript": session.transcript,
        "summary": session.summary,
        "draft": session.draft,
        # Which summary points the draft lost and which tasks have no origin.
        # `None` until there is a draft to judge.
        "coverage": gaps.as_json() if gaps is not None else None,
        "tokens_used": session.tokens_used,
        "project_id": str(session.project_id) if session.project_id else None,
        "applied_batch_id": (
            str(session.applied_batch_id) if session.applied_batch_id else None
        ),
    }


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The interview's language is fixed on the session: otherwise someone who
    #: switched the interface mid-conversation would get a draft in two
    #: languages at once.
    locale: str | None = None


@router.post("/sessions", status_code=201)
def start_session(
    payload: StartIn, user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    provider = _provider(db, org)
    try:
        session = intake.start(
            db, org=org, user=user, locale=payload.locale or user.locale, provider=provider
        )
    except LlmError as error:
        raise _refuse(error)
    return _out(session)


@router.get("/sessions/{session_id}")
def read_session(
    session_id: uuid.UUID, user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    return _out(_session(db, org, session_id))


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


@router.post("/sessions/{session_id}/answers")
def answer_question(
    session_id: uuid.UUID,
    payload: AnswerIn,
    user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        intake.answer(db, session, payload.text, _provider(db, org))
    except intake.IntakeError as error:
        raise HTTPException(status_code=409, detail=error.code)
    except LlmError as error:
        raise _refuse(error)
    return _out(session)


@router.post("/sessions/{session_id}/summary")
def build_summary(
    session_id: uuid.UUID, user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    """Gate 1: "here is what I understood about the project"."""
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        intake.make_summary(db, session, _provider(db, org))
    except LlmError as error:
        raise _refuse(error)
    return _out(session)


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theses: list[str]


@router.put("/sessions/{session_id}/summary")
def edit_summary(
    session_id: uuid.UUID,
    payload: SummaryIn,
    user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        intake.edit_summary(db, session, payload.theses)
    except intake.IntakeError as error:
        raise HTTPException(status_code=409, detail=error.code)
    return _out(session)


@router.post("/sessions/{session_id}/draft")
def build_draft(
    session_id: uuid.UUID, user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    """Gate 2, the main one: the draft. Nothing has been written into the project."""
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        intake.make_draft(db, session, _provider(db, org))
    except intake.IntakeError as error:
        raise HTTPException(status_code=409, detail=error.code)
    except LlmError as error:
        # The session stays on the previous step: the conversation and
        # everything established are still there, and the same request can be
        # repeated.
        raise _refuse(error)
    return _out(session)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: dict


@router.put("/sessions/{session_id}/draft")
def edit_draft(
    session_id: uuid.UUID,
    payload: DraftIn,
    user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        intake.edit_draft(db, session, payload.draft)
    except intake.IntakeError as error:
        raise HTTPException(status_code=409, detail=error.code)
    except LlmError as error:
        # A person's edit is validated by the schema just like the model's
        # answer: otherwise a draft spoiled by hand would make it to being applied.
        raise HTTPException(status_code=422, detail=error.code)
    return _out(session)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


@router.post("/sessions/{session_id}/apply", status_code=201)
def apply_session(
    session_id: uuid.UUID,
    payload: ApplyIn,
    user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    """Step 4: applying as a batch of mutations with a shared batch_id."""
    org = db.get(Organization, membership.org_id)
    session = _session(db, org, session_id)
    try:
        project, batch_id = intake.apply_draft(db, session, name=payload.name, actor=user)
    except intake.IntakeError as error:
        raise HTTPException(status_code=409, detail=error.code)
    except LlmError as error:
        raise HTTPException(status_code=422, detail=error.code)
    return {"project_id": str(project.id), "batch_id": str(batch_id), "session": _out(session)}


# --- a single, targeted action -----------------------------------------------


def _own_task(db: DbSession, org: Organization, task_id: uuid.UUID) -> tuple[Project, Task]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    project = db.get(Project, task.project_id)
    if project is None or project.org_id != org.id:
        raise HTTPException(status_code=404, detail="task_not_found")
    return project, task


@router.post("/tasks/{task_id}/split")
def propose_split(
    task_id: uuid.UUID, user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    """A suggestion to split a task. Writes nothing: it is applied by a button."""
    org = db.get(Organization, membership.org_id)
    _, task = _own_task(db, org, task_id)
    try:
        parts = intake.propose_split(db, task, _provider(db, org), locale=user.locale)
    except LlmError as error:
        raise _refuse(error)
    return {"task_id": str(task.id), "parts": parts}


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[dict]


@router.post("/tasks/{task_id}/split/apply", status_code=201)
def apply_split(
    task_id: uuid.UUID,
    payload: SplitIn,
    user: User = Depends(current_user),
    membership: Membership = Depends(_writer),
    db: DbSession = Depends(get_db),
):
    org = db.get(Organization, membership.org_id)
    project, task = _own_task(db, org, task_id)
    try:
        batch_id = intake.apply_split(db, project, task, payload.parts, actor=user)
    except LlmError as error:
        raise HTTPException(status_code=422, detail=error.code)
    return {"batch_id": str(batch_id)}
