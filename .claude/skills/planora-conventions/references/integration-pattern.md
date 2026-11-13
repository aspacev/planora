# Adding a third-party integration

Planora has one feature shaped like "connect an external service, per
organization, with a secret": the LLM/AI connection used for the AI
project-intake interview (`backend/app/ai/`). It is the template to copy for
anything else in this family: a new AI provider, a chat webhook, a calendar
sync, an issue-tracker import. This doc walks through it in detail.

## The existing shape

- **`backend/app/models.py` → `OrgLlmCredential`** — one row per org
  (`org_id` is `unique`), holding `provider`, `base_url`, `model`, and
  `encrypted_key`. Nothing about the secret is queryable in plaintext.
- **`backend/app/crypto.py`** — `encrypt(value: str) -> str` /
  `decrypt(value: str) -> str`, Fernet symmetric encryption with a key
  derived (`SHA-256`) from `APP_SECRET`. Reuse this module rather than adding
  a second encryption scheme. `DecryptionError` is a distinct
  exception from "key missing" — it means `APP_SECRET` rotated since the
  value was encrypted, and the fix is either restoring the old secret or
  asking the org to re-enter the credential, not retrying.
- **`backend/app/ai/credentials.py`** — `save_credential()` /
  `credential()` / `provider_for()`. Notice the save-time UX detail: an
  **empty key on update means "keep the existing one"** — the key is never
  sent back to the client to prefill a form, so re-saving the base URL alone
  can't require re-typing the secret.
- **`backend/app/ai/provider.py`** — the outbound-call side, behind a
  `Protocol`:
  ```python
  class LlmProvider(Protocol):
      def generate(self, messages: list[dict], schema: dict) -> tuple[dict, int]: ...
  ```
  `HttpProvider` is the real implementation (raw `urllib`, no vendor SDK —
  deliberate, so the same code works against any OpenAI-compatible endpoint,
  including a self-hosted model). `RecordedProvider` replays a fixed list of
  canned responses/exceptions and is a first-class test double, not a
  placeholder — tests exercise real business logic against it with zero
  network access.
- **`backend/app/ai/netguard.py`** — `ensure_public_https(url)`, called
  **both** when the credential is saved (fail fast, in the settings form)
  **and** on every single outbound call (because DNS can change after
  saving — a classic SSRF rebinding trick). Blocks non-https schemes and any
  resolved address that isn't globally routable (loopback, private ranges,
  link-local/cloud-metadata `169.254.0.0/16`, CGN). Redirects are disabled
  separately in `provider.py`'s HTTP opener, because a public host answering
  with a 3xx to an internal address would otherwise bypass the resolve-time
  check entirely. `AI_ALLOW_PRIVATE_URLS=true` is the one escape hatch, for
  a self-hosted install pointed at a model on its own private network — keep
  that pattern (a config flag, off by default) if a new integration needs an
  equivalent.
- **`backend/app/ai/usage.py`** — per-org rate limiting (`ai_requests_per_minute`)
  and a daily token budget (`ai_daily_token_budget`), both configured in
  `app/config.py` with `0` meaning "no limit." A paid third-party API used
  on behalf of an org needs the same kind of budget guard — otherwise one
  member can exhaust the whole org's quota/bill in an evening. Both are
  written outside the request's transaction (the rate hit is committed at
  once, the tokens are charged in a transaction of their own): a model call
  that ends in a refusal rolls the request back, and limits written inside it
  would only ever count successes.

## An integration that writes into the plan

If the integration creates or changes plan rows (an import, a sync), it goes
through the **mutation layer** with one shared `batch_id` per operation — the
way `app/ai/intake.py:apply_draft` applies a draft — so the whole operation
is one-click undoable, exactly like a human-driven multi-edit. Bookkeeping
that links plan rows to the external system lives outside the revision
journal, so decide up front what an undo of the batch does to it.

## What doesn't exist yet (design it before building it)

- **No OAuth-client flow.** Planora issues session cookies to its own users;
  it has never been the OAuth *consumer* side of a flow with an external
  provider. Building "Connect your GitHub/Slack account" via OAuth is still
  new territory — there's no `oauth.py` to copy. Model the stored token the
  same way as `OrgLlmCredential` (encrypted, never returned
  by the API), and keep the state/redirect handling in its own module rather
  than folding it into `auth.py` (which is exclusively about Planora's own
  session lifecycle).
- **No webhook receiver.** Nothing in this codebase accepts inbound HTTP
  from a third party today. If you add one: it sits outside the normal
  session-cookie auth entirely (verify by signature/secret instead, the way
  a payment processor's webhook would), it must **not** go through
  `reject_cross_origin_writes` in `main.py` (that middleware assumes a
  browser Origin header, which a webhook sender won't send) — add an
  explicit path exemption there rather than disabling the check globally,
  and it should call into the mutation layer for anything that changes plan
  state, exactly like a human-triggered route would.

## Where a new route/model for this would plug in

- Router: a new `backend/app/api/<name>_routes.py`, registered in
  `backend/app/main.py` next to the existing `app.include_router(...)` calls.
- Permission: add an `Action` in `app/access.py` and decide, explicitly, for
  every `Role`, whether it can configure/use the integration — don't assume
  "same as `PROJECT_ADMIN`" without checking that's actually the granularity
  wanted (org-wide like the LLM credential, or per-project).
- Migration: a new file in `backend/migrations/versions/`, hand-written
  `upgrade`/`downgrade`, chained via `down_revision` to the current head —
  see `dev-workflow.md` for how to generate and run it.
- Config: any new required setting goes in `backend/app/config.py` as a
  `Settings` field with a validator that **fails at startup** if
  half-configured (mirror `_refuse_a_mail_setup_that_cannot_send`'s pattern:
  check the whole set of required variables together, not field-by-field),
  plus a documented entry in `.env.example` with a comment explaining the
  variable and its default.
