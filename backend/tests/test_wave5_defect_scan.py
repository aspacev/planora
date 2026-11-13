"""Wave 5 regression tests: the defects found by the full scan of the code.

Every test reproduces a path that, before the fix, either showed a reader what
they were not entitled to see, let a narrowed member write outside their
projects, or lost a person's work without a word: an internal remark made public
by a delete and an undo, per-person assessments in a metric's drill-down, the AI
routes ignoring a membership narrowed to particular projects, a PDF that dropped
every task past the first page, a start date that could not be assigned or that
threw tasks back to 2001 on undo, an AI budget that only counted successes, and
a batch rollback that deleted tasks added to its categories afterwards.
"""

import io
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.api.ai_routes as ai_routes
import app.api.export_routes as export_routes
from app.ai.provider import RecordedProvider
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import (
    AiUsage,
    Membership,
    Organization,
    Project,
    ProjectAccess,
    Task,
    ThrottleEvent,
    User,
)
from app.mutations import CreateCategory, CreateTask, apply_op

BAKU = ZoneInfo("Asia/Baku")

QUESTION = {"question": "Q?", "covered_topics": ["goal"]}
SUMMARY = {"theses": ["a", "b"]}
DRAFT = {
    "categories": [
        {
            "name": "Design",
            "tasks": [
                {"name": "Logo", "start_date": "2026-03-02", "duration_days": 5},
                {"name": "Site", "start_date": "2026-03-09", "duration_days": 10},
            ],
        }
    ]
}


