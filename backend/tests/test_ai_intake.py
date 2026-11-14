"""The AI intake — item 10 of section 13.

Against recorded model answers, with no network: a valid schema is applied, a broken
one is rejected and does not bring the session down. `RecordedProvider` is not a stub
"so it compiles" but a full implementation of the same interface as the production
provider.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai import intake
from app.ai.provider import LlmError, RecordedProvider
from app.crypto import decrypt
from app.db import get_db
from app.main import app
from app.models import AiSession, Category, Membership, Organization, OrgLlmCredential, Task, User


@pytest.fixture
def client(db):
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def authed(client):
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    return client


@pytest.fixture
def org(db, authed) -> Organization:
    user_id = authed.get("/api/auth/me").json()["id"]
    membership = db.scalar(select(Membership).where(Membership.user_id == uuid.UUID(user_id)))
    return db.get(Organization, membership.org_id)


@pytest.fixture
def user(db, authed) -> User:
    return db.scalar(select(User).where(User.email == "alex@example.com"))


QUESTION = {"question": "Какой результат считается успехом?", "covered_topics": ["goal"]}
SUMMARY = {"theses": ["Сайт-визитка к июню", "Дизайн делает подрядчик"]}
# Every task cites the summary points it follows from, and every point is cited:
# a draft with a gap is sent back to the model, which would cost these tests an
# extra recorded answer.
DRAFT = {
    "context_theses": [],
    "categories": [
        {
            "name": "Дизайн",
            "tasks": [
                {
                    "name": "Логотип",
                    "description": "Знак и написание",
                    "start_date": "2026-03-02",
                    "duration_days": 5,
                    "criticality": "high",
                    "sources": [1, 2],
                }
            ],
        },
        {
            "name": "Разработка",
            "tasks": [
                {
                    "name": "Вёрстка",
                    "start_date": "2026-03-09",
                    "duration_days": 10,
                    "sources": [1],
                }
            ],
        },
    ]
}
BROKEN_DRAFT = {"categories": [{"name": "Дизайн", "tasks": [{"name": "Логотип"}]}]}


def _interview(db, org, user, provider) -> AiSession:
    return intake.start(db, org=org, user=user, locale="ru", provider=provider)


# --- the interview ------------------------------------------------------------


def test_the_interview_asks_one_question_at_a_time(db, org, user):
    provider = RecordedProvider([QUESTION, {"question": "Кто участвует?", "covered_topics": []}])

    session = _interview(db, org, user, provider)

    assert session.status == "interview"
    assert [turn["question"] for turn in session.transcript] == [QUESTION["question"]]

    intake.answer(db, session, "Сайт-визитка", provider)
    assert session.transcript[0]["answer"] == "Сайт-визитка"
    assert len(session.transcript) == 2


def test_the_language_of_the_session_goes_into_the_prompt(db, org, user):
    """The language is passed as an explicit parameter rather than guessed from the answers' text."""
    provider = RecordedProvider([QUESTION])

    intake.start(db, org=org, user=user, locale="az", provider=provider)

    system = provider.calls[0][0]
    assert system["role"] == "system"
    assert "az" in system["content"]


