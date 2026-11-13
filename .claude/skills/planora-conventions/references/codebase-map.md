# Codebase map: what every part of the repo is

Read this instead of exploring the tree. It names every backend module, every
API route group, every frontend folder and every repo-level file, with the one
fact about each that you would otherwise have to open the file to learn. For
*rules* (how to add a route, a mutation, a permission) see `../SKILL.md`; for
tables see `data-model.md`; for tests see `testing.md`.

Size, for calibration: backend `app/` ≈ 20k lines of Python in 68 files;
`backend/tests/` ≈ 17k lines, 51 files, 885 tests; frontend `src/` has 74
test files with 781 tests. One Alembic head, 29 migrations.

## What the product does (feature inventory)

- **Organizations → projects → categories → tasks** on a Gantt chart. A task
  has dates, duration in working days, status, progress, criticality, risk,
  assignees, dependencies, a description and an internal note.
- **Two time models.** A project starts `relative` ("Day N of the project",
  no real dates) and is anchored to a start date to become `calendar`.
- **Working calendar** per project (working-day mask, holidays, timezone),
  inherited from the organization unless overridden.
- **Undo journal.** Every plan edit is a revision with an inverse; undo the
  last action or a whole batch.
- **Approved baseline.** Approving a plan snapshots it; a later shift beyond a
  threshold demands a written reason. Versions can be restored.
- **Auto-scheduling** (opt-in per project) pushes successors forward along
  dependencies; **critical path** is computed on the server.
- **Comments** on projects and tasks, including guest comments through a
  **public share link** (read-only page, internal notes hidden).
- **Proposal** (quote): sections and work items with effort × rate, tax,
  stages draft → sent → agreed, build-from-plan and push-to-plan, client PDF.
- **Scorecard**: eight weekly health metrics with immutable weekly snapshots,
  alerts, and an auto-created task when a metric is red two weeks running.
- **Exports**: XLSX and PDF of the project; proposal PDF.
- **AI intake**: an LLM interview → summary → draft plan → applied as one
  undoable batch; also "split this task". Bring-your-own key per organization.
- **Live updates** over WebSocket (Docker only), with an offline lock.
- **Accounts**: registration, login, email verification, password reset,
  invitations, multiple organizations per user, roles owner / editor /
  viewer / client, per-project grants, an install-wide director panel.
- **Three interface languages**: Azerbaijani (default), English, Russian.

## Backend — `backend/app/`

### Core and platform

| File | What it is |
|---|---|
| `main.py` | App assembly. Logging with a request id; refuses to start with more than one worker (live hub is in-process); middlewares: `X-Request-ID`, body-size cap (413 `body_too_large`), Origin/Referer CSRF check on writes (403 `csrf_origin_mismatch`), `/api/v1/*` alias of `/api/*`. `/api/health` (liveness, no DB) and `/api/health/ready` (DB ping). Docs at `/api/docs`. |
| `config.py` | `Settings` (pydantic-settings, reads repo-root `.env`), `get_settings()` cached. Validators refuse to boot on a missing/placeholder `APP_SECRET` or `DIRECTOR_EMAIL`, unknown signup mode, incomplete mail transport. Every tunable limit lives here. |
| `db.py` | `Base`, engine, `get_db` (session per request, commit on success). |
| `models.py` | Every table and `StrEnum` — see `data-model.md`. |
| `security.py` | argon2 `hash_password`/`verify_password`, `new_token`, `hash_token`. |
| `crypto.py` | Fernet `encrypt`/`decrypt` keyed from `APP_SECRET`; `DecryptionError`. |
| `text.py` | `normalize_email`, `slugify`. |
| `slugs.py` | Unique slugs through a SAVEPOINT retry (`insert_with_unique_slug`), `slug_check`. |
| `throttle.py` | Durable Postgres-backed rate counters (`check`/`note`/`hit`) for login, signup, password reset, AI calls. |
| `rate_limit.py` | In-memory `SlidingWindow` + `client_key` for guest comments, exports, per-IP sign-in key. |
| `locales.py` | First-visit language from `Accept-Language`, default `az`. |

### Identity, organizations, access

