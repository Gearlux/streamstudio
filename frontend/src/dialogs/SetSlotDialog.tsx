/**
 * Set source… (`slots.ts`): a class for a slot, then its values — written as ONE marker into the slot.
 *
 *     Set the source of stream                         Set the source of stream — HuggingFaceSource
 *       [search…                         ]               HuggingFaceSource goes into the empty source slot.
 *       HuggingFaceSource   source  recordstream…  →     path    [ylecun/mnist      ] [cached…]
 *       ImageFolderSource   source  recordstream…        split   [train             ] [splits…]
 *       …                                                [Back]            [Cancel] [Set source]
 *
 * The list is every class the slot's type takes (the model's meet rule — the sources, and the engines), sources first;
 * the form is the class's constructor schema, through the ONE widget (`ParamWidget`) with its pickers — a folder field's
 * file browser, a HuggingFace source's datasets, columns and splits. A value is held by the dialog until Set; only the
 * values changed from the constructor's defaults are sent. Set waits while a required parameter is empty. A refusal
 * stays in the dialog in the server's words, with everything typed kept; while Set is on its way both buttons wait.
 *
 * Hooks: `data-dialog="set-slot"`, `data-slot-node` / `data-slot-name` (on the body), `data-slot-step` (`pick` | `form`),
 * `data-slot-search`, `data-slot-list`, `data-slot-class=<class path>` per row, `data-slot-chosen` (the class of the
 * form), `data-slot-effect`, `data-slot-missing` (the required parameters still empty), `data-slot-refused`,
 * `data-slot-busy`, `data-slot-back`, `data-slot-cancel`, `data-slot-confirm`; each field's `data-param` is the widget's.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { NodeSpec } from "../api";
import { ParamWidget } from "../canvas/ParamWidget";
import type { Json, Model, Supertypes } from "../model";
import { plainDoc } from "../rst";
import { draftParams, draftValues, missingRequired, slotCandidates, slotEffect, slotParams, slotTitle, type Slot } from "../slots";
import { PALETTE_ROWS } from "../steps";
import { Modal } from "./Modal";

export interface SetSlotDialogProps {
  slot: Slot;
  /** `GET api/palette`, or `null` while it loads. */
  specs: NodeSpec[] | null;
  types: Supertypes;
  /** The level the slot's node is drawn on — what says what fills the slot now. */
  model: Model;
  /** Write it: `null` once written (the dialog is closed then), else the refusal in the server's words. */
  onSet: (classPath: string, params: Record<string, Json>) => Promise<string | null>;
  onCancel: () => void;
}

/** The first sentence of a class's documentation — what a row says of it. */
const firstLine = (doc: string): string => {
  const text = plainDoc(doc).trim();
  const stop = text.search(/(?<=[.!?])\s/);
  return stop < 0 ? text : text.slice(0, stop);
};

