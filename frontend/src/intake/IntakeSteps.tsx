import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  answerQuestion,
  applySession,
  buildDraft,
  buildSummary,
  editDraft,
  editSummary,
} from "../api/ai";
import type { AiSession, Draft, Requirement } from "../api/ai";
import { errorKey } from "../api/errors";
import { useLocale } from "../i18n/LocaleProvider";

/** How a requirement is referred to: by its clause, or by its number where the document has none. */
export function clauseLabel(requirement: Requirement): string {
  return requirement.section !== "" ? requirement.section : `№${requirement.id}`;
}

/**
 * The steps both intakes share: the questions, the summary, the draft.
 *
 * The plain interview starts here; the intake from a document arrives here after its own two
 * steps — the reading and the requirements. From this point the two are one scenario, and one
 * component is what keeps them from drifting apart: the gates are the product's main promise, and
 * a second copy of them would be a second place where it can be broken.
 */
export function IntakeSteps({
  session,
  onSession,
}: {
  session: AiSession;
  onSession: (next: AiSession) => void;
}) {
  const { t } = useLocale();
  const navigate = useNavigate();
  const [answer, setAnswer] = useState("");

  const reply = useMutation({
    mutationFn: (text: string) => answerQuestion(session.id, text),
    onSuccess: (next) => {
      onSession(next);
      setAnswer("");
    },
  });
  const summarize = useMutation({
    mutationFn: () => buildSummary(session.id),
    onSuccess: onSession,
  });
  const saveTheses = useMutation({
    mutationFn: (theses: string[]) => editSummary(session.id, theses),
    onSuccess: onSession,
  });
  const draft = useMutation({ mutationFn: () => buildDraft(session.id), onSuccess: onSession });
  const saveDraft = useMutation({
    mutationFn: (next: Draft) => editDraft(session.id, next),
    onSuccess: onSession,
  });
  const apply = useMutation({
    mutationFn: (name: string) => applySession(session.id, name),
    onSuccess: (result) => navigate(`/projects/${result.project_id}`),
  });

  const failure =
    reply.error ?? summarize.error ?? saveTheses.error ?? draft.error ?? apply.error ?? saveDraft.error;

  const question = session.transcript.at(-1);
  const waiting = session.status === "interview" && question?.answer === null;

  return (
    <>
      {failure != null && (
        <p className="error" role="alert">
          {t(errorKey(failure))}
        </p>
      )}

      {session.status === "interview" && (
        <section className="settings">
          {/* The questions come one at a time: a list of twelve fields turns an interview into a
              questionnaire that gets filled in without reading. */}
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

      {session.status === "summary" && (
        <SummaryGate
          theses={session.summary}
          pending={saveTheses.isPending || draft.isPending}
          onSave={(theses) => saveTheses.mutate(theses)}
          onNext={() => draft.mutate()}
        />
      )}

      {session.status === "draft" && (
        <DraftGate
          draft={session.draft as Draft}
          requirements={session.requirements ?? []}
          uncovered={session.uncovered_requirements ?? []}
          pending={apply.isPending || saveDraft.isPending}
          onSave={(next) => saveDraft.mutate(next)}
          onApply={(name) => apply.mutate(name)}
        />
      )}
    </>
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
  requirements,
  uncovered,
  pending,
  onSave,
  onApply,
}: {
  draft: Draft;
  /** The document's requirements — empty for a plain interview. */
  requirements: Requirement[];
  /** The requirements no task refers to. Counted by the server after every edit. */
  uncovered: Requirement[];
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

  const clauseOf = new Map(requirements.map((item) => [item.id, clauseLabel(item)]));
  /** "3.1, 3.2" — where in the document a task comes from; empty for work the document does not name. */
  const clauses = (ids: number[] | undefined) =>
    [...new Set((ids ?? []).flatMap((id) => clauseOf.get(id) ?? []))].join(", ");

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
                {/* The link back to the document: without it a plan of fifty tasks cannot be checked
                    against the requirements other than by rereading both. */}
                {clauses(task.requirement_ids) !== "" && (
                  <span className="muted ai__source">
                    {t("ai.draft.source", { clauses: clauses(task.requirement_ids) })}
                  </span>
                )}
                <button
                  type="button"
                  className="button--quiet"
                  onClick={() => dropTask(categoryIndex, taskIndex)}
                >
                  {t("ai.draft.drop")}
                </button>
              </li>
            ))}
          </ul>
        </fieldset>
      ))}

      {/* An empty block is not shown: "nothing was left out" needs no heading of its own. */}
      {uncovered.length > 0 && (
        <section className="ai__uncovered" aria-labelledby="ai-uncovered-title">
          <h3 id="ai-uncovered-title">{t("ai.draft.uncovered.title")}</h3>
          <p className="muted">{t("ai.draft.uncovered.hint")}</p>
          <ul>
            {uncovered.map((item) => (
              <li key={item.id}>
                <span className="muted">{clauseLabel(item)}</span> {item.text}
              </li>
            ))}
          </ul>
        </section>
      )}

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
