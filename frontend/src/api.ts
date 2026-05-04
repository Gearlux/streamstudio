import type { GraphSpec, OpSchema, Preferences, StepEvent } from "./types";

const API_BASE = "/api";

export async function listOps(): Promise<OpSchema[]> {
  const res = await fetch(`${API_BASE}/ops`);
  if (!res.ok) throw new Error(`/api/ops -> ${res.status}`);
  const body = (await res.json()) as { schemas: OpSchema[] };
  return body.schemas;
}

export async function listVisualizers(): Promise<{ hints: string[]; types: string[] }> {
  const res = await fetch(`${API_BASE}/visualizers`);
  if (!res.ok) throw new Error(`/api/visualizers -> ${res.status}`);
  return (await res.json()) as { hints: string[]; types: string[] };
}

export async function graphFromYaml(yaml: string): Promise<GraphSpec> {
  const res = await fetch(`${API_BASE}/graph/from-yaml`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml }),
  });
  if (!res.ok) throw new Error(`/api/graph/from-yaml -> ${res.status}: ${await res.text()}`);
  return (await res.json()) as GraphSpec;
}

export async function graphToYaml(graph: GraphSpec): Promise<string> {
  const res = await fetch(`${API_BASE}/graph/to-yaml`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(graph),
  });
  if (!res.ok) throw new Error(`/api/graph/to-yaml -> ${res.status}: ${await res.text()}`);
  const body = (await res.json()) as { yaml: string };
  return body.yaml;
}

export async function startRun(yaml: string, mode: "step" | "continue"): Promise<string> {
  const res = await fetch(`${API_BASE}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml, mode }),
  });
  if (!res.ok) throw new Error(`/api/run -> ${res.status}: ${await res.text()}`);
  const body = (await res.json()) as { run_id: string };
  return body.run_id;
}

export async function controlRun(runId: string, command: "step" | "continue" | "abort"): Promise<void> {
  const res = await fetch(`${API_BASE}/run/${runId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command }),
  });
  if (!res.ok) throw new Error(`/api/run/${runId}/control -> ${res.status}`);
}

export async function getPreferences(): Promise<Preferences> {
  const res = await fetch(`${API_BASE}/preferences`);
  if (!res.ok) throw new Error(`/api/preferences -> ${res.status}`);
  return (await res.json()) as Preferences;
}

export async function updatePreferences(partial: Partial<Preferences>): Promise<Preferences> {
  const res = await fetch(`${API_BASE}/preferences`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(partial),
  });
  if (!res.ok) throw new Error(`/api/preferences -> ${res.status}: ${await res.text()}`);
  return (await res.json()) as Preferences;
}

export function openRunStream(
  runId: string,
  onEvent: (event: StepEvent) => void,
  onEnd: () => void,
): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}${API_BASE}/run/${runId}/ws`);
  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.event_kind === "stream_end") {
      onEnd();
      return;
    }
    onEvent(data as StepEvent);
  };
  ws.onclose = () => onEnd();
  return ws;
}
