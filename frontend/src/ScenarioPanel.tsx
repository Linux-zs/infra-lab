import { useEffect, useState } from "react";
import {
  api,
  type Lab,
  type LoadResult,
  type Task,
  type Topology,
  type Observation,
} from "./types";

export type ScenarioTemplate = {
  id: string;
  version: number;
  title: string;
  node_count: number;
  memory_mb: number;
  metrics: string[];
  steps: {
    id: string;
    title: string;
    effect: string;
    observe: string;
    measure: "load" | "request" | "redis";
  }[];
};
type Snapshot = {
  applied_revision: number | null;
  topology: Topology | null;
  collected_at: number;
  observation: { available: boolean; nodes: Observation[] };
};
type RequestMeasurement = {
  request_id: string;
  status_code: number | null;
  latency_ms: number | null;
  node?: string;
  response_version?: string;
  error?: string;
  trace_complete: boolean | null;
  trace_note: string;
  truncated: boolean;
  bytes_captured: number;
  runtime_config?: string;
  events: {
    seq: number;
    kind: string;
    node_id?: string;
    time: number;
    data: {
      attempts?: {
        node_id?: string;
        address: string;
        status: string;
        mapping?: string;
      }[];
      response_version?: number;
    };
  }[];
};
type Command = {
  node_id: string;
  operation: string;
  key: string;
  value?: string | number | null;
  error?: string;
  expected_error?: boolean;
  observed_at: number;
};
type Run = {
  task_id: string;
  step_id: string;
  status: string;
  parameters: {
    count: number;
    concurrency: number;
    duration: number;
    trace: boolean;
    value: string;
  };
  snapshot: {
    template_version: number;
    submitted_revision: number;
    target_revision: number;
    before: Snapshot | null;
    after: Snapshot | null;
  };
  result: {
    error?: string;
    completed_actions?: { action: string; time: number }[];
    load?: Partial<LoadResult>;
    request?: RequestMeasurement;
    commands?: Command[];
  } | null;
  created_at: number;
};
type State = { template: ScenarioTemplate; runs: Run[] };
const statuses: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  interrupted: "中断 · 后续状态未知",
};
const when = (time: number) =>
  new Date(time * 1000).toLocaleTimeString("zh-CN", { hour12: false });
const metric = (value: number | null | undefined, suffix = "") =>
  value == null ? "—" : `${value}${suffix}`;

