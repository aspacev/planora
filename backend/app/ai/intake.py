"""The intake scenario: interview -> summary -> draft -> application.

The order of the steps and the gates between them are not configurable: an
option to turn the gates off destroys the product's main principle — AI writes
nothing into a project without a person's explicit confirmation.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy.orm import Session as DbSession

from app.ai.coverage import Coverage, coverage, normalize, renumber
from app.ai.provider import LlmError, LlmProvider
from app.calendar import end_date, first_working_on_or_after
from app.settings_resolution import project_calendar
from app.ai.schemas import (
    DRAFT_SCHEMA,
    QUESTION_SCHEMA,
    SPLIT_SCHEMA,
    SUMMARY_SCHEMA,
    Draft,
    NextQuestion,
    Split,
    Summary,
    parse,
)
from app.config import get_settings
from app.models import AiSession, Organization, Project, ScheduleMode, Task, User
from app.mutations import CreateCategory, CreateTask, apply_op
from app.projects import create_project

# Next to this module rather than at the repository root: only backend/ gets
# into the Docker image and only backend/app into the Vercel bundle.
_TOPICS_FILE = Path(__file__).with_name("intake_topics.yml")
_RULES_FILE = Path(__file__).with_name("intake_rules.yml")


@lru_cache
def topics() -> list[dict]:
    """The mandatory interview topics — from a file next to the application.

    A missing file does not bring the application down: an interview without a
    list of topics still works, the model simply picks the questions itself.
    Failing here would mean making the installation depend on a file that has
    nothing to do with the product.
    """
    try:
        loaded = yaml.safe_load(_TOPICS_FILE.read_text(encoding="utf-8")) or {}
    except OSError:
        return []
    return list(loaded.get("topics") or [])


@lru_cache
def _all_rules() -> dict:
    """The per-step rules — from a file, by the same reasoning as the topics.

    A missing or empty file leaves the steps without rules rather than without
    a product: the intake worked before the rules existed.
    """
    try:
        loaded = yaml.safe_load(_RULES_FILE.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return dict(loaded.get("rules") or {})


def rules(step: str) -> str:
    """The rules of one step as a block of prompt text, or an empty string."""
    lines = [str(rule) for rule in _all_rules().get(step) or []]
    if not lines:
        return ""
    return "Правила:\n" + "\n".join(f"- {line}" for line in lines) + "\n"


class IntakeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _system(session: AiSession) -> dict:
    """The shared part of the prompt.

    The language is passed as an explicit parameter rather than guessed by the
    model from the text of the answers: a person may answer in one language while
    running the project in another.
    """
    return {
        "role": "system",
        "content": (
            "Ты помогаешь составить план проекта. Задавай по одному вопросу. "
            f"Язык вопросов и всех значений в ответах: {session.locale}. "
            "Ключи полей в JSON остаются английскими."
        ),
    }


def _history(session: AiSession) -> list[dict]:
    messages: list[dict] = [_system(session)]
    for turn in session.transcript:
        messages.append({"role": "assistant", "content": turn.get("question", "")})
        if turn.get("answer") is not None:
            messages.append({"role": "user", "content": turn["answer"]})
    return messages


def _covered(session: AiSession) -> set[str]:
    return {topic for turn in session.transcript for topic in turn.get("covered", [])}


def _remaining(session: AiSession) -> list[dict]:
    covered = _covered(session)
    return [topic for topic in topics() if topic.get("key") not in covered]


def _generate(session: AiSession, provider: LlmProvider, messages: list[dict], schema: dict):
    """One call to the model, with the token spend recorded.

    Spend is recorded per session — this is the only place where it is counted at
    all, and bypassing it is not allowed.
    """
    payload, tokens = provider.generate(messages, schema)
    session.tokens_used += tokens
    return payload


def start(
    db: DbSession, *, org: Organization, user: User, locale: str, provider: LlmProvider
) -> AiSession:
    session = AiSession(
        org_id=org.id, created_by=user.id, locale=locale, status="interview", transcript=[]
    )
    db.add(session)
    db.flush()
    ask(db, session, provider)
    return session


def ask(db: DbSession, session: AiSession, provider: LlmProvider) -> str:
    """The next question.

    The hard ceiling on questions lives here: without it the model clarifies
    endlessly. Having reached the ceiling, the session moves on to the summary by
    itself — that is not a refusal but a move to the next step.
    """
    if session.status != "interview":
        raise IntakeError("wrong_step", "интервью уже закончено")

    asked = len(session.transcript)
    if asked >= get_settings().ai_max_questions:
        raise IntakeError("interview_exhausted", "вопросы кончились, пора к конспекту")

    remaining = _remaining(session)
    if remaining:
        instruction = (
            "Осталось выяснить: "
            + "; ".join(topic["prompt"] for topic in remaining)
            + ". Задай один следующий вопрос."
        )
    else:
        instruction = "Все темы закрыты. Задай последний уточняющий вопрос."
    instruction = rules("interview") + instruction

    messages = [*_history(session), {"role": "user", "content": instruction}]

    answer = parse(NextQuestion, _generate(session, provider, messages, QUESTION_SCHEMA))
    # Covered topics arrive together with the question and are remembered:
    # without this the remaining topics never shrink, and the interview hits the
    # ceiling instead of ending when there is nothing left to find out.
    #
    # The list is reassigned in full: JSONB does not track an in-place edit of a
    # list, and an append would silently fail to reach the database.
    session.transcript = [
        *session.transcript,
        {"question": answer.question, "answer": None, "covered": answer.covered_topics},
    ]
    db.flush()
    return answer.question


def answer(db: DbSession, session: AiSession, text: str, provider: LlmProvider) -> str | None:
    """A person's answer and the next question — or `None` if it is time for the summary."""
    if session.status != "interview":
        raise IntakeError("wrong_step", "интервью уже закончено")
    if not session.transcript:
        raise IntakeError("nothing_asked", "вопрос ещё не задан")

    turns = [dict(turn) for turn in session.transcript]
    turns[-1]["answer"] = text
    session.transcript = turns
    db.flush()

    if len(session.transcript) >= get_settings().ai_max_questions or not _remaining(session):
        return None
    return ask(db, session, provider)


