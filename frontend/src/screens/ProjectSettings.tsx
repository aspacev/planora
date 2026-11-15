import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { errorKey } from "../api/errors";
import { ORG_QUERY_KEY, organization } from "../api/org";
import {
  checkProjectSlug,
  getProject,
  projectQueryKey,
  updateProject,
} from "../api/projects";
import type { ProjectState } from "../api/projects";
import { useCanWrite, useOrgRole } from "../auth/permissions";
import { SaveMark, TextField, ValueField, useFieldSaves } from "../components/autosave";
import type { FieldSave } from "../components/autosave";
import { ConfirmAction } from "../components/ConfirmAction";
import { Switch } from "../components/Switch";
import { useLocale } from "../i18n/LocaleProvider";
import { SharePanel } from "../project/SharePanel";
import { SourceDocument } from "../project/SourceDocument";
import { useDeleteProject } from "../project/useDeleteProject";
import {
  DateListField,
  SlugField,
  WorkingDaysField,
  parseThresholdDays,
} from "../settings/fields";

/**
 * Level 3 of the settings: the slug, the target date and the organization's
 * overrides.
 *
 * An override is shown as an "inherit / own" toggle rather than as the
 * organization's value filled in: a filled-in number looks like the project's own
 * value, and a person who corrects it "back to how it was" silently turns
 * inheritance into a copy — exactly what the specification says to avoid.
 */
