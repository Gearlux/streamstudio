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
 * Double-clicking the name edits it in place: Enter is `POST …/rename` (the key, every reference, the sidecar
 * entry and group membership in ONE edit), the typed name kept on the node's level (`mono` inside prep is sent
 * as `prep/mono`); a refused name (one in use, a list member) leaves the old name and the page shows the server's
 * message. After a run, the node carries its status (`data-run-status`: ok / paused / error / dropped / not
 * reached — coloured), its wall time and its generation, read from the latest run.
 *
 * Every element a test needs is addressable by `data-*`: the root by `data-node` / `data-kind` / `data-class`
 * (and `data-run-status` / `data-generation` after a run), a handle by `data-node` / `data-socket` / `data-type` /
 * `data-kind`, a widget by `data-param`, the rename field by `data-rename`, a subgraph's open button by
 * `data-open-subgraph` and its step list by `data-inner-steps`.
 */
import { useEffect, useState, type KeyboardEvent } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useEditor } from "../context";
import { isBoundary, levelOfNode, renamedTo, shortName } from "../levels";
import { handleId, shortClass, socketColor, type GenericNode, type HandleKind, type ModelNode } from "../model";
import { ParamWidget } from "./ParamWidget";

/** What the class line says for a node without a class of its own. */
function classLine(node: ModelNode): string {
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
  const { form, run, setParam } = useEditor();
  const node = data.node;
  const schemas = form[node.name] ?? {};
  const params = Object.values(node.params);
  const status = run?.statuses[node.name];
  const traced = status === undefined ? undefined : run?.trace.nodes.find((entry) => entry.node === node.name);
  const generation = run?.generations[node.name];

  return (
    <div
      className={`gnode kind-${node.kind}${selected ? " selected" : ""}${status ? ` run-${status.replace(" ", "-")}` : ""}`}
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
        <div className="cls">{classLine(node)}</div>
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
      {node.inputs.map((socket) => (
        <div className="row in" key={`in:${socket.name}`}>
          <SocketHandle node={node} kind="in" name={socket.name} type={socket.type} doc={socket.doc} />
          <span className="label">
            {socket.name}
            {socket.required && <b title="an input nothing may leave unwired">*</b>}
            <i>{socket.type}</i>
          </span>
        </div>
      ))}
      {params.map((param) => (
        <div className="row param" key={`param:${param.name}`}>
          <SocketHandle node={node} kind="in" name={param.name} type={param.type} doc={param.doc} small />
          <label title={param.doc}>{param.name}</label>
          <ParamWidget
            param={param}
            schema={{ ...param.schema, ...(schemas[param.name] ?? {}) }}
            onCommit={(value) => setParam(node.name, param.name, value)}
          />
        </div>
      ))}
      {node.outputs.map((socket) => (
        <div className="row out" key={`out:${socket.name}`}>
          <span className="label">
            <i>{socket.type}</i>
            {socket.name}
          </span>
          <SocketHandle node={node} kind="out" name={socket.name} type={socket.type} doc={socket.doc} />
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
  const { rename, editing: which, setEditing } = useEditor();
  const editing = which === `node:${node.name}`;
  const own = shortName(node.name);
  const [draft, setDraft] = useState(own);
  useEffect(() => {
    if (editing) setDraft(own);
  }, [editing, own]);
  if (isBoundary(node)) return <span className="name">{own}</span>;
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
}

/** A typed handle: `in:<name>` on the left, `out:<name>` on the right, coloured by type, described by `data-*`. */
function SocketHandle({ node, kind, name, type, doc, small }: SocketHandleProps) {
  return (
    <Handle
      type={kind === "in" ? "target" : "source"}
      position={kind === "in" ? Position.Left : Position.Right}
      id={handleId(kind, name)}
      className={small ? "socket socket-param" : "socket"}
      data-node={node.name}
      data-socket={name}
      data-type={type}
      data-kind={kind}
      style={{ background: socketColor(type) }}
      title={`${name}: ${type}${doc ? ` — ${doc}` : ""}`}
    />
  );
}
