# Testing: what exists, how to run it, and when not to

## The short version

1. **Never run a suite to "see the starting state".** Every pull request and
   every commit on `main` runs all of CI (`.github/workflows/ci.yml`), so a
   branch freshly cut from `origin/main` is green by construction.
2. **Ask the selector what your diff needs** instead of running everything:

   ```sh
   .claude/skills/planora-conventions/scripts/test-env.sh            # once per session
   .claude/skills/planora-conventions/scripts/affected_tests.py      # prints the plan
   .claude/skills/planora-conventions/scripts/affected_tests.py --run
   ```

3. **While iterating**, `--run --top 3` (the few strongest tests per changed
   file) or the one test file you are working on.
4. **Before pushing**, one `--run` without `--top`. A step that already passed
   for the exact same file contents is skipped, so repeating it is free.
5. **The full suite is CI's job.** Run it locally (`--run --full`) only in the
   cases listed under "When the full suite is still required".

Measured on a 4-core cloud container: backend 911 tests ≈ 2 min, frontend 791
tests ≈ 1.5 min, `tsc -b` ≈ 8 s, lint < 1 s. A typical feature change selects
1–4 backend test files (seconds).

## When skipping is valid — and when it is not

| The diff touches only… | Run |
|---|---|
| `*.md`, `docs/`, `.claude/`, `LICENSE`, `.env.example`, `.github/` | Nothing. No test reads these. |
| `backend/**` | Selected backend tests. **No frontend run** — unless `backend/openapi.json` changed (then the contract check and types). |
| `frontend/**` | Lint, types, frontend tests. **No backend run, no Postgres needed.** |
| `frontend/**/*.css` only | The two stylesheet guards (`spacing-scale`, `theme-cascade`). jsdom never evaluates CSS, so no component test can change its verdict. |
| Test files only | Those files. |
| `backend/migrations/**` | `tests/test_migrations.py` (upgrade, downgrade, parity with the models). |
| `requirements.txt` | `tests/test_wave3_operations.py` (pins must equal `uv.lock`). |
| `Caddyfile`, `docker-compose*.yml`, `docker/`, `*/Dockerfile` | Nothing locally — only CI's `docker compose build` checks them. Say so in the summary. |
| `api/index.py`, `vercel.json` | Nothing covers them. Say so. |

The selector implements exactly this table; `affected_tests.py` without
`--run` prints its reasoning.

### When the full suite is still required

- **Backend, automatically** (the selector does it): `app/models.py`,
  `app/db.py`, `app/config.py`, `tests/conftest.py`, `pyproject.toml`,
  `uv.lock`, a new module that is not in the map yet, a deleted module.
- **Frontend, automatically**: any non-CSS source change. Nearly every
  component test renders the whole app through `renderApp`, so the import
  graph links ~80% of the suite to any file — `vitest related` saves almost
  nothing here, and pretending otherwise would be false confidence.
- **By judgement** (`--run --full`): a refactor that moves code between
  modules; a change to behaviour many features share without importing it
  explicitly (a default in `Settings`, a middleware, a DB constraint); a
  dependency upgrade; anything you would be uncomfortable explaining if CI
  went red.

### What the skip does *not* promise

- The backend map is a recording of which tests executed which module **at
  the commit it was generated**. A test added later for an old module is not
  selected until the map is rebuilt. An unmapped *module* is safe (full
  suite); an unmapped *test* is the gap. CI closes it on the pull request.
- Coverage attributes function bodies, not declarations. That is why
  declaration-only files are on the full-suite list and why the OpenAPI
  contract test rides along with every `backend/app` change.
- Pass stamps live in `.git/planora-test-stamps` — per clone, gone with the
  container. Across sessions the equivalent guarantee is CI on `main`.
- A local green selection is a reason to push, not a reason to merge. After
  pushing, the pull request's CI run is the verdict; if it goes red on a test
  the selection skipped, fix it and regenerate the map.

### Keeping the map fresh

```sh
.claude/skills/planora-conventions/scripts/affected_tests.py --regen-map   # ≈ 2.5 min, needs a green suite
```

Regenerate, and commit `scripts/backend-test-map.json`, when a change adds a
backend module or a test file, or when the selector reports "not in the test
map". It is a full backend run, so it doubles as that change's full
verification.

## Environment

`scripts/test-env.sh` is idempotent and does everything a fresh container
lacks: starts the local Postgres cluster, creates role `planora` (with
`CREATEDB` — the migration test creates and drops
`planora_migrations_test`) and databases `planora` + `planora_test`, runs
`uv sync --locked` and `npm ci`. Installs are cached in the image (seconds).

For a bare `uv run pytest`, export
`DATABASE_URL=postgresql+psycopg://planora:planora@localhost:5432/planora`
(or have a root `.env`). `conftest.py` supplies `APP_SECRET`,
`DIRECTOR_EMAIL` and switches the AI/Jira SSRF guards and AI limits off for
the general run. Tests always use `<db>_test` and refuse any other name.

**Two backend runs must not overlap**: the session fixture does
`drop_all`/`create_all` on the shared `planora_test`. Frontend runs are
independent and can run alongside.

## Backend tests — `backend/tests/`

