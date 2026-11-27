/**
 * The ONE node component. Every node of every document renders through it, driven by the model's node:
 * the title is the node's NAME plus its class short name and a kind badge; each input and output is a typed,
 * coloured handle; each constructor parameter is a row with a widget (`ParamWidget`) AND a small target handle
 * (a `bind:` wire lands on a parameter). Nothing is per class — a new op class needs no page change (§2.3).
 *
 * Inside a subgraph a node's name is QUALIFIED (`prep/grey`, DESIGN §2.6): the title shows the step's own name
 * (`grey`) and every hook and every edit keeps the qualified one. Two kinds are drawn their own way by the same
 * component: a SUBGRAPH node (a `Subgraph` step drawn as one node) lists the steps inside it under an "open" button
 * — the button, a double-click on the card (the canvas) or Open in its menu shows its inside; a BOUNDARY node is a
 * subgraph's `<in>` / `<out>`, where the record enters and leaves it — a small pill, never renamed.
 *
 * A graph's ROOT (a `GraphContract`, kind `contract`) shows only what is connected to it: its sockets — one input per
 * output slot it must be delivered, one per record entry its records carry, one per optional slot (no `*`: the host has
 * its own answer when it is left unwired) — and, on the class line, the contract's `name` (`classification source`)
 * instead of `GraphContract`. Its declaration (`name`, `inputs`, `outputs`, `optional`, `records`, `delivered`) is the
 * document's text, never a row of widgets. A slot or entry filled by a value written straight under `delivered:`
 * (`classes: [cat, dog]`, `input: pixels`) shows that value on its row (`data-filled`). What the contract's `inputs`
 * declare is the HOST card's (`<host>`, kind `host`): titled after the host (`from annotaide`, `from the host`
 * standalone, `data-host-title`), never renamed — it is no node of the document.
 *
 * Double-clicking the name edits it in place: Enter is `POST …/rename` (the key, every reference, the sidecar
 * entry and group membership in ONE edit), the typed name kept on the node's level (`mono` inside prep is sent
 * as `prep/mono`); a refused name (one in use, a list member) leaves the old name and the page shows the server's
 * message. After a run, the node carries its status (`data-run-status`: ok / paused / error / dropped / not
 * reached — coloured), its wall time and its generation, read from the latest run.
 *
 * Every element a test needs is addressable by `data-*`: the root by `data-node` / `data-kind` / `data-class`, the class
 * line by `data-subtitle`
 * (and `data-run-status` / `data-generation` after a run), a handle by `data-node` / `data-socket` / `data-type` /
 * `data-kind`, a widget by `data-param`, the rename field by `data-rename`, a subgraph's open button by
 * `data-open-subgraph` and its step list by `data-inner-steps`, a source slot's Set source… button by `data-set-slot`
 * (`slots.ts` — a source goes into a slot as a class and its values, never as a drawn wire).
 *
 * On a level drawn top to bottom (`DownContext`, `layout.runsDown`) the input and output sockets leave their rows for
 * the card's top and bottom edges, spread evenly across it in row order (`down` on the card); a parameter's small
 * handle stays on its row, on the left.
 */
import { useContext, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Handle, Position, useUpdateNodeInternals, type NodeProps } from "@xyflow/react";
import { DownContext, useEditor } from "../context";
import { hostCardTitle } from "../host";
import { isBoundary, levelOfNode, renamedTo, shortName } from "../levels";
import {
  handleId,
  requiredMark,
  shortClass,
  socketColor,
  type GenericNode,
  type HandleKind,
  type Json,
  type ModelNode,
} from "../model";
import { isSourceSlot } from "../slots";
import { ParamWidget, valuesOf } from "./ParamWidget";

/** The `param.doc` of a value written straight under a root's `delivered:` (`document.DELIVERED_LITERALLY`). */
const DELIVERED_LITERALLY = "delivered literally";

/** A filled slot's value as its row shows it: a word as it is, anything else as JSON. */
const filledText = (value: Json): string => (typeof value === "string" ? value : JSON.stringify(value));

