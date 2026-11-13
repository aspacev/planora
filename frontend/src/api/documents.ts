import { request, requestFile } from "./client";
import type { DownloadedFile } from "./client";

/** The requirements document a project was created from. The file itself is fetched separately. */
export type ProjectDocument = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  uploaded_by_name: string | null;
  /** Whether this person may delete it — decided by the server's permission matrix. */
  can_delete: boolean;
};

export const projectDocumentQueryKey = (projectId: string) =>
  ["projects", projectId, "document"] as const;

export function readProjectDocument(projectId: string): Promise<ProjectDocument> {
  return request<ProjectDocument>(`/api/projects/${projectId}/document`);
}

export function downloadProjectDocument(projectId: string): Promise<DownloadedFile> {
  return requestFile(`/api/projects/${projectId}/document/file`);
}

export function deleteProjectDocument(projectId: string): Promise<void> {
  return request<void>(`/api/projects/${projectId}/document`, { method: "DELETE" });
}
