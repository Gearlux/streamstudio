/**
 * The file browser on the EDITOR's machine (`GET api/files`) — Open…, Save As…, Insert steps file…, Move to its own
 * file… and New…'s folder all use it. It is not an upload: the editor edits a file in place on the machine it runs
 * on, so the browser's own file picker (which hands over a copy) is the wrong tool (decision 2 of the scope).
 *
 * Four modes: `open` picks a `.yaml` (a click selects, a double-click opens), `save` picks a folder and types a name
 * (a name that exists turns the button into Replace, and the answer says to overwrite), `folder` picks a folder, and
 * `file` picks ANY file (a `file` field's chooser, `pickers.ts` — the listing is `show=all`). A click on a folder enters
 * it; ↑ goes to the one above; the path field takes a typed folder (Enter). A folder the server cannot list shows the
 * server's message and keeps the last listing — and when the folder the dialog was opened on cannot be listed (a field
 * naming a folder that is not there yet), the graph folder is listed under the message instead of nothing.
 *
 * The path field shows the END of the path — the folder's own name — whenever a folder is shown: a deep path is
 * longer than the field, and scrolled to its start the name was off to the right (measured: 907 px of text in 566).
 *
 * Above the path, a row of shortcut buttons — the graph folder, then `HOME`, `PROJECT_ROOT`, `DATA_ROOT`,
 * `MODEL_ROOT`, `RUN_DIR`, each only while the editor's machine sets it to a folder (the listing's `places`, decided
 * by the server); the button of the folder shown is pressed. In `save` and `folder` mode, New folder makes a folder
 * inside the one shown (`POST api/files/folder`) and enters it — a refusal is the server's line, the typed name kept;
 * Escape closes the name field, not the dialog (user request 2026-10-28).
 *
 * Hooks: `data-dialog="file"`, `data-file-mode`, `data-file-dir` (the folder shown), `data-file-path` (the path
 * field), `data-file-entry` + `data-file-kind` on a row, `data-file-name` (the name field), `data-file-up`,
 * `data-file-confirm`, `data-file-place` (a shortcut, `aria-pressed`), `data-file-new-folder` (the button) and
 * `data-file-new-folder-name` (its field).
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { errorMessage, listFiles, makeFolder, type FileListing, type FileShow } from "../api";
import { joinPath, yamlName } from "../files";
import { Modal } from "./Modal";

export type FileMode = "open" | "save" | "folder" | "file";

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
  /** New folder's name field: `null` while it is closed. */
  const [folderName, setFolderName] = useState<string | null>(null);
  const normalise = request.normalise ?? yamlName;
  const pathField = useRef<HTMLInputElement>(null);
  const show: FileShow = request.mode === "file" ? "all" : "graphs";
  const makesFolders = request.mode === "save" || request.mode === "folder";

  /** List `dir`: `null` once listed, else the server's message (shown). `keep` is a message to show over the listing. */
  const go = useCallback(
    async (dir?: string, keep?: string): Promise<string | null> => {
      try {
        const next = await listFiles(dir, show);
        setListing(next);
        setTypedDir(next.dir);
        setError(keep ?? null);
        setPicked(null);
        return null;
      } catch (exc) {
        const said = errorMessage(exc);
        setError(said);
        return said;
      }
    },
    [show],
  );

  useEffect(() => {
    void go(request.dir).then((said) => {
      // the folder asked for cannot be listed (a field naming one not there yet): the editor's folder, under the reason
      if (said !== null && request.dir !== undefined) void go(undefined, `${said} — showing the editor's folder instead`);
    });
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
    if (request.mode === "open" || request.mode === "file") return picked;
    return name.trim() === "" ? null : joinPath(listing.dir, normalise(name));
  };
  const exists =
    request.mode === "save" && listing !== null && name.trim() !== ""
      ? listing.entries.some((entry) => entry.kind === "yaml" && entry.name === normalise(name))
      : false;
  const chosen = target();

  /** Make the typed folder inside the one shown and enter it; a refusal stays on screen with the name kept. */
  const newFolder = async () => {
    if (listing === null || folderName === null) return;
    try {
      const next = await makeFolder(listing.dir, folderName);
      setListing(next);
      setTypedDir(next.dir);
      setError(null);
      setPicked(null);
      setFolderName(null);
    } catch (exc) {
      setError(errorMessage(exc));
    }
  };
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
            {chosen ?? (request.mode === "open" ? "pick a .yaml file" : request.mode === "file" ? "pick a file" : "")}
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
        {listing !== null && (
          <div className="file-places">
            {listing.places.map((place) => (
              <button
                key={place.name}
                type="button"
                className="button file-place"
                data-file-place={place.name}
                title={place.path}
                aria-pressed={place.path === listing.dir}
                onClick={() => void go(place.path)}
              >
                {place.name}
              </button>
            ))}
          </div>
        )}
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
          {makesFolders && (
            <button
              type="button"
              className="button"
              data-file-new-folder
              title="make a folder inside the one shown"
              disabled={listing === null}
              onClick={() => setFolderName((open) => open ?? "")}
            >
              New folder
            </button>
          )}
        </div>
        {folderName !== null && (
          <label className="modal-field file-new-folder">
            <span>New folder</span>
            <input
              autoFocus
              data-file-new-folder-name
              spellCheck={false}
              value={folderName}
              onChange={(event) => setFolderName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.stopPropagation();
                  void newFolder();
                } else if (event.key === "Escape") {
                  event.stopPropagation(); // the name field closes, the dialog stays
                  setFolderName(null);
                }
              }}
            />
          </label>
        )}
        {error && <div className="file-error">{error}</div>}
        <ul className="file-list" role="listbox">
          {listing === null && !error && <li className="file-empty">reading the folder…</li>}
          {listing !== null && listing.entries.length === 0 && (
            <li className="file-empty">{request.mode === "file" ? "this folder is empty" : "this folder holds no folder and no .yaml"}</li>
          )}
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
                else if (request.mode === "open" || request.mode === "file") setPicked(entry.path);
                else if (request.mode === "save") setName(entry.name);
              }}
              onDoubleClick={() =>
                entry.kind !== "dir" && (request.mode === "open" || request.mode === "file") && onDone({ path: entry.path, overwrite: false })
              }
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
