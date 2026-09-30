/**
 * A question with an answer: a name to type (Save as template…, a group's new title) or a yes/no (Revert, Delete
 * template). Escape cancels; the confirm button is `data-ask-confirm`, the field `data-ask-input`. Enter in the field
 * confirms; on a yes/no Enter presses the focused button — Confirm, except for a question that cannot be undone
 * (`danger`), where Cancel has the focus (P10: measured before, the recovery's Discard had it, and one stray Enter
 * deleted the kept work).
 */
import { useState } from "react";
import { Modal } from "./Modal";

export interface AskRequest {
  title: string;
  /** What the question is about, in a sentence. */
  message?: string;
  /** The field's label; absent for a yes/no question. */
  label?: string;
  value?: string;
  /** An extra yes/no (e.g. "replace a template of the same name"). */
  option?: string;
  confirm: string;
  /** The confirm button is red (a delete, a revert). */
  danger?: boolean;
}

export interface AskAnswer {
  value: string;
  option: boolean;
}

export function AskDialog({ request, onDone }: { request: AskRequest; onDone: (answer: AskAnswer | null) => void }) {
  const [value, setValue] = useState(request.value ?? "");
  const [option, setOption] = useState(false);
  const asking = request.label !== undefined;
  const ok = !asking || value.trim() !== "";
  const confirm = () => ok && onDone({ value: value.trim(), option });
  return (
    <Modal
      title={request.title}
      kind="ask"
      onCancel={() => onDone(null)}
      footer={
        <>
          {/* a question that cannot be undone keeps Cancel focused: one stray Enter never destroys anything (P10) */}
          <button type="button" className="button" data-ask-cancel autoFocus={!asking && request.danger} onClick={() => onDone(null)}>
            Cancel
          </button>
          <button
            type="button"
            className={`button ${request.danger ? "danger" : "primary"}`}
            data-ask-confirm
            disabled={!ok}
            autoFocus={!asking && !request.danger}
            onClick={confirm}
          >
            {request.confirm}
          </button>
        </>
      }
    >
      {request.message && <p className="modal-message">{request.message}</p>}
      {asking && (
        <label className="modal-field">
          <span>{request.label}</span>
          <input
            autoFocus
            data-ask-input
            value={value}
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && confirm()}
          />
        </label>
      )}
      {request.option && (
        <label className="modal-check">
          <input type="checkbox" data-ask-option checked={option} onChange={(event) => setOption(event.target.checked)} />
          {request.option}
        </label>
      )}
    </Modal>
  );
}
