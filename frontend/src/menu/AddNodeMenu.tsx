/**
 * Add Node ▸ — the empty canvas's submenu: a search box over the whole catalogue, and below it the kinds (Sources,
 * Ops, …) each opening a list of its classes under their package names. Typing replaces the kinds with the matching
 * classes (the Nodes sidebar's search: every word, in the name, the class path or the description). A pick adds the
 * class as a step after the selected node and places it where the canvas was right-clicked.
 */
import { useMemo, useState, type KeyboardEvent } from "react";
import type { NodeSpec } from "../api";
import { plainDoc } from "../rst";
import { KIND_ORDER, kindLabel, packageOf, sidebarMatches } from "../sidebar";
import type { Entry } from "./entries";
import { MenuRows } from "./MenuList";

/** The most matches the search lists; typing more narrows. */
const MATCH_ROWS = 60;

export interface AddNodeMenuProps {
  specs: NodeSpec[] | null;
  onPick: (spec: NodeSpec) => void;
  close: () => void;
}

const classEntry = (spec: NodeSpec, onPick: (spec: NodeSpec) => void, withPackage: boolean): Entry => ({
  type: "item",
  key: spec.class_path,
  label: withPackage ? `${spec.name}  ·  ${packageOf(spec.class_path)}` : spec.name,
  hint: spec.problem || plainDoc(spec.doc) || spec.class_path,
  attrs: { "data-class": spec.class_path },
  onSelect: () => onPick(spec),
});

export function AddNodeMenu({ specs, onPick, close }: AddNodeMenuProps) {
  const [query, setQuery] = useState("");

  const kinds = useMemo((): Entry[] => {
    if (specs === null) return [{ type: "header", key: "loading", label: "loading the catalogue…" }];
    const rank = (kind: string) => {
      const index = (KIND_ORDER as readonly string[]).indexOf(kind);
      return index < 0 ? KIND_ORDER.length : index;
    };
    const present = [...new Set(specs.map((spec) => spec.kind))].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    return present.map((kind): Entry => {
      const ofKind = specs.filter((spec) => spec.kind === kind);
      const packages = [...new Set(ofKind.map((spec) => packageOf(spec.class_path)))].sort();
      return {
        type: "submenu",
        key: kind,
        label: `${kindLabel(kind)}  (${ofKind.length})`,
        attrs: { "data-kind": kind },
        items: packages.flatMap((name): Entry[] => [
          { type: "header", key: `pkg:${name}`, label: name },
          ...ofKind
            .filter((spec) => packageOf(spec.class_path) === name)
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((spec) => classEntry(spec, onPick, false)),
        ]),
      };
    });
  }, [specs, onPick]);

  const matches = useMemo((): Entry[] => {
    if (specs === null || query.trim() === "") return [];
    const found = specs.filter((spec) => sidebarMatches(spec, query));
    const shown = found.slice(0, MATCH_ROWS).map((spec) => classEntry(spec, onPick, true));
    if (found.length === 0) return [{ type: "header", key: "none", label: "no class matches" }];
    return found.length > MATCH_ROWS
      ? [...shown, { type: "header", key: "more", label: `${found.length - MATCH_ROWS} more — type to narrow` }]
      : shown;
  }, [specs, query, onPick]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    event.stopPropagation(); // the page's shortcuts (n, g, Delete…) are not for a query being typed
    if (event.key === "Escape") close();
    if (event.key === "Enter" && specs !== null) {
      const first = specs.find((spec) => query.trim() !== "" && sidebarMatches(spec, query));
      if (first) {
        close();
        onPick(first);
      }
    }
  };

  return (
    <div className="add-node" data-add-node>
      <input
        autoFocus
        className="menu-search"
        data-add-node-search
        placeholder="search every class…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="add-node-list">
        <MenuRows entries={query.trim() === "" ? kinds : matches} close={close} />
      </div>
    </div>
  );
}
