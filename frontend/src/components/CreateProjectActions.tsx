import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { errorKey } from "../api/errors";
import { PROJECTS_QUERY_KEY, createProject } from "../api/projects";
import { useLocale } from "../i18n/LocaleProvider";
import { Field } from "./Field";
import { Modal } from "./Modal";
import { useToast } from "./toast";

/**
 * The ways to create a project — the ordinary one, through an interview and from a requirements
 * document — as one group of buttons.
 *
 * A separate component rather than markup inside a screen: the pair lives in the projects list's
 * header and will outlive the next screen that begins with the same "and what if there is no project
 * yet?" question. Two copies of it would have nothing to diverge over in captions or in the dialog's
 * behaviour.
 */
export function CreateProjectActions() {
  const { t } = useLocale();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const showToast = useToast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: (candidate: string) => createProject(candidate),
    onSuccess: (project) => {
      // The list is invalidated rather than written into by hand: the server returned a slug it built
      // itself, and putting client-invented fields next to it means keeping a record in the cache that
      // does not exist on the server.
      void queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY });
      navigate(`/projects/${project.id}`);
      // The toast outlives the navigation: it hangs on the application's frame rather than on the
      // list's screen. An empty chart after the press looks equally like a new project and like a
      // stray click — the name in the toast tells them apart.
      showToast({ message: t("projects.created", { name: project.name }) });
    },
  });

  const trimmed = name.trim();

  function close() {
    setOpen(false);
    setName("");
    create.reset();
  }

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        {t("projects.create")}
      </button>
      {/* The interview is only for a new project: launching it inside an existing one is not part of
          the first version. The link looks like an outlined button: next to a filled blue chip it is
          a second way to do the same thing rather than a footnote under it. */}
      <Link to="/projects/new/ai" className="button-link">
        {t("projects.create_with_ai")}
      </Link>
      {/* The third way: the order already has a document, and retelling it in a chat is long and
          loses requirements. */}
      <Link to="/projects/new/document" className="button-link">
        {t("projects.create_from_document")}
      </Link>

      {open && (
        <Modal title={t("projects.new.title")} onClose={close} dirty={name !== ""}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate(trimmed);
            }}
          >
            <Field id="project-name" label={t("projects.new.name")} value={name} onChange={setName} />

            {create.error && (
              <p className="error" role="alert">
                {t(errorKey(create.error))}
              </p>
            )}

            <div className="modal__actions">
              <button type="submit" disabled={trimmed === "" || create.isPending}>
                {t("common.create")}
              </button>
              <button type="button" className="button--quiet" onClick={close}>
                {t("common.cancel")}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