def test_the_interview_stops_at_the_ceiling(db, org, user, monkeypatch):
    """A hard ceiling on questions: without it the model clarifies endlessly."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_max_questions", 2, raising=False)
    provider = RecordedProvider([QUESTION, QUESTION, QUESTION])
    session = _interview(db, org, user, provider)

    assert intake.answer(db, session, "первый ответ", provider) is not None
    # The second answer hits the ceiling: there is no next question, it is time for the
    # summary.
    assert intake.answer(db, session, "второй ответ", provider) is None
    assert len(session.transcript) == 2


def test_tokens_are_counted_per_session(db, org, user):
    provider = RecordedProvider([QUESTION, QUESTION], tokens=42)
    session = _interview(db, org, user, provider)

    intake.answer(db, session, "ответ", provider)

    assert session.tokens_used == 84


# --- the gates ----------------------------------------------------------------


def test_the_summary_is_a_gate_and_is_editable(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY])
    session = _interview(db, org, user, provider)

    theses = intake.make_summary(db, session, provider)

    assert theses == SUMMARY["theses"]
    assert session.status == "summary"

    edited = intake.edit_summary(db, session, ["Сайт-визитка к июню", "  ", "Домен уже куплен"])
    # Empty summary points are discarded: an edit is an edit precisely because a point
    # is deleted by erasing its text rather than through a separate button.
    assert edited == ["Сайт-визитка к июню", "Домен уже куплен"]


def test_a_valid_draft_is_accepted(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)

    draft = intake.make_draft(db, session, provider)

    assert session.status == "draft"
    assert [category["name"] for category in draft["categories"]] == ["Дизайн", "Разработка"]
    # Nothing has been written into the project meanwhile: the draft lives in the session.
    assert session.project_id is None


def test_a_broken_draft_is_retried_and_then_refused_without_killing_the_session(
    db, org, user, monkeypatch
):
    """A broken schema is rejected and does not bring the session down.

    The conversation and the summary stay in place and the status does not change — the
    session continues from the same place, and the same request can be repeated.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 2, raising=False)
    provider = RecordedProvider([QUESTION, SUMMARY, BROKEN_DRAFT, BROKEN_DRAFT, BROKEN_DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)

    with pytest.raises(LlmError) as error:
        intake.make_draft(db, session, provider)

    assert error.value.code == "llm_schema_mismatch"
    # Three attempts: the first plus two retries.
    assert len(provider.calls) == 5
    assert session.status == "summary"
    assert session.summary == SUMMARY["theses"]
    assert session.transcript


def test_a_retry_succeeds_after_one_broken_answer(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, BROKEN_DRAFT, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)

    draft = intake.make_draft(db, session, provider)

    assert len(draft["categories"]) == 2


def test_an_unreachable_model_is_not_retried(db, org, user):
    """A retry only on a broken schema.

    An unreachable network will not be fixed by a retry, while the token spend triples.
    """
    provider = RecordedProvider(
        [QUESTION, SUMMARY, LlmError("llm_unreachable", "сеть недоступна")]
    )
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)

    with pytest.raises(LlmError) as error:
        intake.make_draft(db, session, provider)

    assert error.value.code == "llm_unreachable"
    assert len(provider.calls) == 3


