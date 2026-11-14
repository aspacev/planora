"""Schemas of what the model returns.

The schema is language-independent: what arrives in the user's language are the
values, not the keys. Otherwise an interview in Azerbaijani would come back
with Azerbaijani field names, and parsing them would depend on the session's
language.

Validation happens on our side, not only in the request's `response_format`:
the model's promises cannot be trusted, and one cloud's "strict mode" says
nothing about a local model behind the same address.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.models import Criticality

# --- step 1: the next question -----------------------------------------------

QUESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question", "covered_topics"],
    "properties": {
        "question": {"type": "string"},
        # The model marks which topics are already covered itself: the backend
        # holds the list of mandatory topics, while deciding "this answer closed
        # the topic of deadlines" requires reading the answer, not matching
        # strings.
        "covered_topics": {"type": "array", "items": {"type": "string"}},
    },
}


class NextQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1)
    covered_topics: list[str] = Field(default_factory=list)


# --- step 2: the summary -----------------------------------------------------

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["theses"],
    "properties": {"theses": {"type": "array", "items": {"type": "string"}}},
}


class Summary(BaseModel):
    """The caps guard against endless summaries: both the model and a person
    editing can send a wall of text that lands in jsonb and rides along in every
    following prompt, inflating the token bill."""
    model_config = ConfigDict(extra="ignore")

    theses: list[str] = Field(min_length=1, max_length=40)

    @field_validator("theses", mode="after")
    @classmethod
    def _each_thesis_within_limit(cls, value: list[str]) -> list[str]:
        limit = get_settings().max_text_len
        for thesis in value:
            if len(thesis) > limit:
                raise ValueError(f"тезис длиннее потолка в {limit} символов")
        return value


# --- step 3: the draft -------------------------------------------------------

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["categories", "context_theses"],
    "properties": {
        # Summary points that need no task of their own: a constraint, an
        # exclusion, a fact. Without this list "every point has a task" would
        # push the model into inventing a task for "the budget is fixed".
        "context_theses": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "tasks"],
                "properties": {
                    "name": {"type": "string"},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "start_date", "duration_days", "sources"],
                            "properties": {
                                "name": {"type": "string"},
                                # The numbers of the summary points the task
                                # follows from. Required of the model, so that a
                                # task with no origin is an explicit empty list
                                # rather than a field it forgot.
                                "sources": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 1},
                                },
                                "description": {"type": "string"},
                                "start_date": {"type": "string"},
                                "duration_days": {"type": "integer", "minimum": 1},
                                "criticality": {
                                    "type": "string",
                                    "enum": [level.value for level in Criticality],
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


class DraftTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    start_date: date
    duration_days: int = Field(ge=1, le=3650)
    criticality: Criticality = Criticality.NORMAL
    # Optional here although the request schema demands it: a draft edited by
    # hand, and a draft stored before references existed, must still parse. A
    # missing list means "no origin", and coverage reports it as such.
    sources: list[int] = Field(default_factory=list, max_length=40)

    @field_validator("description", mode="after")
    @classmethod
    def _description_within_limit(cls, value: str) -> str:
        # The same cap as on the HTTP path (see mutations._Wire): a description
        # from an AI draft lands in the same column, and the path through the
        # model must not be wider than the path through the form.
        limit = get_settings().max_text_len
        if len(value) > limit:
            raise ValueError(f"длиннее потолка в {limit} символов")
        return value


class DraftCategory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    tasks: list[DraftTask] = Field(default_factory=list, max_length=200)


class Draft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categories: list[DraftCategory] = Field(min_length=1, max_length=50)
    context_theses: list[int] = Field(default_factory=list, max_length=40)


# --- the "split a task" step -------------------------------------------------

SPLIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parts"],
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "duration_days"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "duration_days": {"type": "integer", "minimum": 1},
                },
            },
        }
    },
}


class SplitPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    duration_days: int = Field(ge=1)


class Split(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 3-5 parts: one part is not a split, and ten turn the card into a to-do
    # list.
    parts: list[SplitPart] = Field(min_length=2, max_length=8)


def parse(model: type[BaseModel], payload: dict):
    """Parsing the model's answer with an intelligible refusal.

    ValidationError does not escape outward: it is English prose, while messages
    are assembled by the client from a machine code.
    """
    from app.ai.provider import LlmError

    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise LlmError("llm_schema_mismatch", f"ответ не по схеме: {error.error_count()} ошибок")
