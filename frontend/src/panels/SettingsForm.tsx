/**
 * The settings form: `GET …/form` rendered as one form per node — every constructor parameter of every node of the
 * LEVEL the canvas shows (the top, or the inside of a subgraph: `prep/grey`, `prep/scaled`), in the model's order,
 * with the SAME widget the node on the canvas draws (`ParamWidget`) and the SAME write (`setParam` → `POST …/param`).
 * Both read the value from the model the server answers, so an edit made in one shows in the other on the next
 * answer; neither holds a value of its own. A node whose form is empty (the host's side, a subgraph's `<in>`/`<out>`,
 * a class the palette cannot describe) has no section.
 */
import { useEditor } from "../context";
import { EMPTY_MODEL, levelModel } from "../levels";
import type { Graph, Json, Param } from "../model";
import { shortClass } from "../model";
import { ParamWidget } from "../canvas/ParamWidget";

export function SettingsForm({ graph, level = "" }: { graph: Graph; level?: string }) {
  const { form, setParam } = useEditor();
  const view = levelModel(graph.model, level) ?? EMPTY_MODEL;
  const nodes = view.nodes.filter((node) => Object.keys(form[node.name] ?? {}).length > 0);
  return (
    <div className="settings" data-settings>
      {nodes.length === 0 && <p className="dim">No node {level === "" ? "of this graph" : `inside ${level}`} has a parameter to set.</p>}
      {nodes.map((node) => (
        <section key={node.name} className="form-node" data-form-node={node.name}>
          <h3>
            {node.name} <small>{shortClass(node.class_path)}</small>
          </h3>
          {Object.entries(form[node.name]).map(([name, schema]) => {
            const param: Param | undefined = node.params[name];
            return (
              <label className="field" key={name} title={typeof schema.description === "string" ? schema.description : ""}>
                <span>{name}</span>
                {param ? (
                  <ParamWidget
                    param={param}
                    schema={{ ...param.schema, ...(schema as Record<string, Json>) }}
                    onCommit={(value) => setParam(node.name, name, value)}
                  />
                ) : (
                  <code className="ro">not in the model</code>
                )}
              </label>
            );
          })}
        </section>
      ))}
    </div>
  );
}
