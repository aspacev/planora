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
  /** The numbers of the document's requirements the task comes from. */
  requirement_ids?: number[];
};

/** The kinds of project: each asks about its own list of topics. */
export const PROJECT_TYPES = ["software", "event", "general"] as const;
export type ProjectType = (typeof PROJECT_TYPES)[number];

/** One thing the document asks for; `section` is the clause it was taken from. */
export type Requirement = { id: number; section: string; text: string };

export type DraftCategory = { name: string; tasks: DraftTask[] };
export type Draft = { categories: DraftCategory[] };

export type AiSession = {
  id: string;
  status:
    | "reading"
    | "requirements"
    | "interview"
    | "summary"
    | "draft"
    | "applied"
    | "abandoned";
  locale: string;
  project_type: ProjectType;
  /** The document the session started from — its name and how far it has been read. */
  document: {
    filename: string;
    size_bytes: number;
    parts_read: number;
    parts_total: number;
  } | null;
  brief: string;
  requirements: Requirement[];
  document_topics: string[];
  /** The requirements no task of the draft refers to. Counted by the server. */
  uncovered_requirements: Requirement[];
  transcript: { question: string; answer: string | null; covered: string[] }[];
  summary: string[];
  draft: Draft | Record<string, never>;
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

export function startSession(
  locale: string,
  projectType: ProjectType = "general",
): Promise<AiSession> {
  return request<AiSession>("/api/ai/sessions", {
    method: "POST",
    body: JSON.stringify({ locale, project_type: projectType }),
  });
}

export function readSession(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}`);
}

/** The ceiling on an uploaded document — the server's `MAX_DOCUMENT_BYTES` default. */
export const MAX_DOCUMENT_BYTES = 4_000_000;

/**
 * Uploads a requirements document. The body is the file itself rather than a form: the server
 * reads the bytes as they are and takes the name from the address.
 *
 * The bytes are read into a buffer first rather than handing `fetch` the `File`: a buffer is
 * sent the same way by every runtime, the test one included.
 */
export async function uploadDocument(file: File, locale: string): Promise<AiSession> {
  const query = new URLSearchParams({ filename: file.name, locale });
  return request<AiSession>(`/api/ai/documents?${query.toString()}`, {
    method: "POST",
    body: await file.arrayBuffer(),
    headers: { "Content-Type": "application/octet-stream" },
  });
}

/** Reads the next part of the document: one request — one part. */
export function readDocumentPart(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/document/parts`, { method: "POST" });
}

export function saveRequirements(
  sessionId: string,
  projectType: ProjectType,
  requirements: Pick<Requirement, "section" | "text">[],
): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/requirements`, {
    method: "PUT",
    body: JSON.stringify({ project_type: projectType, requirements }),
  });
}

/** The requirements gate: the type and the list are what the person agreed to. */
export function confirmRequirements(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/requirements/confirm`, {
    method: "POST",
  });
}

/** The first question after the gate. A session that comes back with none skips the interview. */
export function nextQuestion(sessionId: string): Promise<AiSession> {
  return request<AiSession>(`/api/ai/sessions/${sessionId}/questions`, { method: "POST" });
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
