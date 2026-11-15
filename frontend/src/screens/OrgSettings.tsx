import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AI_CREDENTIAL_QUERY_KEY, readCredential, saveCredential } from "../api/ai";
import { errorKey } from "../api/errors";
import { ORG_QUERY_KEY, checkOrgSlug, organization, updateOrganization } from "../api/org";
import type { Organization, OrganizationSettings } from "../api/org";
import { SaveMark, SelectField, TextField, useFieldSaves } from "../components/autosave";
import { useToast } from "../components/toast";
import { SUPPORTED_LOCALES } from "../i18n";
import { useLocale } from "../i18n/LocaleProvider";
import {
  DateListField,
  SlugField,
  WorkingDaysField,
  parseThresholdDays,
} from "../settings/fields";

/**
 * Level 2 of the settings: the defaults every project in the organization inherits.
 *
 * There is no "Save" button — as in the task card: every field goes to the server on its own once
 * you are done with it. A form with a button would promise that nothing happened before the
 * press, and then it would have to be explained why leaving the page loses the edits.
 *
 * In exchange every field reports for itself: a "saved" or a refusal in words stands next to it. A
 * shared banner at the top of the page would not do for that — from it you cannot tell which of
 * the ten fields was rejected, and from the field you still have to look up at it.
 *
 * The screen has no `<main>` of its own: it is a tab of the settings section, and the frame has
 * already given it one. A second `<main>` inside the first would break the "skip to main content"
 * jump — a screen reader does not know which of the two is the main one.
 */
export function OrgSettings() {
  const { t } = useLocale();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ORG_QUERY_KEY,
    queryFn: organization,
    retry: false,
    staleTime: Infinity,
  });

  const save = useMutation({
    mutationFn: (patch: Partial<OrganizationSettings & { name: string; slug: string }>) =>
      updateOrganization(patch),
    // The promise that "every field goes to the server on its own" is confirmed by the field
    // itself — with a mark next to it (see `SaveMark`) rather than with a toast over the page:
    // from a toast you cannot tell which of the ten fields got through and which was rejected.
    onSuccess: (org: Organization) => queryClient.setQueryData(ORG_QUERY_KEY, org),
  });
  const saves = useFieldSaves(save.mutateAsync);

  if (query.isPending) return <p role="status">{t("common.loading")}</p>;

  if (query.error) {
    return (
      <p className="error" role="alert">
        {t(errorKey(query.error))}
      </p>
    );
  }

  const org = query.data;
  const settings = org.settings;
  // The permission is decided by the server; here it only disables the fields — offering an action
  // that will end in a refusal is worse than not offering it at all.
  const readOnly = org.role !== "owner";

  return (
    <>
      <div className="screen__head">
        <h1>{t("settings.org.title")}</h1>
      </div>

      {readOnly && <p className="muted">{t("settings.org.read_only")}</p>}

      <section className="settings">
        <TextField
          id="org-name"
          label={t("settings.org.name")}
          value={org.name}
          disabled={readOnly}
          save={saves.at("org-name")}
          onCommit={(value) => saves.commitText("org-name", value, (name) => ({ name }))}
        />

        <SlugField
          id="org-slug"
          label={t("settings.org.slug")}
          value={org.slug}
          disabled={readOnly}
          check={checkOrgSlug}
          save={saves.at("org-slug")}
          onCommit={(slug) => saves.commit("org-slug", { slug })}
        />

        <SelectField
          id="org-locale"
          label={t("settings.org.locale")}
          value={settings.default_locale}
          disabled={readOnly}
          options={SUPPORTED_LOCALES.map((code) => ({ value: code, label: code.toUpperCase() }))}
          save={saves.at("org-locale")}
          onCommit={(default_locale) => saves.commit("org-locale", { default_locale })}
        />

        <TextField
          id="org-timezone"
          label={t("settings.timezone")}
          value={settings.default_timezone}
          disabled={readOnly}
          save={saves.at("org-timezone")}
          onCommit={(value) =>
            saves.commitText("org-timezone", value, (zone) => ({ default_timezone: zone }))
          }
        />

        <WorkingDaysField
          value={settings.working_days}
          disabled={readOnly}
          save={saves.at("org-working-days")}
          onChange={(working_days) => saves.commit("org-working-days", { working_days })}
        />

        <TextField
          id="org-threshold"
          label={t("settings.threshold")}
          hint={t("settings.threshold_hint")}
          type="number"
          min={0}
          value={String(settings.default_shift_threshold_days)}
          disabled={readOnly}
          save={saves.at("org-threshold")}
          onCommit={(value) =>
            saves.commitNumber(
              "org-threshold",
              value,
              (days) => ({ default_shift_threshold_days: days }),
              parseThresholdDays,
            )
          }
        />

        <DateListField
          id="org-holidays"
          label={t("settings.org.holidays")}
          hint={t("settings.org.holidays_hint")}
          value={settings.holiday_calendar}
          disabled={readOnly}
          save={saves.at("org-holidays")}
          onCommit={(holiday_calendar) => saves.commit("org-holidays", { holiday_calendar })}
        />

        <p className="field field--inline">
          <input
            id="org-sharing"
            name="org-sharing"
            type="checkbox"
            checked={settings.public_sharing_enabled}
            disabled={readOnly}
            onChange={(event) =>
              saves.commit("org-sharing", { public_sharing_enabled: event.target.checked })
            }
          />
          <label htmlFor="org-sharing">{t("settings.org.public_sharing")}</label>
          <SaveMark save={saves.at("org-sharing")} />
        </p>

        <LlmConnection readOnly={readOnly} />

        <p className="field field--inline">
          <input
            id="org-comments"
            name="org-comments"
            type="checkbox"
            checked={settings.default_comments_enabled}
            disabled={readOnly}
            onChange={(event) =>
              saves.commit("org-comments", { default_comments_enabled: event.target.checked })
            }
          />
          <label htmlFor="org-comments">{t("settings.org.comments")}</label>
          <SaveMark save={saves.at("org-comments")} />
        </p>
      </section>
    </>
  );
}

