"""An organization's daily LLM token budget.

The organization brings the key, and the organization pays for it. Without a
budget one member — or one scenario stuck in a loop — burns the key's monthly
limit in an evening, and the key's owner learns about it from the invoice. The
product's budget is a fuse on top of the provider's limits, not a replacement
for them.

It is counted by UTC calendar days. The day boundary in someone else's timezone
is not the kind of precision worth dragging the organization's timezone into
the counter for: the fuse works the same whatever the offset of the day
boundary.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.models import AiUsage, Organization


def _today():
    return datetime.now(timezone.utc).date()


def spent_today(db: DbSession, org: Organization) -> int:
    return (
        db.scalar(
            select(AiUsage.tokens).where(AiUsage.org_id == org.id, AiUsage.day == _today())
        )
        or 0
    )


def budget_left(db: DbSession, org: Organization) -> bool:
    """Whether the organization still has budget for today. 0 in the setting means no limit."""
    budget = get_settings().ai_daily_token_budget
    if budget <= 0:
        return True
    return spent_today(db, org) < budget


def charge(db: DbSession, org: Organization, tokens: int) -> None:
    """Records consumption. An upsert rather than read-modify-write: two
    concurrent model calls must not lose each other's tokens."""
    if tokens <= 0:
        return
    statement = pg_insert(AiUsage).values(org_id=org.id, day=_today(), tokens=tokens)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[AiUsage.org_id, AiUsage.day],
            set_={"tokens": AiUsage.tokens + statement.excluded.tokens},
        )
    )


class MeteredProvider:
    """A provider wrapper: every model answer is recorded against the organization.

    The accounting lives here rather than at the call sites: there are several
    paths to the model (the interview, the summary, the draft, splitting a
    task), and a site that forgets to count is a hole in the budget. Before this
    wrapper, propose_split was not counted at all.
    """

    def __init__(self, inner, db: DbSession, org: Organization):
        self._inner = inner
        self._db = db
        self._org = org

    def generate(self, messages: list[dict], schema: dict) -> tuple[dict, int]:
        payload, tokens = self._inner.generate(messages, schema)
        # In a transaction of its own: the tokens are spent at the provider
        # whatever becomes of this request. An answer that fails our schema ends
        # in a refusal, the request is rolled back — and written into the
        # request's transaction, the charge would be rolled back with it: a model
        # that keeps answering off-schema would burn the key with the budget
        # never noticing. The request's own session is not committed instead:
        # that would also commit a half-finished step of the interview.
        with DbSession(self._db.get_bind()) as ledger:
            charge(ledger, self._org, tokens)
            ledger.commit()
        return payload, tokens
