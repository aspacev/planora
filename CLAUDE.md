# Planora

Self-hosted project planner: organizations → projects → categories → tasks on
a Gantt chart, with an undo-able edit journal, a proposal/quoting module, and
a weekly health dashboard ("Scorecard").

Before writing or reviewing code in `backend/app` or `frontend/src` — and
**especially** before adding a new API route, database model, permission,
background job, dashboard metric, or a new third-party integration (OAuth
flow, webhook receiver, API-key-based service) — load the `planora-conventions`
skill. It's the architecture map for this repo (data model, the
mutation/revision engine, the permissions matrix, the existing AI
integration pattern to copy for any new integration, dev-workflow commands)
and is much cheaper than re-deriving any of this by exploring the tree. Its
`references/codebase-map.md` describes every module, route and folder — read
that instead of scanning the code to learn what the app does.

## Stack and layout

- **Backend**: `backend/app/` — FastAPI + SQLAlchemy 2.0 (`Mapped[]` style) +
  Alembic + Postgres, Python 3.12, dependency-managed with `uv`.
- **Frontend**: `frontend/src/` — React 19 + TypeScript + Vite + TanStack
  Query + react-router-dom v7. No Redux/Zustand — server state lives in React
  Query, local UI state in `useState`/context.
- **Not a monorepo tool** — just two sibling directories, `backend/` and
  `frontend/`, put behind one domain by Caddy. The session cookie is
  HTTP-only and same-origin-only, so frontend and API must never be split
  across domains.
- **Deploys two ways**: self-hosted Docker (primary, has live WebSocket
  updates) or Vercel (serverless, `api/index.py`, external Postgres, no
  WebSocket). Root `requirements.txt` is a manually synced mirror of
  `backend/pyproject.toml` for Vercel — CI fails if they drift.
- **No charting/Gantt library anywhere** in `frontend/package.json` — the
  Gantt chart and Scorecard sparkline are hand-built SVG + CSS + React. Don't
  add one for a new visualization; follow that pattern.

## Key files

| Path | What's there |
|---|---|
| `backend/app/api/*_routes.py` | One FastAPI router per resource area. |
| `backend/app/api/deps.py` | `ProjectContext` — project + org + role + `.require(Action)`. |
| `backend/app/models.py` | Every SQLAlchemy model and `StrEnum`, single file on purpose. |
| `backend/app/mutations.py` | The plan-edit engine — every Gantt edit goes through here, not a direct `UPDATE`. |
| `backend/app/access.py` | `Action` enum + `Role → frozenset[Action]` permission matrix. |
| `backend/app/crypto.py` | Fernet encryption for secrets at rest. |
| `backend/app/ai/` | The "add a third-party integration" implementation (credentials, provider, SSRF guard) — the template for a new one. |
| `backend/migrations/versions/` | Alembic, linear history, hand-written upgrade/downgrade. |
| `frontend/src/api/*.ts` | One thin wrapper per backend resource, through `api/client.ts`'s `request<T>()`. |
| `frontend/src/{gantt,scorecard,project,proposal,task,comments,auth,live,screens,components}/` | Feature-first folders; `*.test.tsx` colocated. |

## Dev workflow

```bash
# backend (from backend/)
uv sync --locked
uv run pytest --cov=app --cov-report=term-missing   # needs a real Postgres; see backend/tests/conftest.py

# frontend (from frontend/)
npm ci
npm run lint
npx tsc -b
npm run gen:api && git diff --exit-code -- src/api/schema.d.ts  # keep schema.d.ts in sync with openapi.json
npx vitest run --coverage

# docker images
cp .env.example .env && docker compose build
```

`.github/workflows/ci.yml` is the authoritative source for these commands —
README duplicates them for humans but CI is ground truth.

**Those are the full suites (≈2 min backend, ≈1.5 min frontend) — do not run
them by default.** CI runs them on every pull request, so a fresh branch needs
no baseline run, and a change needs only the tests it can reach:

```bash
.claude/skills/planora-conventions/scripts/test-env.sh              # once per session
.claude/skills/planora-conventions/scripts/affected_tests.py --run  # only what the diff affects
```

Docs-only changes run nothing; backend-only changes skip the frontend and
vice versa. When the full suite is still required, and what a skipped run
does not guarantee: `.claude/skills/planora-conventions/references/testing.md`.

## House habits

- Comment the "why," not the "what" — most non-obvious code already has a
  comment explaining the reasoning; read it before assuming.
- `StrEnum` classes are paired with a derived tuple that feeds a
  `CheckConstraint`, so the enum and the DB constraint can't drift apart.
- Config (`backend/app/config.py`) validates required settings at startup and
  refuses to boot rather than serving with a half-configured integration.
- Sessions/invitations/tokens store `token_hash`, never the raw token.
- Frontend spacing (`padding`/`margin`/`gap`) comes from the `--space-*` scale
  in `frontend/src/styles.css` (4px step, plus 2 and 6 below 8), never a bare
  pixel literal — `spacing-scale.test.ts` fails CI otherwise. A geometric
  exception (border compensation, chevron width) is marked on the line with a
  `off-scale` comment. Sizes and positions are not part of the scale.
