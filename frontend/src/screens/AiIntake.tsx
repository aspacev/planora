import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { AI_CREDENTIAL_QUERY_KEY, readCredential, startSession } from "../api/ai";
import type { AiSession, ProjectType } from "../api/ai";
import { errorKey } from "../api/errors";
import { useLocale } from "../i18n/LocaleProvider";
import { IntakeSteps } from "../intake/IntakeSteps";
import { ProjectTypeField } from "../intake/ProjectTypeField";

/**
 * Creating a project through an interview.
 *
 * Four steps and two gates between them: the summary — where it is cheapest to catch a
 * misunderstanding — and the draft — where nothing has been written into the project yet. They
 * cannot be skipped: the server will refuse, and the interface does not offer it either. The steps
 * themselves live in `IntakeSteps`, shared with the intake from a document.
 */
export function AiIntake() {
  const { t, locale } = useLocale();
  const [session, setSession] = useState<AiSession | null>(null);
  // The type is asked before the first question rather than guessed from the answers: the list of
  // topics depends on it, and the first question is already taken from that list.
  const [projectType, setProjectType] = useState<ProjectType>("general");

  const credential = useQuery({
    queryKey: AI_CREDENTIAL_QUERY_KEY,
    queryFn: readCredential,
    retry: false,
  });

  const start = useMutation({
    mutationFn: () => startSession(locale, projectType),
    onSuccess: setSession,
  });

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

  return (
    <main className="screen">
      <div className="screen__head">
        <h1>{t("ai.title")}</h1>
      </div>

      {start.error != null && (
        <p className="error" role="alert">
          {t(errorKey(start.error))}
        </p>
      )}

      {session === null ? (
        <>
          <p className="muted">{t("ai.intro")}</p>
          <ProjectTypeField
            id="ai-project-type"
            value={projectType}
            disabled={start.isPending}
            onChange={setProjectType}
          />
          <button type="button" onClick={() => start.mutate()} disabled={start.isPending}>
            {t("ai.start")}
          </button>
        </>
      ) : (
        <IntakeSteps session={session} onSession={setSession} />
      )}
    </main>
  );
}