| File | What it is |
|---|---|
| `auth.py` | `register`, `authenticate`, sessions (`open_session`, `session_for_token`, idle/absolute expiry, `close_other_sessions`), `current_user` dependency. |
| `access.py` | `Action` enum and the literal `Role → frozenset[Action]` matrix; `can`/`require`; `needs_project_grant`; `visible_op` strips `internal_note` from journal entries for roles without `READ_INTERNAL_NOTE`. Actions: `PROJECT_READ/WRITE/ADMIN/DELETE/EXPORT`, `ORG_ADMIN`, `COMMENT`, `READ_INTERNAL_NOTE`, `PLAN_APPROVE`, `PLAN_REAPPROVE`, `PROPOSAL_READ`, `TEAM_ASSESSMENT_READ`, `TEAM_PACE_READ`. |
| `director.py` | `is_director()` — install-wide role from `DIRECTOR_EMAIL`. |
| `orgs.py` | Active membership of a session, `switch`, role changes, last-owner protection (`LastOwner`). |
| `invitations.py` | Issue / reissue / revoke / accept; `InvitationError` codes; project grants on accept. |
| `email_verification.py` | Issue and redeem a verification link; never blocks sign-in. |
| `password_reset.py` | Issue and redeem a reset link; redeeming closes all sessions. |
| `mail/` | `send(to, template, params)`; `transports.py` = SMTP / HTTP API / log; `templates.py` + `templates/{az,en,ru}.json`. A failed send returns `False`, never raises into the request. |

### The plan

| File | What it is |
|---|---|
| `mutations.py` | The edit engine (2k lines). Wire models `Public*` (validated, bounded) → `to_internal` → internal ops. `apply_op` takes the project row lock, enforces the shift-reason threshold, applies, writes a `Revision` with op + inverse. `undo`, `undo_last`, `undo_batch`. Ops: create/delete/move/resize/reorder task and category, set duration / milestone / fields / criticality / risk / progress / status, rename and recolor category, add/remove dependency, assign/unassign user, `ApplyPositions`. Errors: `NotFoundInProject` (404), `InvalidOperation` (422), `UndoConflict`, `ReasonRequired`. |
| `projects.py` | `create_project`; `lock_project` — row lock **plus reload**; every serialized writer must use it. |
| `calendar.py` | `Calendar` (working-day mask + holidays), `end_date`, `count_working_days`. |
| `settings_resolution.py` | Project setting or the organization default (`null` = inherit); `project_calendar`. |
| `settings_input.py` | Shared validation of org and project settings payloads. |
| `schedule.py` | Relative axis (`RELATIVE_EPOCH`, a Monday) and anchoring to a start date (`planned_schedule` preview, `apply_schedule`). Anchoring moves every task outside the journal, so it also raises `Project.undo_floor_seq` (earlier revisions stop being undoable) and writes `current_start` into every `PlanVersion` snapshot row (what `restore_plan_version` reads). |
| `cascade.py` | Forward-only push of successors when `auto_schedule` is on. |
| `critical.py` | Critical path by total float in working days. |
| `plans.py` | `approve_plan`, `deviation_days`, plan versions, `restore_plan_version`. |
| `comments.py` | Comments outside the journal; guest and member authors. |
| `sharing.py` | Public link issue / rotate / revoke; URL built from `PUBLIC_BASE_URL`, never from `Host`. |
| `live.py` | In-process `Hub` of rooms per project for WebSocket broadcast. |

### Modules with their own domain layer

| Path | What it is |
|---|---|
| `proposals.py` | Quote sections/items, stages, `build_from_plan`, `push_preview`, `push_to_plan` (through mutations, one batch), role/rate suggestions. |
| `scorecard.py` | Metrics `overdue_tasks`, `finish_drift`, `scope_growth`, `date_shifts`, `close_rate`, `stale_in_progress`, `data_quality`, `team_pace`; ISO weeks in the project timezone; lazy weekly commit on GET; alerts; drill-down tasks. No scheduler anywhere. |
| `export/` | `document.py` builds one localized, permission-trimmed snapshot read by both renderers; `xlsx.py` (openpyxl); `pdf.py` (reportlab canvas, vector Gantt); `proposal_pdf.py` (client document); `budget.py` page/zoom budget (mirrored by `frontend/src/export/pageBudget.ts`); `labels.py` + `labels/{az,en,ru}.json`; `theme.py` colours copied from the frontend CSS; `fonts/` Inter. |
| `ai/` | `credentials.py`, `provider.py` (`LlmProvider` protocol, `HttpProvider`, `RecordedProvider`), `netguard.py` (SSRF), `schemas.py` (model output validation), `intake.py` (interview → summary → draft → apply; split task), `usage.py` (daily token budget, `MeteredProvider`). Topics come from `config/intake_topics.yml`. |

