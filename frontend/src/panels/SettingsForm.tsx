/**
 * The settings form — the face a host's "settings" button opens (`?tab=settings`, `link.ts`): the parameters a person
 * tunes without the canvas, each through the SAME widget the node on the canvas draws (`ParamWidget`, its pickers
 * included) and the SAME write (`setParam` → `POST …/param`). Both read the value from the model the server answers, so
 * an edit made in one shows in the other on the next answer; neither holds a value of its own.
 *
 * What it lists (`settings.ts`): a graph with an `app:` block (`GET …/app`, asked again whenever the document's text
 * changes — a rename moves a field to its node's new name) shows THOSE fields — the Bluetooth pane's 30 of its 247 — in
 * the block's order, one section per node, with a tick box "all parameters"; ticked it shows every parameter of every
 * node of the LEVEL the canvas shows (the top, or the inside of a subgraph: `prep/grey`, `prep/scaled`), in the model's
 * order. A graph WITHOUT an `app:` block names no settings: the tab says so, with the same tick box. The tick is the
 * page's, per graph (`all` / `onAll`): it stays while the person looks at another tab. A step's gate (`requires`) is
 * never a field. An `app:` entry that names nothing is listed under the fields in the server's located words, never
 * dropped silently.
 *
 * Hooks: `data-settings` — present once the first `app:` answer is in, so a test waiting on it reads the fields the tab
 * settled on — with `data-settings-mode` (`app` | `all`) and `data-settings-fields` (the number of fields drawn);
 * `data-settings-all` (the tick box, present once the `app:` answer is in), `data-settings-none` (the sentence of a
 * graph without an `app:` block), `data-form-node` per section, `data-param` per field (`ParamWidget`),
 * `data-settings-problem` per `app:` entry that names nothing.
 */
import { useEffect, useState } from "react";
import { errorMessage, getApp, type AppForm } from "../api";
import { useEditor } from "../context";
import type { Graph } from "../model";
import { shortClass } from "../model";
import { fieldCount, settingsEmpty, settingsMode, settingsSections } from "../settings";
import { ParamWidget, valuesOf } from "../canvas/ParamWidget";

export interface SettingsFormProps {
  graph: Graph;
  level?: string;
  /** "all parameters" is ticked — the page's, per graph, so it stays when another tab is looked at. */
  all: boolean;
  onAll: (all: boolean) => void;
}

export function SettingsForm({ graph, level = "", all: allTicked, onAll }: SettingsFormProps) {
  const { form, setParam } = useEditor();
  /** The graph's `app:` answer (or why there is none); `undefined` until the first one arrives. */
  const [app, setApp] = useState<{ app: AppForm | null; error: string | null } | undefined>(undefined);
  useEffect(() => {
    let live = true;
    getApp(graph.name).then(
      (answer) => live && setApp({ app: answer, error: null }),
      // without the answer the tab lists every parameter, and says why it could not narrow them
      (exc: unknown) => live && setApp({ app: null, error: errorMessage(exc) }),
    );
    return () => {
      live = false;
    };
  }, [graph.name, graph.text]); // asked again for every text: an edit or a rename may move a field
  // the last answer is drawn while a newer one is on its way; only the first is waited for
  if (app === undefined)
    return (
      <div className="settings">
        <p className="dim">reading the graph's settings…</p>
      </div>
    );
  const declared = app.app;
  const mode = settingsMode(declared, allTicked);
  const sections = settingsSections(graph.model, form, declared, mode, level);
  const where = level === "" ? "of this graph" : `inside ${level}`;
  const empty = settingsEmpty(declared, mode, fieldCount(sections), where);
  const named = declared?.declared ? `${fieldCount(sections)} fields the graph's app: block names` : "the graph has no app: block";
  return (
    <div className="settings" data-settings data-settings-mode={mode} data-settings-fields={fieldCount(sections)}>
      {declared !== null && (
        <label className="settings-mode" title="the graph's app: block names the fields shown; tick to list every parameter">
          <input type="checkbox" data-settings-all checked={allTicked} onChange={(event) => onAll(event.target.checked)} />
          all parameters
          <span className="dim">{mode === "app" ? named : `every parameter ${where}`}</span>
        </label>
      )}
      {app.error && <p className="settings-problem">{`the app: fields could not be read — every parameter is listed: ${app.error}`}</p>}
      {empty !== "" && (
        <p className="dim" data-settings-none={declared !== null && !declared.declared && mode === "app" ? "" : undefined}>
          {empty}
        </p>
      )}
      {sections.map(({ node, fields }) => {
        const values = valuesOf(node.params);
        return (
          <section key={node.name} className="form-node" data-form-node={node.name}>
            <h3>
              {node.name} <small>{shortClass(node.class_path)}</small>
            </h3>
            {fields.map(({ name, param, schema }) => (
              <label className="field" key={name} title={typeof schema.description === "string" ? schema.description : ""}>
                <span>{name}</span>
                {param ? (
                  <ParamWidget param={param} schema={schema} values={values} onCommit={(value) => setParam(node.name, name, value)} />
                ) : (
                  <code className="ro">not in the model</code>
                )}
              </label>
            ))}
          </section>
        );
      })}
      {mode === "app" &&
        (declared?.problems ?? []).map((problem) => (
          <p key={`${problem.loc}\u0000${problem.message}`} className="settings-problem" data-settings-problem title={problem.loc}>
            {problem.message}
          </p>
        ))}
    </div>
  );
}
