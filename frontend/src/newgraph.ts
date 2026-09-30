/**
 * File ▸ New… writes the file at once (DESIGN3 — the user, 2026-09-29: "New ... by mistake should not happen. It
 * should ask to save"). There is never a graph that exists only in the editor: the dialog asks the NAME and the
 * FOLDER, shows the file that Create will write, and Create writes it (`POST api/graphs`) and opens it with nothing
 * unsaved.
 *
 *     New flow graph
 *       Name     [ fresh                      ]
 *       Folder   [ /Users/gert/streamstudio-try ] [Browse…]
 *       → /Users/gert/streamstudio-try/fresh.yaml
 *     [Cancel]  [Create]
 *
 * The folder starts as the one the editor was started on (`GET api/workspace`'s `folder`). The name is sent as typed
 * (less a `.yaml` the person added): the SERVER's name rule refuses a bad one, in its own words, which the dialog
 * shows — a second copy of the rule here would drift from it.
 */
import { joinPath } from "./files";
import type { NewGraphKind } from "./model";

const TITLES: Record<NewGraphKind, string> = {
  flow: "New flow graph",
  pipeline: "New pipeline",
  source: "New source graph",
};

/** The dialog's title: the kind the New ▸ entry picked. */
export const newGraphTitle = (kind: NewGraphKind): string => TITLES[kind];

/** A folder as compared: trimmed, without trailing slashes (the root stays `/`); `""` for no folder. */
function folderKey(path: string): string {
  const trimmed = path.trim();
  return trimmed === "" ? "" : trimmed.replace(/\/+$/, "") || "/";
}

/** Whether two spellings name the same folder (`/w/` and `/w`); no folder is never the same as any. */
export function sameFolder(a: string, b: string): boolean {
  const key = folderKey(a);
  return key !== "" && key === folderKey(b);
}

/** What Create sends (`POST api/graphs {name, kind, dir?}`) and the file it writes. */
export interface NewTarget {
  /** The name typed, less a `.yaml` / `.yml` the person added (the server adds `.yaml`). */
  name: string;
  /** The folder, when it is not the editor's own (absent: the server's graph folder). */
  dir?: string;
  /** The file Create writes — what the dialog shows. */
  path: string;
}

/**
 * The target of a typed name in a typed folder, against the folder the editor was started on (`""` when unknown); `null`
 * while there is no name or no folder — Create stays greyed.
 */
export function newTarget(typed: string, folder: string, editorFolder: string): NewTarget | null {
  const name = typed.trim().replace(/\.ya?ml$/, "");
  const dir = folderKey(folder);
  if (name === "" || dir === "") return null;
  const path = joinPath(dir, `${name}.yaml`);
  return sameFolder(dir, editorFolder) ? { name, path } : { name, dir, path };
}
