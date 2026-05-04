import { useCallback, useEffect, useMemo, useState } from "react";
import AppBar from "./components/AppBar";
import ConfirmClearDialog from "./components/ConfirmClearDialog";
import GraphCanvas from "./components/GraphCanvas";
import NodePalette, { type PaletteDragPayload } from "./components/NodePalette";
import RightPane from "./components/RightPane";
import SettingsDialog from "./components/SettingsDialog";
import StepControls from "./components/StepControls";
import * as api from "./api";
import { useTheme } from "./theme";
import type { GraphNode, GraphSpec, OpSchema, Preferences, StepEvent } from "./types";

const STYLE: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "240px 1fr 360px",
  gridTemplateRows: "auto 1fr 100px",
  gridTemplateAreas: `"appbar appbar appbar" "palette canvas viz" "palette controls viz"`,
  height: "100vh",
  fontFamily: "-apple-system, system-ui, sans-serif",
  background: "var(--bg-app)",
  color: "var(--text-primary)",
};

const DEFAULT_PREFS: Preferences = {
  confirm_clear_pipeline: true,
  show_internal_ops: false,
  default_save_filename: "pipeline.yaml",
  theme: "auto",
};

const TINY_PIPELINE_YAML = `!class:dataflux.core.Flux()
source: !class:fluxstudio._fixtures.NumericArraySource()
  count: 4
  size: 8
ops:
  - !class:dataflux.ops.numpy.RescaleOp()
    in_min: 0.0
    in_max: 255.0
    out_min: 0.0
    out_max: 1.0
  - !class:dataflux.ops.numpy.StandardizeOp()
    mean: 0.5
    std: 0.5
`;

const EMPTY_GRAPH: GraphSpec = { nodes: [], edges: [] };

function paramsSummary(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (!entries.length) return "";
  return entries.map(([k, v]) => `${k}=${formatScalar(v)}`).slice(0, 4).join(", ");
}

function formatScalar(v: unknown): string {
  if (typeof v === "number") return String(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "string") return v.length > 16 ? `"${v.slice(0, 14)}…"` : `"${v}"`;
  return JSON.stringify(v);
}

