import { PROJECT_TYPES } from "../api/ai";
import type { ProjectType } from "../api/ai";
import { useLocale } from "../i18n/LocaleProvider";

/**
 * The kind of project — it decides what the interview asks about.
 *
 * One field for both intakes: the plain interview asks for the type before the first question, the
 * intake from a document shows the type the model saw in the text and lets a person change it.
 */
export function ProjectTypeField({
  id,
  value,
  disabled = false,
  onChange,
}: {
  id: string;
  value: ProjectType;
  disabled?: boolean;
  onChange: (next: ProjectType) => void;
}) {
  const { t } = useLocale();

  return (
    <p className="field">
      <label htmlFor={id}>{t("ai.type.label")}</label>
      <select
        id={id}
        name={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as ProjectType)}
      >
        {PROJECT_TYPES.map((kind) => (
          <option key={kind} value={kind}>
            {t(`ai.type.${kind}`)}
          </option>
        ))}
      </select>
    </p>
  );
}