def make_summary(db: DbSession, session: AiSession, provider: LlmProvider) -> list[str]:
    """Step 2: the summary — the first gate.

    This is the cheapest place to catch a misunderstanding: before it turns into
    a hundred wrong tasks.
    """
    messages = _history(session)
    messages.append(
        {
            "role": "user",
            "content": rules("summary")
            + "Сформулируй тезисы: что ты понял про проект. Коротко, по одному факту.",
        }
    )
    result = parse(Summary, _generate(session, provider, messages, SUMMARY_SCHEMA))
    session.summary = result.theses
    session.status = "summary"
    db.flush()
    return result.theses


def edit_summary(db: DbSession, session: AiSession, theses: list[str]) -> list[str]:
    """A person's edit of the summary. A gate is a gate precisely because things are edited through it."""
    if session.status not in {"summary", "draft"}:
        raise IntakeError("wrong_step", "конспекта ещё нет")
    edited = [thesis.strip() for thesis in theses if thesis.strip()]
    if session.draft:
        # The draft refers to summary points by number. Without following the
        # edit, deleting one point would re-aim every later reference at a
        # different sentence, and the screen would show a confident, wrong link.
        draft = renumber(parse(Draft, session.draft), session.summary, edited)
        session.draft = draft.model_dump(mode="json")
    session.summary = edited
    db.flush()
    return session.summary


def _numbered(theses: list[str]) -> str:
    return "\n".join(f"{number}. {thesis}" for number, thesis in enumerate(theses, start=1))


def _draft_request(session: AiSession) -> str:
    """The draft instruction: the numbered summary, the rules, the references.

    The part about `sources` and `context_theses` is in the code rather than in
    the rules file: the server checks it, and a check must not depend on a line
    someone may delete while tuning the wording.

    Today's date is stated because the tasks are laid out from it, and the model
    has no clock: without it the plan starts on whatever date the model happens
    to assume.
    """
    return (
        "Вот утверждённые тезисы:\n"
        + _numbered(session.summary)
        + "\n\n"
        + rules("draft")
        + "Составь категории и задачи. Даты — в формате ГГГГ-ММ-ДД, длительность в "
        f"рабочих днях. Сегодня {datetime.now(timezone.utc).date().isoformat()}.\n"
        "У каждой задачи в поле sources перечисли номера тезисов, из которых она "
        "следует. Задачи без тезиса быть не должно. "
        "У каждого тезиса должна быть хотя бы одна задача. Тезисы, по которым "
        "задача не нужна — ограничение, исключение из объёма, факт о проекте, — "
        "перечисли номерами в context_theses."
    )


