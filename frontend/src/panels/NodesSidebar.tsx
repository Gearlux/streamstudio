/**
 * The Nodes sidebar (View ▸ Nodes sidebar, the bar's Nodes button, key N): every class the catalogue describes
 * (`GET api/palette`) as a tree of folders that each fold — by PACKAGE, inside one by KIND (sources, ops, algorithms,
 * …), inside a kind by the class's `@configurable` group path (`group="fourier/numpy"`: ▸ waivefront ▸ Ops ▸ fourier
 * ▸ numpy) — each folder only when it holds something, its sub-folders before the classes that sit in it directly
 * (`sidebar.groupSpecs`). A package that declares no group shows its classes straight under the kind. An entry shows the class name, its one-line description and coloured chips for what it
 * reads and what it writes (the socket colours the canvas uses); resting the pointer on it shows a card with the whole
 * description and the constructor's parameters — asked when the card opens (`api.settingsOf`, once per class), the card
 * saying "loading the settings…" until they arrive.
 *
 * Drag an entry onto the canvas to add it where it is dropped (the canvas turns the drop point into flow coordinates),
 * or click it to add it after the selected node. Either way it is `POST …/steps` where the footer says
 * (`oplists.insertTarget`): after a flow step (`Click adds after gain`), after a selected op list member, or — on a
 * Stream with no member selected — at the end of its list (`Click adds at the end of stream.ops`). The server decides
 * whether the document can take it there, and says so in its words when it cannot. The
 * Subgraph class is not listed (the app hands `sidebar.addableSpecs`): a subgraph is MADE from steps, and the footer
 * says how.
 *
 * A description's docstring markup — ``` ``Mask`` ```, `` :class:`DirectorySink` `` — is drawn as inline code holding
 * the name (`rst.docSpans`), never as the markup. The hover card stays inside the window: when the parameters do not
 * all fit, it draws the ones that fit WHOLE and says how many it left out (`+36 more parameters`).
 *
 * Every folder starts folded; which ones are unfolded is remembered in this browser (`localStorage`, best effort), by
 * the folder's key — a package by its name, a deeper folder by its path (`waivefront/op/fourier`). A search unfolds
 * every folder on the way to a match, and clearing it puts them back as they were. Hooks: `data-nodes-sidebar`,
 * `data-nodes-search`, `data-nodes-package` (+ `aria-expanded` on its head), `data-nodes-kind` and
 * `data-nodes-folder` (the key; + `aria-expanded` on its `.nodes-folder-head`), `data-nodes-entry` (+ `data-class`),
 * `data-nodes-card` (+ `data-card-settings`: `loading` | `ready` | `refused`), `data-card-more`,
 * `data-nodes-subgraph-hint`.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { errorMessage, settingsOf, type NodeSpec, type NodeSpecWithSettings } from "../api";
import { socketColor } from "../model";
import type { InsertTarget } from "../oplists";
import { docSpans } from "../rst";
import { chips, groupSpecs, paramRows, sidebarMatches, type Chip, type Folder, type KindSection } from "../sidebar";

/** The drag payload a canvas drop reads: the class path. */
export const CLASS_MIME = "application/x-streamstudio-class";

const EXPANDED_KEY = "streamstudio.nodes.expanded";

const readExpanded = (): string[] => {
  try {
    const stored = JSON.parse(window.localStorage.getItem(EXPANDED_KEY) ?? "[]");
    return Array.isArray(stored) ? stored.filter((name): name is string => typeof name === "string") : [];
  } catch {
    return [];
  }
};

const writeExpanded = (names: string[]): void => {
  try {
    window.localStorage.setItem(EXPANDED_KEY, JSON.stringify(names));
  } catch {
    // storage may be blocked (a private window); unfolding still works for this page
  }
};

export interface NodesSidebarProps {
  specs: NodeSpec[] | null;
  /** Where a click adds (`none`: nowhere, with the reason when the page knows it). */
  target: InsertTarget;
  onAdd: (spec: NodeSpec) => void;
  onClose: () => void;
}