/**
 * The LLM connection: the provider, the address, the model, the key.
 *
 * The key is never handed out — only a "configured" flag. So an empty key field means "keep the
 * previous one": demanding it when editing the address would mean demanding the impossible,
 * because the person has nowhere to take it from.
 */
function LlmConnection({ readOnly }: { readOnly: boolean }) {
  const { t } = useLocale();
  const queryClient = useQueryClient();
  const showToast = useToast();
  const [key, setKey] = useState("");

  const credential = useQuery({
    queryKey: AI_CREDENTIAL_QUERY_KEY,
    queryFn: readCredential,
    retry: false,
  });

  const save = useMutation({
    mutationFn: saveCredential,
    onSuccess: (result) => {
      queryClient.setQueryData(AI_CREDENTIAL_QUERY_KEY, result);
      setKey("");
      // The key field is cleared on success — and without a toast this clearing reads as "the input
      // was not accepted", exactly the opposite of what it means.
      showToast({ message: t("common.saved") });
    },
  });

  // A 403 refusal here is not a breakage: a non-owner simply does not see this block.
  if (credential.error || !credential.data) return null;
  const current = credential.data;

  return (
    <form
      className="settings__fieldset"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget as HTMLFormElement);
        save.mutate({
          provider: String(form.get("llm-provider") ?? "openai"),
          base_url: String(form.get("llm-base-url") ?? ""),
          model: String(form.get("llm-model") ?? ""),
          api_key: key,
        });
      }}
    >
      <h2>{t("settings.llm.title")}</h2>
      <p className="muted">{t("settings.llm.hint")}</p>

      <p className="field">
        <label htmlFor="llm-provider">{t("settings.llm.provider")}</label>
        <input
          id="llm-provider"
          name="llm-provider"
          defaultValue={current.provider || "openai"}
          disabled={readOnly}
        />
      </p>

      <p className="field">
        <label htmlFor="llm-base-url">{t("settings.llm.base_url")}</label>
        <input
          id="llm-base-url"
          name="llm-base-url"
          defaultValue={current.base_url}
          disabled={readOnly}
        />
      </p>

      <p className="field">
        <label htmlFor="llm-model">{t("settings.llm.model")}</label>
        <input id="llm-model" name="llm-model" defaultValue={current.model} disabled={readOnly} />
      </p>

      <p className="field">
        <label htmlFor="llm-key">{t("settings.llm.key")}</label>
        <span className="muted">
          {current.configured ? t("settings.llm.key_set") : t("settings.llm.key_missing")}
        </span>
        <input
          id="llm-key"
          name="llm-key"
          type="password"
          autoComplete="off"
          value={key}
          disabled={readOnly}
          onChange={(event) => setKey(event.target.value)}
        />
      </p>

      {save.error !== null && (
        <p className="error" role="alert">
          {t(errorKey(save.error))}
        </p>
      )}

      <button type="submit" disabled={readOnly || save.isPending}>
        {t("settings.llm.save")}
      </button>
    </form>
  );
}
