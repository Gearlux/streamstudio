/**
 * One parameter's widget — the ONE widget every surface uses (a node on the canvas, the settings form, the run
 * panel's rerun form, set source…'s form), so they can never disagree about what a value looks like or how it is typed.
 *
 * The widget comes from the parameter's own JSON-schema fragment (`Param.schema`, from `confluid.to_pydantic`)
 * merged with the settings form (`GET …/form`): an `enum` is a select, `boolean` a checkbox, `number` / `integer`
 * a number input carrying the schema's `minimum` / `maximum`, a `(min, max)` pair two such inputs (`pickers.rangePair` —
 * the ComfyUI editor's `__lo` / `__hi` widgets), `string` a text input, and anything else the document spells as a
 * container (`List[str]`, `object`) is shown read-only — the document text is where that is edited.
 *
 * A fragment the server marked with a `picker` (`pickers.ts`) adds the ComfyUI editor's chooser beside the field: a
 * `directory` or `file` field a `…` button opening the editor's file browser (`data-browse`), a HuggingFace source's
 * dataset, column and split fields a list of what the editor's machine knows (`data-picker`), its
 * `metadata_features` an "add column" list, and a producer's own tag that an installed plugin lists (`lookup: true`) the
 * plugin's list, asked with the node's current values (`data-picker="plugin"`). A pick commits like a typed value; the
 * field stays free text.
 *
 * A change commits on blur (Enter blurs); `onCommit` answers whether it was taken, and a refusal reverts the widget to
 * `param.value`, which is the truth the caller holds (the model's value, or a trial value).
 */
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { errorMessage, hfDatasets, hfFeatures, hfSplits, pluginLookup, type Listed } from "../api";
import { useEditor } from "../context";
import type { Json, Param } from "../model";
import {
  browseStart,
  cachedLookup,
  followPair,
  hfDataset,
  isLookup,
  lookupKey,
  metadataChoices,
  metadataText,
  pairValue,
  parseMetadata,
  pickerFor,
  pluginLookupKey,
  pluginTag,
  pluginValues,
  rangePair,
  withColumn,
  type LookupPicker as LookupKind,
  type PairEnd,
} from "../pickers";

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
  /** The node's current values — what a HuggingFace list is asked about (the dataset its `path` names). */
  values?: Record<string, Json | undefined>;
}

/** The values a node's fields hold now: what its widgets' `values` are on the canvas and in the settings tab. */
export const valuesOf = (params: Record<string, Param>): Record<string, Json> =>
  Object.fromEntries(Object.values(params).map((param) => [param.name, param.value]));

const NO_VALUES: Record<string, Json | undefined> = {};