export function NodesSidebar({ specs, target, onAdd, onClose }: NodesSidebarProps) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string[]>(readExpanded);
  const [card, setCard] = useState<{ spec: NodeSpec; top: number } | null>(null);
  const hoverTimer = useRef<number | undefined>(undefined);
  const root = useRef<HTMLElement>(null);

  useEffect(() => () => window.clearTimeout(hoverTimer.current), []);
  // a new search re-draws the list under the pointer: the entry the card was for may be gone (no mouseleave comes)
  useEffect(() => {
    window.clearTimeout(hoverTimer.current);
    setCard(null);
  }, [query, expanded]);

  const matching = useMemo(() => (specs ?? []).filter((spec) => sidebarMatches(spec, query)), [specs, query]);
  const sections = useMemo(() => groupSpecs(matching), [matching]);
  const searching = query.trim() !== "";

  const toggle = (key: string) => {
    const next = expanded.includes(key) ? expanded.filter((k) => k !== key) : [...expanded, key];
    setExpanded(next);
    writeExpanded(next);
  };
  const isOpen = (key: string): boolean => searching || expanded.includes(key);

  const hover = (spec: NodeSpec, element: HTMLElement) => {
    window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setCard({ spec, top: element.getBoundingClientRect().top }), 280);
  };
  const leave = () => {
    window.clearTimeout(hoverTimer.current);
    setCard(null);
  };

  const onDragStart = (event: DragEvent<HTMLDivElement>, spec: NodeSpec) => {
    leave();
    event.dataTransfer.setData(CLASS_MIME, spec.class_path);
    event.dataTransfer.setData("text/plain", spec.class_path);
    event.dataTransfer.effectAllowed = "copy";
  };

  const total = specs?.length ?? 0;
  return (
    <aside className="nodes-sidebar" data-nodes-sidebar ref={root}>
      <header className="nodes-head">
        <span className="nodes-title">Nodes</span>
        <span className="nodes-count">{specs === null ? "…" : searching ? `${matching.length} of ${total}` : total}</span>
        <button type="button" className="icon-button" title="close (N)" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="nodes-search">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M10.5 10.5l3.5 3.5" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        <input
          data-nodes-search
          placeholder={specs === null ? "loading the catalogue…" : `Search ${total} classes`}
          value={query}
          spellCheck={false}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              if (query) setQuery("");
              else event.currentTarget.blur();
            }
          }}
        />
      </div>
      <div className="nodes-tree" onMouseLeave={leave}>
        {specs === null && <p className="nodes-empty">Reading every class the server can describe…</p>}
        {specs !== null && sections.length === 0 && <p className="nodes-empty">No class matches “{query}”.</p>}
        {sections.map((section) => {
          const open = isOpen(section.name);
          return (
            <section key={section.name} className="nodes-package" data-nodes-package={section.name}>
              <button type="button" className="nodes-package-head" aria-expanded={open} onClick={() => toggle(section.name)}>
                <span className={`caret${open ? " open" : ""}`}>▸</span>
                <span className="nodes-package-name">{section.name}</span>
                <span className="nodes-package-kinds">
                  {section.kinds.map((kind) => (
                    <span key={kind.kind} className={`kind-pip kind-${kind.kind}`} title={`${kind.specs.length} ${kind.label.toLowerCase()}`} />
                  ))}
                </span>
                <span className="nodes-count">{section.count}</span>
              </button>
              {open &&
                section.kinds.map((kind) => (
                  <FolderView
                    key={kind.key}
                    folder={kind}
                    isOpen={isOpen}
                    onToggle={toggle}
                    entry={(spec) => (
                      <Entry
                        key={spec.class_path}
                        spec={spec}
                        onAdd={onAdd}
                        onHover={hover}
                        onLeave={leave}
                        onDragStart={onDragStart}
                      />
                    )}
                  />
                ))}
            </section>
          );
        })}
      </div>
      <footer className="nodes-foot">
        <div data-nodes-target={target.kind}>
          {target.kind === "after" ? (
            <>
              Click adds after <b>{target.node}</b> · drag places it
            </>
          ) : target.kind === "end" ? (
            <>
              Click adds at the end of <b>{target.list}</b> · drag places it
            </>
          ) : (
            (target.why ?? "Drag a class onto the canvas")
          )}
        </div>
        <div className="nodes-subgraph-hint" data-nodes-subgraph-hint>
          A subgraph is made from steps: Graph ▸ Convert selection to subgraph, Insert template ▸ or Insert steps file…
        </div>
      </footer>
      {card && (
        <Card
          key={card.spec.class_path}
          spec={card.spec}
          top={card.top}
          left={(root.current?.getBoundingClientRect().right ?? 320) + 8}
        />
      )}
    </aside>
  );
}

