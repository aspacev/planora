import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { saveFile } from "../api/client";
import {
  deleteProjectDocument,
  downloadProjectDocument,
  projectDocumentQueryKey,
  readProjectDocument,
} from "../api/documents";
import { errorKey } from "../api/errors";
import { ConfirmAction } from "../components/ConfirmAction";
import { useToast } from "../components/toast";
import { formatDate } from "../i18n/dates";
import { useLocale } from "../i18n/LocaleProvider";

/**
 * The requirements document the project was created from: its name, who uploaded it and when, and
 * the file itself.
 *
 * The block decides nothing about who sees it: the server answers "no document" or "not yours to
 * read", and in both cases there is nothing to show — a client is not told that a document exists
 * and is closed to them. Deletion is offered by the flag the server sent, for the same reason.
 */
export function SourceDocument({ projectId }: { projectId: string }) {
  const { t } = useLocale();
  const queryClient = useQueryClient();
  const showToast = useToast();
  const queryKey = projectDocumentQueryKey(projectId);

  const document = useQuery({
    queryKey,
    queryFn: () => readProjectDocument(projectId),
    retry: false,
  });

  const download = useMutation({
    mutationFn: () => downloadProjectDocument(projectId),
    onSuccess: (file) => saveFile(file, document.data?.filename ?? "document"),
  });
  const remove = useMutation({
    mutationFn: () => deleteProjectDocument(projectId),
    onSuccess: () => {
      // The cached answer is thrown away rather than refreshed: the document is gone, and the
      // block must fold at once instead of showing a deleted file until the server says so.
      queryClient.removeQueries({ queryKey });
      showToast({ message: t("settings.project.document.deleted") });
    },
  });

  if (document.data === undefined) return null;
  const { filename, created_at, uploaded_by_name, can_delete } = document.data;
  const failure = download.error ?? remove.error;

  return (
    <section className="source-document" aria-labelledby="source-document-title">
      <h2 id="source-document-title">{t("settings.project.document.title")}</h2>
      {/* The file's name is content: it is shown as the person named it. */}
      <p className="source-document__name">{filename}</p>
      <p className="muted">
        {uploaded_by_name === null
          ? t("settings.project.document.uploaded", { date: formatDate(t, created_at) })
          : t("settings.project.document.uploaded_by", {
              date: formatDate(t, created_at),
              name: uploaded_by_name,
            })}
      </p>

      {failure != null && (
        <p className="error" role="alert">
          {t(errorKey(failure))}
        </p>
      )}

      <div className="source-document__actions">
        <button type="button" onClick={() => download.mutate()} disabled={download.isPending}>
          {t("settings.project.document.download")}
        </button>
        {can_delete && (
          <ConfirmAction
            className="button--quiet button--alert"
            label={t("settings.project.document.delete")}
            warning={t("settings.project.document.delete_warning")}
            confirm={t("settings.project.document.delete_confirm")}
            onConfirm={() => remove.mutate()}
            disabled={remove.isPending}
          />
        )}
      </div>
    </section>
  );
}
