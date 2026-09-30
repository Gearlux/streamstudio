/**
 * New…: the NAME and the FOLDER of a new graph, and the file Create writes (DESIGN3, `newgraph.ts`):
 *
 *     New flow graph
 *       Name     [ fresh                      ]
 *       Folder   [ /Users/gert/streamstudio-try ] [Browse…]
 *       → /Users/gert/streamstudio-try/fresh.yaml
 *     [Cancel]  [Create]
 *
 * The kind is the New ▸ entry's (a flow of named steps, a pipeline — a `Stream` with an `ops:` list — or a source graph,
 * a flow plus its contract); the server has one starter text per kind. The folder starts as the editor's own (`GET
 * api/workspace`), can be typed, and Browse… picks one with the server file dialog in its folder mode.
 *
 * Create WRITES the file at once (`POST api/graphs`) and the editor opens it with nothing unsaved. A refusal writes
 * nothing — the file exists, the folder does not, a name the server's rule refuses — and the dialog STAYS, showing the
 * server's words, with what was typed kept (the fields stay usable, so the name is fixed in place); a change to the
 * name or the folder clears it. While Create is on its way both buttons wait and Escape does nothing: the file may
 * already be written, and the answer opens it.
 *
 * Hooks: `data-dialog="new"`, `data-new-kind` (the kind, on the body), `data-new-name`, `data-new-folder` (the folder
 * field), `data-new-browse`, `data-new-path` (the file Create writes; empty while there is none), `data-new-refused`
 * (the server's refusal), `data-new-busy` (`true` while Create is on its way), `data-new-cancel`, `data-new-confirm`.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { NewGraphKind } from "../model";
import { newGraphTitle, newTarget } from "../newgraph";
import { FileDialog } from "./FileDialog";
import { Modal } from "./Modal";

const ABOUT: Record<NewGraphKind, string> = {
  flow: "named steps, each reading the record of the one before (flow:)",
  pipeline: "a Stream: a source and an ops: list, applied in order",
  source: "a flow plus its contract — the entries it reads and writes, by name",
};

export interface NewAnswer {
  name: string;
  kind: NewGraphKind;
  /** The folder chosen, when it is not the editor's own; absent is the editor's folder. */
  dir?: string;
}

export interface NewDialogProps {
  kind: NewGraphKind;
  /**
   * The folder the editor was started on (`GET api/workspace`) — where the field starts; `""` while the page does not
   * know it (the field then waits for it, and Browse… starts in the editor's folder all the same).
   */
  folder: string;
  /** Write the file: `null` once it is written and opened (the dialog is closed then), else the refusal in the server's words. */
  onCreate: (answer: NewAnswer) => Promise<string | null>;
  onCancel: () => void;
}

export function NewDialog({ kind, folder: editorFolder, onCreate, onCancel }: NewDialogProps) {
  const [name, setName] = useState("");
  const [folder, setFolder] = useState(editorFolder);
  const [browsing, setBrowsing] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const folderField = useRef<HTMLInputElement>(null);
  /** Bumped when the folder is SET (the editor's, or one browsed to): the field then shows the END of the path. */
  const [placed, setPlaced] = useState(0);

  // opened before the page learnt its folder: the field takes it when it arrives, unless something was typed
  useEffect(() => {
    if (editorFolder === "") return;
    setFolder((typed) => (typed.trim() === "" ? editorFolder : typed));
    setPlaced((count) => count + 1);
  }, [editorFolder]);

  // a deep path is longer than the field: show its end — the folder's own name — as the file dialog does
  useLayoutEffect(() => {
    const field = folderField.current;
    if (field) field.scrollLeft = field.scrollWidth;
  }, [placed, browsing]);

  const target = newTarget(name, folder, editorFolder);
  // a second Enter or click before the first answer is dropped (read from a ref: the render's `busy` may lag a frame)
  const inFlight = useRef(false);
  const create = async () => {
    if (target === null || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setRefused(null);
    const said = await onCreate({ name: target.name, kind, ...(target.dir === undefined ? {} : { dir: target.dir }) });
    if (said === null) return; // written and opened: the dialog is gone
    inFlight.current = false;
    setRefused(said);
    setBusy(false);
  };
  const cancel = () => {
    if (!busy) onCancel();
  };

  if (browsing)
    return (
      <FileDialog
        request={{ mode: "folder", title: "The folder of the new graph", confirm: "Choose", dir: folder.trim() || undefined }}
        onDone={(answer) => {
          setBrowsing(false);
          if (answer === null) return;
          setFolder(answer.path);
          setPlaced((count) => count + 1);
          setRefused(null);
        }}
      />
    );

  return (
    <Modal
      title={newGraphTitle(kind)}
      kind="new"
      wide
      onCancel={cancel}
      footer={
        <>
          <button type="button" className="button" data-new-cancel disabled={busy} onClick={cancel}>
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            data-new-confirm
            disabled={target === null || busy}
            onClick={() => void create()}
          >
            Create
          </button>
        </>
      }
    >
      <div className="new-graph" data-new-kind={kind} data-new-busy={busy ? "true" : "false"}>
        <p className="modal-hint new-about">{ABOUT[kind]}</p>
        <label className="modal-field">
          <span>Name</span>
          <input
            autoFocus
            data-new-name
            spellCheck={false}
            placeholder="demo"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setRefused(null);
            }}
            onKeyDown={(event) => event.key === "Enter" && void create()}
          />
        </label>
        <div className="modal-field">
          <span>Folder</span>
          <div className="new-folder">
            <input
              ref={folderField}
              data-new-folder
              spellCheck={false}
              placeholder="the folder the file goes to"
              title={folder}
              value={folder}
              onChange={(event) => {
                setFolder(event.target.value);
                setRefused(null);
              }}
              onKeyDown={(event) => event.key === "Enter" && void create()}
            />
            <button type="button" className="button small" data-new-browse disabled={busy} onClick={() => setBrowsing(true)}>
              Browse…
            </button>
          </div>
        </div>
        <p className="new-path" title={target?.path ?? ""}>
          <span className="dim">→</span>{" "}
          <code data-new-path>{target?.path ?? ""}</code>
          {target === null && <span className="dim">{folder.trim() === "" ? "choose a folder" : "type a name"}</span>}
        </p>
        {refused !== null && (
          <p className="file-error" data-new-refused>
            {refused}
          </p>
        )}
      </div>
    </Modal>
  );
}