/** What the class line says: a root's contract name, the class short name, or — without a class — what the node is. */
function classLine(node: ModelNode): string {
  const named = node.params.name?.value;
  if (node.kind === "contract" && typeof named === "string" && named !== "") return named;
  const cls = shortClass(node.class_path);
  if (cls) return cls;
  if (node.kind === "host") return "what the host provides";
  if (isBoundary(node))
    return shortName(node.name) === "<out>"
      ? `the record ${levelOfNode(node.name)} returns`
      : `the record ${levelOfNode(node.name)} receives`;
  return "";
}

export function GenericNodeView({ data, selected }: NodeProps<GenericNode>) {
  const { graph, form, run, setParam, chooseSlot } = useEditor();
  const node = data.node;
  const schemas = form[node.name] ?? {};
  // a graph's root shows only its sockets: its declaration is the document's text, never a row of widgets
  const params = node.kind === "contract" ? [] : Object.values(node.params);
  const values = valuesOf(node.params);
  const status = run?.statuses[node.name];
  const traced = status === undefined ? undefined : run?.trace.nodes.find((entry) => entry.node === node.name);
  const generation = run?.generations[node.name];
  const down = useContext(DownContext);
  const updateNodeInternals = useUpdateNodeInternals();
  // the handles moved: React Flow measures them again — only on a TURN, never as a card mounts (measured: re-measuring
  // every card on mount held back the first fit of a host's panel and of a subgraph's inside)
  const drawnDown = useRef(down);
  useEffect(() => {
    if (drawnDown.current === down) return;
    drawnDown.current = down;
    updateNodeInternals(node.name);
  }, [down, node.name, updateNodeInternals]);
  const edge = (index: number, count: number) => (down ? { index, count } : undefined);

  return (
    <div
      className={`gnode kind-${node.kind}${down ? " down" : ""}${selected ? " selected" : ""}${status ? ` run-${status.replace(" ", "-")}` : ""}`}
      data-node={node.name}
      data-kind={node.kind}
      data-class={node.class_path}
      data-run-status={status}
      data-generation={generation === undefined || generation === null ? undefined : String(generation)}
      title={node.loc}
    >
      <div className="title">
        <NodeName node={node} />
        <span className="badge">{node.kind}</span>
        <div className="cls" data-subtitle>
          {classLine(node)}
        </div>
      </div>
      {node.kind === "subgraph" && <SubgraphBar node={node} />}
      {status && (
        <div className="run-badge" title={traced && typeof traced.error === "string" ? traced.error : status}>
          <span className="run-dot" />
          {status}
          {typeof traced?.ms === "number" && <span data-ms>{`${traced.ms.toFixed(2)} ms`}</span>}
          {generation !== undefined && generation !== null && <span className="gen">gen {generation}</span>}
        </div>
      )}
      {node.inputs.map((socket, index) => (
        <div className="row in" key={`in:${socket.name}`}>
          <SocketHandle
            node={node}
            kind="in"
            name={socket.name}
            type={socket.type}
            doc={socket.doc}
            edge={edge(index, node.inputs.length)}
          />
          <span className="label">
            {socket.name}
            {requiredMark(socket) && <b title="an input nothing may leave unwired">{requiredMark(socket)}</b>}
            <i>{socket.type}</i>
            {node.params[socket.name]?.doc === DELIVERED_LITERALLY && (
              <span className="filled" data-filled={filledText(node.params[socket.name].value)} title="written under delivered:">
                = {filledText(node.params[socket.name].value)}
              </span>
            )}
          </span>
          {isSourceSlot(socket) && (
            <button
              type="button"
              className="set-slot nodrag"
              data-set-slot={socket.name}
              title={`choose the ${socket.name} — a class and its values (Set ${socket.name}…)`}
              onDoubleClick={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation(); // the card's own click shows its run entry; this one is the slot's
                chooseSlot(node.name, socket.name);
              }}
            >
              set…
            </button>
          )}
        </div>
      ))}
      {params.map((param) => (
        <div className="row param" key={`param:${param.name}`}>
          <SocketHandle node={node} kind="in" name={param.name} type={param.type} doc={param.doc} small />
          <label title={param.doc}>{param.name}</label>
          <ParamWidget
            key={graph} // another graph's node of the same name gets fresh widgets, never this graph's typed draft
            param={param}
            schema={{ ...param.schema, ...(schemas[param.name] ?? {}) }}
            onCommit={(value) => setParam(node.name, param.name, value)}
            values={values}
            oneRow
          />
        </div>
      ))}
      {node.outputs.map((socket, index) => (
        <div className="row out" key={`out:${socket.name}`}>
          <span className="label">
            <i>{socket.type}</i>
            {socket.name}
          </span>
          <SocketHandle
            node={node}
            kind="out"
            name={socket.name}
            type={socket.type}
            doc={socket.doc}
            edge={edge(index, node.outputs.length)}
          />
        </div>
      ))}
      {node.note && (
        <div className="note" title={node.note}>
          {node.note}
        </div>
      )}
    </div>
  );
}

