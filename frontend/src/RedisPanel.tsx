import { useState } from "react";
import type { Lab, Task } from "./types";

export default function RedisPanel({
  lab,
  tasks,
  disabled,
  submit,
}: {
  lab: Lab;
  tasks: Task[];
  disabled: boolean;
  submit: (extra: Record<string, unknown>) => Promise<void>;
}) {
  const nodes = (lab.applied?.nodes || []).filter((n) => n.kind === "redis");
  const [target, setTarget] = useState("");
  const [op, setOp] = useState("GET");
  const [key, setKey] = useState("demo");
  const [value, setValue] = useState("hello");
  const [ttl, setTtl] = useState(0);
  const nodeId = nodes.some((n) => n.id === target)
    ? target
    : nodes[0]?.id || "";
  return (
    <div className="redis-panel">
      <form
        className="redis-command"
        onSubmit={(e) => {
          e.preventDefault();
          void submit({ node_id: nodeId, redis_op: op, key, value, ttl });
        }}
      >
        <select
          aria-label="Redis 节点"
          value={nodeId}
          onChange={(e) => setTarget(e.target.value)}
        >
          {nodes.map((n) => (
            <option key={n.id}>{n.id}</option>
          ))}
        </select>
        <select
          aria-label="Redis 命令"
          value={op}
          onChange={(e) => setOp(e.target.value)}
        >
          {["GET", "SET", "DEL", "TTL", "DBSIZE", "PING"].map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
        {!["PING", "DBSIZE"].includes(op) && (
          <input
            aria-label="键"
            placeholder="键"
            value={key}
            maxLength={256}
            required
            onChange={(e) => setKey(e.target.value)}
          />
        )}
        {op === "SET" && (
          <>
            <input
              aria-label="值"
              placeholder="值"
              value={value}
              maxLength={4096}
              onChange={(e) => setValue(e.target.value)}
            />
            <label>
              TTL / 秒
              <input
                aria-label="TTL 秒，0 为不过期"
                type="number"
                min={0}
                max={86400}
                value={ttl}
                onChange={(e) => setTtl(Number(e.target.value))}
              />
            </label>
          </>
        )}
        <button className="primary" disabled={disabled || !nodeId}>
          执行
        </button>
      </form>
      {!nodes.length && <p>先添加 Redis 节点并应用拓扑。</p>}
      <table className="request-table">
        <thead>
          <tr>
            <th>节点</th>
            <th>实际角色</th>
            <th>复制来源</th>
            <th>连接</th>
            <th>复制偏移</th>
            <th>采集时间</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => {
            const observed = lab.observation?.nodes.find((o) => o.id === n.id);
            const info = observed?.redis;
            const stale =
              !lab.observation?.available ||
              !observed ||
              Date.now() / 1000 - observed.observed_at > 10;
            const fields =
              !stale && observed?.status === "running" && info?.available
                ? info.fields
                : undefined;
            return (
              <tr key={n.id}>
                <td>{n.id}</td>
                <td>
                  {fields
                    ? fields.role === "master"
                      ? "主节点"
                      : fields.role === "slave"
                        ? "从节点"
                        : fields.role
                    : stale
                      ? "未知 / 过期"
                      : observed?.status === "exited"
                        ? "已停止"
                        : "未知"}
                </td>
                <td>{fields?.master_host ?? "—"}</td>
                <td>
                  {(fields?.master_link_status === "up"
                    ? "已连接"
                    : fields?.master_link_status === "down"
                      ? "断开"
                      : fields?.master_link_status) ??
                    (fields?.role === "master"
                      ? `${fields.connected_slaves} 个从节点`
                      : "—")}
                </td>
                <td>
                  {fields?.slave_repl_offset ??
                    fields?.master_repl_offset ??
                    "—"}
                </td>
                <td>
                  {info
                    ? new Date(info.observed_at * 1000).toLocaleTimeString()
                    : "—"}
                  {info?.error && <span title={info.error}> 采集失败</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="redis-history">
        {tasks
          .filter((t) => t.kind === "redis_command")
          .slice(0, 20)
          .map((t) => (
            <div key={t.id}>
              <strong>
                {t.payload.action.node_id} · {t.payload.action.redis_op}
              </strong>{" "}
              <span>
                {{
                  queued: "等待执行",
                  running: "执行中",
                  succeeded: "已完成",
                  failed: "失败",
                  interrupted: "执行中断",
                }[t.status] ?? t.status}
              </span>
              <pre>
                {t.result
                  ? (t.result.error ?? JSON.stringify(t.result.value))
                  : "等待执行结果"}
              </pre>
            </div>
          ))}
      </div>
    </div>
  );
}