export function ProjectSettings() {
  const { t } = useLocale();
  const { projectId = "" } = useParams();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const canWrite = useCanWrite();
  // Deleting a project is the owner's right, as re-approving the plan is: an
  // editor edits the settings but does not part with a whole project. The server
  // decides either way — here we merely refrain from offering an action that will
  // end in a refusal.
  const isOwner = useOrgRole() === "owner";

  const query = useQuery({
    queryKey: projectQueryKey(projectId),
    queryFn: () => getProject(projectId),
    retry: false,
  });
  const org = useQuery({
    queryKey: ORG_QUERY_KEY,
    queryFn: organization,
    retry: false,
    staleTime: Infinity,
  });

  const save = useMutation({
    mutationFn: (patch: Parameters<typeof updateProject>[1]) => updateProject(projectId, patch),
    // There is no "Save" button here, a field leaves for the server on blur — and
    // the field itself reports the write with a mark next to it (see `SaveMark`).
    // A toast cannot say this: there are a dozen fields on the screen while there
    // is one toast, and it does not name whose value got through.
    onSuccess: (state: ProjectState) =>
      queryClient.setQueryData(projectQueryKey(projectId), state),
  });
  const saves = useFieldSaves(save.mutateAsync);
  // One and the same function between renders: the slug field defers its check by
  // it, and a new arrow on every render of the screen — and it is repainted on
  // every answer from any field — would reset the timer and throw away an answer
  // already received, so the "taken" suggestion would never appear at all.
  const checkSlug = useCallback((slug: string) => checkProjectSlug(projectId, slug), [projectId]);

  // What happens to the cache after a deletion is known by the shared hook: the
  // same thing happens when deleting from a card in the list of projects. What is
  // left to the screen is its own business — leaving a place with nothing more to
  // show. replace rather than push: "back" to the deleted project's settings would
  // lead to a screen with nothing to show but an error.
  const remove = useDeleteProject({
    onDeleted: () => navigate("/projects", { replace: true }),
  });

  if (query.isPending) {
    return (
      <main className="screen">
        <p role="status">{t("common.loading")}</p>
      </main>
    );
  }

  if (query.error) {
    return (
      <main className="screen">
        <p className="error" role="alert">
          {t(errorKey(query.error))}
        </p>
      </main>
    );
  }

  const state = query.data;
  const overrides = state.overrides;
  const orgSettings = org.data?.settings;
  const readOnly = !canWrite;

  return (
    <main className="screen">
      <div className="screen__head">
        {/* The project's name is user content: it is not translated. */}
        <h1>{t("settings.project.title", { name: state.name })}</h1>
        <Link to={`/projects/${projectId}`}>{t("settings.project.back")}</Link>
      </div>

      <section className="settings">
        <TextField
          id="project-name"
          label={t("settings.project.name")}
          value={state.name}
          disabled={readOnly}
          save={saves.at("project-name")}
          onCommit={(value) => saves.commitText("project-name", value, (name) => ({ name }))}
        />

        <SlugField
          id="project-slug"
          label={t("settings.project.slug")}
          value={state.slug}
          disabled={readOnly}
          check={checkSlug}
          save={saves.at("project-slug")}
          onCommit={(slug) => saves.commit("project-slug", { slug })}
        />

        <ValueField
          id="project-deadline"
          label={t("settings.project.deadline")}
          type="date"
          value={state.deadline ?? ""}
          disabled={readOnly}
          // An empty date is the absence of a deadline, not a skipped field.
          allowEmpty
          save={saves.at("project-deadline")}
          onCommit={(value) =>
            saves.commit("project-deadline", { deadline: value === "" ? null : value })
          }
        />

        <Override
          id="project-timezone"
          label={t("settings.timezone")}
          inherited={orgSettings?.default_timezone ?? ""}
          overridden={overrides?.timezone ?? null}
          disabled={readOnly}
          save={saves.at("project-timezone")}
          onInherit={() => saves.commit("project-timezone", { timezone: null })}
          onOverride={(value) =>
            saves.commitText("project-timezone", value, (zone) => ({ timezone: zone }))
          }
          render={(value, onCommit, disabled, save) => (
            <TextField
              id="project-timezone"
              labelledBy="project-timezone-label"
              value={value}
              disabled={disabled}
              save={save}
              onCommit={onCommit}
            />
          )}
        />

        <Override
          id="project-threshold"
          label={t("settings.threshold")}
          inherited={String(orgSettings?.default_shift_threshold_days ?? "")}
          overridden={
            overrides?.shift_threshold_days === null || overrides === undefined
              ? null
              : String(overrides.shift_threshold_days)
          }
          disabled={readOnly}
          save={saves.at("project-threshold")}
          onInherit={() => saves.commit("project-threshold", { shift_threshold_days: null })}
          // An empty field is not "a threshold of zero": while there is no number,
          // the override stays as it was rather than turning into "explain every
          // shift". The parse is shared with the organization's settings (see
          // `parseThresholdDays`).
          onOverride={(value) =>
            saves.commitNumber(
              "project-threshold",
              value,
              (days) => ({ shift_threshold_days: days }),
              parseThresholdDays,
            )
          }
          render={(value, onCommit, disabled, save) => (
            <TextField
              id="project-threshold"
              labelledBy="project-threshold-label"
              type="number"
              min={0}
              value={value}
              disabled={disabled}
              save={save}
              onCommit={onCommit}
            />
          )}
        />

        <Override
          id="project-working-days"
          label={t("settings.working_days")}
          inherited={String(orgSettings?.working_days ?? "")}
          overridden={
            overrides?.working_days === null || overrides === undefined
              ? null
              : String(overrides.working_days)
          }
          disabled={readOnly}
          save={saves.at("project-working-days")}
          onInherit={() => saves.commit("project-working-days", { working_days: null })}
          onOverride={(value) =>
            saves.commit("project-working-days", { working_days: Number(value) })
          }
          render={(value, onCommit, disabled, save) => (
            <WorkingDaysField
              value={Number(value) || state.calendar.working_days}
              disabled={disabled}
              save={save}
              onChange={(mask) => onCommit(String(mask))}
            />
          )}
        />

        <DateListField
          id="project-holidays"
          label={t("settings.project.holidays_extra")}
          hint={t("settings.project.holidays_extra_hint")}
          value={overrides?.holidays_extra ?? []}
          disabled={readOnly}
          save={saves.at("project-holidays")}
          onCommit={(holidays_extra) => saves.commit("project-holidays", { holidays_extra })}
        />

        {/* Auto-shifting is a switch rather than an override: the organization has
            no such setting at all, there is nothing to inherit. It stands next to
            the calendar because it answers the same question — by what rules the
            dates are computed — rather than "what they are". */}
        <p className="field">
          <Switch
            id="project-auto-schedule"
            label={t("gantt.auto_schedule.label")}
            checked={state.auto_schedule === true}
            disabled={readOnly}
            onChange={(auto_schedule) =>
              saves.commit("project-auto-schedule", { auto_schedule })
            }
          />
          <span className="muted">{t("gantt.auto_schedule.hint")}</span>
          <SaveMark save={saves.at("project-auto-schedule")} />
        </p>

        <DateListField
          id="project-workdays"
          label={t("settings.project.workdays_extra")}
          hint={t("settings.project.workdays_extra_hint")}
          value={overrides?.workdays_extra ?? []}
          disabled={readOnly}
          save={saves.at("project-workdays")}
          onCommit={(workdays_extra) => saves.commit("project-workdays", { workdays_extra })}
        />

        {/* The document the project was made from. Not behind `readOnly`: a viewer
            reads it too, and whoever may not is simply answered "nothing here" by
            the server — the block then shows nothing. */}
        <SourceDocument projectId={projectId} />

        {/* The public link comes as the last block: it is not a computation
            setting but a decision to show the project outside. */}
        {!readOnly && <SharePanel projectId={projectId} />}

        {/* Deletion comes after everything else: it is not a setting but a parting
            with the project. The confirmation unfolds in place, as with plan
            re-approval — and it warns honestly: the revision journal goes with the
            project, and so does the possibility of an undo. */}
        {isOwner && (
          <div className="settings__danger">
            {remove.error !== null && (
              <p className="error" role="alert">
                {t(errorKey(remove.error))}
              </p>
            )}
            <ConfirmAction
              className="button--quiet button--alert"
              label={t("settings.project.delete")}
              warning={t("settings.project.delete_warning")}
              confirm={t("settings.project.delete_confirm")}
              onConfirm={() => remove.mutate({ id: projectId, name: state.name })}
              disabled={remove.isPending}
            />
          </div>
        )}
      </section>
    </main>
  );
}

