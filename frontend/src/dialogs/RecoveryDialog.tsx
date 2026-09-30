/**
 * The question the page asks before anything else when the last session stopped with unsaved work (DESIGN2 §1):
 *
 *     Unsaved work from the last session (29 Sep 13:25)
 *       demo.yaml             changed
 *       radio_view.yaml       changed — the file on disk changed after the editor stopped
 *       ../other/flow.yaml    changed — the file was deleted or moved away after the editor stopped; Save writes it again
 *     [Restore unsaved work]   [Open the saved files]
 *
 * A choice is required: Escape and the backdrop do nothing (leaving the question unanswered would load the files from
 * disk while the kept work waited), and the rest of the page is out of reach while it is asked (the app makes it
 * inert; the shell keeps Tab inside). A recovery file the server could not read is shown in the server's words (naming
 * the file) instead of the rows; only "Open the saved files" is offered then, which moves it aside — never deletes it.
 * A file is named by its way from the workspace folder (`../other/flow.yaml`), so two of one name can be told apart.
 *
 * Hooks: `data-dialog="recovery"` (the shell), `data-recovery-graph=<name>` per row with `data-recovery-disk`
 * (`unchanged` | `changed` | `gone` | `unreadable`) and `data-changed-on-disk`, `data-recovery-error` (an unreadable
 * file), `data-recovery-refused` (a Restore or discard the server refused), `data-recovery-action="restore"` /
 * `"discard"` on the buttons.
 */
import { recoveryError, recoveryRows, recoveryTitle, type Recovery } from "../recovery";
import { Modal } from "./Modal";

export interface RecoveryDialogProps {
  recovery: Recovery;
  /** The folder the editor serves: the files are named by their way from it. */
  folder: string;
  /** A Restore or a discard the server refused, in its words; the dialog stays up. */
  refused: string | null;
  /** A request is on its way: both buttons wait. */
  busy: boolean;
  onRestore: () => void;
  onDiscard: () => void;
}

export function RecoveryDialog({ recovery, folder, refused, busy, onRestore, onDiscard }: RecoveryDialogProps) {
  const rows = recoveryRows(recovery, folder);
  const error = recoveryError(recovery);
  const unreadable = error !== undefined;
  const said = (error ?? "").replace(/\.$/, "");
  const aside = recovery.aside ?? "a file beside it";
  return (
    <Modal
      title={recoveryTitle(recovery)}
      kind="recovery"
      wide
      onCancel={() => undefined /* a choice is required: see the module comment */}
      footer={
        <>
          <button
            type="button"
            className="button primary"
            data-recovery-action="restore"
            disabled={busy || unreadable || rows.length === 0}
            autoFocus={!unreadable}
            onClick={onRestore}
          >
            Restore unsaved work
          </button>
          <button type="button" className="button" data-recovery-action="discard" disabled={busy} autoFocus={unreadable} onClick={onDiscard}>
            Open the saved files
          </button>
        </>
      }
    >
      {unreadable ? (
        <p className="modal-message" data-recovery-error>
          The unsaved work of the last session could not be read: {said}. Open the saved files moves that file aside to{" "}
          {aside} — nothing is deleted.
        </p>
      ) : (
        <>
          <table className="recovery-rows">
            <tbody>
              {rows.map((row, index) => (
                <tr
                  key={`${row.name}-${index}`}
                  data-recovery-graph={row.name}
                  data-recovery-disk={row.disk}
                  data-changed-on-disk={row.changedOnDisk ? "true" : "false"}
                >
                  <td className="recovery-file">{row.file}</td>
                  <td className={row.disk === "unchanged" ? "recovery-note" : "recovery-note warn"}>{row.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="modal-hint">
            Restore opens them with those changes, marked ● — nothing is written until you Save (the undo history of the
            last session is not kept). A file that holds other unsaved changes opens beside them. Open the saved files
            drops the changes and shows the files as they are on disk.
          </p>
        </>
      )}
      {refused !== null && (
        <p className="file-error" data-recovery-refused>
          {refused}
        </p>
      )}
    </Modal>
  );
}
