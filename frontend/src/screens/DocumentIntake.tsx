import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  AI_CREDENTIAL_QUERY_KEY,
  MAX_DOCUMENT_BYTES,
  buildSummary,
  confirmRequirements,
  nextQuestion,
  readCredential,
  readDocumentPart,
  readSession,
  saveRequirements,
  uploadDocument,
} from "../api/ai";
import type { AiSession, ProjectType } from "../api/ai";
import { ApiError } from "../api/client";
import { errorKey } from "../api/errors";
import { useLocale } from "../i18n/LocaleProvider";
import { IntakeSteps } from "../intake/IntakeSteps";
import { ProjectTypeField } from "../intake/ProjectTypeField";

/** The session's number in the address: a closed tab comes back to the same step by it. */
const SESSION_PARAM = "session";

/**
 * Creating a project from a requirements document.
 *
 * Two steps of its own — the reading and the requirements — and then the same questions, summary
 * and draft as the interview (`IntakeSteps`). The session's number lives in the address, so a tab
 * closed at any step is continued from that step: reading a hundred pages is not something to
 * start over because of a stray click.
 */
export function DocumentIntake() {
  const { t, locale } = useLocale();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const sessionId = params.get(SESSION_PARAM);
  const [session, setSession] = useState<AiSession | null>(null);

  const credential = useQuery({
    queryKey: AI_CREDENTIAL_QUERY_KEY,
    queryFn: readCredential,
    retry: false,
  });

  // Coming back by the address: the session is asked for once. After that the screen holds it
  // itself — every step's answer is the session's new state.
  const resumed = useQuery({
    queryKey: ["ai", "session", sessionId],
    queryFn: () => readSession(sessionId!),
    enabled: sessionId !== null && session === null,
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
  });
  useEffect(() => {
    if (resumed.data && session === null) setSession(resumed.data);
  }, [resumed.data, session]);

  // An applied session has nothing left to show here: its result is the project.
  useEffect(() => {
    if (session?.status === "applied" && session.project_id !== null) {
      navigate(`/projects/${session.project_id}`, { replace: true });
    }
  }, [session, navigate]);

  const upload = useMutation({
    mutationFn: (file: File) => {
      // The size is known before a single byte is sent — and four megabytes over a slow line is
      // a long wait for a refusal.
      if (file.size > MAX_DOCUMENT_BYTES) {
        return Promise.reject(new ApiError("document_too_large", 413));
      }
      return uploadDocument(file, locale);
    },
    onSuccess: (next) => {
      setSession(next);
      // replace: "back" from the reading must lead out of the scenario, not to an empty upload
      // form over a session that is already running.
      setParams({ [SESSION_PARAM]: next.id }, { replace: true });
    },
  });

  if (credential.data && !credential.data.configured) {
    return (
      <main className="screen">
        <div className="screen__head">
          <h1>{t("ai.document.title")}</h1>
        </div>
        <p>{t("ai.not_configured")}</p>
        <Link to="/settings/organization">{t("nav.org_settings")}</Link>
      </main>
    );
  }

  const loadingSession = sessionId !== null && session === null && resumed.error === null;

  return (
    <main className="screen">
      <div className="screen__head">
        <h1>{t("ai.document.title")}</h1>
      </div>

      {resumed.error != null && session === null && (
        <p className="error" role="alert">
          {t(errorKey(resumed.error))}
        </p>
      )}

      {loadingSession && <p className="muted">{t("common.loading")}</p>}

      {session === null && !loadingSession && (
        <UploadStep
          pending={upload.isPending}
          error={upload.error}
          onUpload={(file) => upload.mutate(file)}
        />
      )}

      {session !== null && session.status === "reading" && (
        <ReadingStep session={session} onSession={setSession} />
      )}

      {session !== null && session.status === "requirements" && (
        <RequirementsStep session={session} onSession={setSession} />
      )}

      {session !== null && session.status === "interview" && !hasQuestion(session) && (
        <QuestionsBridge session={session} onSession={setSession} />
      )}

      {session !== null &&
        ((session.status === "interview" && hasQuestion(session)) ||
          session.status === "summary" ||
          session.status === "draft") && <IntakeSteps session={session} onSession={setSession} />}
    </main>
  );
}

/** Whether the interview has got as far as a question — asked or already answered. */
function hasQuestion(session: AiSession): boolean {
  return session.transcript.length > 0;
}

