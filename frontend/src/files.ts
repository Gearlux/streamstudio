/**
 * Path arithmetic for the server file dialog (`dialogs/FileDialog.tsx`). Open, Save As, Insert steps file… and Move
 * to its own file… browse `GET api/files` — the machine the EDITOR runs on, not the browser's (an upload would copy a
 * file; the editor edits one in place) — so every path here is that machine's absolute POSIX path.
 */

/** `dir` joined with `name`; a typed absolute `name` is taken as it is. */
export function joinPath(dir: string, name: string): string {
  if (name.startsWith("/")) return name;
  return `${dir.replace(/\/+$/, "")}/${name}`;
}

/** The folder of `path` (`/` for a file at the root). */
export function dirName(path: string): string {
  const cut = path.replace(/\/+$/, "").lastIndexOf("/");
  return cut <= 0 ? "/" : path.slice(0, cut);
}

/** The last segment of `path`. */
export const baseName = (path: string): string => path.replace(/\/+$/, "").split("/").pop() ?? path;

/** A graph's file name: the typed name, with `.yaml` added unless it already ends in `.yaml` or `.yml`. */
export function yamlName(name: string): string {
  const trimmed = name.trim();
  return /\.ya?ml$/.test(trimmed) ? trimmed : `${trimmed}.yaml`;
}

/** Move to its own file…'s default: `<subgraph>.steps.yaml`, from the inner part of a qualified name, file-safe. */
export function stepsFileName(node: string): string {
  const own = node.split("/").pop() ?? node;
  return `${own.replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/_+$/, "")}.steps.yaml`;
}