/**
 * A value that is either inherited or set by this project.
 *
 * The toggle stands before the field itself deliberately: first it is decided
 * whose value this is, and only then which. The reverse order would offer to edit
 * a number that does not belong to the project.
 */
function Override({
  id,
  label,
  inherited,
  overridden,
  disabled,
  save,
  onInherit,
  onOverride,
  render,
}: {
  id: string;
  label: string;
  /** The organization's value — what applies while there is no override. */
  inherited: string;
  /** `null` — inherited. */
  overridden: string | null;
  disabled?: boolean;
  save?: FieldSave;
  onInherit: () => void;
  onOverride: (value: string) => void;
  render: (
    value: string,
    onCommit: (value: string) => void,
    disabled: boolean,
    save?: FieldSave,
  ) => React.ReactNode;
}) {
  const { t } = useLocale();
  const inherits = overridden === null;

  return (
    <div className="settings__override">
      <span className="settings__override-label" id={`${id}-label`}>
        {label}
      </span>
      <label className="settings__inherit">
        <input
          type="checkbox"
          checked={inherits}
          disabled={disabled}
          onChange={(event) => (event.target.checked ? onInherit() : onOverride(inherited))}
        />
        {t("settings.inherit", { value: inherited })}
      </label>
      {/* The submission mark goes where the control is: while the value is
          inherited that is the checkbox itself, and after that the field shows it.
          Otherwise nobody would report a return to inheritance. */}
      {inherits ? <SaveMark save={save} /> : render(overridden, onOverride, Boolean(disabled), save)}
    </div>
  );
}