/** Step 1: the file. What will happen to it is said here, before it is chosen. */
function UploadStep({
  pending,
  error,
  onUpload,
}: {
  pending: boolean;
  error: unknown;
  onUpload: (file: File) => void;
}) {
  const { t } = useLocale();
  const [file, setFile] = useState<File | null>(null);

  return (
    <section className="settings">
      <p className="muted">{t("ai.document.intro")}</p>
      <p className="field">
        <label htmlFor="ai-document">{t("ai.document.file")}</label>
        <input
          id="ai-document"
          name="ai-document"
          type="file"
          accept=".pdf,.docx,.txt,.md,application/pdf,text/plain,text/markdown"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
      </p>
      <p className="muted">{t("ai.document.formats")}</p>
      {/* Said in words and before the upload: the text leaves for a third party, and a person
          deciding whether to upload a contract has to know that. */}
      <p className="muted">{t("ai.document.privacy")}</p>

      {error != null && (
        <p className="error" role="alert">
          {t(errorKey(error))}
        </p>
      )}

      <button type="button" disabled={file === null || pending} onClick={() => onUpload(file!)}>
        {pending ? t("ai.document.uploading") : t("ai.document.upload")}
      </button>
    </section>
  );
}

/**
 * Step 2: the reading, part by part.
 *
 * Each part is a request of its own (one request — one model call), and the screen asks for the
 * next one as soon as the previous has answered. A refusal stops the loop instead of repeating
 * itself: the per-minute limit will not pass sooner because it is asked again, and a person gets
 * the reason and a button.
 */
function ReadingStep({
  session,
  onSession,
}: {
  session: AiSession;
  onSession: (next: AiSession) => void;
}) {
  const { t } = useLocale();
  // Which part has already been asked for. A ref rather than state: the effect below must not ask
  // for the same part twice — a repeated effect (React's strict mode runs each one twice) would
  // otherwise spend a second model call on it.
  const requested = useRef<string | null>(null);

  const read = useMutation({
    mutationFn: () => readDocumentPart(session.id),
    onSuccess: onSession,
  });
  const { mutate, isError } = read;

  const cursor = session.document?.parts_read ?? 0;
  useEffect(() => {
    const step = `${session.id}:${cursor}`;
    if (isError || requested.current === step) return;
    requested.current = step;
    mutate();
  }, [session.id, cursor, isError, mutate]);

  return (
    <section className="settings">
      {/* role="status": the line changes by itself, and a screen reader has to say so. */}
      <p className="ai__question" role="status">
        {t("ai.document.reading", {
          read: Math.min(cursor + 1, session.document?.parts_total ?? 1),
          total: session.document?.parts_total ?? 1,
        })}
      </p>
      <p className="muted">{t("ai.document.reading_hint")}</p>

      {read.error != null && (
        <>
          <p className="error" role="alert">
            {t(errorKey(read.error))}
          </p>
          <button
            type="button"
            onClick={() => {
              requested.current = null;
              read.reset();
            }}
          >
            {t("ai.document.resume")}
          </button>
        </>
      )}
    </section>
  );
}

type Row = { key: number; section: string; text: string };

/**
 * Step 3: "what we understood from the document" — the type and the requirements on one screen,
 * confirmed by one button.
 *
 * Both are edited here because both are cheapest to fix here: a wrong type asks the wrong
 * questions, and a missed requirement is a task that will not be in the plan.
 */
