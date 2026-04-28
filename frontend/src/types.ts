// Wire types — kept in sync with fluxstudio Python Pydantic models by hand for the slice.
// (A future codegen step can derive these from the FastAPI OpenAPI schema.)

export interface OpSchema {
  path: string; // 'dataflux.ops.numpy:RescaleOp'
  name: string;
  doc: string;
  parameters: Array<{
    name: string;
    type: string;
    default: string | null;
    required: boolean;
  }>;
  module: string;
  category: "DataSource" | "DataOp" | "DataSink" | "Visualizer";
}

export interface GraphNode {
  id: string;
  op_path: string;
  params: Record<string, unknown>;
  viz?: { type_hint?: string; enabled?: boolean } | null;
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface GraphSpec {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export type Payload =
  | { kind: "null" }
  | { kind: "scalar"; value: number | string | boolean }
  | { kind: "ndarray"; shape: number[]; dtype: string; stats?: { min: number; max: number; mean: number }; preview?: { mime: string; data: string } }
  | { kind: "tensor"; shape: number[]; dtype: string; stats?: { min?: number; max?: number; mean?: number }; preview?: { mime: string; data: string } }
  | { kind: "image"; mime: string; width: number; height: number; data: string }
  | { kind: "sequence"; length: number; type: string }
  | { kind: "mapping"; keys: string[] }
  | { kind: "opaque"; type: string; repr: string };

export interface SamplePayload {
  input: Payload;
  target: Payload;
  metadata: Record<string, unknown>;
}

export interface StepEvent {
  sample_index: number;
  op_index: number;
  op_name: string;
  op_path: string;
  payload: SamplePayload | null;
  filtered: boolean;
  final: boolean;
}
