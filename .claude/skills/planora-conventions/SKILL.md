---
name: planora-conventions
description: >
  Architecture map and house conventions for the Planora codebase (self-hosted
  Gantt-chart project planner: FastAPI + SQLAlchemy/Postgres backend, React +
  Vite frontend). Load this before writing or reviewing any code in
  backend/app or frontend/src — especially before adding a new API route,
  database model, permission, background job, dashboard metric, or a new
  third-party integration (OAuth flow, webhook receiver, API-key-based
  service). Also load it before running tests (it says which tests a change
  needs and when the full suite can be skipped), wiring CI, writing an Alembic
  migration, or touching auth/session/encryption code, and at the start of any
  session that needs to understand what the app does or where something lives
  — it replaces exploring the tree. Use it whenever the
  user mentions Planora, this repo, "the scorecard", "the gantt chart", "the
  mutation/revision journal", or asks how something in this codebase already
  works — the answer is almost always "look at the existing pattern first."
---

# Planora conventions

Planora is a self-hosted project planner: organizations → projects → categories
→ tasks on a Gantt chart, with an undo-able edit journal, a proposal/quoting
module, and a weekly health dashboard ("Scorecard"). This skill is a map, not
a tutorial — its job is to point you at the file that already solves your
problem so you extend it instead of reinventing it. The codebase's own code
comments (they explain *why* a line exists, not *what* it does) are the
deeper source of truth; read the file this skill points at before writing
new code near it.

## Stack and layout

- **Backend**: `backend/app/` — FastAPI + SQLAlchemy 2.0 (`Mapped[]` style) +
  Alembic + Postgres, Python 3.12, dependency-managed with `uv`
  (`backend/pyproject.toml` / `backend/uv.lock`).
- **Frontend**: `frontend/src/` — React 19 + TypeScript + Vite + TanStack
  Query + react-router-dom v7. No Redux/Zustand/etc — server state lives in
  React Query, local UI state in `useState`/context.
- **Not a monorepo tool** (no turborepo/nx/lerna) — just two sibling
  directories. `docker-compose.yml` + Caddy put both behind one domain: the
  session cookie is HTTP-only and same-origin-only, so frontend and API must
  never be split across domains. `/api/*` is the only thing Caddy proxies to
  the backend; everything else falls back to `index.html` (client-side
  routing).
- **Deploys two ways**: self-hosted Docker (primary, has live WebSocket
  updates) or Vercel (serverless, `api/index.py` thin ASGI wrapper, external
  Postgres like Neon, no WebSocket — the frontend degrades gracefully when
  `live_enabled` is false). Root `requirements.txt` is a **manually
  synced mirror** of `backend/pyproject.toml` for Vercel's Python builder —
  a test (`test_vercel_requirements_match_uv_lock`) fails CI if they drift.

**Do not explore the tree to orient yourself.** `references/codebase-map.md`
lists every backend module, API route, frontend folder and repo-level file
with what it does; open it first, then go straight to the file it names.

Directory quick-reference — see `references/data-model.md` for the full
entity map and `references/dev-workflow.md` for exact commands:

| Path | What's there |
|---|---|
| `backend/app/api/*_routes.py` | One FastAPI router per resource area (auth, org, project, proposal, scorecard, export, share, public, live, invite, admin, ai, meta). |
| `backend/app/api/deps.py` | Shared per-request dependencies — `ProjectContext` (project + org + role + `.require(Action)`). Read this before writing a new project-scoped route. |
| `backend/app/models.py` | Every SQLAlchemy model and every `StrEnum`. Single file, deliberately — one place to see the whole schema. |
| `backend/app/mutations.py` | The plan-edit engine — see "The mutation/revision pattern" below. |
| `backend/app/access.py` | `Action` enum + `Role → frozenset[Action]` matrix. The permission system. |
| `backend/app/crypto.py` | Fernet encryption for secrets this app stores at rest (the LLM key goes through it). |
| `backend/app/ai/` | The concrete "third-party integration, per organization, with a secret" implementation — see `references/integration-pattern.md`. |
| `backend/app/director.py` | `is_director()` — the single install-wide admin role (from `DIRECTOR_EMAIL`), distinct from any org's `Role.owner`. Backs the `/admin` panel. |
| `backend/migrations/versions/` | Alembic, linear history, hand-written `upgrade`/`downgrade` (no autogenerate magic). |
| `backend/tests/` | pytest, one `test_*.py` per concern, runs against a real `<db>_test` Postgres. |
| `frontend/src/api/*.ts` | One thin wrapper module per backend resource, all through `api/client.ts`'s `request<T>()`. |
| `frontend/src/{gantt,scorecard,project,proposal,task,comments,auth,live,screens,components}/` | Feature-first folders; `*.test.tsx` colocated next to source, no `__tests__/` tree. |
| `frontend/src/test/` | MSW mock server + fake WebSocket harness for component tests. |

