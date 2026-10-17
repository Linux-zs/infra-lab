import { useCallback, useEffect, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  useReactFlow,
  type NodeProps,
  type Node,
  type Connection,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Editor from "@monaco-editor/react";
import ConfigDiff from "./ConfigDiff";
import ClientPanel, { useClientRequests, eventName } from "./ClientPanel";
import {
  Activity,
  ArrowRight,
  Box,
  Cable,
  Check,
  ChevronRight,
  Circle,
  Database,
  FileCode2,
  FlaskConical,
  Layers3,
  LoaderCircle,
  Play,
  Plus,
  Radio,
  RotateCcw,
  Save,
  Server,
  Square,
  Terminal,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import {
  api,
  newNode,
  type Health,
  type Lab,
  type LabNode,
  type Observation,
  type Task,
  type Topology,
} from "./types";

const statusText: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败 · 保留现场",
  interrupted: "执行中断",
  healthy: "健康",
  unhealthy: "不健康",
  starting: "检查中",
  exited: "已停止",
  created: "已创建",
  unknown: "未知",
};
const taskText: Record<string, string> = {
  apply: "应用拓扑",
  start: "启动节点",
  stop: "停止节点",
  cleanup: "清理实验",
  load: "请求实验",
  start_lab: "启动实验",
  stop_lab: "停止实验",
  client_request: "客户端请求",
};
const clock = (time?: number | null) =>
  time
    ? new Date(time * 1000).toLocaleTimeString("zh-CN", { hour12: false })
    : "尚未采集";
type FlowData = {
  spec: LabNode;
  observation?: Observation;
  stale: boolean;
  traceLabel?: string;
};
function ServiceNode({ data, selected }: NodeProps<Node<FlowData>>) {
  const { spec, observation, stale } = data;
  const status = stale
    ? "状态过期"
    : observation
      ? statusText[observation.health] || observation.health
      : "未部署";
  return (
    <div
      className={`service-node ${selected ? "selected" : ""} ${spec.kind} ${data.traceLabel ? "trace-highlight" : ""}`}
    >
      <Handle
        type="target"
        position={Position.Left}
        isConnectable={spec.kind === "app"}
      />
      {data.traceLabel && (
        <div className="node-trace-label">{data.traceLabel}</div>
      )}
      <div className="node-top">
        <span className="node-icon">
          {spec.kind === "nginx" ? <Cable size={20} /> : <Box size={20} />}
        </span>
        <span className="node-type">
          {spec.kind === "nginx" ? "Nginx" : "HTTP 应用"}
        </span>
      </div>
      <strong>{spec.id}</strong>
      <div className="node-sub">
        {spec.kind === "nginx"
          ? "反向代理 · 8080"
          : `延迟 ${spec.delay_ms} ms · 错误 ${spec.error_rate}%`}
      </div>
      <div className="node-bottom">
        <span
          className={`dot ${!stale && observation?.health === "healthy" && observation.status === "running" ? "green" : ""}`}
        />
        {observation?.status === "exited" && !stale ? "已停止" : status}
        <span>{spec.memory_mb} MB</span>
      </div>
      {spec.kind === "nginx" && (
        <Handle type="source" position={Position.Right} />
      )}
    </div>
  );
}
function ClientNode({ data }: NodeProps<Node<FlowData>>) {
  return (
    <div className="virtual-client">
      <Handle type="source" position={Position.Right} isConnectable={false} />
      <span>客户端请求</span>
      <strong>客户端</strong>
      <small>{data.traceLabel}</small>
    </div>
  );
}
const nodeTypes = { service: ServiceNode, client: ClientNode };
const empty: Topology = { nodes: [], edges: [] };

export default function App() {
  const [libraryOpen, setLibraryOpen] = useState(window.innerWidth >= 1350);
  const [dockMode, setDockMode] = useState<
    "normal" | "collapsed" | "maximized"
  >("normal");
  const [dockSize, setDockSize] = useState(() =>
    Math.max(
      20,
      Math.min(75, Number(localStorage.getItem("infra-dock-size")) || 35),
    ),
  );
  const [inspectorWidth, setInspectorWidth] = useState(() =>
    Math.max(
      260,
      Math.min(
        500,
        Number(localStorage.getItem("infra-inspector-width")) || 320,
      ),
    ),
  );
  useEffect(() => {
    localStorage.setItem("infra-dock-size", String(dockSize));
  }, [dockSize]);
  useEffect(() => {
    localStorage.setItem("infra-inspector-width", String(inspectorWidth));
  }, [inspectorWidth]);
  useEffect(() => {
    const resize = () => {
      if (window.innerWidth < 1350) setLibraryOpen(false);
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const [labs, setLabs] = useState<Lab[]>([]);
  const [lab, setLab] = useState<Lab | null>(null);
  const [topology, setTopology] = useState<Topology>(empty);
  const [measurements, setMeasurements] = useState<
    Record<string, { width: number; height: number }>
  >({});
  const canvasRef = useRef<HTMLDivElement>(null);
  const topoRef = useRef(topology);
  topoRef.current = topology;
  const draftRevision = useRef(0);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const [selected, setSelected] = useState<string | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [realtime, setRealtime] = useState(false);
  const [panel, setPanel] = useState<
    "client" | "requests" | "results" | "tasks" | "logs"
  >("client");
  const [traceEnabled, setTraceEnabled] = useState(false);
  const [replay, setReplay] = useState(-1);
  const clientStore = useClientRequests(lab?.id);
  const [logs, setLogs] = useState("选择已部署的节点，读取真实容器日志。");
  const [diff, setDiff] = useState<string | null>(null);
  const [modal, setModal] = useState<"cleanup" | "new" | null>(null);
  const [name, setName] = useState("负载均衡实验");
  const [count, setCount] = useState(120);
  const [concurrency, setConcurrency] = useState(4);
  const [duration, setDuration] = useState(30);
  const [tick, setTick] = useState(Date.now());
  const [loadTarget, setLoadTarget] = useState("");
  const [revisionHistory, setRevisionHistory] = useState<
    { revision: number; topology: Topology }[] | null
  >(null);
  const [historyConfig, setHistoryConfig] = useState<string | null>(null);
  const [expandedEditor, setExpandedEditor] = useState(false);
  const [resultTaskId, setResultTaskId] = useState("");
  const { screenToFlowPosition, fitView } = useReactFlow();
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let timer: ReturnType<typeof setTimeout>;
    const observer = new ResizeObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (topoRef.current.nodes.length)
          void fitView({ padding: 0.18, maxZoom: 1 });
      }, 120);
    });
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      clearTimeout(timer);
    };
  }, [fitView]);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (topoRef.current.nodes.length) void fitView({ padding: 0.18, maxZoom: 1 });
    }, 150);
    return () => clearTimeout(timer);
  }, [lab?.id, topology.nodes.length, fitView]);
  const node = topology.nodes.find((n) => n.id === selected);
  const stale =
    !connected ||
    !lab?.observed_at ||
    tick / 1000 - lab.observed_at > 20 ||
    !health?.worker?.docker ||
    health.worker_stale;
  const dockerReady =
    (!lab || connected) && !!health?.worker?.docker && !health.worker_stale;
  const pending = tasks.some((t) => ["queued", "running"].includes(t.status));
  const observation = lab?.observation?.nodes.find((n) => n.id === selected);
  const completedRuns = tasks.filter(
    (t) => t.kind === "load" && t.status === "succeeded",
  );
  const resultTask =
    completedRuns.find((t) => t.id === resultTaskId) || completedRuns[0];
  const result = resultTask?.result;
  const traceVisible = traceEnabled && !!clientStore.detail?.trace;
  const traceEvents = traceVisible ? clientStore.events : [];
  const traceCompletion = traceEvents.find((e) => e.kind === "nginx.completed");
  const traceTarget = topology.nodes.find(
    (n) => n.id === clientStore.detail?.node_id,
  );
  const attempts = traceCompletion?.data.attempts || [];
  useEffect(() => {
    if (!traceVisible || !traceTarget) return;
    const timer = setTimeout(() => {
      void fitView({ padding: 0.18 });
    }, 50);
    return () => clearTimeout(timer);
  }, [traceVisible, traceTarget?.id, fitView]);
  const traceNodeLabels = new Map<string, string>();
  for (const event of traceEvents)
    if (event.node_id) traceNodeLabels.set(event.node_id, eventName(event));
  if (replay >= 0 && traceVisible) {
    traceNodeLabels.clear();
    const current =
      replay === 1 ? traceTarget?.id : attempts[replay - 2]?.node_id;
    if (current) traceNodeLabels.set(current, "事后回放 · 已观测路径");
  }

  const selectLab = useCallback((value: Lab) => {
    draftRevision.current = value.revision;
    setLab(value);
    setTopology(value.draft);
    setDirty(false);
    setSelected(null);
    setTasks([]);
    setLoadTarget("");
    setResultTaskId("");
    setLogs("选择已部署的节点，读取真实容器日志。");
    setError("");
    setTraceEnabled(false);
  }, []);
  useEffect(() => {
    api<Lab[]>("/labs")
      .then((values) => {
        setLabs(values);
        if (values[0]) selectLab(values[0]);
      })
      .catch((e) => setError(String(e)));
    const refreshHealth = () =>
      api<Health>("/health")
        .then(setHealth)
        .catch(() => setHealth(null));
    void refreshHealth();
    const healthTimer = setInterval(refreshHealth, 5000);
    const timer = setInterval(() => setTick(Date.now()), 3000);
    return () => {
      clearInterval(timer);
      clearInterval(healthTimer);
    };
  }, [selectLab]);
  useEffect(() => {
    if (!lab?.id) return;
    const events = new EventSource(`/api/labs/${lab.id}/events`);
    events.addEventListener("snapshot", (event) => {
      const value = JSON.parse((event as MessageEvent).data);
      if (!dirtyRef.current && draftRevision.current !== value.lab.revision) {
        draftRevision.current = value.lab.revision;
        setTopology(value.lab.draft);
      }
      setLab(value.lab);
      setTasks(value.tasks);
      setHealth(value.health);
      setConnected(true);
    });
    events.onerror = () => setConnected(false);
    return () => {
      events.close();
      setConnected(false);
    };
  }, [lab?.id]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function safely(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function save(next = topoRef.current) {
    if (!lab) throw new Error("请先创建实验");
    const updated = await api<Lab>(`/labs/${lab.id}/draft`, "PUT", {
      base_revision: draftRevision.current,
      topology: next,
    });
    draftRevision.current = updated.revision;
    setLab(updated);
    setTopology(updated.draft);
    setDirty(false);
    return updated;
  }
  async function submit(
    kind: string,
    current = lab,
    extra: Record<string, unknown> = {},
  ) {
    if (!current) return;
    const task = await api<Task>(`/labs/${current.id}/tasks`, "POST", {
      request_id: crypto.randomUUID(),
      kind,
      revision: current.revision,
      ...extra,
    });
    setTasks((values) => [task, ...values]);
    if (kind === "load") setResultTaskId("");
    setPanel(
      kind === "client_request"
        ? "client"
        : kind === "load"
          ? "results"
          : "tasks",
    );
    if (kind === "client_request") clientStore.select(task.id);
  }
  function change(next: Topology, structural = false) {
    setTopology(next);
    setDirty(true);
    if (structural && realtime && dockerReady && !busy && !pending)
      void safely(async () => submit("apply", await save(next)));
  }
  function editNode(patch: Partial<LabNode>) {
    change({
      ...topology,
      nodes: topology.nodes.map((n) =>
        n.id === selected ? { ...n, ...patch } : n,
      ),
    });
  }
  function add(kind: "nginx" | "app", position?: { x: number; y: number }) {
    if (!lab || topology.nodes.length >= 10) return;
    const item = newNode(
      kind,
      position?.x ?? 80 + topology.nodes.length * 70,
      position?.y ?? 120,
    );
    change({ ...topology, nodes: [...topology.nodes, item] }, true);
    setSelected(item.id);
  }
  function connect(edge: Connection) {
    if (
      !edge.source ||
      !edge.target ||
      topology.nodes.find((n) => n.id === edge.source)?.kind !== "nginx" ||
      topology.nodes.find((n) => n.id === edge.target)?.kind !== "app" ||
      topology.edges.some(
        (e) => e.source === edge.source && e.target === edge.target,
      )
    )
      return;
    change(
      {
        ...topology,
        edges: [
          ...topology.edges,
          { source: edge.source, target: edge.target, weight: 1 },
        ],
      },
      true,
    );
  }
  async function regenerate() {
    if (!node) return;
    const value = await api<{ config: string }>(
      "/config/preview?node_id=" + node.id,
      "POST",
      { base_revision: lab?.revision || 0, topology },
    );
    setDiff(value.config);
  }
  async function createLab() {
    const created = await api<Lab>("/labs", "POST", { name });
    setLabs((values) => [...values, created]);
    selectLab(created);
    setModal(null);
  }
  function template() {
    const gateway = newNode("nginx", 70, 210);
    gateway.id = "gateway";
    const apps = [0, 1, 2].map((i) => ({
      ...newNode("app", 440, 40 + i * 180),
      id: `app-${i + 1}`,
    }));
    change({
      nodes: [gateway, ...apps],
      edges: apps.map((n) => ({ source: gateway.id, target: n.id, weight: 1 })),
    });
    setSelected(gateway.id);
    setTimeout(() => fitView({ padding: 0.2 }), 100);
  }
  const inputProps = { disabled: busy || pending };
  return (
    <div className="shell">
      <header className="header compact-header">
        <button
          className="icon-button"
          title="切换节点库"
          aria-label="切换节点库"
          onClick={() => setLibraryOpen(!libraryOpen)}
        >
          <Layers3 size={20} />
        </button>
        <strong className="compact-brand">Infra Lab</strong>
        <select
          aria-label="切换实验"
          value={lab?.id || ""}
          disabled={dirty || busy}
          onChange={(event) => {
            const target = labs.find((v) => v.id === event.target.value);
            if (target)
              void safely(async () =>
                selectLab(await api<Lab>("/labs/" + target.id)),
              );
          }}
        >
          <option value="" disabled>
            选择实验
          </option>
          {labs.map((value) => (
            <option key={value.id} value={value.id}>
              {value.name}
            </option>
          ))}
        </select>
        <button
          className="icon-button"
          title="新建实验"
          disabled={dirty || busy}
          onClick={() => setModal("new")}
        >
          <Plus size={16} />
        </button>
        {lab && (
          <span className="tag">
            r{lab.revision}
            {dirty ? " · 未保存" : ""}
          </span>
        )}
        <span
          className={`dot ${dockerReady ? "green" : ""}`}
          title={dockerReady ? "引擎已连接" : "引擎未连接"}
        />
        <div className="main-actions">
          <button
            onClick={() => void safely(() => save())}
            disabled={!lab || busy || pending || !dirty}
          >
            <Save size={15} />
            保存草稿
          </button>
          <button
            className="primary"
            disabled={!lab || busy || pending || !dockerReady}
            onClick={() =>
              void safely(async () =>
                submit("apply", dirty ? await save() : lab),
              )
            }
          >
            {busy || pending ? (
              <LoaderCircle className="spin" size={16} />
            ) : (
              <Play size={16} />
            )}
            应用拓扑
          </button>
        </div>{" "}
        <div className="lab-lifecycle">
          <button
            disabled={!lab || !dockerReady || busy || pending}
            title="只启动已有容器，不应用草稿"
            onClick={() => void safely(() => submit("start_lab"))}
          >
            <Play size={13} />
            启动实验
          </button>
          <button
            disabled={!lab || !dockerReady || busy || pending}
            title="保留容器、配置和历史结果"
            onClick={() => void safely(() => submit("stop_lab"))}
          >
            <Square size={13} />
            停止实验
          </button>
        </div>
      </header>
      {error && (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button className="icon-button" onClick={() => setError("")}>
            <X size={16} />
          </button>
        </div>
      )}
      <main
        className={`workbench ${libraryOpen ? "" : "library-collapsed"} ${node ? "has-inspector" : ""}`}
        style={
          { "--inspector-width": `${inspectorWidth}px` } as React.CSSProperties
        }
      >
        <aside className="library">
          <div className="section-label spaced">
            服务节点<span className="tiny">拖入画布</span>
          </div>
          <button
            className="palette"
            draggable={!!lab && !busy && !pending}
            disabled={!lab || busy || pending || topology.nodes.length >= 10}
            onDragStart={(event) =>
              event.dataTransfer.setData("application/infra-node", "nginx")
            }
            onClick={() => add("nginx")}
          >
            <span className="palette-icon mint">
              <Cable size={21} />
            </span>
            <span>
              <strong>Nginx</strong>
              <small>反向代理 / 负载均衡</small>
            </span>
            <Plus size={14} />
          </button>
          <button
            className="palette"
            draggable={!!lab && !busy && !pending}
            disabled={!lab || busy || pending || topology.nodes.length >= 10}
            onDragStart={(event) =>
              event.dataTransfer.setData("application/infra-node", "app")
            }
            onClick={() => add("app")}
          >
            <span className="palette-icon blue">
              <Box size={21} />
            </span>
            <span>
              <strong>HTTP 应用</strong>
              <small>真实响应 / 故障实验</small>
            </span>
            <Plus size={14} />
          </button>
          <div className="library-note">
            <Database size={15} />
            <p>
              Redis / MySQL
              <br />
              <span>后续组件 · 尚未开放</span>
            </p>
          </div>
          <div className="section-label spaced">快速开始</div>
          <button
            className="template"
            disabled={!lab || busy || pending || topology.nodes.length > 0}
            onClick={template}
          >
            <FlaskConical size={17} />
            <span>
              一进三出拓扑<small>Nginx + 3 个应用</small>
            </span>
            <ArrowRight size={15} />
          </button>
          <div className="library-footer">
            <div className="quota">
              <span>节点配额</span>
              <span>{topology.nodes.length} / 10</span>
            </div>
            <div className="quota-bar">
              <i style={{ width: `${topology.nodes.length * 10}%` }} />
            </div>
            <button
              className="danger subtle"
              disabled={!lab || !dockerReady || pending || busy}
              onClick={() => setModal("cleanup")}
            >
              <Trash2 size={14} />
              清理运行资源
            </button>
          </div>
        </aside>
        <section
          className={`center dock-${dockMode}`}
          style={
            {
              "--dock-size": dockMode === "collapsed" ? "44px" : `${dockSize}%`,
            } as React.CSSProperties
          }
        >
          <div className="canvas-toolbar">
            <div>
              <span className="tab-active">
                <Layers3 size={15} />
                拓扑画布
              </span>
              <span className="tiny">
                {topology.nodes.length} 节点 / {topology.edges.length} 连接
              </span>
            </div>

            <label className="toggle">
              <input
                type="checkbox"
                checked={realtime}
                disabled={!dockerReady || busy || pending}
                onChange={(event) => setRealtime(event.target.checked)}
              />
              实时添加
            </label>
          </div>
          <div
            ref={canvasRef}
            className="canvas"
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = "copy";
            }}
            onDrop={(event) => {
              event.preventDefault();
              const kind = event.dataTransfer.getData("application/infra-node");
              if ((kind === "app" || kind === "nginx") && !busy && !pending)
                add(
                  kind,
                  screenToFlowPosition({ x: event.clientX, y: event.clientY }),
                );
            }}
          >
            <ReactFlow
              nodes={[
                ...topology.nodes.map((spec) => ({
                  id: spec.id,
                  type: "service",
                  position: spec.position,
                  measured: measurements[spec.id],
                  selected: selected === spec.id,
                  data: {
                    spec,
                    observation: lab?.observation?.nodes.find(
                      (n) => n.id === spec.id,
                    ),
                    stale,
                    traceLabel: traceNodeLabels.get(spec.id),
                  },
                })),
                ...(traceVisible && traceTarget
                  ? [
                      {
                        id: "__client",
                        type: "client",
                        position: {
                          x: traceTarget.position.x - 270,
                          y: traceTarget.position.y,
                        },
                        measured: measurements.__client,
                        draggable: false,
                        selectable: false,
                        data: {
                          spec: traceTarget,
                          stale: !clientStore.online,
                          traceLabel: clientStore.online
                            ? eventName(traceEvents.at(-1))
                            : "观测断开 · 状态过期",
                        },
                      },
                    ]
                  : []),
              ]}
              edges={[
                ...topology.edges.map((e) => ({
                  id: `${e.source}-${e.target}`,
                  source: e.source,
                  target: e.target,
                  label: `w ${e.weight}`,
                  type: "smoothstep",
                  style: { stroke: "#638c7d", strokeWidth: 1.6 },
                  labelStyle: { fill: "#b7cbbf", fontSize: 11 },
                  labelBgStyle: { fill: "#18231e" },
                })),
                ...(traceVisible && traceTarget
                  ? [
                      {
                        id: "trace-client",
                        source: "__client",
                        target: traceTarget.id,
                        label: traceCompletion
                          ? "日志已确认"
                          : traceEvents.some((e) => e.kind === "client.sent")
                            ? "已发起 · 入口待确认"
                            : "等待发送",
                        type: "smoothstep",
                        style: {
                          stroke: "#e6bf71",
                          strokeWidth: 2,
                          strokeDasharray: traceCompletion ? undefined : "5 5",
                        },
                        labelStyle: { fill: "#e6bf71" },
                        labelBgStyle: { fill: "#18231e" },
                      },
                      ...attempts
                        .filter(
                          (a) =>
                            a.node_id &&
                            topology.nodes.some((n) => n.id === a.node_id),
                        )
                        .map((a, i) => ({
                          id: `trace-attempt-${i}`,
                          source: traceTarget.id,
                          target: a.node_id!,
                          label: `实际尝试 ${i + 1} · ${a.status}${a.mapping === "last_observed" ? " · 上次地址" : ""}`,
                          type: "smoothstep",
                          style: { stroke: "#e6bf71", strokeWidth: 3 },
                          labelStyle: { fill: "#e6bf71" },
                          labelBgStyle: { fill: "#18231e" },
                        })),
                    ]
                  : []),
              ]}
              nodeTypes={nodeTypes}
              onNodeClick={(_, n) => {
                if (n.id !== "__client") setSelected(n.id);
              }}
              onPaneClick={() => setSelected(null)}
              onConnect={connect}
              onNodesChange={(changes) => {
                // Controlled nodes must retain React Flow's measured dimensions.
                // Otherwise a subsequent SSE render resets their visibility to hidden.
                setMeasurements((previous) => {
                  let next = previous;
                  for (const update of changes) {
                    if (
                      update.type === "dimensions" &&
                      update.dimensions &&
                      (previous[update.id]?.width !== update.dimensions.width ||
                        previous[update.id]?.height !==
                          update.dimensions.height)
                    ) {
                      next = { ...next, [update.id]: update.dimensions };
                    }
                  }
                  return next;
                });
                let next = topoRef.current;
                for (const change of changes)
                  if (change.type === "position" && change.position)
                    next = {
                      ...next,
                      nodes: next.nodes.map((n) =>
                        n.id === change.id
                          ? { ...n, position: change.position! }
                          : n,
                      ),
                    };
                if (next !== topoRef.current) change(next);
              }}
              nodesDraggable={!busy && !pending}
              nodesConnectable={!busy && !pending}
              deleteKeyCode={null}
              minZoom={0.3}
              maxZoom={1.4}
              fitView
              proOptions={{ hideAttribution: true }}
            >
              <Background color="#293b34" gap={22} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
            {!topology.nodes.length && (
              <div className="canvas-empty">
                <span className="empty-icon">
                  <Layers3 size={32} />
                </span>
                <h2>从一个节点开始</h2>
                <p>
                  将左侧服务拖入画布，连接真实应用。
                  <br />
                  配置保存后，由实验引擎创建容器。
                </p>
                <button
                  className="primary"
                  onClick={() => (lab ? template() : setModal("new"))}
                >
                  <Plus size={16} />
                  {lab ? "载入基础拓扑" : "创建实验"}
                </button>
              </div>
            )}
            <div className="canvas-caption">
              <span className={`dot ${!stale ? "green" : ""}`} />
              {stale ? "运行状态未获取或已过期" : "运行状态已同步"}
              <span>{clock(lab?.observed_at)}</span>
            </div>
          </div>
          <div
            className="dock-separator"
            role="separator"
            aria-label="调整工具区高度"
            aria-orientation="horizontal"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                e.preventDefault();
                setDockSize((v) =>
                  Math.max(
                    20,
                    Math.min(75, v + (e.key === "ArrowUp" ? 5 : -5)),
                  ),
                );
              }
            }}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              setDockMode("normal");
            }}
            onPointerMove={(e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId)) {
                const r =
                  e.currentTarget.parentElement!.getBoundingClientRect();
                setDockSize(
                  Math.max(
                    20,
                    Math.min(75, (100 * (r.bottom - e.clientY)) / r.height),
                  ),
                );
              }
            }}
            onPointerUp={(e) =>
              e.currentTarget.releasePointerCapture(e.pointerId)
            }
          />
          <section className="console">
            <div className="console-tabs">
              <div className="dock-actions">
                <button
                  title={dockMode === "collapsed" ? "展开工具区" : "收起工具区"}
                  onClick={() =>
                    setDockMode(
                      dockMode === "collapsed" ? "normal" : "collapsed",
                    )
                  }
                >
                  {dockMode === "collapsed" ? "展开" : "收起"}
                </button>
                <button
                  title="切换工具区最大化"
                  onClick={() =>
                    setDockMode(
                      dockMode === "maximized" ? "normal" : "maximized",
                    )
                  }
                >
                  {dockMode === "maximized" ? "还原" : "最大化"}
                </button>
              </div>
              <div>
                {(
                  ["client", "requests", "results", "tasks", "logs"] as const
                ).map((key) => (
                  <button
                    className={panel === key ? "active" : ""}
                    onClick={() => {
                      setPanel(key);
                      if (dockMode === "collapsed") setDockMode("normal");
                    }}
                    key={key}
                  >
                    {key === "results" ? (
                      <Activity size={15} />
                    ) : key === "tasks" ? (
                      <Terminal size={15} />
                    ) : (
                      <FileCode2 size={15} />
                    )}
                    {key === "client"
                      ? "客户端"
                      : key === "requests"
                        ? "请求记录"
                        : key === "results"
                          ? "请求实验"
                          : key === "tasks"
                            ? "执行记录"
                            : "容器日志"}
                    {key === "tasks" && pending && (
                      <span className="dot green" />
                    )}
                  </button>
                ))}
              </div>
              <span className="tiny">
                {lab?.applied_revision != null
                  ? `最近完整部署 r${lab.applied_revision}`
                  : "尚未完成部署"}
              </span>
            </div>
            {panel === "client" && (
              <ClientPanel
                lab={lab}
                store={clientStore}
                trace={traceEnabled}
                setTrace={setTraceEnabled}
                disabled={!dockerReady || busy || pending}
                replay={replay}
                setReplay={setReplay}
                send={(target, path) =>
                  safely(() =>
                    submit("client_request", lab, {
                      node_id: target,
                      path,
                      trace: traceEnabled,
                    }),
                  )
                }
              />
            )}
            {panel === "requests" && (
              <div className="requests-panel">
                <table className="request-table">
                  <thead>
                    <tr>
                      <th>来源</th>
                      <th>方法</th>
                      <th>路径</th>
                      <th>状态</th>
                      <th>耗时</th>
                      <th>请求 ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {clientStore.records.map((r) => (
                      <tr
                        key={r.id}
                        className={
                          clientStore.selected === r.id ? "active" : ""
                        }
                        onClick={() => {
                          clientStore.select(r.id);
                          setPanel("client");
                        }}
                      >
                        <td>{r.source === "browser" ? "网站" : "单次"}</td>
                        <td>{r.method || "GET"}</td>
                        <td className="path" title={r.path}>
                          {r.path}
                        </td>
                        <td>
                          {r.error ? "传输失败" : (r.status_code ?? statusText[r.status] ?? r.status)}
                        </td>
                        <td>
                          {r.latency_ms != null ? `${r.latency_ms} ms` : "—"}
                        </td>
                        <td>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              clientStore.select(r.id);
                              setPanel("client");
                            }}
                          >
                            {r.id.slice(0, 8)}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!clientStore.records.length && (
                  <p className="client-empty">尚无请求记录</p>
                )}
              </div>
            )}
            {panel === "results" && (
              <div className="results">
                <div className="load-form">
                  <select
                    aria-label="请求目标"
                    value={loadTarget}
                    onChange={(e) => setLoadTarget(e.target.value)}
                  >
                    <option value="">选择 Nginx 目标</option>
                    {lab?.applied?.nodes
                      .filter((n) => n.kind === "nginx")
                      .map((n) => (
                        <option key={n.id}>{n.id}</option>
                      ))}
                  </select>
                  <label>
                    请求数
                    <input
                      aria-label="请求数"
                      type="number"
                      min="1"
                      max="1000"
                      value={count}
                      onChange={(e) => setCount(Number(e.target.value))}
                    />
                  </label>
                  <label>
                    并发
                    <input
                      aria-label="并发"
                      type="number"
                      min="1"
                      max="20"
                      value={concurrency}
                      onChange={(e) => setConcurrency(Number(e.target.value))}
                    />
                  </label>
                  <label>
                    时限 / 秒
                    <input
                      aria-label="时限"
                      type="number"
                      min="1"
                      max="60"
                      value={duration}
                      onChange={(e) => setDuration(Number(e.target.value))}
                    />
                  </label>
                  <button
                    disabled={!dockerReady || !loadTarget || busy || pending}
                    onClick={() =>
                      void safely(() =>
                        submit("load", lab, {
                          node_id: loadTarget,
                          count,
                          concurrency,
                          duration,
                        }),
                      )
                    }
                  >
                    <Zap size={14} />
                    发起请求
                  </button>
                </div>
                {result ? (
                  <>
                    <label className="run-history">
                      实验结果
                      <select
                        aria-label="历史实验结果"
                        value={resultTask?.id}
                        onChange={(e) => setResultTaskId(e.target.value)}
                      >
                        {completedRuns.map((run) => (
                          <option value={run.id} key={run.id}>
                            {clock(run.created_at)} · r
                            {run.result?.applied_revision} ·{" "}
                            {run.result?.completed} 次请求
                          </option>
                        ))}
                      </select>
                      <span>可切换历史结果对比</span>
                    </label>
                    <div className="metrics">
                      <div>
                        <span>完成请求</span>
                        <strong>
                          {result.completed}
                          <small> / {result.requested}</small>
                        </strong>
                      </div>
                      <div>
                        <span>错误率</span>
                        <strong>
                          {result.error_rate == null
                            ? "—"
                            : (result.error_rate * 100).toFixed(1)}
                          <small>%</small>
                        </strong>
                      </div>
                      <div>
                        <span>P95 延迟</span>
                        <strong>
                          {result.p95_ms?.toFixed(1) ?? "—"}
                          <small>ms</small>
                        </strong>
                      </div>
                      <div>
                        <span>完整部署版本</span>
                        <strong>
                          r{result.applied_revision}
                          <small>{clock(resultTask?.created_at)}</small>
                        </strong>
                      </div>
                    </div>
                    <div className="distribution">
                      {Object.entries(result.distribution).map(
                        ([id, total]) => (
                          <div key={id}>
                            <span>{id}</span>
                            <div>
                              <i
                                style={{
                                  width: `${(total / result.completed) * 100}%`,
                                }}
                              />
                            </div>
                            <b>{total}</b>
                          </div>
                        ),
                      )}
                      {!Object.keys(result.distribution).length && (
                        <p>响应未包含实例标识；查看请求明细和日志。</p>
                      )}
                    </div>
                    <details>
                      <summary>请求明细与 upstream 重试路径</summary>
                      <pre>
                        {JSON.stringify(
                          {
                            samples: result.samples,
                            upstream_logs: result.upstream_logs,
                            upstream_logs_available:
                              result.upstream_logs_available,
                            runtime_config: result.runtime_config,
                            runtime_nodes: result.runtime_nodes,
                          },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  </>
                ) : (
                  <div className="console-empty">
                    <Activity size={24} />
                    <div>
                      等待第一次真实请求
                      <span>
                        部署拓扑后，选择 Nginx
                        节点开始实验。这里不会生成演示指标。
                      </span>
                    </div>
                  </div>
                )}
              </div>
            )}
            {panel === "tasks" && (
              <div className="task-list">
                {!tasks.length && (
                  <div className="console-empty">
                    <Terminal size={23} />
                    暂无执行记录
                  </div>
                )}
                {tasks.map((task) => (
                  <details
                    key={task.id}
                    open={["running", "failed", "interrupted"].includes(
                      task.status,
                    )}
                  >
                    <summary>
                      <span className={`task-status ${task.status}`}>
                        {task.status === "succeeded" ? (
                          <Check size={14} />
                        ) : (
                          <Circle size={12} />
                        )}
                        {statusText[task.status]}
                      </span>
                      <strong>{taskText[task.kind]}</strong>
                      <span>r{task.payload.action.revision}</span>
                      <time>{clock(task.created_at)}</time>
                    </summary>
                    <pre>
                      {task.logs
                        .map((line) => `[${clock(line.time)}] ${line.message}`)
                        .join("\n") || "任务已持久化，等待 Worker。"}
                    </pre>
                  </details>
                ))}
              </div>
            )}
            {panel === "logs" && (
              <div className="logs">
                <button
                  disabled={!node || !dockerReady || busy}
                  onClick={() =>
                    void safely(async () => {
                      const result = await api<{ text: string }>(
                        `/labs/${lab?.id}/nodes/${selected}/logs`,
                      );
                      setLogs(
                        `[${new Date().toLocaleTimeString()}] ${selected}\n${result.text}`,
                      );
                    })
                  }
                >
                  <RotateCcw size={14} />
                  读取选中节点日志
                </button>
                <pre>{logs}</pre>
              </div>
            )}
          </section>
        </section>
        <aside className="inspector">
          <div
            className="inspector-resizer"
            role="separator"
            aria-label="调整检查器宽度"
            aria-orientation="vertical"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                e.preventDefault();
                setInspectorWidth((v) =>
                  Math.max(
                    260,
                    Math.min(500, v + (e.key === "ArrowLeft" ? 20 : -20)),
                  ),
                );
              }
            }}
            onPointerDown={(e) =>
              e.currentTarget.setPointerCapture(e.pointerId)
            }
            onPointerMove={(e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId))
                setInspectorWidth(
                  Math.max(
                    260,
                    Math.min(
                      500,
                      e.currentTarget.parentElement!.getBoundingClientRect()
                        .right - e.clientX,
                    ),
                  ),
                );
            }}
            onPointerUp={(e) =>
              e.currentTarget.releasePointerCapture(e.pointerId)
            }
          />
          <button
            className="inspector-close icon-button"
            aria-label="关闭检查器"
            onClick={() => setSelected(null)}
          >
            <X size={16} />
          </button>
          <div className="section-label">
            <span>
              <Server size={15} />
              节点检查器
            </span>
            {node && <span className="tag">{node.kind}</span>}
          </div>
          {node ? (
            <>
              <div className="inspector-title">
                <h2>{node.id}</h2>
                <span>内部域名 · {node.id}:8080</span>
              </div>
              <div className="status-grid">
                <div>
                  容器
                  <strong>
                    {stale ? "未知 / 过期" : observation?.status || "未部署"}
                  </strong>
                </div>
                <div>
                  健康检查
                  <strong>
                    {stale
                      ? "未知 / 过期"
                      : statusText[observation?.health || "unknown"]}
                  </strong>
                </div>
                <div>
                  内存实测
                  <strong>
                    {!stale && observation?.memory_bytes != null
                      ? `${(observation.memory_bytes / 1048576).toFixed(1)} MB`
                      : "—"}
                  </strong>
                </div>
                <div>
                  采集时间<strong>{clock(observation?.observed_at)}</strong>
                </div>
              </div>
              <div className="node-actions">
                <button
                  disabled={!observation || busy || pending || !dockerReady}
                  onClick={() =>
                    void safely(() =>
                      submit("start", lab, { node_id: node.id }),
                    )
                  }
                >
                  <Play size={13} />
                  启动
                </button>
                <button
                  disabled={!observation || busy || pending || !dockerReady}
                  onClick={() =>
                    void safely(() => submit("stop", lab, { node_id: node.id }))
                  }
                >
                  <Square size={13} />
                  停止
                </button>
                <button
                  className="danger"
                  disabled={busy || pending}
                  title="从草稿移除；应用后才删除容器"
                  onClick={() => {
                    change({
                      nodes: topology.nodes.filter((n) => n.id !== node.id),
                      edges: topology.edges.filter(
                        (e) => e.source !== node.id && e.target !== node.id,
                      ),
                    });
                    setSelected(null);
                  }}
                >
                  <Trash2 size={13} />
                  移除
                </button>
              </div>
              <div className="section-label spaced">资源限制</div>
              <div className="field-row">
                <label>
                  内存 / MB
                  <input
                    {...inputProps}
                    type="number"
                    min="64"
                    max="512"
                    value={node.memory_mb}
                    onChange={(e) =>
                      editNode({ memory_mb: Number(e.target.value) })
                    }
                  />
                </label>
                <label>
                  CPU / 核
                  <input
                    {...inputProps}
                    type="number"
                    min="0.1"
                    max="1"
                    step="0.1"
                    value={node.cpu}
                    onChange={(e) => editNode({ cpu: Number(e.target.value) })}
                  />
                </label>
              </div>
              {node.kind === "app" ? (
                <>
                  <div className="section-label spaced">响应行为</div>
                  <label className="full-field">
                    响应延迟 / ms
                    <input
                      {...inputProps}
                      type="number"
                      min="0"
                      max="10000"
                      value={node.delay_ms}
                      onChange={(e) =>
                        editNode({ delay_ms: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="full-field">
                    错误响应比例 / %
                    <input
                      {...inputProps}
                      type="number"
                      min="0"
                      max="100"
                      value={node.error_rate}
                      onChange={(e) =>
                        editNode({ error_rate: Number(e.target.value) })
                      }
                    />
                  </label>
                  <p className="hint">
                    错误响应为 HTTP 503。应用变更后重建此实例，Nginx
                    的转发与重试行为由真实服务执行。
                  </p>
                </>
              ) : (
                <>
                  <div className="section-label spaced">UPSTREAM 连接</div>
                  <div className="edge-list">
                    {topology.edges
                      .filter((e) => e.source === node.id)
                      .map((edge) => (
                        <div key={edge.target}>
                          <span>{edge.target}</span>
                          <label>
                            权重
                            <input
                              {...inputProps}
                              aria-label={`${edge.target} 权重`}
                              type="number"
                              min="1"
                              max="100"
                              value={edge.weight}
                              onChange={(event) =>
                                change({
                                  ...topology,
                                  edges: topology.edges.map((e) =>
                                    e === edge
                                      ? {
                                          ...e,
                                          weight: Number(event.target.value),
                                        }
                                      : e,
                                  ),
                                })
                              }
                            />
                          </label>
                          <button
                            className="icon-button"
                            title="移除连接"
                            disabled={busy || pending}
                            onClick={() =>
                              change(
                                {
                                  ...topology,
                                  edges: topology.edges.filter(
                                    (e) => e !== edge,
                                  ),
                                },
                                true,
                              )
                            }
                          >
                            <X size={13} />
                          </button>
                        </div>
                      ))}
                    {!topology.edges.some((e) => e.source === node.id) && (
                      <p className="hint">
                        从 Nginx 右侧端点连到应用左侧端点。
                      </p>
                    )}
                  </div>
                  <div className="section-label spaced">
                    <span>nginx.conf</span>
                    <button
                      className="link"
                      onClick={() => setExpandedEditor(true)}
                    >
                      展开编辑
                    </button>
                    <button
                      className="link"
                      disabled={busy || pending}
                      onClick={() => void safely(regenerate)}
                    >
                      重新生成 / 比较
                    </button>
                  </div>
                  <div className="config-meta">
                    <span>
                      {node.mode === "manual"
                        ? "手动配置 · 拓扑不覆盖"
                        : "拓扑生成 · 保存时更新"}
                    </span>
                    <button
                      className="link"
                      onClick={() =>
                        void safely(async () => {
                          setRevisionHistory(
                            await api(`/labs/${lab?.id}/revisions`),
                          );
                        })
                      }
                    >
                      版本
                    </button>
                  </div>
                  <div className="editor">
                    <Editor
                      theme="vs-dark"
                      language="plaintext"
                      path={node.id + ".conf"}
                      value={node.config}
                      onChange={(value) =>
                        editNode({ config: value || "", mode: "manual" })
                      }
                      options={{
                        minimap: { enabled: false },
                        fontSize: 12,
                        lineNumbersMinChars: 3,
                        scrollBeyondLastLine: false,
                        wordWrap: "on",
                        readOnly: busy || pending,
                        automaticLayout: true,
                      }}
                    />
                  </div>
                  <p className="hint">
                    保存不会重载服务。应用时先执行 nginx -t，再确认新 worker
                    启动。手动配置需保留 8080 端口和健康检查路径。
                  </p>
                </>
              )}
            </>
          ) : (
            <div className="inspector-empty">
              <Server size={30} />
              <h3>查看节点详情</h3>
              <p>选中画布节点，编辑配置、查看状态，或控制服务启停。</p>
              <div>
                <Radio size={14} />
                真实状态 · 持续同步
              </div>
            </div>
          )}
        </aside>
      </main>
      <footer className="footer">
        <span>
          <span className={`dot ${connected ? "green" : ""}`} />
          {connected ? "事件流已连接" : "事件流未连接"}
        </span>
        <span>
          {pending ? "实验任务执行中" : dirty ? "有未保存的编辑" : "工作台就绪"}
        </span>
      </footer>
      {modal && (
        <div className="modal-backdrop">
          <div className="modal">
            <button
              className="close icon-button"
              onClick={() => setModal(null)}
            >
              <X size={18} />
            </button>
            <h2>{modal === "new" ? "创建独立实验" : "清理运行资源"}</h2>
            {modal === "new" ? (
              <>
                <p>草稿与实验结果独立保存，部署时创建专用网络。</p>
                <label className="full-field">
                  实验名称
                  <input
                    autoFocus
                    value={name}
                    maxLength={80}
                    onChange={(e) => setName(e.target.value)}
                  />
                </label>
                <button
                  className="primary"
                  disabled={busy || !name.trim()}
                  onClick={() => void safely(createLab)}
                >
                  创建实验
                </button>
              </>
            ) : (
              <>
                <p>
                  将停止并删除此实验的容器和网络。草稿、配置版本和历史结果保留，可以再次应用。
                </p>
                <button
                  className="danger"
                  disabled={busy}
                  onClick={() =>
                    void safely(async () => {
                      await submit("cleanup");
                      setModal(null);
                    })
                  }
                >
                  确认清理此实验
                </button>
              </>
            )}
          </div>
        </div>
      )}
      {diff !== null && node && (
        <div className="modal-backdrop">
          <div className="diff-modal">
            <div className="section-label">
              当前配置 → 按拓扑生成
              <button className="icon-button" onClick={() => setDiff(null)}>
                <X size={18} />
              </button>
            </div>
            <ConfigDiff original={node.config} modified={diff} />
            <div className="diff-actions">
              <button onClick={() => setDiff(null)}>保留当前配置</button>
              <button
                className="primary"
                onClick={() => {
                  editNode({ config: diff, mode: "generated" });
                  setDiff(null);
                }}
              >
                采用生成配置
              </button>
            </div>
          </div>
        </div>
      )}
      {revisionHistory && (
        <div className="modal-backdrop">
          <div className="diff-modal">
            <div className="section-label">
              配置历史（只读）
              <button
                className="icon-button"
                onClick={() => {
                  setRevisionHistory(null);
                  setHistoryConfig(null);
                }}
              >
                <X size={18} />
              </button>
            </div>
            <div className="history-picker">
              {revisionHistory.map((r) => (
                <button
                  key={r.revision}
                  onClick={() =>
                    setHistoryConfig(
                      r.topology.nodes.find((n) => n.id === selected)?.config ||
                        "此版本无该节点配置",
                    )
                  }
                >
                  r{r.revision}
                </button>
              ))}
            </div>
            <Editor
              height="60vh"
              theme="vs-dark"
              value={historyConfig || "选择版本查看保存的完整配置"}
              options={{ readOnly: true, minimap: { enabled: false } }}
            />
          </div>
        </div>
      )}
      {expandedEditor && node && (
        <div className="modal-backdrop">
          <div className="diff-modal">
            <div className="section-label">
              {node.id} / nginx.conf · 编辑草稿
              <button
                className="icon-button"
                onClick={() => setExpandedEditor(false)}
              >
                <X size={18} />
              </button>
            </div>
            <Editor
              height="65vh"
              theme="vs-dark"
              path={node.id + ".expanded.conf"}
              value={node.config}
              onChange={(value) =>
                editNode({ config: value || "", mode: "manual" })
              }
              options={{
                fontSize: 14,
                minimap: { enabled: false },
                automaticLayout: true,
                readOnly: busy || pending,
              }}
            />
            <div className="diff-actions">
              <span className="hint">关闭后保留编辑；保存草稿不会部署。</span>
              <button onClick={() => setExpandedEditor(false)}>完成编辑</button>
              <button
                className="primary"
                disabled={busy || pending || !dirty}
                onClick={() => void safely(() => save())}
              >
                <Save size={15} />
                保存草稿
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