export function SetSlotDialog({ slot, specs, types, model, onSet, onCancel }: SetSlotDialogProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [chosen, setChosen] = useState<NodeSpec | null>(null);
  const [typed, setTyped] = useState<Record<string, Json>>({});
  const [refused, setRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const list = useRef<HTMLUListElement>(null);
  const form = useRef<HTMLDivElement>(null);
  // the highlighted class stays in view as the arrows move it (measured before: the 14th ArrowDown was below the list)
  useEffect(() => {
    list.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [active]);
  // a class picked: the keyboard goes to its first field (else Set) — measured before: the search box went with the list
  // and the focus fell to the page, where Escape and Tab did nothing
  useEffect(() => {
    const here = form.current;
    if (chosen === null || here === null) return;
    const field = here.querySelector<HTMLElement>("input[data-param], select[data-param], textarea[data-param]");
    (field ?? here.closest("[data-dialog]")?.querySelector<HTMLElement>("[data-slot-confirm]"))?.focus();
  }, [chosen]);

  const candidates = useMemo(() => (specs === null ? [] : slotCandidates(specs, slot.type, types, query)), [specs, slot.type, types, query]);
  const shown = candidates.slice(0, PALETTE_ROWS);
  const params = useMemo(() => (chosen === null ? [] : draftParams(chosen)), [chosen]);
  const values = draftValues(params, typed);
  const missing = missingRequired(params, typed);

  const choose = (spec: NodeSpec) => {
    setChosen(spec);
    setTyped({});
    setRefused(null);
  };
  const back = () => {
    setChosen(null);
    setRefused(null);
  };
  const cancel = () => {
    if (!busy) onCancel();
  };
  const set = async () => {
    if (chosen === null || missing.length > 0 || busy) return;
    setBusy(true);
    setRefused(null);
    const said = await onSet(chosen.class_path, slotParams(params, typed));
    if (said === null) return; // written: the dialog is gone
    setRefused(said);
    setBusy(false);
  };
  const onSearchKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && shown[active]) choose(shown[active]);
    else if (event.key === "ArrowDown") setActive((index) => Math.min(index + 1, shown.length - 1));
    else if (event.key === "ArrowUp") setActive((index) => Math.max(index - 1, 0));
    else return;
    event.preventDefault();
  };

  const confirm = `Set ${slot.slot}`;
  return (
    <Modal
      title={chosen === null ? slotTitle(slot) : `${slotTitle(slot)} — ${chosen.name}`}
      kind="set-slot"
      wide
      onCancel={cancel}
      footer={
        <>
          {chosen !== null && (
            <button type="button" className="button" data-slot-back disabled={busy} onClick={back} style={{ marginRight: "auto" }}>
              Back
            </button>
          )}
          <button type="button" className="button" data-slot-cancel disabled={busy} onClick={cancel}>
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            data-slot-confirm
            disabled={chosen === null || missing.length > 0 || busy}
            onClick={() => void set()}
          >
            {confirm}
          </button>
        </>
      }
    >
      <div
        className="set-slot"
        data-slot-node={slot.node}
        data-slot-name={slot.slot}
        data-slot-step={chosen === null ? "pick" : "form"}
        data-slot-busy={busy ? "true" : "false"}
      >
        {chosen === null ? (
          <>
            <input
              autoFocus
              className="set-slot-search"
              data-slot-search
              placeholder={`search the classes a ${slot.type} slot takes…`}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setActive(0);
              }}
              onKeyDown={onSearchKey}
            />
            <p className="modal-hint">
              {specs === null
                ? "loading the catalogue…"
                : `${shown.length} of ${candidates.length} classes whose output a ${slot.type} slot takes${
                    slot.filled === undefined ? "" : ` · replaces ${slot.filled}`
                  }`}
            </p>
            <ul ref={list} className="set-slot-list" data-slot-list role="listbox">
              {shown.map((spec, index) => (
                <li
                  key={spec.class_path}
                  role="option"
                  aria-selected={index === active}
                  className={index === active ? "active" : undefined}
                  data-slot-class={spec.class_path}
                  title={spec.problem || plainDoc(spec.doc)}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => choose(spec)}
                >
                  <b>{spec.name}</b>
                  <span className={`badge kind-${spec.kind}`}>{spec.kind}</span>
                  <span className="set-slot-path">{spec.class_path}</span>
                  <span className="set-slot-doc">{firstLine(spec.doc)}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <div ref={form} className="set-slot-form" data-slot-chosen={chosen.class_path}>
            <p className="modal-message" data-slot-effect>
              {slotEffect(slot, model, chosen.name)}
            </p>
            {params.length === 0 && <p className="dim">{chosen.name} takes no parameter.</p>}
            {params.map((param) => (
              <label className="field" key={param.name} title={param.doc}>
                <span>
                  {param.name}
                  {param.required && <b title="the constructor requires it"> *</b>}
                </span>
                <ParamWidget
                  param={{ ...param, value: values[param.name] ?? null }}
                  schema={param.schema}
                  values={values}
                  onCommit={async (value) => {
                    setTyped((all) => ({ ...all, [param.name]: value }));
                    setRefused(null);
                    return true;
                  }}
                />
              </label>
            ))}
            {missing.length > 0 && (
              <p className="modal-hint" data-slot-missing={missing.join(",")}>
                {`${chosen.name} needs ${missing.join(", ")} — fill ${missing.length === 1 ? "it" : "them"} in to set it.`}
              </p>
            )}
          </div>
        )}
        {refused !== null && (
          <p className="file-error" data-slot-refused>
            {refused}
          </p>
        )}
      </div>
    </Modal>
  );
}