export default function ScenarioPanel({
  lab,
  tasks,
  disabled,
  dirty,
  connected,
  trace,
  onTraceChange,
  run,
  openRequest,
  newScenario,
}: {
  lab: Lab;
  tasks: Task[];
  disabled: boolean;
  dirty: boolean;
  connected: boolean;
  trace: boolean;
  onTraceChange: (value: boolean) => Promise<void>;
  run: (step: string, parameters: Record<string, unknown>) => Promise<void>;
  openRequest: (id: string) => void;
  newScenario: () => void;
}) {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [selectedStep, setSelectedStep] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [comparison, setComparison] = useState("");
  const [working, setWorking] = useState(false);
  const [count, setCount] = useState(120);
  const [concurrency, setConcurrency] = useState(1);
  const [duration, setDuration] = useState(30);
  const [value, setValue] = useState("场景复制验证");
  const stamp = tasks
    .map((t) => `${t.id}:${t.status}:${t.logs.length}`)
    .join("|");
  useEffect(() => {
    let active = true;
    api<State>(`/labs/${lab.id}/scenario`)
      .then((next) => {
        if (active) {
          setState(next);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, [lab.id, stamp]);
  if (!state)
    return (
      <div className="scenario-loading" role="status">
        {error || "正在读取实验步骤…"}
      </div>
    );
  const step =
    state.template.steps.find((s) => s.id === selectedStep) ||
    state.template.steps[0];
  const current =
    state.runs.find((r) => r.task_id === selectedRun) || state.runs[0];
  const before = state.runs.find((r) => r.task_id === comparison);
  const paramsError =
    step.measure === "load" &&
    (!Number.isInteger(count) ||
      count < 1 ||
      count > 1000 ||
      !Number.isInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > 20 ||
      !Number.isInteger(duration) ||
      duration < 1 ||
      duration > 60);
  async function execute() {
    setWorking(true);
    try {
      await run(step.id, { count, concurrency, duration, trace, value });
      setSelectedRun("");
    } finally {
      setWorking(false);
    }
  }
  return (
    <section className="scenario-panel">
      <div className="scenario-toolbar">
        <strong>{state.template.title}</strong>
        <span className="tag">模板 v{state.template.version}</span>
        <label className="toggle">
          <input
            type="checkbox"
            aria-label="场景请求链路"
            checked={trace}
            disabled={working || !connected}
            onChange={(e) => {
              setWorking(true);
              void onTraceChange(e.target.checked).finally(() =>
                setWorking(false),
              );
            }}
          />
          请求链路
        </label>
        <button onClick={newScenario}>新建场景实验</button>
      </div>
      <div className="scenario-content">
        <nav className="scenario-steps" aria-label="实验步骤">
          {state.template.steps.map((s, i) => (
            <button
              key={s.id}
              className={step.id === s.id ? "active" : ""}
              aria-current={step.id === s.id ? "step" : undefined}
              onClick={() => setSelectedStep(s.id)}
            >
              <span>{i + 1}</span>
              <strong>{s.title}</strong>
            </button>
          ))}
        </nav>
        <div className="scenario-detail">
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {!connected && (
            <p className="client-notice" role="status">
              连接中断，运行状态可能已过期。历史结果保留。
            </p>
          )}
          {!lab.applied && (
            <p className="client-notice">尚未部署，请先点击“应用拓扑”。</p>
          )}
          {dirty && (
            <p className="client-notice">
              请先保存当前拓扑编辑，再执行实验步骤。
            </p>
          )}
          <div className="scenario-effect">
            <strong>执行内容</strong>
            <p>{step.effect}</p>
            <strong>观察内容</strong>
            <p>{step.observe}</p>
          </div>
          <div className="scenario-run-controls">
            {step.measure === "load" && (
              <>
                <label>
                  请求数
                  <input
                    aria-label="场景请求数"
                    type="number"
                    min={1}
                    max={1000}
                    step={1}
                    value={count}
                    onChange={(e) => setCount(Number(e.target.value))}
                  />
                </label>
                <label>
                  并发
                  <input
                    aria-label="场景并发"
                    type="number"
                    min={1}
                    max={20}
                    step={1}
                    value={concurrency}
                    onChange={(e) => setConcurrency(Number(e.target.value))}
                  />
                </label>
                <label>
                  时限 / 秒
                  <input
                    aria-label="场景时限"
                    type="number"
                    min={1}
                    max={60}
                    step={1}
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                  />
                </label>
              </>
            )}
            {step.measure === "redis" && step.id === "replicate" && (
              <label className="scenario-value">
                写入内容
                <input
                  aria-label="场景写入内容"
                  value={value}
                  maxLength={4096}
                  onChange={(e) => setValue(e.target.value)}
                />
              </label>
            )}
            <button
              className="primary"
              disabled={
                disabled || dirty || working || !lab.applied || !!paramsError
              }
              onClick={() => void execute()}
            >
              执行此步骤
            </button>
          </div>
          {paramsError && (
            <p className="error" role="alert">
              请求数需为 1–1000、并发需为 1–20、时限需为 1–60 的整数。
            </p>
          )}
          <div className="scenario-history-picker">
            <label>
              本次结果
              <select
                aria-label="选择场景执行记录"
                value={current?.task_id || ""}
                onChange={(e) => setSelectedRun(e.target.value)}
              >
                {!state.runs.length && <option value="">尚无执行记录</option>}
                {state.runs.map((r) => (
                  <option key={r.task_id} value={r.task_id}>
                    {when(r.created_at)} ·{" "}
                    {state.template.steps.find((s) => s.id === r.step_id)
                      ?.title || r.step_id}{" "}
                    · {statuses[r.status] || r.status}
                  </option>
                ))}
              </select>
            </label>
            <label>
              对比记录
              <select
                aria-label="选择场景对比记录"
                value={comparison}
                onChange={(e) => setComparison(e.target.value)}
              >
                <option value="">不对比</option>
                {state.runs
                  .filter((r) => r.task_id !== current?.task_id)
                  .map((r) => (
                    <option key={r.task_id} value={r.task_id}>
                      {when(r.created_at)} ·{" "}
                      {state.template.steps.find((s) => s.id === r.step_id)
                        ?.title || r.step_id}{" "}
                      · {statuses[r.status] || r.status}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          {current ? (
            <>
              <div className="scenario-outcomes">
                {before && (
                  <RunResult
                    item={before}
                    label="对比记录"
                    openRequest={openRequest}
                  />
                )}
                <RunResult
                  item={current}
                  label="本次结果"
                  openRequest={openRequest}
                />
              </div>
              {before?.result?.load && current.result?.load && (
                <DistributionComparison before={before} after={current} />
              )}
              <details className="scenario-snapshot">
                <summary>配置与运行快照</summary>
                <pre>
                  {JSON.stringify(
                    {
                      parameters: current.parameters,
                      snapshot: current.snapshot,
                      actual_nginx_config:
                        current.result?.load?.runtime_config ||
                        current.result?.request?.runtime_config,
                      completed_actions: current.result?.completed_actions,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </>
          ) : (
            <div className="scenario-empty">
              执行步骤后显示真实结果和运行配置。
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function RunResult({
  item,
  label,
  openRequest,
}: {
  item: Run;
  label: string;
  openRequest: (id: string) => void;
}) {
  const load = item.result?.load,
    request = item.result?.request;
  const nginx = request?.events.find((e) => e.kind === "nginx.completed");
  const nodes = item.snapshot.after?.observation?.nodes;
  return (
    <article className="scenario-outcome">
      <div className="scenario-outcome-heading">
        <strong>{label}</strong>
        <span>{statuses[item.status] || item.status}</span>
        <time>{when(item.created_at)}</time>
      </div>
      <div className="scenario-run-version">
        运行 r
        {item.snapshot.after?.applied_revision ??
          item.snapshot.before?.applied_revision ??
          "未知"}{" "}
        · 模板 v{item.snapshot.template_version} · {item.task_id.slice(0, 8)}
      </div>
      {item.result?.error && <p className="error">{item.result.error}</p>}
      {!item.result && (
        <p>
          {item.status === "queued"
            ? "等待执行，尚无运行观测。"
            : "测量尚未完成，结果未知。"}
        </p>
      )}
      {load && (
        <>
          <div className="scenario-metrics">
            <span>
              完成
              <strong>
                {metric(load.completed)} / {metric(load.requested)}
              </strong>
            </span>
            <span>
              错误率
              <strong>
                {metric(
                  load.error_rate == null
                    ? null
                    : Math.round(load.error_rate * 10000) / 100,
                  "%",
                )}
              </strong>
            </span>
            <span>
              P95<strong>{metric(load.p95_ms, " ms")}</strong>
            </span>
          </div>
          <div className="scenario-parameters">
            请求 {item.parameters.count} · 并发 {item.parameters.concurrency} ·
            时限 {item.parameters.duration} 秒
          </div>
          {!!load.unidentified && (
            <p className="client-notice">
              {load.unidentified} 次响应未确认实例标识。
            </p>
          )}
          <table className="scenario-table">
            <thead>
              <tr>
                <th>实例</th>
                <th>实际请求数</th>
                <th>实际占比</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(load.distribution || {}).map(([name, count]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{count}</td>
                  <td>
                    {load.completed
                      ? `${Math.round((count / load.completed) * 1000) / 10}%`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {request && (
        <>
          <div className="scenario-metrics">
            <span>
              HTTP<strong>{metric(request.status_code)}</strong>
            </span>
            <span>
              耗时<strong>{metric(request.latency_ms, " ms")}</strong>
            </span>
            <span>
              实例<strong>{request.node || "未确认"}</strong>
            </span>
          </div>
          <p>
            响应版本{" "}
            {request.response_version
              ? `v${request.response_version}`
              : "未确认"}{" "}
            · 请求 {request.request_id}
          </p>
          {request.error && <p className="error">{request.error}</p>}
          <div className="scenario-attempts">
            {nginx
              ? nginx.data.attempts?.length
                ? nginx.data.attempts.map((a, i) => (
                    <span key={i}>
                      {i > 0 && " → "}
                      {a.node_id || a.address} [{a.status}]
                      {a.mapping === "last_observed" && " · 历史地址"}
                    </span>
                  ))
                : "Nginx 日志未记录应用 upstream。"
              : "未确认 upstream 路径。"}
          </div>
          <p className="scenario-trace-state">
            {item.parameters.trace
              ? request.trace_complete
                ? "日志已关联 · 路径为事后还原"
                : request.trace_note
              : "本次未采集详细链路"}
          </p>
          <button onClick={() => openRequest(request.request_id)}>
            查看请求
          </button>
        </>
      )}
      {item.result?.commands && (
        <table className="scenario-table">
          <thead>
            <tr>
              <th>节点 / 操作</th>
              <th>真实返回</th>
              <th>采集时间</th>
            </tr>
          </thead>
          <tbody>
            {item.result.commands.map((command, i) => (
              <tr key={i}>
                <td>
                  {command.node_id}
                  <br />
                  {command.operation} {command.key}
                </td>
                <td>
                  {command.error ||
                    (command.value == null
                      ? "nil（键不存在或尚未同步）"
                      : String(command.value))}
                </td>
                <td>{when(command.observed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {nodes && item.result?.commands && (
        <div className="scenario-roles">
          {nodes.map((n) => (
            <span key={n.id}>
              {n.id}：
              {n.status === "running" && n.redis?.available
                ? `${n.redis.fields?.role || "未知"} · ${n.redis.fields?.master_link_status || "无复制来源"}`
                : n.status === "exited"
                  ? "已停止"
                  : "未知"}
            </span>
          ))}
        </div>
      )}
    </article>
  );
}

function DistributionComparison({
  before,
  after,
}: {
  before: Run;
  after: Run;
}) {
  const a = before.result!.load!,
    b = after.result!.load!;
  const names = [
    ...new Set([
      ...Object.keys(a.distribution || {}),
      ...Object.keys(b.distribution || {}),
    ]),
  ];
  return (
    <table className="scenario-table scenario-comparison">
      <caption>请求分布对比</caption>
      <thead>
        <tr>
          <th>实例</th>
          <th>对比记录</th>
          <th>本次结果</th>
        </tr>
      </thead>
      <tbody>
        {names.map((name) => (
          <tr key={name}>
            <td>{name}</td>
            {[a, b].map((v, i) => {
              const count = v.distribution?.[name];
              return (
                <td key={i}>
                  {count == null
                    ? "未观测到该实例"
                    : `${count} 次${v.completed ? ` · ${Math.round((count / v.completed) * 1000) / 10}%` : ""}`}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
