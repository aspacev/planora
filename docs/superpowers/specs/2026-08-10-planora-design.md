# Planora — a Gantt chart with AI intake

Date: 2026-08-10
Status: approved for implementation

> **Historical, partially superseded.** This is the original v1 design spec.
> The core model (org → project → category → task, the mutation/revision
> journal, AI intake, public links, i18n) is still accurate, but two "out of
> scope" calls below were reversed later and two whole subsystems shipped
> that this spec predates entirely — see the inline notes at §4 and §14.
> For current architecture, see the repo's `CLAUDE.md` and the
> `planora-conventions` skill.

## 1. What this is and what for

A self-hosted project planner with a Gantt chart. Two jobs:

1. Ordinary work on a plan: categories, tasks, owners, dates, criticality, the change history.
2. Creating a project through an AI interview: the system asks questions, and what comes out is a ready structure of categories and tasks that a person edits and approves.

A separate goal is showing a project to the outside by a pretty link with no registration for the recipient.

The key principle: **the AI writes nothing into a project without a person's explicit confirmation.**

## 2. Users, roles and invitations

A multi-tenant system. The organization is the unit of data isolation; everything belongs to it.

| Role | Permissions |
|---|---|
| `owner` | Everything, including the organization's settings, the members, the LLM key, deleting projects |
| `editor` | Creating and editing projects and tasks, approving the plan |
| `viewer` | Reading all the organization's projects, comments |
| `client` | Reading **only the projects they were explicitly invited to**, comments. Does not see the list of the other projects, the organization's members or the settings |
| a guest by link | Reading a single project by a public link, comments (if enabled). Has no account |

`client` and a guest **do not see the "internal note" field** — neither on the page nor in the API responses. This is the only field with restricted visibility; there will be no elaborate per-field visibility system.

### How a person joins an organization

**Registration is open.** A person visits the site, enters a name, an address and a password — and finds themselves inside. Registration creates their own organization: they become its `owner`, the slug is suggested from the name and is editable. No console commands and no pre-created accounts: an install must be usable right after deployment, without access to the server.

Invitations exist for the second job — inviting people into an **already existing** organization. One person can belong to several: they registered with their own company, accepted an invitation into somebody else's, and both are available from a switcher.

**Address confirmation** is requested only if mail is configured in the install. If there is no mail transport, the account works straight away — otherwise an install without a mail server turns into a broken one, and that is exactly the scenario the whole project is self-hosted for. Confirmation does not block work at that: until it is confirmed you cannot invite others, everything else is available.

For closed installs there is still the `SIGNUP_MODE` switch in the environment: `open` by default, `invite_only` — by invitation only, `closed` — there is a sign-in but no registration. This is a setting for whoever deploys rather than the product's default behaviour.

**An invitation is one entity with two delivery methods.** The owner opens the member list, enters addresses (several at once is fine), picks a role, and for the `client` role also the projects it grants access to straight away. After that both actions are available on every invitation:

1. **Send an email** — a link goes to the address.
2. **Copy the link** — the very same link, to send however is convenient: in a messenger, by voice, on paper.

The second path is not a fallback for when mail breaks but an equal one. Emails get lost in spam, corporate filters cut unfamiliar senders, and for half the people work correspondence lives in a messenger. A tool whose only way to invite someone is to hope an email is delivered regularly turns out to be unusable at the least convenient moment.

**The invitation's rules:**

- **One-time.** An accepted invitation is dead immediately — the link cannot be reused by forwarding it on.
- **A lifetime** of 7 days by default, set by a setting. An expired invitation shows "it has expired, ask for a new one" rather than a silent error.
- **The role is fixed at the moment of the invitation** and cannot be changed by the recipient. A link for the `viewer` role does not turn into an `editor` one, however it is opened.
- **The address ties the invitation down.** If an address is given, it is filled in at registration and is not editable; to someone signed in under a different account the system says outright who the invitation is addressed to and offers to sign out. An invitation created without an address (for copying the link only) goes to whoever holds it — that is a deliberate trade-off, and it is named in words in the interface.
- **Revocation.** The owner can revoke an unused invitation; the link dies instantly.
- **Re-sending** issues a new token and kills the old one, otherwise revoking "that particular old email" becomes impossible.
- **The token is stored as a hash**, like a password. A leaked database dump must not hand out access to organizations. A direct consequence that has to be accepted deliberately: **we show the link once** — at the moment the invitation is created. After that there is nowhere to take it from, because the server does not remember it; in the member list an unaccepted invitation has an "issue a new link" button, which creates a new token and kills the previous one. The alternative — storing the token in a decryptable form so it can be copied later — saves one click and turns a database dump into a set of working keys to every organization.
- **A limit on the number of invitations** per hour per organization. Without it the application turns into a free email blaster from somebody else's domain — and the sender's domain quickly ends up on blocklists.