def test_a_draft_edited_by_hand_is_validated_the_same_way(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    intake.make_draft(db, session, provider)

    with pytest.raises(LlmError):
        intake.edit_draft(db, session, BROKEN_DRAFT)

    # The previous draft survived: the rejected edit erased nothing.
    assert len(session.draft["categories"]) == 2


# --- application ---------------------------------------------------------------


def test_applying_writes_the_project_as_one_batch(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    intake.make_draft(db, session, provider)

    project, batch_id = intake.apply_draft(db, session, name="Сайт", actor=user)

    categories = db.scalars(select(Category).where(Category.project_id == project.id)).all()
    tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    assert sorted(category.name for category in categories) == ["Дизайн", "Разработка"]
    assert sorted(task.name for task in tasks) == ["Вёрстка", "Логотип"]
    # Every task remembers which session brought it: "created by an AI session" stays in
    # the history.
    assert {task.created_by_ai_session_id for task in tasks} == {session.id}
    assert session.applied_batch_id == batch_id
    assert session.status == "applied"


def test_the_whole_batch_rolls_back_with_one_call(db, org, user):
    from app.mutations import undo_batch

    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    intake.make_draft(db, session, provider)
    project, batch_id = intake.apply_draft(db, session, name="Сайт", actor=user)

    undo_batch(db, project, batch_id, actor_id=user.id)

    assert db.scalars(select(Task).where(Task.project_id == project.id)).all() == []
    assert db.scalars(select(Category).where(Category.project_id == project.id)).all() == []


def test_applying_twice_is_refused(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    intake.make_draft(db, session, provider)
    intake.apply_draft(db, session, name="Сайт", actor=user)

    with pytest.raises(intake.IntakeError) as error:
        intake.apply_draft(db, session, name="Сайт ещё раз", actor=user)

    assert error.value.code == "already_applied"


def test_nothing_can_be_applied_before_the_draft(db, org, user):
    """The gates cannot be bypassed: there is nothing to apply while there is no draft."""
    provider = RecordedProvider([QUESTION])
    session = _interview(db, org, user, provider)

    with pytest.raises(intake.IntakeError) as error:
        intake.apply_draft(db, session, name="Сайт", actor=user)

    assert error.value.code == "wrong_step"


# --- splitting a task ----------------------------------------------------------


def test_split_proposes_but_does_not_write(db, org, user):
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    intake.make_draft(db, session, provider)
    project, _ = intake.apply_draft(db, session, name="Сайт", actor=user)
    task = db.scalar(select(Task).where(Task.name == "Вёрстка"))
    before = len(db.scalars(select(Task).where(Task.project_id == project.id)).all())

    split_provider = RecordedProvider(
        [
            {
                "parts": [
                    {"name": "Шапка", "duration_days": 3},
                    {"name": "Главная", "duration_days": 4},
                    {"name": "Внутренние страницы", "duration_days": 3},
                ]
            }
        ]
    )
    parts = intake.propose_split(db, task, split_provider, locale="ru")

    assert [part["name"] for part in parts] == ["Шапка", "Главная", "Внутренние страницы"]
    # A suggestion is a suggestion: nothing in the project has changed.
    assert len(db.scalars(select(Task).where(Task.project_id == project.id)).all()) == before

    batch_id = intake.apply_split(db, project, task, parts, actor=user)
    after = db.scalars(select(Task).where(Task.project_id == project.id)).all()
    assert len(after) == before + 3
    # The original task is in place: a person decides its fate, and a silent deletion
    # would carry away its history and assignments.
    assert task in after
    assert batch_id is not None


# --- the key -------------------------------------------------------------------


def test_the_key_is_stored_encrypted_and_never_returned(authed, db, org):
    response = authed.put(
        "/api/ai/credential",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-secret-value",
        },
    )

    assert response.status_code == 200
    assert "api_key" not in response.json()
    assert response.json()["configured"] is True

    row = db.scalar(select(OrgLlmCredential).where(OrgLlmCredential.org_id == org.id))
    assert "sk-secret-value" not in row.encrypted_key
    assert decrypt(row.encrypted_key) == "sk-secret-value"

    # It is never handed outward — only the flag "a key is configured".
    read = authed.get("/api/ai/credential").json()
    assert read == {
        "provider": "openai",
        "base_url": "https://api.example.com/v1",
        "model": "gpt-4o-mini",
        "configured": True,
    }


def test_the_address_and_model_can_be_changed_without_retyping_the_key(authed, db, org):
    authed.put(
        "/api/ai/credential",
        json={"base_url": "https://api.example.com/v1", "model": "a", "api_key": "sk-one"},
    )

    authed.put(
        "/api/ai/credential",
        json={"base_url": "http://localhost:8080/v1", "model": "llama", "api_key": ""},
    )

    row = db.scalar(select(OrgLlmCredential).where(OrgLlmCredential.org_id == org.id))
    assert row.base_url == "http://localhost:8080/v1"
    assert decrypt(row.encrypted_key) == "sk-one"


def test_the_first_connection_needs_a_key(authed):
    response = authed.put(
        "/api/ai/credential", json={"base_url": "https://x/v1", "model": "m", "api_key": ""}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "api_key_required"


def test_without_a_key_the_ai_routes_say_so_plainly(authed):
    """No key means the AI buttons are inactive with a link into settings.

    A separate code rather than a generic error: this is not a breakage but an
    unconfigured installation.
    """
    response = authed.post("/api/ai/sessions", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "llm_not_configured"


def test_only_the_owner_sees_and_sets_the_connection(authed, db):
    user_id = authed.get("/api/auth/me").json()["id"]
    membership = db.scalar(select(Membership).where(Membership.user_id == uuid.UUID(user_id)))
    membership.role = "editor"
    db.flush()

    assert authed.get("/api/ai/credential").status_code == 403
    assert (
        authed.put(
            "/api/ai/credential",
            json={"base_url": "https://x/v1", "model": "m", "api_key": "sk"},
        ).status_code
        == 403
    )


# --- tracing the draft back to the summary -------------------------------------

THREE_THESES = {
    "theses": ["Сайт-визитка к июню", "Нужна форма заявки", "Бюджет согласован и не меняется"]
}


def _draft(*tasks: dict, context: list[int] | None = None) -> dict:
    return {
        "context_theses": context or [],
        "categories": [
            {
                "name": "Разработка",
                "tasks": [
                    {"start_date": "2026-03-02", "duration_days": 3, **task} for task in tasks
                ],
            }
        ],
    }


LOST_A_POINT = _draft({"name": "Вёрстка", "sources": [1]}, context=[3])
INVENTED_A_TASK = _draft(
    {"name": "Вёрстка", "sources": [1]},
    {"name": "Форма заявки", "sources": [2]},
    {"name": "Мобильное приложение", "sources": []},
    context=[3],
)
TRACED = _draft(
    {"name": "Вёрстка", "sources": [1]}, {"name": "Форма заявки", "sources": [2]}, context=[3]
)


def _at_the_summary(db, org, user, provider) -> AiSession:
    session = _interview(db, org, user, provider)
    intake.make_summary(db, session, provider)
    return session


def test_the_summary_points_reach_the_model_numbered(db, org, user):
    """The references are numbers, so the model has to be shown the numbers."""
    provider = RecordedProvider([QUESTION, THREE_THESES, TRACED])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    request = provider.calls[-1][-1]["content"]
    assert "1. Сайт-визитка к июню" in request
    assert "3. Бюджет согласован и не меняется" in request


def test_a_draft_that_lost_a_summary_point_is_sent_back_with_the_point_named(db, org, user):
    provider = RecordedProvider([QUESTION, THREE_THESES, LOST_A_POINT, TRACED])
    session = _at_the_summary(db, org, user, provider)

    draft = intake.make_draft(db, session, provider)

    repair = provider.calls[-1]
    # The model sees its own draft and the exact gap rather than "try again".
    assert repair[-2]["role"] == "assistant"
    assert "Тезисы без задач: 2." in repair[-1]["content"]
    assert [task["name"] for task in draft["categories"][0]["tasks"]] == ["Вёрстка", "Форма заявки"]
    assert intake.draft_coverage(session).clean


def test_a_task_with_no_origin_is_sent_back_by_name(db, org, user):
    provider = RecordedProvider([QUESTION, THREE_THESES, INVENTED_A_TASK, TRACED])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    assert "Задачи без тезиса: «Мобильное приложение»." in provider.calls[-1][-1]["content"]
    assert intake.draft_coverage(session).clean


def test_a_point_declared_as_context_needs_no_task(db, org, user):
    """"The budget is fixed" is a constraint: demanding a task for it would make the model invent one."""
    provider = RecordedProvider([QUESTION, THREE_THESES, TRACED])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    gaps = intake.draft_coverage(session)
    assert gaps.context == [3]
    assert gaps.uncovered == []
    # One call for the draft: nothing to repair.
    assert len(provider.calls) == 3


def test_gaps_that_survive_the_retries_are_reported_rather_than_refused(db, org, user, monkeypatch):
    """A draft with a gap is still a plan: the person at the gate sees the gap and decides."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 1, raising=False)
    provider = RecordedProvider([QUESTION, THREE_THESES, INVENTED_A_TASK, LOST_A_POINT])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    assert session.status == "draft"
    gaps = intake.draft_coverage(session)
    # Both attempts have one gap; the later one is kept.
    assert gaps.uncovered == [2]
    assert gaps.unsourced == []


def test_a_repair_that_came_back_worse_does_not_replace_the_better_draft(
    db, org, user, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 1, raising=False)
    worse = _draft({"name": "Мобильное приложение", "sources": []})
    provider = RecordedProvider([QUESTION, THREE_THESES, LOST_A_POINT, worse])
    session = _at_the_summary(db, org, user, provider)

    draft = intake.make_draft(db, session, provider)

    assert [task["name"] for task in draft["categories"][0]["tasks"]] == ["Вёрстка"]


def test_a_broken_repair_keeps_the_draft_that_passed_the_schema(db, org, user, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 1, raising=False)
    provider = RecordedProvider([QUESTION, THREE_THESES, LOST_A_POINT, BROKEN_DRAFT])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    assert session.status == "draft"
    assert intake.draft_coverage(session).uncovered == [2]


def test_out_of_range_references_are_stripped_from_the_stored_draft(db, org, user, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 0, raising=False)
    cites_nothing_real = _draft(
        {"name": "Вёрстка", "sources": [2, 1, 1]}, {"name": "Реклама", "sources": [9]}, context=[3, 7]
    )
    provider = RecordedProvider([QUESTION, THREE_THESES, cites_nothing_real])
    session = _at_the_summary(db, org, user, provider)

    draft = intake.make_draft(db, session, provider)

    tasks = draft["categories"][0]["tasks"]
    assert tasks[0]["sources"] == [1, 2]
    assert tasks[1]["sources"] == []
    assert draft["context_theses"] == [3]
    assert intake.draft_coverage(session).unsourced == [(0, 1)]


def test_deleting_a_task_by_hand_uncovers_its_point(db, org, user):
    provider = RecordedProvider([QUESTION, THREE_THESES, TRACED])
    session = _at_the_summary(db, org, user, provider)
    intake.make_draft(db, session, provider)

    intake.edit_draft(db, session, LOST_A_POINT)

    assert intake.draft_coverage(session).uncovered == [2]


def test_editing_the_summary_moves_the_references_with_the_points(db, org, user):
    """References are positions: deleting point 1 must not re-aim "2" at another sentence."""
    provider = RecordedProvider([QUESTION, THREE_THESES, TRACED])
    session = _at_the_summary(db, org, user, provider)
    intake.make_draft(db, session, provider)

    intake.edit_summary(
        db, session, ["Нужна форма заявки", "Бюджет согласован и не меняется", "Домен уже куплен"]
    )

    tasks = session.draft["categories"][0]["tasks"]
    # "Вёрстка" followed from the deleted point and lost its origin; the form task
    # followed its point from number 2 to number 1.
    assert [task["sources"] for task in tasks] == [[], [1]]
    assert session.draft["context_theses"] == [2]
    gaps = intake.draft_coverage(session)
    assert gaps.unsourced == [(0, 0)]
    # The point added by hand has no task yet.
    assert gaps.uncovered == [3]


def test_a_draft_without_references_still_parses(db, org, user, monkeypatch):
    """A draft stored before references existed, or edited by hand, is unsourced rather than invalid."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 0, raising=False)
    legacy = {
        "categories": [
            {"name": "Дизайн", "tasks": [{"name": "Логотип", "start_date": "2026-03-02", "duration_days": 5}]}
        ]
    }
    provider = RecordedProvider([QUESTION, SUMMARY, legacy])
    session = _at_the_summary(db, org, user, provider)

    intake.make_draft(db, session, provider)

    gaps = intake.draft_coverage(session)
    assert gaps.uncovered == [1, 2]
    assert gaps.unsourced == [(0, 0)]


def test_the_rules_of_each_step_reach_the_prompt(db, org, user):
    """The rules file is read and lands in the request of the step it belongs to."""
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _at_the_summary(db, org, user, provider)
    intake.make_draft(db, session, provider)

    question, summary, draft = (call[-1]["content"] for call in provider.calls)
    assert "Не додумывай за человека" in question
    assert "Один тезис — один факт" in summary
    assert "Категории не пересекаются" in draft
    # The rules of one step do not leak into another.
    assert "Категории не пересекаются" not in summary


def test_the_draft_request_states_todays_date(db, org, user):
    """The model has no clock, and the plan is laid out from today."""
    from datetime import datetime, timezone

    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    session = _at_the_summary(db, org, user, provider)
    intake.make_draft(db, session, provider)

    assert datetime.now(timezone.utc).date().isoformat() in provider.calls[-1][-1]["content"]


def test_the_config_files_ship_inside_the_backend_package():
    """Both files must sit under backend/app: that is all the Docker image and the Vercel bundle carry."""
    from pathlib import Path

    import app

    package = Path(app.__file__).resolve().parent
    for path in (intake._TOPICS_FILE, intake._RULES_FILE):
        assert path.is_file()
        assert package in path.parents


def test_topics_are_read_from_the_config_file():
    """The list of mandatory topics is a file next to the application rather than a table in the database."""
    keys = {topic["key"] for topic in intake.topics()}

    assert {"goal", "scope", "deadline", "out_of_scope"} <= keys


# --- the routes ----------------------------------------------------------------


def test_the_session_walks_the_whole_way_over_http(authed, db, org, monkeypatch):
    """Through HTTP: interview -> summary -> draft -> application."""
    import app.api.ai_routes as routes
    from app.config import get_settings

    # One question instead of twelve: what is checked is the whole road rather than the
    # interview's depth — that is checked by a separate test about the ceiling.
    monkeypatch.setattr(get_settings(), "ai_max_questions", 1, raising=False)
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    monkeypatch.setattr(routes, "provider_for", lambda db, org: provider)

    started = authed.post("/api/ai/sessions", json={"locale": "ru"})
    assert started.status_code == 201
    session_id = started.json()["id"]

    authed.post(f"/api/ai/sessions/{session_id}/answers", json={"text": "Сайт-визитка"})
    summary = authed.post(f"/api/ai/sessions/{session_id}/summary")
    assert summary.json()["summary"] == SUMMARY["theses"]

    assert summary.json()["coverage"] is None

    draft = authed.post(f"/api/ai/sessions/{session_id}/draft")
    assert draft.json()["status"] == "draft"
    assert draft.json()["coverage"] == {"uncovered": [], "context": [], "unsourced": []}

    applied = authed.post(f"/api/ai/sessions/{session_id}/apply", json={"name": "Сайт"})
    assert applied.status_code == 201
    project_id = applied.json()["project_id"]

    state = authed.get(f"/api/projects/{project_id}").json()
    assert sorted(category["name"] for category in state["categories"]) == ["Дизайн", "Разработка"]
    # The batch is visible as one action: it rolls back as a whole rather than operation by operation.
    assert state["undoable"]["batch_id"] == applied.json()["batch_id"]


def test_a_broken_answer_over_http_keeps_the_session_alive(authed, db, org, monkeypatch):
    import app.api.ai_routes as routes
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_schema_retries", 0, raising=False)
    provider = RecordedProvider([QUESTION, SUMMARY, BROKEN_DRAFT])
    monkeypatch.setattr(routes, "provider_for", lambda db, org: provider)

    session_id = authed.post("/api/ai/sessions", json={"locale": "ru"}).json()["id"]
    authed.post(f"/api/ai/sessions/{session_id}/summary")

    refused = authed.post(f"/api/ai/sessions/{session_id}/draft")

    assert refused.status_code == 502
    assert refused.json()["detail"] == "llm_schema_mismatch"
    # The session is alive and stands on the previous step: the conversation and the summary are in place.
    alive = authed.get(f"/api/ai/sessions/{session_id}").json()
    assert alive["status"] == "summary"
    assert alive["summary"] == SUMMARY["theses"]


def test_a_session_of_another_organization_is_not_found(authed, db, monkeypatch):
    import app.api.ai_routes as routes

    monkeypatch.setattr(routes, "provider_for", lambda db, org: RecordedProvider([QUESTION]))
    other = Organization(name="Globex", slug="globex")
    db.add(other)
    db.flush()
    stranger = AiSession(org_id=other.id, locale="ru", status="interview", transcript=[])
    db.add(stranger)
    db.flush()

    assert authed.get(f"/api/ai/sessions/{stranger.id}").status_code == 404
