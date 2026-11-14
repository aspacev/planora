"""The end-to-end scenario — item 11 of section 13.

Create a project through an interview, approve the plan, shift a task with a reason,
open the public link in another browser, and make sure the chart and the comments are
visible while the internal notes are not.

The test is deliberately single and long: it checks not individual rules but the fact
that they add up into a working product. Broken into eight small ones it would stop
answering the single question it was written for.
"""

import pytest
from urllib.parse import urlsplit
from fastapi.testclient import TestClient

from app.ai.provider import RecordedProvider
from app.db import get_db
from app.main import app

QUESTION = {"question": "Какой результат считается успехом?", "covered_topics": ["goal"]}
SUMMARY = {"theses": ["Сайт-визитка к июню", "Дизайн делает подрядчик"]}
DRAFT = {
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
                    # Both summary points are behind the one task: a draft that
                    # leaves a point without a task is sent back to the model.
                    "sources": [1, 2],
                }
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


@pytest.fixture
def guest(db):
    """Another browser: its own session, its own client, the same server."""

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _public_path(url: str) -> str:
    """The public page's address for the test client.

    The link arrives with the domain from PUBLIC_BASE_URL while TestClient goes by
    paths: the domain is cut off, and the slugs and the token stay exactly as a guest
    will see them in a browser.
    """
    parts = urlsplit(url)
    return f"{parts.path.replace('/p/', '/api/public/', 1)}?{parts.query}"


def _with_comments(path: str) -> str:
    """The same address, but of the comment feed: the token stays in the parameters."""
    page, _, query = path.partition("?")
    return f"{page}/comments?{query}"


