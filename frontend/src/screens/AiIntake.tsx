import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  AI_CREDENTIAL_QUERY_KEY,
  AI_LIMITS_QUERY_KEY,
  answerQuestion,
  applySession,
  buildDraft,
  buildSummary,
  editDraft,
  editSummary,
  extractSpec,
  readCredential,
  readLimits,
  startSession,
} from "../api/ai";
import type { AiSession, Draft, DraftCoverage } from "../api/ai";
import { errorKey } from "../api/errors";
import { useLocale } from "../i18n/LocaleProvider";

/**
 * Creating a project through an interview.
 *
 * Four steps and two gates between them: the summary — where it is cheapest to catch a
 * misunderstanding — and the draft — where nothing has been written into the project yet. They
 * cannot be skipped: the server will refuse, and the interface does not offer it either.
 */
export function AiIntake() {
  const { t, locale } = useLocale();
  const navigate = useNavigate();
  const [session, setSession] = useState<AiSession | null>(null);
  const [answer, setAnswer] = useState("");

  const credential = useQuery({
    queryKey: AI_CREDENTIAL_QUERY_KEY,
    queryFn: readCredential,
    retry: false,
  });

  // The ceilings of a specification. Without them the screen still works: the server refuses
  // what is over, the screen just cannot say so before sending.
  const limits = useQuery({ queryKey: AI_LIMITS_QUERY_KEY, queryFn: readLimits, retry: false });

  const start = useMutation({
    mutationFn: (spec: string) => startSession(locale, spec),
    onSuccess: setSession,
  });
  const [spec, setSpec] = useState("");
  const readFile = useMutation({
    mutationFn: (file: File) => extractSpec(file, limits.data?.spec_max_bytes),
    onSuccess: (result) => setSpec(result.text),
  });
  const reply = useMutation({
    mutationFn: (text: string) => answerQuestion(session!.id, text),
    onSuccess: (next) => {
      setSession(next);
      setAnswer("");
    },
  });
  const summarize = useMutation({
    mutationFn: () => buildSummary(session!.id),
    onSuccess: setSession,
  });
  const saveTheses = useMutation({
    mutationFn: (theses: string[]) => editSummary(session!.id, theses),
    onSuccess: setSession,
  });
  const draft = useMutation({ mutationFn: () => buildDraft(session!.id), onSuccess: setSession });
  const saveDraft = useMutation({
    mutationFn: (next: Draft) => editDraft(session!.id, next),
    onSuccess: setSession,
  });
  const apply = useMutation({
    mutationFn: (name: string) => applySession(session!.id, name),
    onSuccess: (result) => navigate(`/projects/${result.project_id}`),
  });

  const failure =
    readFile.error ??
    start.error ??
    reply.error ??
    summarize.error ??
    saveTheses.error ??
    draft.error ??
    apply.error ??
    saveDraft.error;

  // No key — the AI buttons are disabled with a link to the settings. Not hidden: a hidden button
  // does not explain why the interview is unavailable.
  if (credential.data && !credential.data.configured) {
    return (
      <main className="screen">
        <div className="screen__head">
          <h1>{t("ai.title")}</h1>
        </div>
        <p>{t("ai.not_configured")}</p>
        <Link to="/settings/organization">{t("nav.org_settings")}</Link>
      </main>
    );
  }

  const question = session?.transcript.at(-1);
  const waiting = session?.status === "interview" && question?.answer === null;

  return (
    <main className="screen">
      <div className="screen__head">
        <h1>{t("ai.title")}</h1>
      </div>

      {failure != null && (
        <p className="error" role="alert">
          {t(errorKey(failure))}
        </p>
      )}

      {session === null && (
        <>
          <p className="muted">{t("ai.intro")}</p>
          <button type="button" onClick={() => start.mutate("")} disabled={start.isPending}>
            {t("ai.start")}
          </button>
          <SpecStart
            text={spec}
            maxChars={limits.data?.spec_max_chars}
            reading={readFile.isPending}
            pending={start.isPending}
            onText={setSpec}
            onFile={(file) => readFile.mutate(file)}
            onStart={() => start.mutate(spec.trim())}
          />
        </>
      )}

      {session !== null && session.status === "interview" && (
        <section className="settings">
          {/* The questions come one at a time: a list of twelve fields turns an interview into a
              questionnaire that gets filled in without reading. */}
          {session.spec_chars > 0 && <p className="muted">{t("ai.spec.interview_hint")}</p>}
          <p className="ai__question">{question?.question}</p>
          <p className="field">
            <label htmlFor="ai-answer">{t("ai.answer")}</label>
            <textarea
              id="ai-answer"
              name="ai-answer"
              rows={3}
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
            />
          </p>
          <div className="modal__actions">
            <button
              type="button"
              disabled={answer.trim() === "" || reply.isPending || !waiting}
              onClick={() => reply.mutate(answer.trim())}
            >
              {t("ai.reply")}
            </button>
            {/* The "enough, generate it" button is available at any step: without it a person for
                whom everything is clear has to sit it out to the ceiling. */}
            <button
              type="button"
              className="button--quiet"
              onClick={() => summarize.mutate()}
              disabled={summarize.isPending}
            >
              {t("ai.enough")}
            </button>
          </div>
          <p className="muted">
            {t("ai.progress", {
              asked: session.transcript.length,
              tokens: session.tokens_used,
            })}
          </p>
        </section>
      )}

      {session !== null && session.status === "summary" && (
        <SummaryGate
          theses={session.summary}
          pending={saveTheses.isPending || draft.isPending}
          onSave={(theses) => saveTheses.mutate(theses)}
          onNext={() => draft.mutate()}
        />
      )}

      {session !== null && session.status === "draft" && (
        <DraftGate
          draft={session.draft as Draft}
          theses={session.summary}
          coverage={session.coverage}
          pending={apply.isPending || saveDraft.isPending}
          onSave={(next) => saveDraft.mutate(next)}
          onApply={(name) => apply.mutate(name)}
        />
      )}
    </main>
  );
}