## The mutation/revision pattern (core to the backend)

Every edit to a project's plan (move a task, rename a category, set a
dependency, change status...) goes through `backend/app/mutations.py`, not
direct SQLAlchemy `UPDATE`s from a route. A mutation function:

1. Takes the current DB state and a typed "op" (Pydantic model, `Literal`
   discriminated by an operation-name field).
2. Applies it, raising `NotFoundInProject` (→ 404) or `InvalidOperation` (→
   422) — never a bare exception — on anything wrong.
3. Computes and returns an **inverse op** alongside the applied one.
4. The route writes both into `revisions` (`op`, `inverse`, `batch_id`,
   `undoes_seq`) — this is what makes "Undo" possible and what the project
   history / activity feed reads from. `Revision.op` is `JSONB` and
   GIN-indexed because task history is found by searching op payloads for a
   `task_id`, not by a foreign key.

**If you're adding a new kind of plan edit, add a new op type here and give
it an inverse — don't bypass this layer with a direct model update, even for
something that looks like "just a field."** Things that live *outside* this
journal on purpose: `Comment`, `ScorecardMetric`/`Snapshot`/`Alert`, and
`Proposal*` rows — they're not part of the plan's undo history, and
`app/scorecard.py`'s docstring explains why explicitly.

## Permissions: extend `access.py`, don't hand-roll a check

`Role` (owner/editor/viewer/client) → `Action` is a literal
`dict[Role, frozenset[Action]]` table in `backend/app/access.py`, not
computed from a hierarchy — that's deliberate, so a new `Action` forces you
to visit every role explicitly instead of silently inheriting. In a route:

```python
context: ProjectContext = Depends(project_context)
...
context.require(Action.PROJECT_WRITE)   # raises 403 if not permitted
```

A brand-new capability (e.g. "connect an integration") needs a new `Action`
member added to the matrix for every `Role`, even if the answer is "nobody
but owner." `Role.CLIENT` and the anonymous-guest role (`None`) are
`_NEEDS_GRANT` — they only see projects they were explicitly invited to via
`ProjectAccess`; most other roles see everything in their org unless
`Membership.project_scoped` narrows them.

## Secrets, integrations, and outbound HTTP — read `references/integration-pattern.md`

Planora has one "connect a third-party service, per organization, with a
secret" feature: the LLM/AI connection (`backend/app/ai/credentials.py` +
`provider.py` + `netguard.py`). It is the template to copy for anything
OAuth/API-key/webhook shaped (a Jira integration built on the same shape
existed and was removed; its tables are dropped by migration
`f4b8d2c6a1e9`):

- Encrypt the secret with `app.crypto.encrypt`/`decrypt` (Fernet, keyed from
  `APP_SECRET`), store it in an `encrypted_*` column, and **never return it
  from the API** — only a boolean "configured" flag, same as
  `OrgLlmCredential`.
- If the integration makes outbound requests to a URL the *user* supplied,
  copy `app/ai/netguard.py`'s SSRF guard (https-only, reject non-public
  resolved IPs, block redirects) — don't skip it because "it's just an API
  call."
- No vendor SDKs for outbound HTTP in this codebase (`HttpProvider` in
  `provider.py` uses raw `urllib`) — match that unless the target API leaves
  no reasonable alternative.
- Rate/usage limits go through `app/throttle.py` (durable, Postgres-backed —
  use for anything security-sensitive like login/API calls) or
  `app/rate_limit.py` (in-memory, for cheap abuse-prevention like guest
  comments) — pick based on whether the limit must survive a process
  restart and be correct across replicas.
- There is currently **no OAuth-client flow and no webhook receiver** in the
  codebase — Planora only issues its own session tokens today, and the AI
  connection uses an API key rather than OAuth. Building an OAuth-consumer
  flow or a webhook receiver is still new ground; follow the secrets-at-rest
  and SSRF conventions above, and put the new provider behind a `Protocol`
  like `LlmProvider` so it's swappable/fakeable in tests the way
  `RecordedProvider` fakes the real outbound call.

## Dashboards and charts: no charting library — hand-build SVG