interface FolderViewProps {
  folder: Folder | KindSection;
  isOpen: (key: string) => boolean;
  onToggle: (key: string) => void;
  entry: (spec: NodeSpec) => ReactNode;
}

/** A folder that folds: a kind (its pip and label on the head) or a group segment; its sub-folders, then its classes. */
function FolderView({ folder, isOpen, onToggle, entry }: FolderViewProps) {
  const open = isOpen(folder.key);
  const kind = "kind" in folder ? folder : undefined;
  return (
    <div
      className={kind ? "nodes-folder nodes-kind" : "nodes-folder"}
      data-nodes-kind={kind?.kind}
      data-nodes-folder={kind ? undefined : folder.key}
    >
      <button type="button" className="nodes-folder-head" aria-expanded={open} onClick={() => onToggle(folder.key)}>
        <span className={`caret${open ? " open" : ""}`}>▸</span>
        {kind && <span className={`kind-pip kind-${kind.kind}`} />}
        <span className="nodes-folder-name">{kind ? kind.label : folder.name}</span>
        <span className="nodes-count">{folder.count}</span>
      </button>
      {open && (
        <>
          {folder.folders.map((child) => (
            <FolderView key={child.key} folder={child} isOpen={isOpen} onToggle={onToggle} entry={entry} />
          ))}
          {folder.specs.map(entry)}
        </>
      )}
    </div>
  );
}

function ChipList({ chips: list, max = 4 }: { chips: Chip[]; max?: number }) {
  const shown = list.slice(0, max);
  return (
    <>
      {shown.map((chip) => (
        <span key={chip.name} className="chip" style={{ ["--chip" as string]: socketColor(chip.type) }} title={`${chip.name}: ${chip.type}`}>
          {chip.name}
        </span>
      ))}
      {list.length > max && <span className="chip more">+{list.length - max}</span>}
    </>
  );
}

interface EntryProps {
  spec: NodeSpec;
  onAdd: (spec: NodeSpec) => void;
  onHover: (spec: NodeSpec, element: HTMLElement) => void;
  onLeave: () => void;
  onDragStart: (event: DragEvent<HTMLDivElement>, spec: NodeSpec) => void;
}

function Entry({ spec, onAdd, onHover, onLeave, onDragStart }: EntryProps) {
  const { reads, writes } = chips(spec);
  return (
    <div
      className={`nodes-entry${spec.problem ? " broken" : ""}`}
      data-nodes-entry
      data-class={spec.class_path}
      role="button"
      tabIndex={0}
      draggable
      onClick={() => onAdd(spec)}
      onKeyDown={(event) => event.key === "Enter" && onAdd(spec)}
      onMouseEnter={(event) => onHover(spec, event.currentTarget)}
      onMouseLeave={onLeave}
      onDragStart={(event) => onDragStart(event, spec)}
    >
      <div className="nodes-entry-name">
        {spec.name}
        {spec.problem && <span className="nodes-entry-warn" title={spec.problem}>!</span>}
      </div>
      {spec.doc && (
        <div className="nodes-entry-doc">
          <DocText text={spec.doc} />
        </div>
      )}
      {(reads.length > 0 || writes.length > 0) && (
        <div className="nodes-entry-chips">
          <ChipList chips={reads} />
          {writes.length > 0 && <span className="chip-arrow">→</span>}
          <ChipList chips={writes} />
        </div>
      )}
    </div>
  );
}

/** A docstring line with its markup drawn: a literal, an interpreted name or a role's target as inline code. */
function DocText({ text }: { text: string }) {
  return (
    <>
      {docSpans(text).map((span, index) =>
        span.code ? (
          <code key={index} className="doc-code">
            {span.text}
          </code>
        ) : (
          <span key={index}>{span.text}</span>
        ),
      )}
    </>
  );
}

type CardSettings = NodeSpecWithSettings | "loading" | { refused: string };

/** The hover card of one class: asks for its settings as it opens and draws the card again once they are in. */
function Card({ spec, top, left }: { spec: NodeSpec; top: number; left: number }) {
  const [settings, setSettings] = useState<CardSettings>("loading");
  useEffect(() => {
    let current = true;
    setSettings("loading");
    settingsOf(spec.class_path).then(
      (full) => current && setSettings(full),
      (exc: unknown) => current && setSettings({ refused: errorMessage(exc) }),
    );
    return () => {
      current = false;
    };
  }, [spec.class_path]);
  // keyed by the state, so the body measures its rows afresh when the parameters arrive
  const state = settings === "loading" ? "loading" : "refused" in settings ? "refused" : "ready";
  return <CardBody key={state} spec={spec} settings={settings} state={state} top={top} left={left} />;
}