### HTTP layer — `backend/app/api/`

Routes hold no business logic. `deps.py` gives `ProjectContext`
(`project_context` dependency: 404 `project_not_found` for a foreign,
missing or ungranted project; `.require(Action)` → 403 `forbidden`).
`serialization.py` builds the project state for both the member screen and
the public page (two visibility flags, one assembly).

| Router file | Prefix | Routes |
|---|---|---|
| `meta_routes.py` | `/api/config` | Install config for the SPA (mail enabled, signup mode, locales, sharing, `live_enabled`). |
| `auth_routes.py` | `/api/auth` | `register`, `login`, `logout`, `me` (GET/PATCH), `password`, `password/forgot`, `password/reset`, `sessions/close-others`, `verify-email`, `verify-email/resend`. |
| `org_routes.py` | `/api/org` | `GET/PATCH /`, `list`, `switch`, `slug-check`, `members`, `members/{user_id}` (PATCH role, DELETE). |
| `invite_routes.py` | `/api/org/invitations`, `/api/invitations/{token}` | Manage (owner) and preview/accept (anyone with the link). Two routers. |
| `project_routes.py` | `/api/projects` | List/create, `GET/PATCH/DELETE /{id}`, `mutations` (POST, `Idempotency-Key` header), `undo`, `batches/{batch_id}/undo`, `revisions`, `schedule` + `schedule/preview`, `plan/approvals` (+ `/{version}/restore`), `comments`, `comments/counts`, `slug-check`. |
| `proposal_routes.py` | `/api/projects/{id}/proposal` | `GET/PATCH`, `stage`, `categories…`, `tasks…`, task comments, `build-from-plan`, `push-plan` (preview), `push-to-plan`. |
| `scorecard_routes.py` | `/api/projects/{id}/scorecard` | `GET`, `recalculate`, `metrics/{key}` (PATCH), `metrics/{key}/tasks`. |
| `export_routes.py` | `/api/projects/{id}` | `export.xlsx`, `export.pdf`, `export/facts`, `proposal/export.pdf`. |
| `share_routes.py` | `/api/projects/{id}/share` | GET, POST (issue), `rotate`, PATCH (comments on/off), DELETE. |
| `public_routes.py` | `/api/public/{org_slug}/{project_slug}` | State, comments, counts, guest comment, `export.xlsx/pdf`. No session; every refusal is 404 `link_not_found`. |
| `live_routes.py` | `/api/projects/{id}/live` | WebSocket; Origin-checked handshake; per-socket visibility of notes. |
| `ai_routes.py` | `/api/ai` | `credential`, `sessions` and their `answers` / `summary` / `draft` / `apply`, `tasks/{id}/split` (+ `/apply`). |
| `admin_routes.py` | `/api/admin/users` | Director only. |

`backend/openapi.json` is the committed snapshot of `app.openapi()`;
`frontend/src/api/schema.d.ts` is generated from it.

### Migrations — `backend/migrations/versions/`

Linear, hand-written. Order (oldest → head): `da03b89192b8` initial →
`3b3648273b7a` JSONB journal + indexes → `822f5eb3ea88` invitations / project
access / active org → `4d44f75a2766` email verification → `adee83e4d023` share
links / comments → `036f4c8b7f36` plan versions → `6681178a1115` undo
bookkeeping → `9c584d8968ac` AI sessions + LLM credentials → `e604dc58fa04`
wave 2 (throttle, AI usage) → `07266c100dff` wave 4 (indexes, idempotency) →
`b7c4e1f0a9d3` task status → `c1a7d2f4e8b9` user timezone → `c9d2f6b8a154`
password reset → `e8b3d6a1c4f7` relative schedule mode → `f2a71c9d4b3e` task
milestone → `a3f9c25e71b0` project auto_schedule → `d7e2b41c8f60` proposal →
`b8d1e5f30a72` proposal notes → `e3c9a5d17b42` verification sent/used →
`c1d4a8f6e293` membership project_scoped → `f1a4c8d7b6e2` user
last_active_at → `b4e7a2c9d3f1` scorecard → `342f2b35de69` jira →
`12c30f757a97` jira push due date (both undone by the last one) → `a1b2c3d4e5f6` scorecard signal cleanup →
`c4d8e2f1a9b7` proposal status + plan links → `d5e8f1a2b3c4` task risk →
`e7a3c9b1d4f2` project undo floor →
**`f4b8d2c6a1e9` drop jira integration (head)**. A new migration sets `down_revision` to the current head — confirm
it with `uv run alembic heads` rather than trusting this list.