@pytest.fixture
def client(db):
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _register(client, email="alex@example.com", company="Acme"):
    response = client.post(
        "/api/auth/register",
        json={"name": "Alex", "email": email, "password": "s3cret-pass", "company_name": company},
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def authed(client):
    return _register(client)


def _mutate(client, project_id, op, expect=201):
    response = client.post(f"/api/projects/{project_id}/mutations", json={"op": op})
    assert response.status_code == expect, response.text
    return response.json()


def _project(client, name="Redesign"):
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _category(client, project_id, name="Design"):
    op = {"type": "create_category", "name": name, "color": "#3b82f6"}
    return _mutate(client, project_id, op)["op"]["category_id"]


def _task(client, project_id, category_id, *, start="2026-03-02", days=3, name="T"):
    op = {
        "type": "create_task", "category_id": category_id, "name": name,
        "start_date": start, "duration_days": days,
    }
    return _mutate(client, project_id, op)["op"]["task_id"]


def _state(client, project_id):
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _as_member(db, org_id, *, email, role, scoped=False):
    """A second person in the same organization, signed in on a client of their own."""
    other = TestClient(app)
    _register(other, email, "Elsewhere")
    user = db.scalar(select(User).where(User.email == email))
    db.add(Membership(org_id=org_id, user_id=user.id, role=role, project_scoped=scoped))
    db.flush()
    assert other.post("/api/org/switch", json={"org_id": str(org_id)}).status_code == 200
    return other, user


def _org_id(db, email="alex@example.com"):
    user = db.scalar(select(User).where(User.email == email))
    return db.scalar(select(Membership.org_id).where(Membership.user_id == user.id))


# --- 5.1: an internal remark survives a delete and an undo as internal ---------


@pytest.mark.parametrize("deleted", ["task", "category"])
def test_an_internal_remark_stays_internal_after_delete_and_undo(authed, deleted):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    task_id = _task(authed, project_id, category_id)
    token = authed.post(f"/api/projects/{project_id}/share").json()["url"].split("?s=")[1]
    for body, internal in (("the client is behind on payments", True), ("see you Monday", False)):
        response = authed.post(
            f"/api/projects/{project_id}/comments",
            json={"body": body, "task_id": task_id, "internal": internal},
        )
        assert response.status_code == 201, response.text

    op = (
        {"type": "delete_task", "task_id": task_id}
        if deleted == "task"
        else {"type": "delete_category", "category_id": category_id}
    )
    _mutate(authed, project_id, op)
    assert authed.post(f"/api/projects/{project_id}/undo", json={}).status_code == 201

    guest_feed = authed.get("/api/public/acme/redesign/comments", params={"s": token}).json()
    assert [comment["body"] for comment in guest_feed] == ["see you Monday"]
    counts = authed.get("/api/public/acme/redesign/comments/counts", params={"s": token}).json()
    assert counts == {task_id: 1}
    # The member still has both, and the aside is still marked as one.
    own_feed = authed.get(f"/api/projects/{project_id}/comments").json()
    assert {comment["body"]: comment["internal"] for comment in own_feed} == {
        "the client is behind on payments": True,
        "see you Monday": False,
    }


# --- 5.2: the team-pace drill-down is trimmed like the scorecard itself --------


def test_the_team_pace_drilldown_hides_people_from_those_not_entitled(authed, db):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    project = db.get(Project, uuid.UUID(project_id))
    today = datetime.now(BAKU).date()
    project.schedule_mode = "calendar"
    project.start_date = today - timedelta(days=60)
    db.flush()
    week = today - timedelta(days=today.weekday())
    task_id = _task(authed, project_id, category_id, start=week.isoformat(), days=1)
    owner_id = authed.get("/api/auth/me").json()["id"]
    _mutate(authed, project_id, {"type": "assign_user", "task_id": task_id, "user_id": owner_id})
    url = f"/api/projects/{project_id}/scorecard/metrics/team_pace/tasks"

    people = authed.get(url).json()["details"]["by_person"]
    assert people and "signal" in people[0] and "reason" in people[0]

    org_id = _org_id(db)
    for role in ("editor", "viewer"):
        member, _ = _as_member(db, org_id, email=f"{role}@example.com", role=role)
        people = member.get(url).json()["details"]["by_person"]
        assert people, role
        assert "signal" not in people[0] and "reason" not in people[0], role
        assert people[0]["planned"] == 1, role

    client, user = _as_member(db, org_id, email="client@example.com", role="client")
    db.add(ProjectAccess(project_id=uuid.UUID(project_id), user_id=user.id))
    db.flush()
    details = client.get(url).json()["details"]
    assert "by_person" not in details and "unassigned" not in details
    # The totals are the same numbers the scorecard's header already shows them.
    assert details["planned"] == 1


# --- 5.3: the AI routes respect a membership narrowed to particular projects ---


def _interview(client, monkeypatch, name):
    monkeypatch.setattr(get_settings(), "ai_max_questions", 1, raising=False)
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    monkeypatch.setattr(ai_routes, "provider_for", lambda db, org: provider)
    session_id = client.post("/api/ai/sessions", json={"locale": "ru"}).json()["id"]
    client.post(f"/api/ai/sessions/{session_id}/answers", json={"text": "x"})
    client.post(f"/api/ai/sessions/{session_id}/summary")
    client.post(f"/api/ai/sessions/{session_id}/draft")
    return session_id, client.post(f"/api/ai/sessions/{session_id}/apply", json={"name": name})


def test_a_narrowed_editor_cannot_reach_other_projects_through_the_ai_routes(
    authed, db, monkeypatch
):
    session_id, applied = _interview(authed, monkeypatch, "Site")
    project_id = uuid.UUID(applied.json()["project_id"])
    task = db.scalar(select(Task).where(Task.project_id == project_id, Task.name == "Logo"))
    narrowed, user = _as_member(
        db, _org_id(db), email="bob@example.com", role="editor", scoped=True
    )
    parts = {"parts": [{"name": "a", "duration_days": 1}, {"name": "b", "duration_days": 1}]}
    count = lambda: db.scalar(  # noqa: E731
        select(func.count()).select_from(Task).where(Task.project_id == project_id)
    )
    before = count()

    # A project they were not invited to: its tasks do not exist for them.
    assert narrowed.post(f"/api/ai/tasks/{task.id}/split").status_code == 404
    assert narrowed.post(f"/api/ai/tasks/{task.id}/split/apply", json=parts).status_code == 404
    assert count() == before
    # The interview ends in a new project, and a narrowed membership creates none.
    assert narrowed.post("/api/ai/sessions", json={"locale": "ru"}).status_code == 403
    assert narrowed.get(f"/api/ai/sessions/{session_id}").status_code == 403
    assert (
        narrowed.post(f"/api/ai/sessions/{session_id}/apply", json={"name": "X"}).status_code
        == 403
    )

    # Once granted, the same editor splits the task like any other.
    db.add(ProjectAccess(project_id=project_id, user_id=user.id))
    db.flush()
    assert narrowed.post(f"/api/ai/tasks/{task.id}/split/apply", json=parts).status_code == 201
    assert count() == before + 2


# --- 5.4: a PDF shows every task, on as many pages as it takes ----------------


def _positions(page, wanted):
    """Where on the page each wanted text sits, by its y coordinate."""
    found: dict[str, float] = {}

    def visit(text, cm, tm, font_dict, font_size):
        stripped = text.strip()
        if stripped in wanted:
            found.setdefault(stripped, tm[5])

    page.extract_text(visitor_text=visit)
    return found


def test_a_long_project_spans_pages_in_the_pdf_without_losing_a_task(authed, monkeypatch):
    monkeypatch.setattr(export_routes, "_limiter", None)
    project_id = _project(authed)
    authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"})
    names = set()
    for stage in ("Design", "Build"):
        category_id = _category(authed, project_id, stage)
        for number in range(40):
            name = f"{stage} {number + 1:03d}"
            names.add(name)
            _task(authed, project_id, category_id, days=2, name=name)

    def pages_of(sections):
        query = "&".join(f"include={section}" for section in sections) + "&locale=en"
        response = authed.get(f"/api/projects/{project_id}/export.pdf?{query}")
        assert response.status_code == 200, response.text
        return PdfReader(io.BytesIO(response.content)).pages

    for sections in (["tasks"], ["gantt"]):
        pages = pages_of(sections)
        assert len(pages) > 1, sections
        seen: set[str] = set()
        for page in pages:
            placed = _positions(page, names)
            # Nothing is drawn below the footer, let alone below the sheet.
            assert all(y > 40 for y in placed.values()), (sections, placed)
            seen |= set(placed)
        assert seen == names, (sections, sorted(names - seen)[:3])
        # A stage that continues on the next page is named there again.
        assert sum("BUILD" in (page.extract_text() or "") for page in pages) >= 2, sections


# --- 5.5: assigning and moving the start date -------------------------------


def test_the_start_date_moves_even_with_a_task_before_it(authed):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    task_id = _task(authed, project_id, category_id, start="2001-01-03", days=2)
    assert authed.post(
        f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"}
    ).status_code == 200
    # A task dragged to before the project's start — the chart allows that.
    _mutate(authed, project_id, {"type": "move_task", "task_id": task_id, "start_date": "2026-02-25"})

    later = {"start_date": "2026-04-06"}
    assert authed.post(f"/api/projects/{project_id}/schedule/preview", json=later).status_code == 200
    assert authed.post(f"/api/projects/{project_id}/schedule", json=later).status_code == 200
    # It keeps its distance from the start: five calendar days before it.
    assert _state(authed, project_id)["tasks"][0]["start_date"] == "2026-04-01"


def test_a_relative_plan_with_a_task_before_day_one_can_be_anchored(authed):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    _task(authed, project_id, category_id, start="2000-12-29", days=2)
    response = authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"})
    assert response.status_code == 200, response.text
    assert response.json()["tasks"][0]["start_date"] == "2026-02-27"


def test_undo_does_not_reach_behind_the_assignment_of_the_start_date(authed, db):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    task_id = _task(authed, project_id, category_id, start="2001-01-01", days=2)
    _mutate(authed, project_id, {"type": "move_task", "task_id": task_id, "start_date": "2001-01-08"})

    anchored = authed.post(
        f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"}
    ).json()
    assert anchored["tasks"][0]["start_date"] == "2026-03-09"
    # The earlier moves hold dates of the relative axis: they are history now.
    assert anchored["undoable"] is None
    refused = authed.post(f"/api/projects/{project_id}/undo", json={})
    assert refused.status_code == 404 and refused.json()["detail"] == "nothing_to_undo"
    assert _state(authed, project_id)["tasks"][0]["start_date"] == "2026-03-09"

    # An edit made after the assignment is undone as before.
    _mutate(authed, project_id, {"type": "move_task", "task_id": task_id, "start_date": "2026-03-16"})
    assert authed.post(f"/api/projects/{project_id}/undo", json={}).status_code == 201
    assert _state(authed, project_id)["tasks"][0]["start_date"] == "2026-03-09"


def test_a_batch_applied_before_the_start_date_is_not_rolled_back_after_it(authed, db):
    project_id = _project(authed)
    project = db.get(Project, uuid.UUID(project_id))
    batch_id = uuid.uuid4()
    category_id = apply_op(
        db, project, CreateCategory(name="Design", color="#3b82f6"),
        actor_id=None, batch_id=batch_id,
    ).op["category_id"]
    apply_op(
        db, project,
        CreateTask(category_id=category_id, name="A", start_date="2001-01-01", duration_days=2),
        actor_id=None, batch_id=batch_id,
    )
    authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"})

    refused = authed.post(f"/api/projects/{project_id}/batches/{batch_id}/undo", json={})
    assert refused.status_code == 404 and refused.json()["detail"] == "nothing_to_undo"
    assert len(_state(authed, project_id)["tasks"]) == 1


def test_re_saving_the_same_start_date_keeps_the_undo_history(authed):
    project_id = _project(authed)
    authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"})
    category_id = _category(authed, project_id)
    _task(authed, project_id, category_id, start="2026-03-04", days=2)
    again = authed.post(
        f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"}
    ).json()
    assert again["undoable"] is not None


def test_a_version_approved_before_the_start_date_is_restored_onto_todays_axis(authed):
    project_id = _project(authed)
    category_id = _category(authed, project_id)
    task_id = _task(authed, project_id, category_id, start="2001-01-03", days=2)
    assert authed.post(f"/api/projects/{project_id}/plan/approvals").status_code == 201
    authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-03-02"})
    # The start moves once more: the promise has now left two axes behind.
    authed.post(f"/api/projects/{project_id}/schedule", json={"start_date": "2026-04-06"})
    moved = authed.post(
        f"/api/projects/{project_id}/mutations",
        json={
            "op": {"type": "move_task", "task_id": task_id, "start_date": "2026-04-20"},
            "reason": "the client asked for a later start",
        },
    )
    assert moved.status_code == 201, moved.text
    assert authed.post(f"/api/projects/{project_id}/plan/approvals").status_code == 201

    assert authed.post(f"/api/projects/{project_id}/plan/approvals/1/restore").status_code == 201
    task = _state(authed, project_id)["tasks"][0]
    # Day 3 of the plan, as promised — on the calendar the project now lives in.
    assert task["baseline_start"] == "2026-04-08"
    assert task["start_date"] == "2026-04-20"
    # The chronicle still reads as it was written, with no bookkeeping in it.
    versions = authed.get(f"/api/projects/{project_id}/plan/approvals").json()
    first = next(version for version in versions if version["version"] == 1)
    assert first["snapshot"][task_id] == {
        "name": "T", "start_date": "2001-01-03", "duration_days": 2,
    }


# --- 5.6: the AI limits count calls that end in a refusal ---------------------


def test_failed_model_calls_still_count_against_the_rate_and_the_budget(engine, monkeypatch):
    """With the real get_db: a request that raises is rolled back, and the limits
    must have been written by then. The other AI tests share one transaction that
    is never rolled back, which is exactly why they could not see this."""
    import app.db as app_db

    Real = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(app_db, "SessionLocal", Real)
    app.dependency_overrides.pop(get_db, None)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_requests_per_minute", 3, raising=False)
    monkeypatch.setattr(settings, "ai_daily_token_budget", 12_000, raising=False)
    monkeypatch.setattr(settings, "ai_schema_retries", 0, raising=False)
    # Every answer is off-schema and costs 5000 tokens at the provider.
    provider = RecordedProvider([{"nonsense": True}] * 20, tokens=5000)
    monkeypatch.setattr(ai_routes, "provider_for", lambda db, org: provider)

    client = TestClient(app)
    email = f"u{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email, "Co " + uuid.uuid4().hex[:6])
    try:
        codes = [
            client.post("/api/ai/sessions", json={"locale": "ru"}).status_code for _ in range(5)
        ]
        with Real() as db:
            org_id = _org_id(db, email)
            spent = db.scalar(select(AiUsage.tokens).where(AiUsage.org_id == org_id))
            hits = db.scalar(
                select(func.count())
                .select_from(ThrottleEvent)
                .where(ThrottleEvent.bucket == f"ai:org:{org_id}")
            )
        # Three calls reach the model and fail; the rate limit stops the rest.
        assert codes[:3] == [502, 502, 502] and set(codes[3:]) == {429}, codes
        assert len(provider.calls) == 3
        assert hits == 3
        assert spent == 15_000

        # The minute passes; the budget, spent on failures alone, still refuses.
        monkeypatch.setattr(settings, "ai_requests_per_minute", 0, raising=False)
        refused = client.post("/api/ai/sessions", json={"locale": "ru"})
        assert refused.status_code == 429 and refused.json()["detail"] == "ai_budget_exhausted"
        assert len(provider.calls) == 3
    finally:
        # These rows were really committed: nothing rolls them back for us. Every
        # other test's throttle events live in a transaction that is never
        # committed, so the ones visible here are this test's own — the sign-up
        # hit included, which would otherwise count against later registrations.
        with Real() as db:
            org_id = _org_id(db, email)
            db.execute(ThrottleEvent.__table__.delete())
            db.delete(db.get(Organization, org_id))
            db.delete(db.scalar(select(User).where(User.email == email)))
            db.commit()


# --- 5.7: a batch rollback does not take other people's tasks with it ---------


def test_a_batch_is_not_rolled_back_over_tasks_added_to_it_later(authed, db):
    project_id = _project(authed)
    project = db.get(Project, uuid.UUID(project_id))
    batch_id = uuid.uuid4()
    category_id = apply_op(
        db, project, CreateCategory(name="Design", color="#3b82f6"),
        actor_id=None, batch_id=batch_id,
    ).op["category_id"]
    apply_op(
        db, project,
        CreateTask(category_id=category_id, name="From the batch", start_date="2026-03-02",
                   duration_days=2),
        actor_id=None, batch_id=batch_id,
    )
    own = _task(authed, project_id, category_id, name="My own task")

    refused = authed.post(f"/api/projects/{project_id}/batches/{batch_id}/undo", json={})
    assert refused.status_code == 409 and refused.json()["detail"] == "batch_has_later_tasks"
    # Nothing was touched — neither the person's task nor the batch itself.
    state = _state(authed, project_id)
    assert sorted(task["name"] for task in state["tasks"]) == ["From the batch", "My own task"]
    # "Undo" still works, and takes back the person's own last step.
    assert authed.post(f"/api/projects/{project_id}/undo", json={}).status_code == 201

    # With the person's task gone, the batch rolls back whole.
    assert own not in {task["id"] for task in _state(authed, project_id)["tasks"]}
    rolled = authed.post(f"/api/projects/{project_id}/batches/{batch_id}/undo", json={})
    assert rolled.status_code == 201, rolled.text
    state = _state(authed, project_id)
    assert state["tasks"] == [] and state["categories"] == []