/**
 * The hover card's body: the whole description, what it reads and writes with the types, the parameters. It never
 * leaves the window (its height is capped, its top clamped), and a parameter list longer than the room left is cut at
 * the last row that fits WHOLE, followed by how many were left out — measured before: a 48-parameter card ran 18 px
 * past a 900-px window and cut its 13th row mid-sentence, with nothing saying 36 more existed.
 */
function CardBody({
  spec,
  settings,
  state,
  top,
  left,
}: {
  spec: NodeSpec;
  settings: CardSettings;
  state: "loading" | "refused" | "ready";
  top: number;
  left: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const params = settings === "loading" || "refused" in settings ? [] : paramRows(settings);
  const [shown, setShown] = useState(params.length);
  const [y, setY] = useState(top);
  useLayoutEffect(() => {
    // rows the list has no room for are not drawn; each pass can only draw fewer, so it settles (the "+N more" line
    // it adds takes room too, which may cost one more row)
    const area = list.current;
    if (area !== null) {
      const bottom = area.getBoundingClientRect().bottom;
      const rows = [...area.querySelectorAll<HTMLElement>(".node-card-param")];
      const whole = rows.filter((row) => row.getBoundingClientRect().bottom <= bottom + 0.5).length;
      if (whole < shown) {
        setShown(whole);
        return;
      }
    }
    const height = box.current?.getBoundingClientRect().height ?? 0;
    setY(Math.max(48, Math.min(top - 8, window.innerHeight - height - 8)));
  }, [top, shown]);
  const { reads, writes } = chips(spec);
  const hidden = params.length - shown;
  return (
    <div ref={box} className="node-card" data-nodes-card={spec.class_path} data-card-settings={state} style={{ top: y, left }}>
      <div className="node-card-head">
        <span className="node-card-name">{spec.name}</span>
        <span className={`badge kind-${spec.kind}`}>{spec.kind}</span>
      </div>
      <code className="node-card-path">{spec.class_path}</code>
      {spec.problem && <div className="node-card-problem">{spec.problem}</div>}
      {spec.doc && (
        <p className="node-card-doc">
          <DocText text={spec.doc} />
        </p>
      )}
      {(reads.length > 0 || writes.length > 0) && (
        <div className="node-card-io">
          {reads.length > 0 && (
            <div>
              <span className="dim">reads</span>
              {reads.map((chip) => (
                <span key={chip.name} className="chip" style={{ ["--chip" as string]: socketColor(chip.type) }}>
                  {chip.name} <i>{chip.type}</i>
                </span>
              ))}
            </div>
          )}
          {writes.length > 0 && (
            <div>
              <span className="dim">writes</span>
              {writes.map((chip) => (
                <span key={chip.name} className="chip" style={{ ["--chip" as string]: socketColor(chip.type) }}>
                  {chip.name} <i>{chip.type}</i>
                </span>
              ))}
            </div>
          )}
        </div>
      )}
      {params.length > 0 && (
        <div className="node-card-params">
          <div className="node-card-section">parameters</div>
          <div className="node-card-param-list" ref={list}>
          {params.slice(0, shown).map((param) => (
            <div key={param.name} className="node-card-param">
              <div className="node-card-param-head">
                <span className="name">
                  {param.name}
                  {param.required && <b title="required">*</b>}
                </span>
                <span className="type">{param.type}</span>
                {param.default !== "" && <span className="default">= {param.default}</span>}
              </div>
              {param.doc && (
                <div className="node-card-param-doc">
                  <DocText text={param.doc} />
                </div>
              )}
            </div>
          ))}
          </div>
          {hidden > 0 && (
            <div className="node-card-more" data-card-more>
              +{hidden} more {hidden === 1 ? "parameter" : "parameters"}
            </div>
          )}
        </div>
      )}
      {state === "loading" && <div className="dim small">loading the settings…</div>}
      {settings !== "loading" && "refused" in settings && <div className="node-card-problem">{settings.refused}</div>}
      {state === "ready" && params.length === 0 && <div className="dim small">no parameters</div>}
    </div>
  );
}