## Frontend — `frontend/src/`

Entry: `main.tsx` → `App.tsx` (QueryClient with `retry: false`, `LocaleProvider`,
`BrowserRouter`, `AuthProvider`) → `AppRoutes.tsx`. Styles: `styles.css`
(tokens incl. the `--space-*` scale, base rules) then `northstar-theme.css`
(light theme, overrides `styles.css` only), then per-feature CSS imported by
its component.

### Routes (`AppRoutes.tsx`)

Public: `/register`, `/login`, `/forgot-password`, `/reset-password`,
`/verify-email`, `/invite/:token`, `/p/:orgSlug/:projectSlug` (public page).
Behind `RequireAuth`: `/` → `/projects`; `/projects`; `/my-tasks`;
`/projects/new/ai`; `/projects/:projectId` and its tabs
`/proposal`, `/scorecard`, `/history`; `/projects/:projectId/settings`;
`/settings` with `organization`, `members`, `profile`; `/admin`. Legacy
`/members`, `/portfolio`, `/reports` redirect.

### Folders

| Folder | Contents |
|---|---|
| `api/` | `client.ts`: `request<T>()`, `requestFile()`, `ApiError { code, status, hints }` (hints from `X-Shift-*` headers), `saveFile`. One wrapper per resource: `auth`, `org`, `projects`, `revisions`, `comments`, `proposal`, `scorecard`, `share`, `public`, `export`, `invitations`, `ai`, `admin`, `config`. `errors.ts` = explicit list of error codes that have a translation. `schema.d.ts` generated. |
| `auth/` | `AuthProvider` (three states: checking / signed in / anonymous), `RequireAuth`, `afterAuth` (where to land), `invite` (pending-invite handoff), `permissions.ts` (`roleCanWrite`, `roleCanReadProposal`, `useCanWrite`, `useOrgRole` — mirrors the server matrix for *showing* controls only), `verificationNotice`. |
| `screens/` | One file per route: `Login`, `Register`, `ForgotPassword`, `ResetPassword`, `VerifyEmail`, `Invite`, `Projects`, `MyTasks`, `Project` (tabs: chart / proposal / scorecard / history), `ProjectSettings`, `PublicProject`, `Settings` (+ `OrgSettings`, `Members`, `Profile`), `Admin`, `AiIntake`; `ShareDialog`, `CategoryForm`; `projectStates.ts` (list + per-project state queries shared by Projects and My tasks). |
| `gantt/` | The hand-built chart. Pure math: `scale.ts` (zoom levels, `DAY_WIDTH`), `timescale.ts` (ISO-string dates ↔ pixels, no `Date` objects), `relative.ts` (Day N axis), `columns.ts` (pinned table columns), `motion.ts`, `edgeScroll.ts`. Components: `Gantt`, `Grid`, `Header`, `Row`, `Cells`, `Arrows` (dependency elbows), `BarTip`, `NewTaskRow`, `RowMenu`, `AssignMenu`, `ViewControls`, `BottomActions`, `UndoMove`. Hooks: `useDragDates`, `useDragCategory`, `useLinkDrag`, `useReorder`, `useBarMotion`, `useGanttView`, `useViewportFit`, `useLaneWidth`, `useQuickTask`, `useQuickCategory`, `useTruncatedTitle`, `scalePreference`. |
| `project/` | Everything around the chart: `useProjectMutation` (the single optimistic write path: snapshot → show → send → roll back or refetch; handles `reason_required`), `optimistic.ts` (state transforms, no calendar math), `useUndo`, `UndoHotkey`, `baseline.ts` (deviation rule mirrored from the server), `ShiftReason`, `PlanApproval`, `PlanChangesPanel` + `planChanges.ts`, `PlanSummary` + `summary.ts`, `progress.ts` (duration-weighted readiness), `verdict.ts` (overdue vs. past-deadline), `ProjectHead`, `ProjectBar`, `ProjectHistory`, `DependencyNudge`, `StartDateDialog`, `SharePanel`, `ShareControls`, `categoryColors`, `useDeleteProject`. |
| `task/` | `TaskPanel` (the task card: fields, dependencies, tabs), `Comments`, `History`, `TaskProgress`, `formatEvent.ts` (journal entry → localized phrase), `fields`, `PanelSection`. |
| `proposal/` | `Proposal` screen, table, stepper, params, summary, notes, task panel, push-to-plan dialog and button, `money.ts`. |
| `scorecard/` | `Scorecard.tsx` (incl. the SVG `Sparkline`), `scorecard.css`. |
| `comments/` | `CommentThread`, `guestName.ts`. |
| `export/` | `ExportDialog`, `ExportPreview`, `pageBudget.ts` (same table as `backend/app/export/budget.py`). |
| `live/` | `useProjectLive` (states connecting / online / offline / unavailable), `LiveProvider`, `OfflineBar`. Offline locks editing. |
| `i18n/` | `index.ts` (`translate`, plural rules via `Intl.PluralRules`, fallback to `az`, then the key), `LocaleProvider`, `dates.ts` (month names from dictionaries — `Intl` has none for `az`), `az.json` / `en.json` / `ru.json`. |
| `components/` | Shared UI: `Header`, `Menu`, `Modal`, `ConfirmAction`, `Field`, `Switch`, `StatusChip`, `Avatar`, `toast`, `autosave.tsx` (fields save themselves — text on blur), `rows.tsx` (in-place editable cells, shared by chart and proposal), `InviteDialog`, `OrgSwitch`, `LocaleSwitch`, `VerifyEmailBar`, `CreateProjectActions`, `icons`, `roles`, `hotkeys`, `useEscape`. |
| `settings/` | `fields.tsx` — fields shared by organization and project settings. |
| `time/` | `zone.ts` ("today" as a calendar day in the reader's zone), `useToday`. |
| `test/` | Test harness — see `testing.md`. |

Frontend habits that are easy to break: user-visible text only through
`t("key")` with the key present in **all three** dictionaries; error text
only from an `ApiError.code` listed in `api/errors.ts`; dates as ISO strings;
end dates and working-day arithmetic come from the server, never computed in
the browser; every plan write through `useProjectMutation`.

## Repo-level files

| Path | Role |
|---|---|
| `docker-compose.yml` | Services `db` (postgres:16), `migrate` (`alembic upgrade head`), `api` (uvicorn, one worker), `backup` (daily `pg_dump`, `docker/backup.sh`), `web` (Caddy + built SPA). |
| `docker-compose.dev.yml` | Overlay: publishes Postgres, `uvicorn --reload`, Vite dev server. |
| `docker/postgres-init/10-create-test-db.sh` | Creates `<db>_test` on first volume init. |
| `Caddyfile` | TLS, security headers, `/api/*` → api, everything else → `index.html`. |
| `backend/Dockerfile`, `frontend/Dockerfile` | Images; the web image's context is the repo root (it needs `Caddyfile`). |
| `api/index.py`, `vercel.json`, `requirements.txt` | Vercel layout. `requirements.txt` pins must equal `backend/uv.lock` (tested). |
| `config/intake_topics.yml` | Required AI interview topics. |
| `.env.example` | Every setting with comments; the placeholders for `APP_SECRET` and `DIRECTOR_EMAIL` are rejected at startup. |
| `.github/workflows/ci.yml` | The three CI jobs. |
| `README.md`, `README.ru.md` | Product overview (EN) and the full operations guide (RU). |
| `docs/superpowers/specs/` | Original product specification ("section N" references in code comments point here). |
| `docs/superpowers/plans/` | Historical build plans. |
| `docs/audit/remediation-plan.md` | The "waves" behind `test_wave1…4_*.py`. `docs/audit/ui-audit-2026-08.md` — UI audit. |
| `docs/design/` | Northstar redesign plan and mockups. `docs/brand/`, `docs/screenshots/` — assets. |