**The email contains the minimum:** who invited, into which organization, which role, the link and its lifetime. No project names and no tasks: the email goes to an address nobody has confirmed yet. The email's language is the organization's, because nothing is known about the recipient's yet.

If the invited address already belongs to an existing user, the invitation does not create a second account but adds a membership in the organization.

If a person with an invitation in hand goes and registers the ordinary way instead of accepting it, nothing breaks: they get their own organization, while the invitation stays in force and fires when they open the link. There is no automatic placement into somebody else's organization by a matching address: a membership appears only after an explicit action by the person.

## 3. The data model

### The hierarchy

`Organization` → `Project` → `Category` → `Task`. Exactly two levels inside a project, with no nesting of categories and no subtasks.

### The entities

**Organization** — `id`, `name`, `slug` (globally unique) plus the block of defaults the projects inherit: `default_locale`, `default_timezone`, `working_days` (a weekday mask, Mon–Fri by default), `week_start`, `holiday_calendar` (the list of non-working dates — the shared public-holiday calendar), `default_shift_threshold_days`, `public_sharing_enabled`, `default_comments_enabled`.

**User** — `id`, `email`, `password_hash`, `name`, `locale` (`az` | `en` | `ru`, `az` by default), `email_verified_at` (nullable — stays empty in installs without mail).

**Membership** — `org_id`, `user_id`, `role` (`owner` | `editor` | `viewer` | `client`).

**ProjectAccess** — `project_id`, `user_id`. Needed only for the `client` role: the list of projects they were invited to.

**Invitation** — `id`, `org_id`, `email` (nullable — a link-only invitation), `role`, `project_ids` (for the `client` role), `token_hash`, `invited_by`, `created_at`, `expires_at`, `accepted_at` (nullable), `accepted_by` (nullable), `revoked_at` (nullable), `last_sent_at` (nullable — filled in only when an email is sent).

An invitation lives in the database after being accepted too: it is a record of who brought whom, and `accepted_at` doubles as the "the token no longer works" flag.

**Project** — `id`, `org_id`, `name`, `slug` (unique within the organization), `deadline` (a target date, nullable), `plan_approved_at`, `plan_version` (an integer, grows on re-approval), `schedule_mode` (`relative` | `calendar`), `start_date` (the assigned start date, nullable — see "The relative plan").

Plus overrides of the organization's settings, all **nullable**: `timezone`, `working_days`, `shift_threshold_days`. `null` means "inherit from the organization" rather than "empty". That is fundamental: if the organization's values were copied when a project is created, a later edit of the organization's default would not reach the existing projects, and in six months nobody would understand why two projects have different thresholds.