pytest against a real Postgres. `conftest.py` fixtures: `engine` (session
scope, recreates the schema from the models — *not* from migrations), `db`
(a session in a transaction rolled back after the test), `mailbox`
(captures letters instead of sending). API tests build a `TestClient` with
`app.dependency_overrides[get_db]` pointing at that `db`. No test touches
the network: LLM and Jira go through `RecordedProvider` /
`RecordedJiraClient`, mail through `mailbox` or stubbed `smtplib`/`urlopen`.

| Concern | Test files |
|---|---|
| Mutation engine, undo | `test_mutations` (89), `test_undo_api`, `test_wave1_integrity`, `test_cascade` (auto-shift + critical path) |
| Plan approval, shift reasons | `test_plan_threshold`, `test_plan_api` |
| Project routes, settings, schedule | `test_project_api`, `test_settings_api`, `test_settings_resolution`, `test_schedule_api`, `test_slug_api`, `test_slugs`, `test_calendar`, `test_models` |
| Concurrency | `test_project_lock_races`, `test_proposal_push_race` (real threads, real commits) |
| Auth and accounts | `test_auth`, `test_email_verification`, `test_password_reset`, `test_locales`, `test_admin_api` |
| Organizations, invitations, access | `test_org_api`, `test_invitations` (domain), `test_invite_api` (HTTP), `test_access` (the matrix) |
| Comments, sharing, public page | `test_comments`, `test_comments_api`, `test_comment_api`, `test_sharing`, `test_share_api`, `test_public_api` |
| Proposal | `test_proposal_api`, `test_proposal_money` |
| Scorecard | `test_scorecard` |
| Export | `test_export_api` (opens the real XLSX/PDF with openpyxl/pypdf) |
| AI, Jira | `test_ai_intake`, `test_jira_api`, `test_jira_mapping`, `test_jira_netguard` |
| Mail, live | `test_mail`, `test_live_hub`, `test_live_api` |
| Whole product | `test_end_to_end` (interview → approve → shift → public link) |
| Remediation waves | `test_wave1_integrity`, `test_wave2_security`, `test_wave3_operations`, `test_wave4_api_design` — regression tests named after `docs/audit/remediation-plan.md` |
| Repo guards | `test_openapi_contract` (routes vs. `openapi.json`), `test_migrations` (chain vs. models, both directions), `test_wave3_operations::test_vercel_requirements_match_uv_lock`, `test_config`, `test_rate_limit`, `test_text`, `test_health` |

Guards that fail on an *unrelated-looking* edit — know them before they
surprise you:

- **Any route, parameter or schema change** fails `test_openapi_contract`.
  Intended change: `UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest
  tests/test_openapi_contract.py`, then `cd ../frontend && npm run gen:api`,
  commit both `backend/openapi.json` and `frontend/src/api/schema.d.ts`.
- **A model change without a migration** (or the reverse) fails
  `test_migrations`.
- **A dependency bump** without updating root `requirements.txt` fails the
  pin test.
- **A new key in one mail/export dictionary** but not the other languages
  fails the dictionary-completeness tests (`test_mail`, `test_export_api`).

## Frontend tests — `frontend/src/**/*.test.{ts,tsx}`

Vitest + jsdom + Testing Library, colocated with the source. Harness in
`src/test/`:

- `setup.ts` — MSW server; **a request the test did not declare fails the
  test** (collected and thrown in `afterEach`); a fresh fake WebSocket before
  every test; `localStorage` cleared after; async timeout 5 s (test timeout
  15 s in `vite.config.ts`).
- `server.ts` — default handlers for what the app frame always asks
  (`/api/config`, `/api/org/list`, comment counts, the Jira link).
- `utils.tsx` — `renderApp({ route, locale })` (the whole app with real
  routes and a `data-testid="location"` probe — assert navigation by where
  the user landed, not by mocking `useNavigate`), `renderWithProviders`,
  `sessionHandlers()` (profile + organization), fixtures `USER`, `ORG`.
- `project.ts` — one shared project state (`STATE`, `MEMBERS`) and handlers
  for the project screen; `pointer.ts` — `drag` / `dragDays` (send the
  trailing click a real browser sends); `socket.ts` — the controllable
  socket; `motion.ts` — `matchMedia` stub.

Test names and most assertions are in Russian and assert on translated UI
text, so an edit to `i18n/ru.json` can fail tests far from the dictionary.

Repo-wide frontend guards:

- `src/spacing-scale.test.ts` — no bare pixel literal in
  `padding`/`margin`/`gap`; use `--space-*` or mark the line `off-scale`.
- `src/theme-cascade.test.ts` — `northstar-theme.css` must not redeclare a
  selector that a component stylesheet already owns.
- `src/i18n/i18n.test.ts` — all three dictionaries have the same keys.
- `src/export/pageBudget.test.ts` — same expectation table as the server's
  `export/budget.py`.
- CI's "API contract" step — `npm run gen:api` must leave `schema.d.ts`
  unchanged.

`npx vitest run --coverage` is what CI runs; coverage has no threshold, so
drop `--coverage` locally.

## Writing a new test

- Backend: put it in the file for that concern; use `db` and, for HTTP, the
  file's existing `client`/`authed` fixtures rather than inventing new setup.
  A race is tested with real threads and commits, like
  `test_project_lock_races.py`, and cleans up after itself.
- Frontend: next to the component; declare every request with `server.use`;
  prefer `renderApp` for anything involving navigation or auth.
- A new backend module or test file → regenerate the map (above).
