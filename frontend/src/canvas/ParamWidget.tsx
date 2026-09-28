/**
 * One parameter's widget — the ONE widget every surface uses (a node on the canvas, the settings form, the run
 * panel's rerun form), so the three can never disagree about what a value looks like or how it is typed.
 *
 * The widget comes from the parameter's own JSON-schema fragment (`Param.schema`, from `confluid.to_pydantic`)
 * merged with the settings form (`GET …/form`): an `enum` is a select, `boolean` a checkbox, `number` / `integer`
 * a number input carrying the schema's `minimum` / `maximum`, `string` a text input, and anything the document
 * spells as a container (`List[str]`, `object`) is shown read-only — the document text is where that is edited.
 * A change commits on blur (Enter blurs); `onCommit` answers whether it was taken, and a refusal reverts the
 * widget to `param.value`, which is the truth the caller holds (the model's value, or a trial value).
 */
import { useEffect, useState, type KeyboardEvent } from "react";
import type { Json, Param } from "../model";

export const asText = (value: Json): string =>
  value === null || value === undefined ? "" : typeof value === "string" ? value : JSON.stringify(value);

const same = (a: Json, b: Json): boolean => JSON.stringify(a) === JSON.stringify(b);

const blurOnEnter = (event: KeyboardEvent<HTMLInputElement>) => {
  if (event.key === "Enter") event.currentTarget.blur();
};

export interface ParamWidgetProps {
  param: Param;
  schema: Record<string, Json>;
  /** Take the value; `false` (a refusal, already shown) reverts the widget. */
  onCommit: (value: Json) => Promise<boolean>;
}

export function ParamWidget({ param, schema, onCommit }: ParamWidgetProps) {
  const [draft, setDraft] = useState<string>(asText(param.value));
  const truth = asText(param.value);
  // The caller's value is the truth: when it changes (an accepted edit, an undo, the other surface), follow it.
  useEffect(() => setDraft(truth), [truth]);

  const commit = async (value: Json) => {
    if (same(value, param.value)) return;
    const taken = await onCommit(value);
    if (!taken) setDraft(truth);
  };
  const revert = () => setDraft(truth);

  const options = Array.isArray(schema.enum) ? (schema.enum as Json[]) : undefined;
  if (options) {
    const index = options.findIndex((option) => same(option, param.value));
    return (
      <select
        className="nodrag"
        data-param={param.name}
        value={index < 0 ? "" : String(index)}
        onChange={(event) => void commit(options[Number(event.target.value)])}
      >
        {index < 0 && <option value="">{truth || "(unset)"}</option>}
        {options.map((option, i) => (
          <option key={i} value={String(i)}>
            {asText(option)}
          </option>
        ))}
      </select>
    );
  }
  if (param.type === "boolean") {
    return (
      <input
        type="checkbox"
        className="nodrag"
        data-param={param.name}
        checked={param.value === true}
        onChange={(event) => void commit(event.target.checked)}
      />
    );
  }
  if (param.type === "number" || param.type === "integer") {
    const integer = param.type === "integer";
    return (
      <input
        type="number"
        className="nodrag"
        data-param={param.name}
        value={draft}
        step={integer ? 1 : "any"}
        min={typeof schema.minimum === "number" ? schema.minimum : undefined}
        max={typeof schema.maximum === "number" ? schema.maximum : undefined}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={blurOnEnter}
        onBlur={() => {
          if (draft.trim() === "") return revert(); // an empty box is not a value; the constructor's default stays
          const value = Number(draft);
          if (!Number.isFinite(value) || (integer && !Number.isInteger(value))) return revert();
          void commit(value);
        }}
      />
    );
  }
  if (param.type === "string") {
    return (
      <input
        type="text"
        className="nodrag"
        data-param={param.name}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={blurOnEnter}
        onBlur={() => void commit(draft)}
      />
    );
  }
  // a container (`List[str]`, `object`) or an unspelled default: shown as the document would spell it
  return (
    <code className="ro" data-param={param.name} title={`${param.type} — edit it in the document text`}>
      {truth || "∅"}
    </code>
  );
}