Separately, `holidays_extra` (this project's additional non-working days) and `workdays_extra` (working Saturdays and other transfers). Both apply on top of the organization's calendar rather than instead of it.

**Category** — `id`, `project_id`, `name`, `color`, `position`.

**Task** — `id`, `project_id`, `category_id`, `name`, `description`, `start_date`, `duration_days`, `criticality` (`low` | `normal` | `high` | `critical`), `progress_pct` (0–100), `internal_note`, `position`, `baseline_start`, `baseline_duration` (both `null` if the task was created after the plan was approved), `created_by_ai_session_id` (nullable).

The end date is not stored — it is computed from `start_date` and `duration_days` by the project's calendar.

**TaskAssignee** — `task_id`, `user_id`. There can be several owners.

**Dependency** — `id`, `project_id`, `from_task_id`, `to_task_id`. Without link types and without lag: this is an arrow in a picture, not a calculation rule.

**Revision** — `id`, `project_id`, `seq` (a sequence number within the project), `actor_user_id`, `op` (the operation's JSON: the event type and its parameters, from which the history's phrase is assembled in the reader's language — see the "Languages" section), `inverse` (the inverse operation's JSON), `reason` (nullable, filled in on a shift past the threshold, stored as entered and not translated), `batch_id` (nullable, groups a batch from the AI), `created_at`.

**PlanVersion** — `id`, `project_id`, `version`, `approved_by`, `approved_at`, `snapshot` (JSON: the dates and durations of all the tasks at the moment of approval).

**ShareLink** — `id`, `project_id`, `token`, `comments_enabled`, `revoked_at`.

**Comment** — `id`, `project_id`, `task_id` (nullable — a comment can be on the whole project), `author_user_id` (nullable), `guest_name` (nullable), `body`, `created_at`.

**OrgLlmCredential** — `org_id`, `provider`, `base_url`, `model`, `encrypted_key`. The key is encrypted symmetrically with the application's secret and is never handed out — only a "the key is configured" flag. `base_url` and `model` are mandatory as settings rather than constants: without them BYOK works only with one cloud on one hard-wired model, and the promise "you can plug in a local model" stays words.

**AiSession** — `id`, `org_id`, `project_id` (filled in after applying), `locale` (the language of the interview and the draft), `status` (`interview` | `summary` | `draft` | `applied` | `abandoned`), `transcript` (JSON: the list of questions and answers), `summary` (JSON: the theses), `draft` (JSON: the categories and tasks), `tokens_used`, `applied_batch_id` (nullable).

### The calendar

Duration is set in **working days**. Which weekdays are working is a setting (`working_days`) rather than a constant: Monday–Friday by default, but weekends fall on different days in different countries, and hard-wiring Saturday and Sunday into a tool shown to external clients will not do.

A project's final calendar is assembled in this order: the weekday mask → minus the holidays from the organization's calendar → minus the project's `holidays_extra` → plus the project's `workdays_extra`. All date work goes through two functions: "add N working days" and "how many working days between two dates". There are no direct calendar-day operations in the code.

The reasoning: if a task starts on a Friday and lasts 3 days, an end on Sunday looks like a bug and breaks trust in the chart.

### The relative plan and binding to a start date

A project's plan and its calendar dates are separated. A new project is born in `relative` mode: the start date is not asked for at creation, the scale reads as "Month 1 / Week 1 / Day 1" (a "month" is a visual group of four weeks, the computations run in days), and the "today" line and real month names are not shown. The tasks are at that fully defined by their offset from the project's start in working days, their duration and their links — the structure and the overall length can be shown to a customer before the start date is known.

The offset is stored in the same `start_date` column — as a coordinate on the relative axis: project day N = `RELATIVE_EPOCH` (2001-01-01, a Monday) + (N − 1) calendar days. The offset and the coordinate map one to one given a fixed weekly mask, while the coordinate is already spoken by the revision journal, the undo, the shift threshold and the plan snapshots — a second column would be a second representation of the same thing and would one day diverge from the first. The relative axis's calendar is a single weekly mask with no holidays: a holiday is a property of a real date, which the plan does not have yet. The serialization also hands out the derived `start_offset_days`.

The "Assign a start date" button opens a dialog with a date, a choice of working week (5/2, 6/1, calendar days) and a server-computed end date before the confirmation. Applying moves the project into `calendar`: every task's offset is laid out from the assigned start by the real calendar now — with weekends and holidays — the baselines travel along the same axis, the durations and the links do not change. The source of truth before and after is the start plus the durations plus the links plus the working calendar rather than the end date: that is why changing the start again recomputes the plan without drift (or, by choice, leaves the tasks' dates in place). The "Relative | Calendar" view switcher and the relative-mode indicator itself disappear from the toolbar as soon as a start date is assigned: a calendar project has real dates, and there is no point showing it again as "12 weeks".

## 4. Scheduling: what the system does and does not do

**It does:** store a start date and a duration on every task, compute the end date by the calendar, derive a category's bounds as the minimum and maximum over its tasks.

**It does not:** recompute dates along links, move tasks automatically, compute the critical path and float, or level people's workload.

