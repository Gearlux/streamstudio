/**
 * Check graph: every problem `GET …/check` reports, each with the `file:line:col` it is at — the list a person works
 * through, so it stays open until closed (a toast would be gone before the third line is read). A clean graph is a
 * toast instead; this dialog only opens when there is something to fix.
 *
 * Hooks: `data-dialog="check"`, `data-check-problem` on each row (its `loc` in `data-loc`).
 */
import type { Problem } from "../model";
import { Modal } from "./Modal";

export function CheckDialog({ file, problems, onClose }: { file: string; problems: Problem[]; onClose: () => void }) {
  return (
    <Modal
      title={`${file}: ${problems.length} problem${problems.length === 1 ? "" : "s"}`}
      kind="check"
      wide
      onCancel={onClose}
      footer={
        <button type="button" className="button primary" autoFocus onClick={onClose}>
          Close
        </button>
      }
    >
      <ol className="check-list">
        {problems.map((problem, index) => (
          <li key={index} data-check-problem data-loc={problem.loc}>
            {problem.loc && <code className="check-loc">{problem.loc}</code>}
            <span className="check-message">{problem.message}</span>
          </li>
        ))}
      </ol>
    </Modal>
  );
}