/**
 * Starting from a specification instead of from an empty interview.
 *
 * The file is not sent to the model: it is turned into text, and the text lands in a field. What
 * the person sees in the field is exactly what the model will read — a scanned PDF gives nothing,
 * a table gives a column of cells, and both have to be visible before the plan is built on them.
 * The same field takes pasted text, so a format the server cannot read is still one paste away.
 */
function SpecStart({
  text,
  maxChars,
  reading,
  pending,
  onText,
  onFile,
  onStart,
}: {
  text: string;
  maxChars: number | undefined;
  reading: boolean;
  pending: boolean;
  onText: (text: string) => void;
  onFile: (file: File) => void;
  onStart: () => void;
}) {
  const { t } = useLocale();
  const picker = useRef<HTMLInputElement>(null);
  const chars = text.trim().length;
  const tooLong = maxChars !== undefined && chars > maxChars;

  return (
    <section className="settings ai__spec" aria-labelledby="ai-spec-title">
      <h2 id="ai-spec-title">{t("ai.spec.title")}</h2>
      <p className="muted">{t("ai.spec.hint")}</p>
      <p className="field">
        <label htmlFor="ai-spec-file">{t("ai.spec.file")}</label>
        <input
          ref={picker}
          id="ai-spec-file"
          name="ai-spec-file"
          type="file"
          accept=".docx,.pdf,.txt,.md"
          disabled={reading}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onFile(file);
            // Cleared so that picking the same file again — after fixing it — fires a change.
            if (picker.current) picker.current.value = "";
          }}
        />
      </p>
      <p className="field">
        <label htmlFor="ai-spec-text">{t("ai.spec.text")}</label>
        <textarea
          id="ai-spec-text"
          name="ai-spec-text"
          rows={12}
          value={text}
          onChange={(event) => onText(event.target.value)}
        />
      </p>
      {reading && (
        <p className="muted" role="status">
          {t("ai.spec.reading")}
        </p>
      )}
      {chars > 0 && maxChars !== undefined && (
        <p className={tooLong ? "error" : "muted"}>
          {t(tooLong ? "ai.spec.too_long" : "ai.spec.count", { chars, limit: maxChars })}
        </p>
      )}
      <button type="button" onClick={onStart} disabled={chars === 0 || tooLong || pending || reading}>
        {t("ai.spec.start")}
      </button>
    </section>
  );
}