def _gap_feedback(draft: Draft, gaps: Coverage) -> str:
    """What exactly the previous draft lost — as a request to repair it.

    The gaps are named one by one. "Check your work" gets the same draft back;
    "point 4 has no task" gets point 4 a task.
    """
    parts: list[str] = []
    if gaps.uncovered:
        numbers = ", ".join(str(number) for number in gaps.uncovered)
        parts.append(
            f"Тезисы без задач: {numbers}. Добавь по ним задачи либо, если задача "
            "не нужна, перечисли их в context_theses."
        )
    if gaps.unsourced:
        names = "; ".join(
            f"«{draft.categories[category].tasks[task].name}»" for category, task in gaps.unsourced
        )
        parts.append(
            f"Задачи без тезиса: {names}. Укажи в sources номера тезисов, из которых "
            "они следуют, либо убери эти задачи."
        )
    return " ".join(parts) + " Верни черновик целиком."


def make_draft(db: DbSession, session: AiSession, provider: LlmProvider) -> dict:
    """Step 3: the draft — the main gate.

    If it did not pass the schema — repeat the request, at most
    `AI_SCHEMA_RETRIES` times, then an honest error message with the session
    preserved. The session stays on the previous step: the conversation and
    everything established are still there.

    A draft that passed the schema but lost a summary point, or grew a task
    with no origin, is sent back once more with the gaps named — out of the same
    retry allowance, so that self-checking cannot multiply the token spend. If
    gaps remain when the allowance runs out, the draft is accepted anyway: it is
    a usable plan, the gaps are shown on the draft screen, and the person at the
    gate is a better judge of them than another round with the model.
    """
    if session.status not in {"summary", "draft"}:
        raise IntakeError("wrong_step", "сначала конспект")

    request = [*_history(session), {"role": "user", "content": _draft_request(session)}]
    messages = request
    theses = len(session.summary)

    attempts = get_settings().ai_schema_retries + 1
    last: LlmError | None = None
    best: Draft | None = None
    best_gaps: Coverage | None = None
    for _ in range(attempts):
        try:
            draft = parse(Draft, _generate(session, provider, messages, DRAFT_SCHEMA))
        except LlmError as error:
            last = error
            # A retry only on a broken schema: an unreachable network will not be
            # fixed by a retry, while the token spend would triple.
            if error.code not in {"llm_schema_mismatch", "llm_bad_json"}:
                raise
            continue
        draft = normalize(draft, theses)
        gaps = coverage(draft, theses)
        # A repair attempt can come back worse than what it was repairing; the
        # draft kept is the one with the fewest gaps, the later one on a tie.
        if best_gaps is None or gaps.gaps <= best_gaps.gaps:
            best, best_gaps = draft, gaps
        if gaps.clean:
            break
        messages = [
            *request,
            {
                "role": "assistant",
                "content": json.dumps(draft.model_dump(mode="json"), ensure_ascii=False),
            },
            {"role": "user", "content": _gap_feedback(draft, gaps)},
        ]

    if best is None:
        raise last

    session.draft = best.model_dump(mode="json")
    session.status = "draft"
    db.flush()
    return session.draft


def draft_coverage(session: AiSession) -> Coverage | None:
    """The gaps of the stored draft against the current summary, or `None` before a draft.

    Computed on every read rather than stored: both the draft and the summary
    are edited by hand, and a stored verdict would describe a draft that no
    longer exists.
    """
    if not session.draft:
        return None
    return coverage(parse(Draft, session.draft), len(session.summary))


def edit_draft(db: DbSession, session: AiSession, draft: dict) -> dict:
    """A person's edit of the draft: nothing has been written into the project."""
    if session.status != "draft":
        raise IntakeError("wrong_step", "черновика ещё нет")
    parsed = normalize(parse(Draft, draft), len(session.summary))
    session.draft = parsed.model_dump(mode="json")
    db.flush()
    return session.draft