export default function App() {
  const [ops, setOps] = useState<OpSchema[]>([]);
  const [graph, setGraph] = useState<GraphSpec | null>(null);
  const [nodePositions, setNodePositions] = useState<Record<string, { x: number; y: number }>>({});
  const [nextNodeId, setNextNodeId] = useState(0);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const [latestEvent, setLatestEvent] = useState<StepEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [prefs, setPrefs] = useState<Preferences>(DEFAULT_PREFS);
  const [pipelineFilename, setPipelineFilename] = useState<string | null>(null);
  const [showConfirmClear, setShowConfirmClear] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const resolvedTheme = useTheme(prefs.theme);

  useEffect(() => {
    api.listOps().then(setOps).catch((e) => setError(String(e)));
    api.getPreferences().then(setPrefs).catch((e) => setError(String(e)));
  }, []);

  // After loading a graph (Open / demo / from-yaml), reset positions and
  // counter so freshly inserted nodes don't collide with the loaded ids.
  const adoptGraph = useCallback((next: GraphSpec, filename: string | null) => {
    setGraph(next);
    setNodePositions({});
    setNextNodeId(next.nodes.length);
    setSelectedNodeId(null);
    setActiveNodeId(null);
    setLatestEvent(null);
    setPipelineFilename(filename);
  }, []);

  const selectedNode = useMemo<GraphNode | null>(() => {
    if (!graph || !selectedNodeId) return null;
    return graph.nodes.find((n) => n.id === selectedNodeId) ?? null;
  }, [graph, selectedNodeId]);

  const opsByPath = useMemo(() => {
    const m = new Map<string, OpSchema>();
    for (const op of ops) m.set(op.path, op);
    return m;
  }, [ops]);

  const selectedSchema = selectedNode ? opsByPath.get(selectedNode.op_path) ?? null : null;

  const paramSummaryFor = useCallback((n: GraphNode) => paramsSummary(n.params), []);

  // ── Toolbar handlers ──────────────────────────────────────────────────
  const handleNew = useCallback(() => {
    const isNonEmpty = !!graph && graph.nodes.length > 0;
    if (isNonEmpty && prefs.confirm_clear_pipeline) {
      setShowConfirmClear(true);
      return;
    }
    adoptGraph(EMPTY_GRAPH, null);
  }, [graph, prefs, adoptGraph]);

  const handleConfirmClear = useCallback(
    async (dontAskAgain: boolean) => {
      setShowConfirmClear(false);
      adoptGraph(EMPTY_GRAPH, null);
      if (dontAskAgain) {
        try {
          const next = await api.updatePreferences({ confirm_clear_pipeline: false });
          setPrefs(next);
        } catch (e) {
          setError(String(e));
        }
      }
    },
    [adoptGraph],
  );

  const handleOpen = useCallback(
    async (file: File) => {
      try {
        const text = await file.text();
        const g = await api.graphFromYaml(text);
        adoptGraph(g, file.name);
      } catch (e) {
        setError(String(e));
      }
    },
    [adoptGraph],
  );

  const handleLoadDemo = useCallback(async () => {
    try {
      const g = await api.graphFromYaml(TINY_PIPELINE_YAML);
      adoptGraph(g, "tiny_pipeline.yaml");
    } catch (e) {
      setError(String(e));
    }
  }, [adoptGraph]);

  const handleSave = useCallback(async () => {
    if (!graph || graph.nodes.length === 0) return;
    try {
      const yaml = await api.graphToYaml(graph);
      const filename = pipelineFilename ?? prefs.default_save_filename;
      const blob = new Blob([yaml], { type: "text/yaml;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  }, [graph, pipelineFilename, prefs]);

  const handlePrefChange = useCallback(async (partial: Partial<Preferences>) => {
    try {
      const next = await api.updatePreferences(partial);
      setPrefs(next);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  // ── Canvas mutation handlers ──────────────────────────────────────────
  const handleDropOp = useCallback(
    (payload: PaletteDragPayload, position: { x: number; y: number }, hoveredEdgeId: string | null) => {
      const id = `n${nextNodeId}`;
      setNextNodeId((c) => c + 1);
      setNodePositions((prev) => ({ ...prev, [id]: position }));
      setGraph((prev) => {
        const cur = prev ?? EMPTY_GRAPH;
        const newNode: GraphNode = { id, op_path: payload.op_path, params: {} };
        if (hoveredEdgeId) {
          // Splice into the hovered edge: source → newId → target.
          const parts = hoveredEdgeId.split("--");
          if (parts.length === 2) {
            const [src, tgt] = parts;
            const insertIdx = cur.nodes.findIndex((n) => n.id === src);
            const newNodes = [...cur.nodes];
            const at = insertIdx >= 0 ? insertIdx + 1 : newNodes.length;
            newNodes.splice(at, 0, newNode);
            const newEdges = cur.edges.filter((e) => !(e.source === src && e.target === tgt));
            newEdges.push({ source: src, target: id }, { source: id, target: tgt });
            return { nodes: newNodes, edges: newEdges };
          }
        }
        // Plain append. If there's a unique tail node (one with no outgoing
        // edge), auto-wire it into the new node so linear pipelines stay
        // connected without forcing the user to draw every edge.
        const newNodes = [...cur.nodes, newNode];
        const sources = new Set(cur.edges.map((e) => e.source));
        const tails = cur.nodes.filter((n) => !sources.has(n.id));
        const newEdges = [...cur.edges];
        if (tails.length === 1) newEdges.push({ source: tails[0].id, target: id });
        return { nodes: newNodes, edges: newEdges };
      });
    },
    [nextNodeId],
  );

  const handlePositionChange = useCallback((id: string, position: { x: number; y: number }) => {
    setNodePositions((prev) => ({ ...prev, [id]: position }));
  }, []);

  const handleSelectNode = useCallback((id: string | null) => {
    setSelectedNodeId(id);
  }, []);

  const handleConnect = useCallback((source: string, target: string) => {
    setGraph((prev) => {
      if (!prev) return prev;
      // Linear pipelines: each source has at most one outgoing edge.
      const filtered = prev.edges.filter((e) => e.source !== source);
      if (filtered.some((e) => e.source === source && e.target === target)) return prev;
      return { ...prev, edges: [...filtered, { source, target }] };
    });
  }, []);

  const handleNodesDelete = useCallback(
    (ids: string[]) => {
      const set = new Set(ids);
      setGraph((prev) => {
        if (!prev) return prev;
        return {
          nodes: prev.nodes.filter((n) => !set.has(n.id)),
          edges: prev.edges.filter((e) => !set.has(e.source) && !set.has(e.target)),
        };
      });
      setNodePositions((prev) => {
        const next = { ...prev };
        for (const id of ids) delete next[id];
        return next;
      });
      if (selectedNodeId && set.has(selectedNodeId)) setSelectedNodeId(null);
    },
    [selectedNodeId],
  );

  const handleEdgesDelete = useCallback((edgeIds: string[]) => {
    const set = new Set<string>();
    for (const id of edgeIds) {
      const [s, t] = id.split("--");
      if (s && t) set.add(`${s}--${t}`);
    }
    setGraph((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        edges: prev.edges.filter((e) => !set.has(`${e.source}--${e.target}`)),
      };
    });
  }, []);

  const handleParamChange = useCallback((paramName: string, value: unknown) => {
    setGraph((prev) => {
      if (!prev || !selectedNodeId) return prev;
      return {
        ...prev,
        nodes: prev.nodes.map((n) => {
          if (n.id !== selectedNodeId) return n;
          const nextParams: Record<string, unknown> = { ...n.params };
          if (value === undefined) delete nextParams[paramName];
          else nextParams[paramName] = value;
          return { ...n, params: nextParams };
        }),
      };
    });
  }, [selectedNodeId]);

  // ── Run handler (existing) ────────────────────────────────────────────
  async function startRun(mode: "step" | "continue") {
    if (!graph || graph.nodes.length === 0) return;
    try {
      const yaml = await api.graphToYaml(graph);
      const id = await api.startRun(yaml, mode);
      setRunId(id);
      setLatestEvent(null);
      setActiveNodeId(graph.nodes[0]?.id ?? null);
      api.openRunStream(
        id,
        (event) => {
          setLatestEvent(event);
          // Map sample_index/op_index → node id (n{op_index+1}, since n0 is the source).
          setActiveNodeId(`n${event.op_index + 1}`);
        },
        () => {
          /* stream ended */
        },
      );
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div style={STYLE}>
      <div style={{ gridArea: "appbar" }}>
        <AppBar
          hasGraph={!!graph && graph.nodes.length > 0}
          themePref={prefs.theme}
          resolvedTheme={resolvedTheme}
          onCycleTheme={() => {
            const next: Preferences["theme"] =
              prefs.theme === "auto" ? "light" : prefs.theme === "light" ? "dark" : "auto";
            handlePrefChange({ theme: next });
          }}
          onNew={handleNew}
          onOpen={handleOpen}
          onSave={handleSave}
          onLoadDemo={handleLoadDemo}
          onOpenSettings={() => setShowSettings(true)}
        />
      </div>
      <div style={{ gridArea: "palette", borderRight: "1px solid var(--border)", overflow: "auto" }}>
        <NodePalette
          ops={ops}
          showInternal={prefs.show_internal_ops}
          onToggleShowInternal={(value) => handlePrefChange({ show_internal_ops: value })}
        />
      </div>
      <div style={{ gridArea: "canvas", position: "relative" }}>
        <GraphCanvas
          graph={graph}
          nodePositions={nodePositions}
          activeNodeId={activeNodeId}
          selectedNodeId={selectedNodeId}
          paramSummaryFor={paramSummaryFor}
          onDropOp={handleDropOp}
          onPositionChange={handlePositionChange}
          onSelectNode={handleSelectNode}
          onConnect={handleConnect}
          onNodesDelete={handleNodesDelete}
          onEdgesDelete={handleEdgesDelete}
        />
      </div>
      <div style={{ gridArea: "viz", borderLeft: "1px solid var(--border)", overflow: "auto", padding: 12 }}>
        <RightPane
          selectedNode={selectedNode}
          selectedSchema={selectedSchema}
          onParamChange={handleParamChange}
          latestEvent={latestEvent}
        />
      </div>
      <div style={{ gridArea: "controls", borderTop: "1px solid var(--border)", padding: 12 }}>
        <StepControls
          runId={runId}
          canStart={!!graph && graph.nodes.length > 0}
          onStart={startRun}
          onCommand={(cmd) => runId && api.controlRun(runId, cmd)}
        />
        {error && (
          <div style={{ color: "var(--danger-text)", fontSize: 12, marginTop: 8 }}>
            {error}{" "}
            <button onClick={() => setError(null)} style={{ marginLeft: 8 }}>
              dismiss
            </button>
          </div>
        )}
      </div>
      {showConfirmClear && (
        <ConfirmClearDialog
          message="Discard the current pipeline and start a new one?"
          onCancel={() => setShowConfirmClear(false)}
          onConfirm={handleConfirmClear}
        />
      )}
      {showSettings && (
        <SettingsDialog prefs={prefs} onClose={() => setShowSettings(false)} onChange={handlePrefChange} />
      )}
    </div>
  );
}