/** Gate 1: the theses are edited and deleted — that is what a gate is for. */
function SummaryGate({
  theses,
  pending,
  onSave,
  onNext,
}: {
  theses: string[];
  pending: boolean;
  onSave: (theses: string[]) => void;
  onNext: () => void;
}) {
  const { t } = useLocale();
  const [text, setText] = useState(theses.join("\n"));

  return (
    <section className="settings">
      <h2>{t("ai.summary.title")}</h2>
      <p className="muted">{t("ai.summary.hint")}</p>
      <p className="field">
        <label htmlFor="ai-theses">{t("ai.summary.theses")}</label>
        <textarea
          id="ai-theses"
          name="ai-theses"
          rows={8}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => {
            const next = text.split("\n").filter((line) => line.trim() !== "");
            if (next.join("\n") !== theses.join("\n")) onSave(next);
          }}
        />
      </p>
      <button type="button" onClick={onNext} disabled={pending}>
        {t("ai.summary.next")}
      </button>
    </section>
  );
}

/** Gate 2: the draft is edited whole, and nothing has been written into the project. */
function DraftGate({
  draft,
  theses,
  coverage,
  pending,
  onSave,
  onApply,
}: {
  draft: Draft;
  theses: string[];
  coverage: DraftCoverage | null;
  pending: boolean;
  onSave: (draft: Draft) => void;
  onApply: (name: string) => void;
}) {
  const { t } = useLocale();
  const [name, setName] = useState("");
  // A working copy of the draft. The edits go through it rather than through the prop: the prop is
  // the server's answer to the previous edit, and it arrives after the next one. Two edits in a row
  // — a date on one task, a name on another — each built from the prop, would erase one another: the
  // second would leave without the first, and the last answer would put the screen into a state
  // without it.
  const [work, setWork] = useState(draft);
  useEffect(() => setWork(draft), [draft]);

  const edit = (next: Draft) => {
    setWork(next);
    onSave(next);
  };

  const patchTask = (
    categoryIndex: number,
    taskIndex: number,
    patch: Partial<Draft["categories"][number]["tasks"][number]>,
  ) =>
    edit({
      categories: work.categories.map((category, index) =>
        index !== categoryIndex
          ? category
          : {
              ...category,
              tasks: category.tasks.map((task, position) =>
                position === taskIndex ? { ...task, ...patch } : task,
              ),
            },
      ),
    });

  const dropTask = (categoryIndex: number, taskIndex: number) =>
    edit({
      categories: work.categories.map((category, index) =>
        index !== categoryIndex
          ? category
          : { ...category, tasks: category.tasks.filter((_, position) => position !== taskIndex) },
      ),
    });

  return (
    <section className="settings">
      <h2>{t("ai.draft.title")}</h2>
      <p className="muted">{t("ai.draft.hint")}</p>

      {coverage !== null && <DraftTrace theses={theses} coverage={coverage} draft={work} />}

      {/* The key is a number rather than a name: the names are given to the categories by the model,
          and nothing stops two of them from coinciding; the edits, meanwhile, are addressed by number. */}
      {work.categories?.map((category, categoryIndex) => (
        <fieldset key={categoryIndex} className="settings__fieldset">
          {/* The category's name is content: it came from the model in the session's language and is
              not translated. */}
          <legend>{category.name}</legend>
          <ul className="members__list">
            {category.tasks.map((task, taskIndex) => (
              <li key={`${task.name}-${taskIndex}`} className="members__row">
                <input
                  aria-label={t("ai.draft.task_name", { name: task.name })}
                  defaultValue={task.name}
                  onBlur={(event) =>
                    event.target.value.trim() !== task.name &&
                    patchTask(categoryIndex, taskIndex, { name: event.target.value.trim() })
                  }
                />
                <input
                  type="date"
                  aria-label={t("ai.draft.task_start", { name: task.name })}
                  defaultValue={task.start_date}
                  onChange={(event) =>
                    patchTask(categoryIndex, taskIndex, { start_date: event.target.value })
                  }
                />
                <input
                  type="number"
                  min={1}
                  aria-label={t("ai.draft.task_duration", { name: task.name })}
                  defaultValue={task.duration_days}
                  onBlur={(event) =>
                    patchTask(categoryIndex, taskIndex, {
                      duration_days: Number(event.target.value),
                    })
                  }
                />
                <button
                  type="button"
                  className="button--quiet"
                  onClick={() => dropTask(categoryIndex, taskIndex)}
                >
                  {t("ai.draft.drop")}
                </button>
                <TaskOrigin sources={task.sources ?? []} theses={theses} />
              </li>
            ))}
          </ul>
        </fieldset>
      ))}

      <p className="field">
        <label htmlFor="ai-project-name">{t("ai.draft.project_name")}</label>
        <input
          id="ai-project-name"
          name="ai-project-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </p>

      <button
        type="button"
        onClick={() => onApply(name.trim())}
        disabled={pending || name.trim() === ""}
      >
        {t("ai.draft.apply")}
      </button>
    </section>
  );
}