def apply_draft(
    db: DbSession, session: AiSession, *, name: str, actor: User
) -> tuple[Project, uuid.UUID]:
    """Step 4: application — as a batch of ordinary mutations with a shared `batch_id`.

    Ordinary — because otherwise the history of tasks created by AI would differ
    from the history of the rest, and undo for them would be its own thing. The
    whole batch rolls back with one button precisely because these are the very
    same revisions.
    """
    # "Already applied" is checked before "wrong step": after application the
    # status is not draft either, and a generic refusal would tell the person
    # something other than what happened.
    if session.applied_batch_id is not None:
        raise IntakeError("already_applied", "черновик уже применён")
    if session.status != "draft":
        raise IntakeError("wrong_step", "черновика ещё нет")

    draft = parse(Draft, session.draft)
    project = create_project(db, org_id=session.org_id, name=name)
    # An AI draft is a plan with real dates: the model lays the tasks out from
    # today. Such a project is born calendar-based rather than relative: the
    # relative axis is for plans whose dates have not been assigned yet.
    project.schedule_mode = ScheduleMode.CALENDAR
    batch_id = uuid.uuid4()

    for index, category in enumerate(draft.categories):
        created = apply_op(
            db,
            project,
            CreateCategory(name=category.name, color=_color(index)),
            actor_id=actor.id,
            batch_id=batch_id,
        )
        category_id = uuid.UUID(created.op["category_id"])
        for task in category.tasks:
            revision = apply_op(
                db,
                project,
                CreateTask(
                    category_id=category_id,
                    name=task.name,
                    description=task.description,
                    start_date=task.start_date,
                    duration_days=task.duration_days,
                    criticality=task.criticality.value,
                ),
                actor_id=actor.id,
                batch_id=batch_id,
            )
            row = db.get(Task, uuid.UUID(revision.op["task_id"]))
            row.created_by_ai_session_id = session.id

    session.project_id = project.id
    session.applied_batch_id = batch_id
    session.status = "applied"
    db.flush()
    return project, batch_id


# The palette is presentation, not data: colours for AI categories are taken
# from the same short set the manual category-creation form offers.
_COLORS = ("#3b82f6", "#a855f7", "#f97316", "#10b981", "#ef4444", "#eab308")


def _color(index: int) -> str:
    return _COLORS[index % len(_COLORS)]


def propose_split(
    db: DbSession, task: Task, provider: LlmProvider, *, locale: str
) -> list[dict]:
    """The targeted "split into several" action.

    It returns a suggestion and writes nothing: it is applied only by a button —
    by the same rule as the draft.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "Ты помогаешь разбить задачу на части. "
                f"Язык значений: {locale}. Ключи полей остаются английскими."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Задача «{task.name}» длится {task.duration_days} рабочих дней. "
                f"Описание: {task.description or '—'}. "
                "Разбей её на 3–5 частей с поделёнными сроками."
            ),
        },
    ]
    payload, _ = provider.generate(messages, SPLIT_SCHEMA)
    split = parse(Split, payload)
    return [part.model_dump(mode="json") for part in split.parts]


def apply_split(
    db: DbSession, project: Project, task: Task, parts: list[dict], *, actor: User
) -> uuid.UUID:
    """Applying a split: a batch of mutations, rolled back as a whole.

    The original task is not deleted automatically — a person decides its fate:
    a silent deletion carries away its history and assignments, and bringing them
    back with an undo is only possible together with the whole batch.
    """
    split = parse(Split, {"parts": parts})
    batch_id = uuid.uuid4()
    # The parts are laid out along the project's calendar rather than along
    # calendar days: the duration is given in working days, and a "5 days" part
    # started on Wednesday finishes on Tuesday, not on Monday. The previous count
    # by ordinal shifted every following part by the weekends of all the previous ones.
    org = db.get(Organization, project.org_id)
    cal = project_calendar(project, org)
    start = task.start_date
    for part in split.parts:
        apply_op(
            db,
            project,
            CreateTask(
                category_id=task.category_id,
                name=part.name,
                description=part.description,
                start_date=start,
                duration_days=part.duration_days,
                criticality=task.criticality,
            ),
            actor_id=actor.id,
            batch_id=batch_id,
        )
        # The parts run consecutively rather than on top of one another: the next
        # one starts on the first working day after the previous one's end.
        finished = end_date(start, part.duration_days, cal)
        start = first_working_on_or_after(finished + timedelta(days=1), cal)
    return batch_id