export function ParamWidget({ param, schema, onCommit, values = NO_VALUES }: ParamWidgetProps) {
  const [draft, setDraft] = useState<string>(asText(param.value));
  const truth = asText(param.value);
  // The caller's value is the truth: when it changes (an accepted edit, an undo, the other surface), follow it.
  useEffect(() => setDraft(truth), [truth]);

  /** Send a value the caller does not hold yet; answers whether it was taken (a refusal reverts the field). */
  const commit = async (value: Json): Promise<boolean> => {
    if (same(value, param.value)) return true;
    const taken = await onCommit(value);
    if (!taken) setDraft(truth);
    return taken;
  };
  const revert = () => setDraft(truth);
  const picker = pickerFor(schema);

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
  const ends = rangePair(schema);
  if (ends) return <PairWidget param={param} ends={ends} fallback={schema.default} commit={commit} />;
  if (picker === "hf_metadata") return <MetadataField param={param} values={values} commit={commit} />;
  if (param.type === "string") {
    const field = (
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
    if (picker === undefined) return field;
    const pick = (value: string) => {
      setDraft(value);
      void commit(value);
    };
    return (
      <span className="picked" data-picked={picker}>
        {field}
        {isLookup(picker) ? (
          <LookupPicker
            picker={picker}
            tag={pluginTag(schema)}
            param={param.name}
            values={values}
            label={LOOKUP_LABELS[picker]}
            onPick={pick}
          />
        ) : (
          <BrowseButton picker={picker} param={param.name} draft={draft} onPick={pick} />
        )}
      </span>
    );
  }
  // a container (`List[str]`, `object`) or an unspelled default: shown as the document would spell it
  return (
    <code className="ro" data-param={param.name} title={`${param.type} — edit it in the document text`}>
      {truth || "∅"}
    </code>
  );
}

/** The first entry of a lookup picker's list: what it offers. */
const LOOKUP_LABELS: Record<LookupKind, string> = {
  hf_dataset: "cached…",
  hf_feature: "columns…",
  hf_split: "splits…",
  hf_metadata: "add column…",
  plugin: "list…",
};

/** What a lookup about a dataset says while the node names none. */
const UNNAMED: Record<LookupKind, string> = {
  hf_dataset: "",
  hf_feature: "name a dataset first — its columns are listed here",
  hf_split: "name a dataset first — its splits are listed here",
  hf_metadata: "name a dataset first — its columns are listed here",
  plugin: "",
};

/** What a lookup says when the editor's machine knows nothing to offer. */
const NOTHING_FOUND: Record<LookupKind, string> = {
  hf_dataset: "no HuggingFace dataset is cached on the editor's machine",
  hf_feature: "the editor's machine knows no columns of this dataset (not cached, or not reachable)",
  hf_split: "the editor's machine knows no splits of this dataset (not cached, or not reachable)",
  hf_metadata: "the editor's machine knows no columns of this dataset (not cached, or not reachable)",
  plugin: "the editor's machine has nothing to list here",
};

/** The editor's file browser for a `directory` or `file` field, starting where the field points (`pickers.browseStart`). */
function BrowseButton({
  picker,
  param,
  draft,
  onPick,
}: {
  picker: "directory" | "file";
  param: string;
  draft: string;
  onPick: (path: string) => void;
}) {
  const { browse } = useEditor();
  return (
    <button
      type="button"
      className="picker-browse nodrag"
      data-browse={param}
      title={picker === "directory" ? "choose a folder on the editor's machine" : "choose a file on the editor's machine"}
      onClick={() => void browse(picker, browseStart(picker, draft)).then((path) => path !== null && onPick(path))}
    >
      …
    </button>
  );
}

/** What the server is asked for a lookup's list. */
function fetchFor(picker: LookupKind, values: Record<string, Json | undefined>, tag: string): Promise<Listed> {
  if (picker === "plugin") return pluginLookup(tag, pluginValues(values));
  const dataset = hfDataset(values);
  if (picker === "hf_dataset") return hfDatasets();
  if (picker === "hf_split") return hfSplits(dataset.path, dataset.name);
  return hfFeatures(dataset.path, dataset.name, dataset.split);
}

interface LookupPickerProps {
  picker: LookupKind;
  /** A plugin list's tag (`pickers.pluginTag`); empty for the HuggingFace lists. */
  tag?: string;
  param: string;
  values: Record<string, Json | undefined>;
  label: string;
  /** The entries offered, from the server's list (the list itself by default). */
  choices?: (listed: string[]) => string[];
  onPick: (value: string) => void;
}

/** Where a lookup is: nothing to ask yet (no dataset named), not asked, on its way, answered, or failed. */
type LookupState = "unnamed" | "idle" | "loading" | "ready" | "failed";

/**
 * A list of what the editor's machine knows, beside a field: the cached datasets, the named dataset's columns or splits.
 * It is asked when the person reaches for it (the pointer over it, or the focus on it) — never as a canvas draws: a
 * column lookup can open a dataset on the editor's machine, which nobody asked for by opening a graph — and once per
 * question for the page's life (`pickers.cachedLookup`). An answer with nothing in it, or a failed one, says why — and
 * a list that failed asks again the next time the person reaches for it.
 * `data-picker-state` is where it is (`LookupState`), `data-picker-count` how many entries it offers.
 */
function LookupPicker({ picker, tag = "", param, values, label, choices, onPick }: LookupPickerProps) {
  const key = picker === "plugin" ? pluginLookupKey(tag, values) : lookupKey(picker, hfDataset(values));
  const [asked, setAsked] = useState(false);
  /** Bumped when the person reaches for a list whose last lookup FAILED: the effect asks the editor again. */
  const [attempt, setAttempt] = useState(0);
  const [listed, setListed] = useState<{ key: string | undefined; names: string[]; reason: string; error: string | null }>({
    key: undefined,
    names: [],
    reason: "",
    error: null,
  });
  useEffect(() => {
    if (!asked || key === undefined) return;
    let live = true;
    cachedLookup(key, () => fetchFor(picker, values, tag)).then(
      (answer) => live && setListed({ key, names: answer.names, reason: answer.reason, error: null }),
      (exc: unknown) => live && setListed({ key, names: [], reason: "", error: errorMessage(exc) }),
    );
    return () => {
      live = false;
    };
  }, [asked, key, picker, attempt]); // `values` only spell the question, and `key` names it
  const answered = key !== undefined && listed.key === key;
  const state: LookupState =
    key === undefined ? "unnamed" : !asked ? "idle" : !answered ? "loading" : listed.error !== null ? "failed" : "ready";
  const names = answered ? listed.names : [];
  const offered = choices ? choices(names) : names;
  let note: string | undefined;
  if (state === "unnamed") note = UNNAMED[picker];
  else if (state === "loading") note = "asking the editor…";
  else if (state === "failed") note = listed.error ?? "the editor did not answer";
  // the server's own reason first ("no datasets cached at <the cache it reads>"), else what an empty list means here
  else if (state === "ready" && offered.length === 0) note = listed.reason.trim() || NOTHING_FOUND[picker];
  // Reaching for the list asks it; reaching for it again after a failure asks again (`cachedLookup` forgot the failure).
  // Measured before: the effect depended on nothing that changed, so a list that failed once never asked again.
  const ask = () => {
    setAsked(true);
    if (state !== "failed") return;
    setListed((was) => ({ ...was, key: undefined })); // asking again: "loading" until the editor answers
    setAttempt((count) => count + 1);
  };
  return (
    <select
      className="picker nodrag"
      data-picker={picker}
      data-picker-for={param}
      data-picker-state={state}
      data-picker-count={offered.length}
      title={note || `${offered.length} to pick from`}
      disabled={state === "unnamed" && offered.length === 0}
      value=""
      onPointerEnter={ask}
      onFocus={ask}
      onMouseDown={ask}
      onChange={(event) => {
        if (event.target.value !== "") onPick(event.target.value);
      }}
    >
      <option value="">{label}</option>
      {offered.map((name) => (
        <option key={name} value={name}>
          {name}
        </option>
      ))}
      {note && state !== "unnamed" && (
        <option value="" disabled>
          {note}
        </option>
      )}
    </select>
  );
}

/**
 * `metadata_features`: the columns as a comma-separated text (`*` alone for every column but input/target, empty for none)
 * and "add column…", which appends the column picked (`*` replaces the list).
 */
function MetadataField({
  param,
  values,
  commit,
}: {
  param: Param;
  values: Record<string, Json | undefined>;
  commit: (value: Json) => Promise<boolean>;
}) {
  const truth = metadataText(param.value);
  const [draft, setDraft] = useState(truth);
  useEffect(() => setDraft(truth), [truth]);
  return (
    <span className="picked" data-picked="hf_metadata">
      <input
        type="text"
        className="nodrag"
        data-param={param.name}
        placeholder="no metadata columns"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={blurOnEnter}
        onBlur={() => void commit(parseMetadata(draft))}
      />
      <LookupPicker
        picker="hf_metadata"
        param={param.name}
        values={{ ...values, metadata_features: param.value }}
        label={LOOKUP_LABELS.hf_metadata}
        choices={(columns) => metadataChoices(columns, { ...values, metadata_features: param.value })}
        onPick={(column) => void commit(withColumn(param.value, column))}
      />
    </span>
  );
}

const endText = (value: number | undefined): string => (value === undefined ? "" : String(value));

/**
 * A `(min, max)` pair: two numbers, committed together as `[min, max]` when either is left. When the value under it
 * changes, a box the person is typing into keeps what they typed (`pickers.followPair`); the commits go out one after
 * the other, so a `[min, old max]` and the `[min, max]` typed right after reach the editor in that order.
 */
function PairWidget({
  param,
  ends,
  fallback,
  commit,
}: {
  param: Param;
  ends: [PairEnd, PairEnd];
  fallback: Json | undefined;
  commit: (value: Json) => Promise<boolean>;
}) {
  const shown = pairValue(param.value, fallback);
  const truth: [string, string] = [endText(shown?.[0]), endText(shown?.[1])];
  const key = truth.join("\u0000");
  const [draft, setDraft] = useState<[string, string]>(truth);
  /** The value the boxes were last brought up to — what tells a box the person changed from one they did not. */
  const followed = useRef<[string, string]>(truth);
  useEffect(() => {
    const before = followed.current;
    followed.current = truth;
    setDraft((now) => followPair(now, before, truth));
  }, [key]); // `key` is `truth`, spelled as one string
  /** The commits on their way, in order. */
  const sending = useRef<Promise<unknown>>(Promise.resolve());
  const leave = () => {
    const sent: [string, string] = [draft[0], draft[1]];
    const numbers = sent.map((text) => Number(text));
    const fits = numbers.every(
      (value, i) => sent[i].trim() !== "" && Number.isFinite(value) && (ends[i].type === "number" || Number.isInteger(value)),
    );
    if (!fits) return setDraft((now) => followPair(now, sent, followed.current));
    sending.current = sending.current.then(async () => {
      // refused: the boxes still showing what was sent go back to the value the editor holds
      if (!(await commit(numbers))) setDraft((now) => followPair(now, sent, followed.current));
    });
  };
  return (
    <span className="pair" data-param={param.name} title={`${param.name}: (min, max)`}>
      {ends.map((end, i) => (
        <input
          key={i}
          type="number"
          className="nodrag"
          data-pair-end={i === 0 ? "min" : "max"}
          aria-label={`${param.name} ${i === 0 ? "min" : "max"}`}
          value={draft[i]}
          step={end.type === "integer" ? 1 : "any"}
          min={end.minimum}
          max={end.maximum}
          onChange={(event) => setDraft((was) => (i === 0 ? [event.target.value, was[1]] : [was[0], event.target.value]))}
          onKeyDown={blurOnEnter}
          onBlur={leave}
        />
      ))}
    </span>
  );
}