/** A subgraph node's own look: the steps it holds, and the button that shows them (a double-click does the same). */
function SubgraphBar({ node }: { node: ModelNode }) {
  const { openSubgraph, innerSteps } = useEditor();
  const steps = innerSteps(node.name);
  return (
    <div className="sub-bar">
      <span className="sub-steps" data-inner-steps={steps.join(",")} title={steps.join(" → ")}>
        {steps.length === 0 ? "no steps" : steps.join(" → ")}
      </span>
      <button
        type="button"
        className="sub-open nodrag"
        data-open-subgraph={node.name}
        title="show its inside (double-click the card does the same)"
        onDoubleClick={(event) => event.stopPropagation()}
        onClick={() => openSubgraph(node.name)}
      >
        open ▸
      </button>
    </div>
  );
}

/** The name in the title; a double-click, F2 or Rename… edits it in place (the editor context says which title). */
function NodeName({ node }: { node: ModelNode }) {
  const { rename, editing: which, setEditing, host } = useEditor();
  const editing = which === `node:${node.name}`;
  const own = shortName(node.name);
  const [draft, setDraft] = useState(own);
  useEffect(() => {
    if (editing) setDraft(own);
  }, [editing, own]);
  if (isBoundary(node)) return <span className="name">{own}</span>;
  if (node.kind === "host")
    return (
      <span className="name" data-host-title title="what the host hands this graph — not a node of the document">
        {hostCardTitle(host)}
      </span>
    );
  if (!editing) {
    return (
      <span
        className="name"
        title={own === node.name ? "double-click to rename (F2)" : `${node.name} — double-click to rename (F2)`}
        onDoubleClick={(event) => {
          event.stopPropagation(); // a double-click on the name renames; on the card it opens a subgraph
          setEditing(`node:${node.name}`);
        }}
      >
        {own}
      </span>
    );
  }
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    event.stopPropagation(); // the page's shortcuts (g, Ctrl+Z …) are not for a text being typed
    if (event.key === "Escape") setEditing(null);
    if (event.key !== "Enter") return;
    const wanted = draft.trim();
    if (wanted === "" || wanted === own) return setEditing(null);
    // on success the graph answers with the node under its new name, and this component is drawn afresh
    void rename(node.name, renamedTo(node.name, wanted)).then(() => setEditing(null));
  };
  return (
    <input
      className="rename nodrag nopan"
      data-rename
      autoFocus
      value={draft}
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={onKeyDown}
      onDoubleClick={(event) => event.stopPropagation()}
      onBlur={() => setEditing(null)}
    />
  );
}

interface SocketHandleProps {
  node: ModelNode;
  kind: HandleKind;
  name: string;
  type: string;
  doc: string;
  small?: boolean;
  /** On a card drawn top to bottom: the socket's place among its side's, spread across the top or bottom edge. */
  edge?: { index: number; count: number };
}

/**
 * A typed handle: `in:<name>` on the left, `out:<name>` on the right — on the top and bottom edges when `edge` is given —
 * coloured by type, described by `data-*`.
 */
function SocketHandle({ node, kind, name, type, doc, small, edge }: SocketHandleProps) {
  const side = edge === undefined ? (kind === "in" ? Position.Left : Position.Right) : kind === "in" ? Position.Top : Position.Bottom;
  const across = edge === undefined ? {} : { left: `${((edge.index + 1) / (edge.count + 1)) * 100}%` };
  return (
    <Handle
      type={kind === "in" ? "target" : "source"}
      position={side}
      id={handleId(kind, name)}
      className={small ? "socket socket-param" : "socket"}
      data-node={node.name}
      data-socket={name}
      data-type={type}
      data-kind={kind}
      style={{ background: socketColor(type), ...across }}
      title={`${name}: ${type}${doc ? ` — ${doc}` : ""}`}
    />
  );
}
