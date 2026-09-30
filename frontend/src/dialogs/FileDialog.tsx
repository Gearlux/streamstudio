/**
 * The file browser on the EDITOR's machine (`GET api/files`) — Open…, Save As…, Insert steps file…, Move to its own
 * file… and New…'s folder all use it. It is not an upload: the editor edits a file in place on the machine it runs
 * on, so the browser's own file picker (which hands over a copy) is the wrong tool (decision 2 of the scope).
 *
 * Three modes: `open` picks a `.yaml` (a click selects, a double-click opens), `save` picks a folder and types a name
 * (a name that exists turns the button into Replace, and the answer says to overwrite), `folder` picks a folder.
 * A click on a folder enters it; ↑ goes to the one above; the path field takes a typed folder (Enter). A folder the
 * server cannot list shows the server's message and keeps the last listing.
 *
 * The path field shows the END of the path — the folder's own name — whenever a folder is shown: a deep path is
 * longer than the field, and scrolled to its start the name was off to the right (measured: 907 px of text in 566).
 *
 * Hooks: `data-dialog="file"`, `data-file-mode`, `data-file-dir` (the folder shown), `data-file-path` (the path
 * field), `data-file-entry` + `data-file-kind` on a row, `data-file-name` (the name field), `data-file-up`,
 * `data-file-confirm`.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { errorMessage, listFiles, type FileListing } from "../api";
import { joinPath, yamlName } from "../files";
import { Modal } from "./Modal";

export type FileMode = "open" | "save" | "folder";

export interface FileRequest {
  mode: FileMode;
  title: string;
  confirm: string;
  /** The folder to start in; absent is the graph folder. */
  dir?: string;
  /** Save mode: the name to start from. */
  name?: string;
  /** Save mode: the suffix a typed name gets (`.yaml` unless it has one). */
  normalise?: (name: string) => string;
}

export interface FileAnswer {
  path: string;
  /** Save mode: the file exists and the person chose to replace it. */
  overwrite: boolean;
}

const FolderIcon = () => (
  <svg className="file-icon" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M1.5 3.5h4l1.5 1.5h7.5v8h-13z" fill="currentColor" opacity=".85" />
  </svg>
);

const FileIcon = () => (
  <svg className="file-icon" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M3.5 1.5h6l3 3v10h-9z" fill="none" stroke="currentColor" strokeWidth="1.2" />
    <path d="M5.5 8h5M5.5 10.5h5" stroke="currentColor" strokeWidth="1" />
  </svg>
);

export function FileDialog({ request, onDone }: { request: FileRequest; onDone: (answer: FileAnswer | null) => void }) {
  const [listing, setListing] = useState<FileListing | null>(null);
  const [typedDir, setTypedDir] = useState(request.dir ?? "");
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [name, setName] = useState(request.name ?? "");
  const normalise = request.normalise ?? yamlName;
  const pathField = useRef<HTMLInputElement>(null);

  const go = useCallback(async (dir?: string) => {
    try {
      const next = await listFiles(dir);
      setListing(next);
      setTypedDir(next.dir);
      setError(null);
      setPicked(null);
    } catch (exc) {
      setError(errorMessage(exc));
    }
  }, []);

  useEffect(() => {
    void go(request.dir);
  }, [go, request.dir]);

  // a folder shown: its name, at the end of the path, is what shows in the field
  const shownDir = listing?.dir;
  useLayoutEffect(() => {
    const field = pathField.current;
    if (field && shownDir !== undefined) field.scrollLeft = field.scrollWidth;
  }, [shownDir]);

  const target = (): string | null => {
    if (listing === null) return null;
    if (request.mode === "folder") return listing.dir;
    if (request.mode === "open") return picked;
    return name.trim() === "" ? null : joinPath(listing.dir, normalise(name));
  };
  const exists =
    request.mode === "save" && listing !== null && name.trim() !== ""
      ? listing.entries.some((entry) => entry.kind === "yaml" && entry.name === normalise(name))
      : false;
  const chosen = target();
  const confirm = () => chosen !== null && onDone({ path: chosen, overwrite: exists });

  return (
    <Modal
      title={request.title}
      kind="file"
      wide
      onCancel={() => onDone(null)}
      footer={
        <>
          <span className="file-chosen" title={chosen ?? ""}>
            {chosen ?? (request.mode === "open" ? "pick a .yaml file" : "")}
          </span>
          <button type="button" className="button" data-file-cancel onClick={() => onDone(null)}>
            Cancel
          </button>
          <button
            type="button"
            className={`button ${exists ? "danger" : "primary"}`}
            data-file-confirm
            disabled={chosen === null}
            onClick={confirm}
          >
            {exists ? "Replace" : request.confirm}
          </button>
        </>
      }
    >
      <div className="file-dialog" data-file-mode={request.mode} data-file-dir={listing?.dir ?? ""}>
        <div className="file-path">
          <button
            type="button"
            className="button icon"
            data-file-up
            title="the folder above"
            disabled={!listing?.parent}
            onClick={() => listing?.parent && void go(listing.parent)}
          >
            ↑
          </button>
          <input
            ref={pathField}
            className="file-path-input"
            data-file-path
            spellCheck={false}
            value={typedDir}
            onChange={(event) => setTypedDir(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && void go(typedDir)}
            title="type a folder and press Enter"
          />
        </div>
        {error && <div className="file-error">{error}</div>}
        <ul className="file-list" role="listbox">
          {listing === null && !error && <li className="file-empty">reading the folder…</li>}
          {listing !== null && listing.entries.length === 0 && <li className="file-empty">this folder holds no folder and no .yaml</li>}
          {listing?.entries.map((entry) => (
            <li
              key={entry.path}
              role="option"
              aria-selected={picked === entry.path || (request.mode === "save" && entry.name === normalise(name))}
              className={`file-entry kind-${entry.kind}`}
              data-file-entry={entry.name}
              data-file-kind={entry.kind}
              title={entry.path}
              onClick={() => {
                if (entry.kind === "dir") void go(entry.path);
                else if (request.mode === "open") setPicked(entry.path);
                else if (request.mode === "save") setName(entry.name);
              }}
              onDoubleClick={() => entry.kind === "yaml" && request.mode === "open" && onDone({ path: entry.path, overwrite: false })}
            >
              {entry.kind === "dir" ? <FolderIcon /> : <FileIcon />}
              <span className="file-name">{entry.name}</span>
              {entry.kind === "dir" && <span className="file-open">▸</span>}
            </li>
          ))}
        </ul>
        {request.mode === "save" && (
          <label className="modal-field">
            <span>File name</span>
            <input
              autoFocus
              data-file-name
              spellCheck={false}
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && confirm()}
            />
          </label>
        )}
        {exists && <div className="file-warning">{normalise(name)} exists in this folder — {request.confirm} replaces it.</div>}
      </div>
    </Modal>
  );
}
