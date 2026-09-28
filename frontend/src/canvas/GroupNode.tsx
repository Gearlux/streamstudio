/**
 * A group's box: a coloured frame with a title bar, drawn BEHIND its members (React Flow puts a child above its
 * parent). The title bar is the only part that takes the pointer — dragging it moves the box and every member by
 * the same delta; the frame lets clicks through to the pane, so box selection and the palette's double-click
 * still work inside a group. Double-clicking the title edits it; the two buttons step the colour and ungroup.
 * Every change is `PUT …/groups` through the editor context — a group is layout, never a document edit.
 */
import { useState, type KeyboardEvent } from "react";
import type { NodeProps } from "@xyflow/react";
import { useEditor } from "../context";
import { nextColor } from "../groups";
import type { GroupNode } from "../model";

export function GroupNodeView({ data }: NodeProps<GroupNode>) {
  const { updateGroup, ungroup } = useEditor();
  const { group, members } = data;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(group.title);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    event.stopPropagation(); // the page's shortcuts are not for a title being typed
    if (event.key === "Escape") setEditing(false);
    if (event.key !== "Enter") return;
    setEditing(false);
    if (draft.trim() !== "" && draft !== group.title) void updateGroup(group.id, { title: draft.trim() });
  };

  return (
    <div
      className="group-box"
      data-group={group.id}
      data-members={members.join(",")}
      style={{ borderColor: group.color, background: `${group.color}1f` }}
    >
      <div
        className="group-title"
        style={{ background: group.color }}
        onDoubleClick={() => {
          setDraft(group.title);
          setEditing(true);
        }}
      >
        {editing ? (
          <input
            className="nodrag nopan"
            data-group-title
            autoFocus
            value={draft}
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            onBlur={() => setEditing(false)}
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
    </div>
  );
}