function RequirementsStep({
  session,
  onSession,
}: {
  session: AiSession;
  onSession: (next: AiSession) => void;
}) {
  const { t } = useLocale();
  // A working copy, for the same reason as in the draft: the server's answer to one edit arrives
  // after the next edit has been made. The rows carry a key of their own rather than the
  // requirement's number — the server renumbers the list on every save, and a key that changed
  // would throw the focus out of the field being typed in.
  const nextKey = useRef(0);
  const [rows, setRows] = useState<Row[]>(() =>
    session.requirements.map((item) => ({
      key: nextKey.current++,
      section: item.section,
      text: item.text,
    })),
  );
  const [projectType, setProjectType] = useState<ProjectType>(session.project_type);

  const payload = (list: Row[]) =>
    list
      .map((row) => ({ section: row.section.trim(), text: row.text.trim() }))
      // A row with no text is a row being typed or one to be dropped — not a requirement yet.
      .filter((row) => row.text !== "");

  // Saved in the background on every edit: closing the tab must not lose the corrections. The
  // answer is deliberately not put back onto the screen — the working copy is ahead of it.
  const save = useMutation({
    mutationFn: (next: { type: ProjectType; rows: Row[] }) =>
      saveRequirements(session.id, next.type, payload(next.rows)),
  });
  const confirm = useMutation({
    mutationFn: async () => {
      await saveRequirements(session.id, projectType, payload(rows));
      return confirmRequirements(session.id);
    },
    onSuccess: onSession,
  });

  const change = (next: Row[], type: ProjectType = projectType) => {
    setRows(next);
    setProjectType(type);
    save.mutate({ type, rows: next });
  };
  const patch = (key: number, part: Partial<Row>) =>
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...part } : row)));

  const failure = confirm.error ?? save.error;

  return (
    <section className="settings">
      <h2>{t("ai.requirements.title")}</h2>
      {/* The description is content: it came from the model in the session's language. */}
      {session.brief !== "" && <p>{session.brief}</p>}

      <ProjectTypeField
        id="ai-project-type"
        value={projectType}
        disabled={confirm.isPending}
        onChange={(type) => change(rows, type)}
      />

      <h3 id="ai-requirements-list">{t("ai.requirements.list")}</h3>
      <p className="muted">{t("ai.requirements.hint")}</p>
      <ol className="ai__requirements" aria-labelledby="ai-requirements-list">
        {rows.map((row, index) => (
          <li key={row.key} className="ai__requirement">
            <input
              className="ai__requirement-section"
              aria-label={t("ai.requirements.section", { number: index + 1 })}
              value={row.section}
              onChange={(event) => patch(row.key, { section: event.target.value })}
              onBlur={() => save.mutate({ type: projectType, rows })}
            />
            <input
              className="ai__requirement-text"
              aria-label={t("ai.requirements.text", { number: index + 1 })}
              value={row.text}
              onChange={(event) => patch(row.key, { text: event.target.value })}
              onBlur={() => save.mutate({ type: projectType, rows })}
            />
            <button
              type="button"
              className="button--quiet"
              aria-label={t("ai.requirements.drop", { number: index + 1 })}
              onClick={() => change(rows.filter((other) => other.key !== row.key))}
            >
              {t("ai.draft.drop")}
            </button>
          </li>
        ))}
      </ol>

      <button
        type="button"
        className="button--quiet"
        onClick={() => setRows([...rows, { key: nextKey.current++, section: "", text: "" }])}
      >
        {t("ai.requirements.add")}
      </button>

      {failure != null && (
        <p className="error" role="alert">
          {t(errorKey(failure))}
        </p>
      )}

      <div className="modal__actions">
        <button
          type="button"
          disabled={confirm.isPending || payload(rows).length === 0}
          onClick={() => confirm.mutate()}
        >
          {t("ai.requirements.next")}
        </button>
      </div>
    </section>
  );
}

/**
 * Between the requirements and the questions: the first question is asked for — and if the
 * document has answered everything, the interview is skipped and the summary is built at once.
 *
 * A step of its own rather than a part of the confirmation: each of these is a model call, and one
 * request holds one of them (a serverless function has thirty seconds).
 */
function QuestionsBridge({
  session,
  onSession,
}: {
  session: AiSession;
  onSession: (next: AiSession) => void;
}) {
  const { t } = useLocale();
  const requested = useRef<string | null>(null);

  const advance = useMutation({
    mutationFn: async () => {
      const asked = await nextQuestion(session.id);
      return hasQuestion(asked) ? asked : buildSummary(session.id);
    },
    onSuccess: onSession,
  });
  const { mutate, isError } = advance;

  useEffect(() => {
    if (isError || requested.current === session.id) return;
    requested.current = session.id;
    mutate();
  }, [session.id, isError, mutate]);

  return (
    <section className="settings">
      <p className="ai__question" role="status">
        {t("ai.document.preparing")}
      </p>
      {advance.error != null && (
        <>
          <p className="error" role="alert">
            {t(errorKey(advance.error))}
          </p>
          <button
            type="button"
            onClick={() => {
              requested.current = null;
              advance.reset();
            }}
          >
            {t("ai.document.resume")}
          </button>
        </>
      )}
    </section>
  );
}
