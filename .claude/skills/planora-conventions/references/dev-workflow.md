# Dev workflow: tests, lint, build, migrations

Source of truth is `.github/workflows/ci.yml` — three jobs (backend,
frontend, docker). The README explains the same things for humans with more
context; this file is the condensed command list.

These are the *full* commands — what CI runs. Day to day, do not run the full
suites: `scripts/affected_tests.py` picks what a diff needs and
`scripts/test-env.sh` prepares a fresh container; see `testing.md`.

## Backend (Python / FastAPI)

```sh
cd backend
uv sync --locked                                    # install deps (uv, not pip/poetry)
uv run pytest --cov=app --cov-report=term-missing    # full suite with coverage
uv run pytest tests/test_scorecard.py                # one file
uv run alembic upgrade head                          # apply migrations
uv run alembic revision -m "short_slug"               # new empty migration — write upgrade/downgrade by hand
```

Tests need a **real Postgres**, database named `<DATABASE_URL's db>_test`
(e.g. `planora_test`), never the dev database — `backend/tests/conftest.py`
derives that name and hard-refuses to run `drop_all`/`create_all` against
anything not ending in `_test`. Create it once:

```sh
docker compose exec db createdb -U planora planora_test
```

(In plain `docker compose up`, Postgres isn't published to the host — bring
up `docker-compose.dev.yml` alongside it, or run tests inside the container:
`docker compose exec api pytest`.)

Required env vars for the backend to even import (`backend/app/config.py`
raises otherwise): `DATABASE_URL`, `APP_SECRET` (≥16 chars, not the
`.env.example` placeholder), `DIRECTOR_EMAIL` (not the placeholder). Copy
`.env.example` → `.env` at repo root before anything else. `conftest.py`
sets safe test defaults for `APP_SECRET`/`DIRECTOR_EMAIL`/AI rate limits via
`os.environ.setdefault`, so tests don't need a real `.env` for those.

## Frontend (React / Vite)

```sh
cd frontend
npm ci                    # exact versions from package-lock.json
npm run lint               # oxlint
npx tsc -b                 # typecheck (no separate "build" step needed to just typecheck)
npx vitest run --coverage  # full suite
npx vitest run src/gantt/Gantt.test.tsx   # one file
npm run dev                # Vite dev server, proxies /api to a locally-running backend
npm run build               # tsc -b && vite build
npm run gen:api             # regenerate src/api/schema.d.ts from ../backend/openapi.json
```

**`schema.d.ts` is generated, not hand-edited.** CI fails if it's stale
(`npm run gen:api && git diff --exit-code`). Whenever a backend route's
request/response shape changes, regenerate and commit the diff in the same
change — don't hand-patch the `.d.ts` file.

Frontend tests never touch a real backend or database — MSW
(`frontend/src/test/server.ts`) intercepts `fetch`, and
`frontend/src/test/setup.ts` makes an **undeclared request fail the test**
(not silently pass through), so every test must explicitly set up the
network responses it needs. `frontend/src/test/socket.ts` fakes the
WebSocket the same way, installed fresh before each test
(`beforeEach(installFakeWebSocket)`).

## Local dev environments

```sh
# Production-like, single command, everything in containers:
docker compose up --build

# Hot reload for active development (mounts the working copy, uvicorn --reload, Vite):
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# Frontend only, against a backend already running (in the dev-compose stack
# or directly), dev server on :5173:
cd frontend && npm install && npm run dev
```

`docker compose down` is safe (data persists in the `pgdata` volume);
`docker compose down -v` destroys the database — never run it without a
recent dump in `./backups` (see README "Operations checklist" for the
backup/restore commands).

## Migrations

Alembic, `backend/migrations/versions/`, **linear history, no
autogenerate** — every migration's `upgrade()`/`downgrade()` is written by
hand (see e.g. `b4e7a2c9d3f1_scorecard.py`, which also backfills two
columns from existing `revisions` rows using a `DISTINCT ON` query — a good
model for "new column derived from existing data" migrations). Schema is
applied automatically before the API container starts in the Docker
layouts; on Vercel there's no automatic step — it's a manual one-time
`uv run alembic upgrade head` against the external database after each
deploy that adds a migration (see README's Vercel section).

## CI jobs, summarized

1. **backend** — spins up a real `postgres:16` service container, creates
   `planora_test`, runs `uv run pytest --cov=app`.
2. **frontend** — lint, typecheck, the `gen:api` no-diff check, then
   `vitest run --coverage`.
3. **docker** — `docker compose build` (catches Dockerfile breakage;
   doesn't run the app).

`push` is restricted to `main`; `pull_request` covers branches — so a PR
branch's CI runs once per push, not twice.