def test_the_whole_way_from_interview_to_the_public_link(client, guest, db, monkeypatch):
    import app.api.ai_routes as ai_routes
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_max_questions", 1, raising=False)
    provider = RecordedProvider([QUESTION, SUMMARY, DRAFT])
    monkeypatch.setattr(ai_routes, "provider_for", lambda db, org: provider)

    # 1. A person registers and ends up inside with an organization of their own.
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
        headers={"Accept-Language": "ru-RU,ru;q=0.9"},
    )
    assert client.get("/api/auth/me").json()["locale"] == "ru"

    # 2. The project is assembled through an interview: a question, an answer, a summary, a draft.
    session_id = client.post("/api/ai/sessions", json={"locale": "ru"}).json()["id"]
    client.post(f"/api/ai/sessions/{session_id}/answers", json={"text": "Сайт-визитка к июню"})
    client.post(f"/api/ai/sessions/{session_id}/summary")
    client.post(f"/api/ai/sessions/{session_id}/draft")

    applied = client.post(f"/api/ai/sessions/{session_id}/apply", json={"name": "Сайт"})
    assert applied.status_code == 201
    project_id = applied.json()["project_id"]

    state = client.get(f"/api/projects/{project_id}").json()
    task_id = state["tasks"][0]["id"]
    assert state["categories"][0]["name"] == "Дизайн"

    # 3. The internal note — the one a guest must not see under any circumstances.
    client.post(
        f"/api/projects/{project_id}/mutations",
        json={
            "op": {
                "type": "set_task_fields",
                "task_id": task_id,
                "name": "Логотип",
                "description": "Знак и написание",
                "internal_note": "подрядчик просит предоплату",
            }
        },
    )

    # 4. The plan is approved: a snapshot of the dates lands in the baseline plan.
    assert client.post(f"/api/projects/{project_id}/plan/approvals").status_code == 201
    approved = client.get(f"/api/projects/{project_id}").json()
    assert approved["tasks"][0]["baseline_start"] == "2026-03-02"

    # 5. A shift past the threshold with no reason is rejected...
    refused = client.post(
        f"/api/projects/{project_id}/mutations",
        json={"op": {"type": "move_task", "task_id": task_id, "start_date": "2026-03-16"}},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "reason_required"

    # ...and passes with a reason, which stays in the task's history as it was.
    client.post(
        f"/api/projects/{project_id}/mutations",
        json={
            "op": {"type": "move_task", "task_id": task_id, "start_date": "2026-03-16"},
            "reason": "заказчик не прислал брендбук",
        },
    )
    history = client.get(f"/api/projects/{project_id}/revisions?task_id={task_id}").json()
    assert history[0]["reason"] == "заказчик не прислал брендбук"

    # 6. The owner issues a public link.
    share = client.post(f"/api/projects/{project_id}/share", json={}).json()
    path = _public_path(share["url"])
    assert "/p/" in share["url"]

    # 7. Another browser, with no session at all: the chart is visible.
    public = guest.get(path)
    assert public.status_code == 200
    page = public.json()
    assert page["name"] == "Сайт"
    assert page["tasks"][0]["start_date"] == "2026-03-16"
    # The baseline plan is visible to a guest too: the ghost under a bar is the answer to
    # the question "and when was it promised".
    assert page["tasks"][0]["baseline_start"] == "2026-03-02"
    # And the internal note is in no field of the answer.
    assert "internal_note" not in page["tasks"][0]
    assert "подрядчик просит предоплату" not in public.text
    # And a guest has nothing to undo: the last action is not offered to them.
    assert page["undoable"] is None

    # 8. The guest comments, giving a name; their remark differs from a member's.
    client.post(
        f"/api/projects/{project_id}/comments",
        json={"body": "Держим сроки", "task_id": task_id},
    )
    posted = guest.post(
        _with_comments(path),
        json={"body": "А успеем до июня?", "name": "Мария"},
    )
    assert posted.status_code == 201
    assert posted.json()["author"] == {"name": "Мария", "guest": True}

    comments = guest.get(_with_comments(path)).json()
    assert [(item["author"]["name"], item["author"]["guest"]) for item in comments] == [
        ("Alex", False),
        ("Мария", True),
    ]

    # 9. Reissuing the link kills the previous one instantly. A separate rotate route
    # (wave 4.6): a repeated POST /share no longer touches the distributed address.
    fresh = client.post(f"/api/projects/{project_id}/share/rotate", json={}).json()
    assert guest.get(path).status_code == 404
    assert guest.get(_public_path(fresh["url"])).status_code == 200


def test_a_guest_cannot_comment_when_comments_are_off(client, guest, db):
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    project_id = client.post("/api/projects", json={"name": "Сайт"}).json()["id"]
    share = client.post(f"/api/projects/{project_id}/share", json={}).json()
    # Comments are turned off by a separate request: issuing a link and configuring it
    # are different actions, and the second is available after the first too.
    client.patch(f"/api/projects/{project_id}/share", json={"comments_enabled": False})
    path = _public_path(share["url"])

    refused = guest.post(_with_comments(path), json={"body": "привет", "name": "Мария"})

    assert refused.status_code == 403
    assert refused.json()["detail"] == "comments_closed"
    # Reading the project is still possible: what is off is the comments, not the link.
    assert guest.get(path).status_code == 200


def test_a_guest_must_name_themselves(client, guest, db):
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    project_id = client.post("/api/projects", json={"name": "Сайт"}).json()["id"]
    path = _public_path(client.post(f"/api/projects/{project_id}/share", json={}).json()["url"])

    refused = guest.post(_with_comments(path), json={"body": "привет"})

    # The guest's name is a mandatory field of the schema: an unsigned remark on a public
    # page is indistinguishable from someone else's.
    assert refused.status_code == 422


def test_a_revoked_link_is_indistinguishable_from_a_missing_one(client, guest, db):
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    project_id = client.post("/api/projects", json={"name": "Сайт"}).json()["id"]
    path = _public_path(client.post(f"/api/projects/{project_id}/share", json={}).json()["url"])

    client.delete(f"/api/projects/{project_id}/share")

    revoked = guest.get(path)
    missing = guest.get("/api/public/nobody/nothing?s=nonexistent-token")
    # A difference between them would be a hint to whoever is enumerating tokens.
    assert revoked.status_code == missing.status_code == 404
    assert revoked.json() == missing.json()


def test_public_sharing_can_be_switched_off_by_the_organization(client, db):
    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    project_id = client.post("/api/projects", json={"name": "Сайт"}).json()["id"]
    client.patch("/api/org", json={"public_sharing_enabled": False})

    refused = client.post(f"/api/projects/{project_id}/share", json={})

    assert refused.status_code == 403
    assert refused.json()["detail"] == "sharing_disabled"


def test_the_guest_comment_rate_limit_bites(client, guest, db, monkeypatch):
    from app.api import public_routes
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "guest_comment_rate_limit", 2, raising=False)
    # The counter lives in the process's memory and outlives tests: a neighbouring test
    # must not decide this one's fate. It is reset together with the whole counter — that
    # is assembled on first demand and will pick up the ceiling above.
    monkeypatch.setattr(public_routes, "_guest_comments", None)

    client.post(
        "/api/auth/register",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": "s3cret-pass",
            "company_name": "Acme",
        },
    )
    project_id = client.post("/api/projects", json={"name": "Сайт"}).json()["id"]
    path = _with_comments(
        _public_path(client.post(f"/api/projects/{project_id}/share", json={}).json()["url"])
    )

    body = {"body": "привет", "name": "Мария"}
    assert guest.post(path, json=body).status_code == 201
    assert guest.post(path, json=body).status_code == 201
    third = guest.post(path, json=body)

    assert third.status_code == 429
    assert third.json()["detail"] == "too_many_comments"