> **Superseded.** Two of these were later built: critical-path/float
> calculation (`backend/app/critical.py`) and opt-in auto-shift of
> successor tasks along dependency edges (`backend/app/cascade.py`,
> gated by the project's `auto_schedule` setting, off by default). Workload
> balancing across people is still not implemented.

Links exist only as arrows. The single behaviour: after a task with an outgoing link is moved, if the linked task now starts earlier than the predecessor's end, an unobtrusive offer appears — "Move 'Build the site' by 5 days?" with a single button. Refusing breaks nothing: the arrow is simply drawn askew.

A category has no dates of its own and is not dragged with the mouse.

## 5. The approved plan and the mandatory shift reason

**A draft.** Before "Approve plan" is pressed, edits are free and nothing is asked.

**Approval.** The button is available to `owner` and `editor`. A snapshot of all the tasks' dates and durations is taken: it is written into `PlanVersion.snapshot` and at the same time into every task's `baseline_start` / `baseline_duration` fields.

**After approval** a change to `start_date` or `duration_days` is checked like this:

```
deviation = max(|start_date - baseline_start|, |duration_days - baseline_duration|)
if deviation > shift_threshold_days → a reason is mandatory
```

The deviation is measured **from the baseline plan rather than from the previous value**. Otherwise a task gets moved five times by one day, travels a week in total, and there is not a single explanation in the history.

**How it is asked.** A modal right when the mouse is released (or on saving in the card): the text "A shift of 7 days", an explanation, a mandatory reason field, and the "Revert" and "Save" buttons. An empty reason — the button is disabled. The change **is not applied** until the reason has been entered; the intermediate "shifted but not explained" state does not exist in the system.

**Tasks created after the approval** have no baseline plan, are marked with a "beyond the original plan" sign and are exempt from explanations: adding work is normal, a hidden shift of dates is not.

**Re-approval.** The `owner` presses "Re-approve plan": a new `PlanVersion` is created and the tasks' baseline values are updated. The old versions stay available together with the accumulated reasons — this is a chronicle of "what was promised in January, what in March and why it slipped".

**The lag is displayed on two levels at once — the task's and the project's.**

*The task level — the baseline's ghost.* Under the current bar a thin grey bar with the approved plan's dates, and to the right a deviation badge ("+7 days"). In the card, a summary of the deviation and a list of all the moves with their reasons. That is precise and honest: both the start's shift and the duration's stretch are visible.

*The project level — the target date and overshooting it.* A project has a `deadline`. On the chart it is drawn as a red dashed vertical across every row, with a caption in the header. Any task ending later than it is painted red and gets a flag in the left column. A chip with the verdict hangs permanently in the project's header: either "The project ends on 8 June — 7 days later than the 1 June deadline" in red, or "We fit into 1 June — 5 days of slack" in green. The chip is visible by the public link too: it is the only figure that genuinely interests the customer.

Why exactly these two and not more. The ghost answers the question "where should the task have stood", the deadline line answers "are we going to make it at all". A lag in days interests nobody until it breaks the promised date, so without the second level the first stays bookkeeping for bookkeeping's sake.

A rejected option worth recording: filling the gap between the planned and the actual end right inside the bar, extending it. It looks the most illustrative, but then the bar's start means the planned date and its end the actual one, and the bar stops meaning the task's real dates. Having deceived the eye once, the chart loses trust entirely.

Deferred until there is demand: a lag heat scale in the left column with a reduction over categories (needed on long projects, when the bars do not fit on screen) and a chart of how the promised end date moved across plan versions (only meaningful once several versions have accumulated).

## 6. Mutations, history, live updates

The client does not send a whole task object. It sends an operation: `create_task`, `move_task`, `set_duration`, `set_criticality`, `assign_user`, `create_category`, `add_dependency` and so on. Every operation can apply itself and build its own inverse.

It follows from this that:

- **A task's history** is the revision journal filtered by that task. "19 March, Alexey moved the start from 12 to 19 March. Reason: the customer did not send the brand book".
- **Undo** is applying the last revision's `inverse`.
- **Applying the AI** is a batch of ordinary mutations with a shared `batch_id`, rolled back whole with one button.
- **Live updates** — the revisions are broadcast into the project's WebSocket, and the clients apply them to their own copy of the state.

**Conflicts.** The mutations are pointwise, so edits of different tasks do not conflict at all. On simultaneous edits of one field of one task the last one wins; both changes stay in the history, and a lost edit can always be found. There are no locks and no "someone is editing" indicators.

**Guests** are connected to the same WebSocket in listening mode: they see comments appearing and bars moving without a reload.

## 7. Public access

An address of the form `planora.ru/p/acme/redesign-2026` — the organization's slug and the project's slug, both editable by the owner. A taken slug suggests a free variant right in the input field.

The page shows the same layout as the working screen, but without the editing tools and without the internal notes.

A guest enters a name on their first comment. It is remembered in the browser and signs their replies with a "guest" mark, visually distinct from members with accounts. The owner can switch the comments off with a toggle and reissue the link: the old one dies instantly.

## 8. AI intake

The interview is available **only when creating a new project**. Launching a full interview inside an existing project is the next stage, not the first version. The pointwise "split the task" action (see below) works in any project and requires no interview.

### Step 1: the interview

The backend holds a list of mandatory topics: the project's goal, the scope of work, the deadline, the participants, the constraints, what is already done, what is definitely out of scope. After every answer it is marked which topics are closed; the model sees what is left and picks the next question. The questions are asked one at a time.

A hard ceiling of 12 questions. At any step a "that's enough, generate it" button is available. Without a ceiling the model clarifies endlessly.

### Step 2: the summary (a gate)

Before generating, a list of theses is shown: "here is what I understood about the project". The theses are editable and deletable. This is where a misunderstanding is cheapest to catch — before it turns into a hundred wrong tasks.

### Step 3: the draft (the main gate)

The model returns a strict structure: categories, each with tasks carrying a name, a description, a duration in days, a criticality and a suggested start date. The answer is validated against a schema on the backend. If it does not pass — a repeat request, at most twice, then an honest error message with the session preserved.

The draft opens as an editable table: editing names and dates, deleting, adding your own, moving tasks between categories. **Nothing is written into the project until "Apply" is pressed.**

### Step 4: applying

The draft is applied as a batch of mutations with a shared `batch_id` and the AI session's mark. Every task's history keeps "created by the AI session of 10 August". The whole batch is rolled back with one button.

### The pointwise action

On any task — "split into several". The AI offers 3–5 tasks in the same category with the dates divided up, shows a preview and applies only on a button press. There are no other AI actions in the first version.

### Keys

The key is stored per organization, encrypted in the database, and entered in the settings by the owner. The provider is hidden behind a thin interface (`generate(messages, schema) -> dict`), so that changing providers or moving to a local model does not touch the rest of the code. No key — the AI buttons are disabled with a link to the settings. Token spend is recorded per session.

## 9. Languages

The interface is in three languages: **Azerbaijani (by default), English and Russian**.

Azerbaijani is the default language: it is set for a new user if the browser does not explicitly ask for another supported one, and it is also what the interface falls back to when a key is missing from a dictionary.

**What is translated and what is not.** Only the interface is translated: the captions, the buttons, the error messages, the month and weekday names. User content — project, category and task names, descriptions, internal notes, comments and shift reasons — is stored exactly as entered and is never translated. An Azerbaijani team running a project in Azerbaijani will show it to an English client with Azerbaijani task names, and that is right: replacing content with a machine translation is worse than an honest foreign language.

**A task's history is stored structurally rather than as text.** The journal holds `{event, parameters}` — for example `{k: "moved_start", from: …, to: …}` — rather than a ready phrase "moved the start from 12 to 19 March". The phrase is assembled at display time, in the reader's language. Otherwise a history written by an Azerbaijani would stay Azerbaijani forever for an English client. The shift reason is the exception: it is the user's text and stays as is.

**Choosing a language.** The language is stored in the user's profile. On the first sign-in it is taken from the `Accept-Language` header if it asks for one of the three supported ones; otherwise Azerbaijani. After that, only what the person chose themselves. On the public page a guest has no profile: the language is determined from the browser by the same rule, and a switcher stands in the page's corner, because a client may not share a language with the team.

**Formats.** The week starts on Monday in all three languages. Dates are printed in each locale's own format. A known limitation, accepted deliberately: a native `input[type=date]` shows the date in the browser's locale format rather than the application's — a date picker of our own is not in the first version.

**The Azerbaijani i trap.** Azerbaijani and Turkish have two different "i"s: `İ/i` with a dot and `I/ı` without. Locale-aware case conversion sends them the wrong way: in a browser `"I".toLocaleLowerCase("az")` gives `ı` rather than `i`, and a database collation configured for the Azerbaijani locale does the same. Everything that relies on case starts behaving differently depending on whose locale happened to be active. The rules:

- Case conversion for comparisons, search and logins is done **only in the invariant locale**, never in the user's. On the frontend that means `toLowerCase` rather than `toLocaleLowerCase`; on the backend `casefold`, which does not depend on the process's locale at all.
- A project's slug is built from an explicit transliteration table: `ə→e, ğ→g, ı→i, İ→i, ö→o, ş→s, ü→u, ç→c` plus Cyrillic. Automatic diacritic stripping is forbidden **as the only mechanism**: `ə` is a letter in its own right rather than "an e with an ornament", and it decomposes into no `e` at all. As a fallback for languages outside the table the automatic route is permissible and useful: a French project name is better turned into `cafe-central` than into `caf-central`.
- The slug and email uniqueness checks rely on an explicit comparison of the normalized form rather than on the database's collation.

A consequence that has to be accepted deliberately: `İsmail@x.com` and `Ismail@x.com` will remain **different** addresses, because full case folding gives different strings for them. That is right — silently merging two accounts is worse than two similar addresses.

**The AI in the right language.** The language of whoever started the session is fixed in `AiSession.locale` and passed into the prompt as an explicit parameter: the interview is conducted and the task draft generated in it. The response schema stays language-independent at that — what arrives in the user's language are the values, not the keys. The language is fixed on the session rather than taken from the profile every time: otherwise a person who switched the interface mid-interview would get a draft half in one language and half in another.

**How it works technically.** One JSON dictionary file per language (`az`, `en`, `ru`), with meaningful keys (`task.deadline_missed`) rather than phrases in one of the languages: otherwise editing the text in one language silently breaks the rest. A missing key falls back to Azerbaijani and writes a warning to the log rather than showing emptiness. A third language changes nothing architecturally — it is one more dictionary file; the cost is elsewhere: every new interface string now requires three translations, and drift accumulates unnoticed, so the dictionaries' completeness check goes in the tests rather than by eye.

**As a separate point:** the Azerbaijani strings written during development must be proofread by a native speaker before release. A machine-translated project tool's interface reads as an unfinished product — and that is especially noticeable in the default language, which most users will see.

## 10. Settings and configuration

Four levels. The rule is simple: the less often a value changes and the more dangerous it is to change, the lower its level. A value lives **on one level**; a lower level sets the default, an upper one overrides it through `null` inheritance.

### Level 1. Environment variables (whoever deploys)

Changed at deploy time, unavailable from the interface.

| Variable | What for |
|---|---|
| `DATABASE_URL` | the connection to Postgres |
| `APP_SECRET` | encrypting the LLM keys and signing the sessions; on rotation the keys need re-encrypting |
| `PUBLIC_BASE_URL` | what the public links to projects are assembled from |
| `SITE_DOMAIN`, `ACME_EMAIL` | the domain and the email for issuing TLS in Caddy |
| `DEFAULT_LOCALE`, `SUPPORTED_LOCALES` | the default language and the list of available ones |
| `MAIL_TRANSPORT` | `smtp` / `api` / `none` — how to send emails; `none` switches mail off entirely |
| `SMTP_URL` | the address, port and credentials of the SMTP server |
| `MAIL_API_KEY`, `MAIL_API_URL` | if the transport through a mailing service's API is chosen |
| `MAIL_FROM` | the sender's address, in whose name the invitations go out |
| `INVITE_TTL_DAYS`, `INVITE_RATE_LIMIT` | an invitation's lifetime and the ceiling of invitations per hour per organization |
| `PUBLIC_SHARING_ENABLED` | a global ban on public links for closed installs |
| `SIGNUP_MODE` | `open` (the default) / `invite_only` / `closed` — whether open registration is available |
| `AI_MAX_QUESTIONS`, `AI_SCHEMA_RETRIES`, `AI_REQUEST_TIMEOUT` | the interview's ceiling, the number of retries on a broken schema, the request timeout |
| `GUEST_COMMENT_RATE_LIMIT` | a rate limit on guest comments by IP |
| `MAX_TASKS_PER_PROJECT`, `MAX_TEXT_LEN` | safety catches against degenerate data |
| `LOG_LEVEL` | the logging level |

The domain and the default language are the most galling of the hard-wired values: without them an install cannot be deployed on a different domain and for a different country without touching the code.

**What "from the environment" means more precisely.** Mandatory and without a default value are only those variables for which no safe default exists: `APP_SECRET` and `DATABASE_URL`. Without them the application does not start — and that is right, because a secret invented for the deployer is worse than a missing one. The rest have a safe default value in the code, overridden by an environment variable. Demanding an explicit `DEFAULT_LOCALE` from everyone who deploys means breaking the "works right after deployment" promise for the sake of literalism.

The test by which a value falls into the first group: it cannot be guessed correctly. A secret, a database address, credentials. Everything with a sensible default — the language, the time zone, the thresholds, the limits — lives in the second.

### Level 2. The organization's settings (`owner`)

The defaults inherited by all the organization's projects: the language, the time zone, the working weekdays, the first day of the week, the public-holiday calendar, the shift threshold, whether public links are allowed and whether comments are enabled in them by default. Plus the LLM connection: the provider, the address, the model, the key.

The public-holiday calendar lives on the organization rather than on the project precisely because nobody will type the dates of Novruz into every new project by hand, while a forgotten holiday quietly shifts every date.

### Level 3. The project's settings (`owner`, `editor`)

The slug, the target date, the public link and its comments. Plus overrides of the organization's defaults where a project really does differ: the time zone, the working days, the shift threshold, the additional holidays and working Saturdays.

An override is stored as `null` = "inherit" rather than as a copy of the value.

### Level 4. The user's settings

The interface language. That is all.

### What stays in the code — and why

Not every constant deserves a setting. Here a setting would do harm:

- **The roles and the permission matrix.** Configurable permissions are a classic trap: flexibility used by one and a half clients, at the cost of the answer to "who sees this" ceasing to be unambiguous. Four roles plus a guest cover the job.
- **The criticality levels** (`low` / `normal` / `high` / `critical`). An enum in the code; their captions come from the language dictionaries anyway, and their colours from the theme.
- **The order of the AI's steps and the gates between them.** The ability to switch the gates off destroys the product's main principle.
- **The "the deviation is measured from the baseline plan" rule.** It must not be made configurable: the ability to measure from the previous value is a hole through which shifts accumulate unnoticed rather than an alternative working mode.
- **The category colour palette, the progress step, the chart's sizes and scale.** Styling and interface state; the scale lives in the user's browser rather than on the server.
- **The list of mandatory interview topics** lies in a configuration file next to the application (`backend/app/ai/intake_topics.yml`) rather than in the database: it will be edited by whoever deploys rather than by a user. Moving it into the organization's settings is for when there is demand for industry intake templates.

## 11. Architecture and deployment

One `docker compose`: FastAPI under uvicorn, Postgres, Caddy as the TLS terminator and the server of React's built static files. Migrations are Alembic. There are no external services: no queues, no object storage, no Redis.

The backend's modules, each tested separately:

- `calendar/` — working days, holidays, date arithmetic. Pure functions.
- `mutations/` — the operation registry, applying and building the inverse. Knows nothing about HTTP.
- `access/` — the permission matrix: subject, role, action, object. The only place where "is this allowed" is decided.
- `api/` — HTTP and WebSocket, serialization, request authorization. No business logic.
- `ai/` — the LLM provider, the interview scenario, the output schemas. Writes into `AiSession`, not into a project.
- `mail/` — sending emails behind a `send(to, template, params)` interface. The implementations: SMTP, a mailing service's API and a stub that writes the email into the log.

**Mail follows the same scheme as the LLM: a thin interface with several implementations.** The main option is SMTP: it works with any provider, including your own server, and does not tie you to a paid service. An implementation through a mailing service's API is added for whoever cares about deliverability and statistics, but nobody is forced into that dependency in advance.

The emails go out **synchronously, in the same request**, because there is no queue in the first version and an invitation is the only email in the whole application. If sending failed, the invitation is created anyway and the link is available for copying: the interface honestly says "the email did not go out, copy the link" instead of rolling the whole action back. With `MAIL_TRANSPORT=none` the send button is not shown at all, leaving only copying the link — an install without a mail server must stay fully usable.

The WebSocket broadcast is held in the process's memory. While there is one server that is enough; Redis is added by replacing one class, should horizontal scaling be needed.

The frontend is React and thin: it draws the state, sends mutations and applies incoming revisions. There are no scheduling computations on the frontend.

**The screen's layout** (the same for members and for the public link): a narrow left column with the tasks' names and the owners' avatars, and on the right a timeline across all the remaining width. The categories are heading rows with a coloured dot and a band spanning their tasks' dates.

**The time scale is by days rather than by weeks or months.** The header has two levels: the months on top, and below every day with its date and weekday. The non-working days — the project's weekends and holidays — are filled with a background across the chart's full height, so that the gaps in the plan are visible at once and nobody puts a task on a Sunday by inattention. The strip scrolls horizontally while the left column with the names stays put; on opening, the project scrolls to today, which is marked with a vertical line.

The task detail panel is **hidden by default**: the chart takes the whole width of the screen. The panel opens on a click on a task and closes with the cross, the Esc key or a repeat click on the same task. A permanently hanging panel takes a quarter of the width from the very thing the person came for — the chart itself.

**The order of tasks is changed by dragging a row by the left column.** The separation is strict: dragging a bar horizontally on the timeline changes the dates, dragging a row vertically in the left column changes the order. One and the same task must not answer for two things at once, otherwise a person will accidentally move dates while trying to reorder a row. An insertion line is shown during a drag; a row can be dropped on a category heading — then the task moves into it and goes last. A change of category is written into the task's history, a reorder within a category is not: that is layout rather than a change to the plan.

**Creating and editing by hand.** A category is created with a button in the header (a name and a colour), a task with a button in the header or a plus on the required category's row, in which case the category is filled in itself. The task form asks for a name, a description, a category, a criticality, a start date, a duration and the owners. Editing an existing task happens in its card: every field is edited in place, without a separate edit mode and a "Save" button. Changing the start date or the duration goes through the same threshold check as dragging with the mouse — the rule is one regardless of the input method. Deleting a task gives an undo strip.

## 12. Error handling

- **A dropped WebSocket** — a "no connection, showing data as of 14:32" strip, editing is blocked. On recovery the project's state is refetched whole rather than replayed from the missed revisions.
- **A rejected mutation** — the optimistic change is rolled back and a concrete reason is shown: "Maria deleted this task a minute ago" rather than "save error".
- **An LLM failure** (a timeout, junk instead of a schema, the key's limit exhausted) — the conversation and the draft are preserved, the session continues from the same place.
- **A taken slug** — a free variant is offered in the input field before the form is submitted.
- **The email did not go out** — the invitation is created, the link is shown for copying, the error is named outright. The action is not rolled back: an invitation exists regardless of whether it was delivered by email.
- **An invitation is expired, revoked or already accepted** — three different messages rather than a single "the link is invalid". A person must understand whether to ask for a new link or whether they are already in the system and simply need to sign in.

## 13. Testing

In descending order of importance:

1. **The mutations** — an apply test and an inverse test for every operation. The foundation of the history, the undo and applying the AI.
2. **The shift threshold** — including accumulation: three one-day shifts from the baseline plan demand a reason on the third.
3. **The permissions** — a matrix of five subjects (owner, editor, viewer, client, guest) against a list of actions, a test per cell. Separately: the internal note does not leak to a client or a guest, neither through the page nor through the API.
4. **The calendar** — crossings of weekends and holidays, a duration of one day, a duration starting on a Friday, a non-standard working week, a working Saturday from `workdays_extra`.
5. **Registration** — a new account gets its own organization and the `owner` role in it; with `SIGNUP_MODE=invite_only` and `closed` registration is rejected; without mail configured the account is usable at once, with mail configured you cannot invite others until the address is confirmed.
6. **Invitations** — an accepted one does not fire again; expired and revoked ones are rejected; the role from the link cannot be substituted on acceptance; an invitation with an address is not accepted under a different account; re-sending kills the previous token; the count limit fires. Separately: the token lies in the database as a hash rather than as plain text.
7. **Settings inheritance** — changing the organization's default changes the behaviour of the projects where `null` stands and does not touch those where the value is explicitly overridden.
8. **The deadline** — a task ending exactly on the deadline's day does not count as overdue; the project's end is taken as the maximum over all the tasks, including those created beyond the plan.
9. **Languages** — the dictionaries' completeness: `en` and `ru` have no keys absent from `az`, and vice versa; the test fails on drift. Separately, the Azerbaijani i trap: `"I"` and `"İ"` in slugs, emails and search are case-converted identically regardless of the process's locale.
10. **The AI** — against recorded model answers, with no network: a valid schema is applied, a broken one is rejected and does not bring the session down.
11. **An end-to-end scenario** — create a project through the interview, approve the plan, move a task with a reason, open the public link in another browser, make sure the chart and the comments are visible but the internal notes are not.

## 14. Out of the first version

Recorded explicitly so as not to come back to the arguments: an AI session inside an existing project, auto-shifting along links and link types, the critical path and float, resources and people's workload, billing, OG previews for the links, export to MS Project and Excel, a mobile application, collaborative editing with cursors.

> **Superseded / incomplete list.** Auto-shift by dependencies and
> critical-path/float were built after all (see the §4 note above). This
> list also predates two entire subsystems that shipped later and aren't
> mentioned anywhere in this spec: the Proposal/quoting module
> (`backend/app/proposals.py`) and the weekly Scorecard health dashboard
> (`backend/app/scorecard.py`) — plus the Jira import/sync integration
> (`backend/app/jira/`), which is a new external-service connection this
> spec doesn't anticipate at all. Resource/workload balancing, billing,
> OG previews, MS Project/Excel export, a mobile app, and collaborative
> cursors are still out of scope as of this writing.

About mail separately, so as not to confuse things: the invitation email **does** exist in the first version — it is the only transactional email in the whole application. Notification emails (a task was assigned, a date slipped, a comment arrived) do **not**: inside the application there is a badge, and that is all. As soon as notifications are needed, a queue, retries and unsubscribing will have to be added to them — that is separate work rather than "one more `send` call".

Also out of the first version: a reusable invitation link for a whole team. One-time invitations cover the job and do not spread uncontrollably; a shared link will be needed when people start being invited by the dozen, and then it needs limiters of its own — a usage ceiling and a lifetime.
