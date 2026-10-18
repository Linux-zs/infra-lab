export type LabNode = {
  id: string;
  kind: "nginx" | "app" | "redis";
  replica_of?: string | null;
  position: { x: number; y: number };
  memory_mb: number;
  cpu: number;
  delay_ms: number;
  error_rate: number;
  error_status: 500 | 502 | 503 | 504;
  mode: "generated" | "manual";
  config: string;
};
export type Topology = {
  nodes: LabNode[];
  edges: { source: string; target: string; weight: number }[];
};
export type Observation = {
  id: string;
  kind: string;
  status: string;
  health: string;
  container_id: string;
  memory_bytes?: number;
  ip?: string;
  observed_at: number;
  client_trace?: boolean;
  browser_site?: boolean;
  custom_response?: boolean;
  response?: {
    available: boolean;
    version?: number;
    mode?: "website" | "custom";
    status?: number;
    delay_ms?: number;
    observed_at: number;
    error?: string;
  } | null;
  redis?: {
    available: boolean;
    fields?: Record<string, string>;
    error?: string;
    observed_at: number;
  } | null;
  image?: string;
};
export type Lab = {
  id: string;
  scenario_id?: string | null;
  name: string;
  revision: number;
  draft: Topology;
  applied: Topology | null;
  applied_revision: number | null;
  observation: { available: boolean; nodes: Observation[] } | null;
  observed_at: number | null;
};
export type LoadResult = {
  identified?: number;
  unidentified?: number;
  completed: number;
  requested: number;
  errors: number;
  error_rate: number | null;
  p95_ms: number | null;
  distribution: Record<string, number>;
  applied_revision: number;
  samples: {
    request_id: string;
    node: string | null;
    status: number | null;
    latency_ms: number;
    error: string | null;
  }[];
  upstream_logs: Record<string, unknown>[];
  upstream_logs_available: boolean;
  runtime_config?: string;
  runtime_nodes?: unknown;
};
export type Task = {
  id: string;
  kind: string;
  status: string;
  created_at: number;
  payload: {
    scenario_plan?: { measure: "load" | "request" | "redis" };
    action: {
      revision: number;
      node_id?: string;
      redis_op?: string;
      response_version?: number;
    };
  };
  logs: { time: number; message: string }[];
  result:
    | (LoadResult & { error?: string; value?: string | number | null })
    | null;
};
export type Health = {
  api: string;
  worker_stale: boolean;
  worker: { docker: boolean; error?: string; time: number } | null;
};
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || response.statusText),
    );
  }
  return response.json();
}
export function newNode(
  kind: "nginx" | "app" | "redis",
  x: number,
  y: number,
): LabNode {
  return {
    id: `${kind}-${crypto.randomUUID().slice(0, 6)}`,
    kind,
    position: { x, y },
    memory_mb: 64,
    cpu: 0.25,
    delay_ms: 0,
    error_rate: 0,
    error_status: 503,
    mode: "generated",
    config: "",
  };
}