/**
 * Where a task came from: the numbers of the summary points behind it.
 *
 * Read from the task itself rather than from the server's list of unsourced tasks: that list
 * addresses tasks by position, and between a deletion and the server's answer to it the positions
 * on screen and in the list differ by one — the warning would sit on the neighbouring row.
 */
function TaskOrigin({ sources, theses }: { sources: number[]; theses: string[] }) {
  const { t } = useLocale();

  if (sources.length === 0) {
    return <span className="ai__origin ai__origin--missing">{t("ai.draft.task_unsourced")}</span>;
  }
  return (
    // The full text of the points is in the tooltip: a number alone sends the eyes up the page.
    <span
      className="ai__origin"
      title={sources.map((number) => `${number}. ${theses[number - 1] ?? ""}`).join("\n")}
    >
      {t("ai.draft.task_sources", { numbers: sources.join(", ") })}
    </span>
  );
}

/**
 * The draft checked against the summary: which points got a task, which were set aside as context,
 * and which the plan lost.
 *
 * It stands above the tasks because it answers the first question a person has about a generated
 * plan — "is everything I said in here?" — and that question is not answerable by reading forty rows.
 */
function DraftTrace({
  theses,
  coverage,
  draft,
}: {
  theses: string[];
  coverage: DraftCoverage;
  draft: Draft;
}) {
  const { t } = useLocale();
  const unsourced = (draft.categories ?? []).reduce(
    (count, category) =>
      count + category.tasks.filter((task) => (task.sources ?? []).length === 0).length,
    0,
  );
  const clean = coverage.uncovered.length === 0 && unsourced === 0;

  const state = (number: number) => {
    if (coverage.uncovered.includes(number)) return "uncovered";
    if (coverage.context.includes(number)) return "context";
    return "covered";
  };

  return (
    <section className="ai__trace" aria-labelledby="ai-trace-title">
      <h3 id="ai-trace-title">{t("ai.draft.trace_title")}</h3>
      {/* A status rather than an alert: gaps do not block applying, a person may know better than
          the check — but they must not be able to miss them. */}
      <p className={clean ? "muted" : "ai__trace-gaps"} role="status">
        {clean
          ? t("ai.draft.trace_clean")
          : t("ai.draft.trace_gaps", { uncovered: coverage.uncovered.length, unsourced })}
      </p>
      <ol className="ai__theses">
        {theses.map((thesis, index) => {
          const kind = state(index + 1);
          return (
            <li key={index} className={`ai__thesis ai__thesis--${kind}`}>
              {/* The thesis is content in the session's language and is not translated. */}
              <span>{thesis}</span>
              <span className={`ai__mark ai__mark--${kind}`}>{t(`ai.draft.thesis_${kind}`)}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
