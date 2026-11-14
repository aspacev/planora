import { request } from "./client";
import type { Criticality } from "./projects";

export const AI_CREDENTIAL_QUERY_KEY = ["ai", "credential"] as const;

/** The LLM connection. There is and can be no key here — only a flag. */
export type LlmCredential = {
  provider: string;
  base_url: string;
  model: string;
  configured: boolean;
};

export type DraftTask = {
  name: string;
  description?: string;
  start_date: string;
  duration_days: number;
  criticality?: Criticality;
  /** The 1-based numbers of the summary points the task follows from. Empty — it follows from none. */
  sources?: number[];
};

export type DraftCategory = { name: string; tasks: DraftTask[] };
export type Draft = {
  categories: DraftCategory[];
  /** Summary points that need no task: a constraint, an exclusion, a fact about the project. */
  context_theses?: number[];
};

/**
 * The draft checked against the summary, by the server: the model's own word that it lost nothing
 * is not evidence. Thesis numbers are 1-based; tasks are addressed by their position in the draft.
 */
export type DraftCoverage = {
  uncovered: number[];
  context: number[];
  unsourced: { category: number; task: number }[];
};

export type AiSession = {
  id: string;
  status: "interview" | "summary" | "draft" | "applied" | "abandoned";
  locale: string;
  transcript: { question: string; answer: string | null; covered: string[] }[];
  summary: string[];
  draft: Draft | Record<string, never>;
  /** `null` until there is a draft to check. */
  coverage: DraftCoverage | null;
  tokens_used: number;
  project_id: string | null;
  applied_batch_id: string | null;
};

export function readCredential(): Promise<LlmCredential> {
  return request<LlmCredential>("/api/ai/credential");
}

export function saveCredential(payload: {
  provider?: string;
  base_url: string;
  model: string;
  api_key: string;
}): Promise<LlmCredential> {
  return request<LlmCredential>("/api/ai/credential", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function startSession(locale: string): Promise<AiSession> {
  return request<AiSession>("/api/ai/sessions", {
    method: "POST",
    body: JSON.stringify({ locale }),
  });
}

export function answerQuestion(sessionId: string, text: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/answers`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

/** Gate 1: "here is what I understood about the project". */
export function buildSummary(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/summary`, { method: "POST" });
}

export function editSummary(sessionId: string, theses: string[]): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/summary`, {
    method: "PUT",
    body: JSON.stringify({ theses }),
  });
}

/** Gate 2, the main one: the draft. Nothing has been written into the project. */
export function buildDraft(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/draft`, { method: "POST" });
}

export function editDraft(sessionId: string, draft: Draft): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/draft`, {
    method: "PUT",
    body: JSON.stringify({ draft }),
  });
}

export function applySession(
  sessionId: string,
  name: string,
): Promise<{ project_id: string; batch_id: string; session: AiSession }> {
  return request(`/api/ai/sessions/${sessionId}/apply`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}
