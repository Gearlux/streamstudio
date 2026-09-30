/**
 * A group's box: a coloured frame with a title bar, drawn BEHIND its members (React Flow puts a child above its
 * parent). The title bar takes the pointer — dragging it moves the box and every member by the same delta, a
 * right-click on it opens the group's menu; the frame lets clicks through to the pane, so box selection and the
 * palette's double-click still work inside a group. Double-clicking the title (or Rename… / F2) edits it; the two
 * buttons step the colour and ungroup.
 *
 * The box resizes like a window: a grip in the bottom-right corner always, and handles on every edge while the box is
 * selected (React Flow's `NodeResizer`). A resize gives the group `bounds` and makes it hold exactly the nodes whose
 * centre it covers — so growing a box around nodes adds them, shrinking it lets them go. Every change is
 * `PUT …/groups` through the editor context — a group is layout, never a document edit.
 */
import { useEffect, useState, type KeyboardEvent } from "react";
import { NodeResizeControl, NodeResizer, type NodeProps, type ResizeParams } from "@xyflow/react";
import { useEditor } from "../context";
import { nextColor } from "../groups";
import type { GroupNode } from "../model";

/** The smallest box a resize leaves: room for the title bar and one card's edge. */
const MIN = { width: 180, height: 90 };

export function GroupNodeView({ data, selected }: NodeProps<GroupNode>) {
  const { updateGroup, ungroup, resizeGroup, editing, setEditing } = useEditor();
  const { group, members } = data;
  const [draft, setDraft] = useState(group.title);
  const isEditing = editing === `group:${group.id}`;
  useEffect(() => {
    if (isEditing) setDraft(group.title);
  }, [isEditing, group.title]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    event.stopPropagation(); // the page's shortcuts are not for a title being typed
    if (event.key === "Escape") setEditing(null);
    if (event.key !== "Enter") return;
    setEditing(null);
    if (draft.trim() !== "" && draft !== group.title) void updateGroup(group.id, { title: draft.trim() });
  };
  const onResizeEnd = (_event: unknown, params: ResizeParams) => void resizeGroup(group.id, params);

  return (
    <div
      className={`group-box${selected ? " selected" : ""}`}
      data-group={group.id}
      data-members={members.join(",")}
      style={{ borderColor: group.color, background: `${group.color}1f` }}
    >
      <NodeResizer
        isVisible={selected}
        minWidth={MIN.width}
        minHeight={MIN.height}
        color={group.color}
        handleClassName="group-handle"
        lineClassName="group-edge"
        onResizeEnd={onResizeEnd}
      />
      <NodeResizeControl className="group-grip" position="bottom-right" minWidth={MIN.width} minHeight={MIN.height} onResizeEnd={onResizeEnd}>
        <svg viewBox="0 0 10 10" aria-hidden="true" data-group-grip>
          <path d="M9 1L1 9M9 5L5 9" stroke={group.color} strokeWidth="1.4" />
        </svg>
      </NodeResizeControl>
      <div className="group-title" style={{ background: group.color }} onDoubleClick={() => setEditing(`group:${group.id}`)}>
        {isEditing ? (
          <input
            className="nodrag nopan"
            data-group-title
            autoFocus
            value={draft}
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            onBlur={() => setEditing(null)}
          />
        ) : (
          <span className="group-name">{group.title}</span>
        )}
        <span className="group-actions">
          <button
            className="nodrag"
            data-action="recolor"
            title="the next colour"
            onDoubleClick={(event) => event.stopPropagation()}
            onClick={() => void updateGroup(group.id, { color: nextColor(group.color) })}
          >
            ◐
          </button>
          <button
            className="nodrag"
            data-action="ungroup"
            title="ungroup — the members stay where they are"
            onDoubleClick={(event) => event.stopPropagation()}
            onClick={() => void ungroup(group.id)}
          >
            ×
          </button>
        </span>
      </div>
      {members.length === 0 && <div className="group-empty">drag nodes here</div>}
    </div>
  );
}