There is **no chart or Gantt dependency anywhere** in `frontend/package.json`
— no d3, chart.js, recharts, visx, dhtmlx-gantt. The Gantt chart
(`frontend/src/gantt/`) and the Scorecard's sparkline
(`frontend/src/scorecard/Scorecard.tsx`, `function Sparkline`) are hand-built
with plain SVG + CSS + React drag handlers. **Don't add a charting
dependency** for a new visualization — follow the existing hand-built-SVG
approach, and reuse pieces from `gantt/` (scale/timescale math) or
`scorecard/` (sparkline) where the shape matches.

To add a new dashboard metric or extend Scorecard specifically:
`backend/app/api/scorecard_routes.py` + `backend/app/scorecard.py` is the
concrete pattern — `GET` lazily backfills the current week's snapshot as a
side effect, `POST /recalculate` force-recomputes (rate-limited to 1/min),
`PATCH` edits per-metric config (owner/target/enabled), and a drill-down
endpoint returns the contributing tasks. `ScorecardSnapshot` rows for past
weeks are immutable (copy target/direction into the snapshot at write time)
— only the current week's row is ever overwritten.

## Frontend API calls: thin wrapper + typed error codes, no prose from the server

Every network call goes through `frontend/src/api/client.ts`'s
`request<T>()`. It never lets raw response prose reach the UI: failures throw
`ApiError` carrying a machine `code` (from FastAPI's `detail`) that the
frontend maps through an i18n dictionary — the backend has no message
dictionary of its own on purpose (see `app/mutations.py` `MutationError`
docstring: the interface's default locale is Azerbaijani, and English prose
in `detail` is directly unusable there). When you add an endpoint:

1. Add a route in `backend/app/api/*_routes.py` that raises `HTTPException`
   with a short stable `detail` string (a code, not a sentence) or a domain
   `*Error` class that the route maps to one, exactly like
   `scorecard_routes.py`'s `_refuse()`.
2. Add a matching `frontend/src/api/<resource>.ts` wrapper that calls
   `request<T>()`.
3. Regenerate `frontend/src/api/schema.d.ts` from the OpenAPI schema — see
   below.

## Tests: run what the diff needs, not everything

A fresh branch off `origin/main` is green by construction (CI gates every
pull request), so there is no baseline run. For your own change:

```sh
.claude/skills/planora-conventions/scripts/test-env.sh         # once per session: Postgres, DBs, deps
.claude/skills/planora-conventions/scripts/affected_tests.py   # what this diff needs, and why
.claude/skills/planora-conventions/scripts/affected_tests.py --run [--top 3]
```

It skips whole suites the diff cannot reach (backend-only change → no
frontend run and vice versa; docs-only → nothing), picks backend test files
from a recorded coverage map, and never repeats a step that already passed on
identical file contents. The full suite runs in CI on the pull request.
`references/testing.md` has the rules, the cases where the full suite is
still required locally, and what the skip does not promise — read it before
relying on a skipped run.

## Reference files

- `references/codebase-map.md` — the whole repo in one page: product feature
  inventory, every backend module and what it owns, every API route group,
  the migration chain, frontend routes and folders, repo-level files, docs.
- `references/testing.md` — test inventory by concern, the harness on both
  sides, guard tests that fail on unrelated-looking edits, and the policy for
  which tests to run when.
- `references/data-model.md` — every table, grouped by subsystem
  (org/auth, plan/Gantt, proposal, scorecard, sharing), with the
  invariants worth knowing before adding a column or a new entity.
- `references/dev-workflow.md` — exact commands for tests, lint, typecheck,
  build, migrations, and local dev, taken from `.github/workflows/ci.yml`
  (the authoritative source — README duplicates it for humans but CI is
  ground truth).
- `references/integration-pattern.md` — the full walkthrough of the AI/LLM
  credential feature as a template for any new external-service integration.

## A few house habits worth matching

- **Comment the "why," not the "what."** Nearly every non-obvious column,
  branch, or constraint in this codebase has a comment explaining the
  reasoning or the incident that motivated it. A column called `internal` or
  a `server_default` next to a Python `default` usually has one — read it
  before assuming the obvious explanation.
- **Push invariants into the database, not just the app layer.** `StrEnum`
  classes are paired with a derived tuple constant (`tuple(x.value for x in
  Enum)`) that feeds a `CheckConstraint`, so the enum and the DB constraint
  can never drift apart. Follow this when adding a new enum-backed column.
- **Fail loudly at startup, not silently at first use.** `backend/app/config.py`
  validates `APP_SECRET`/`DIRECTOR_EMAIL`/mail-transport completeness with
  Pydantic validators that raise before the app serves a single request — a
  half-configured integration should refuse to boot, not log-and-continue.
- **Hash tokens, don't store them raw.** Sessions, invitations, email
  verification, and password reset all store `token_hash` and show the raw
  token exactly once. Match this for any new one-time-link or bearer token.
