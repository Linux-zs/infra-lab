import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { api, type Lab } from "./types";

export type TraceEvent = {
  seq: number;
  request_id: string;
  kind: string;
  source: string;
  node_id: string | null;
  time: number;
  evidence: string;
  data: {
    response_version?: number | null;
    attempts?: {
      node_id?: string;
      address: string;
      status: string;
      mapping?: string;
    }[];
  };
};
type RequestSummary = {
  error?: string;
  source?: "browser" | "single";
  method?: string;
  status_code?: number;
  latency_ms?: number;
  id: string;
  node_id: string;
  path: string;
  trace: boolean;
  status: string;
  updated_at: number;
};
export type ClientRequest = RequestSummary & {
  events: TraceEvent[];
  result?: {
    body?: string;
    content_type?: string;
    status_code?: number;
    headers?: [string, string][];
    latency_ms?: number;
    bytes_captured?: number;
    truncated?: boolean;
    error?: string;
    error_kind?: string;
    trace_complete?: boolean;
    trace_note?: string;
    applied_revision?: number;
  };
};
const names: Record<string, string> = {
  "client.sent": "客户端已发起请求 · 入口接收待确认",
  "client.headers": "客户端已收到响应头",
  "client.response": "客户端响应接收结束",
  "client.failed": "客户端请求失败",
  "client.timeout": "客户端已超时 · 应用是否结束另行观测",
  "app.received": "应用已接收",
  "app.processing": "应用处理中",
  "app.response_written": "应用已写出响应",
  "app.response_aborted": "应用写出响应时连接已断开",
  "nginx.completed": "Nginx 完成日志已采集（事后证据）",
  "worker.interrupted": "采集器中断 · 后续状态未知",
  "worker.failed": "采集失败",
};
export const eventName = (event?: TraceEvent) =>
  event ? names[event.kind] || event.kind : "等待观测证据";

export function useClientRequests(labId?: string) {
  const [ownerLab, setOwnerLab] = useState(labId);
  const [records, setRecords] = useState<RequestSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<ClientRequest | null>(null);
  const [online, setOnline] = useState(false);
  const [error, setError] = useState("");
  const [activeIncoming, setActiveIncoming] = useState<TraceEvent[]>([]);
  const eventCache = useRef(new Map<string, TraceEvent[]>());
  const cursor = useRef(0);
  const active = ownerLab === labId ? selected || records[0]?.id || "" : "";
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => {
    setOwnerLab(labId);
    setRecords([]);
    setSelected("");
    setDetail(null);
    setActiveIncoming([]);
    eventCache.current.clear();
    setError("");
    cursor.current = 0;
    if (!labId) return;
    let lastReceived = Date.now();
    const offline = () => setOnline(false);
    window.addEventListener("offline", offline);
    const watchdog = setInterval(() => {
      if (Date.now() - lastReceived > 5000 || !navigator.onLine)
        setOnline(false);
    }, 1000);
    const stream = new EventSource(`/api/labs/${labId}/requests/events`);
    stream.addEventListener("requests", (e) => {
      const next: RequestSummary[] = JSON.parse((e as MessageEvent).data);
      setRecords((previous) =>
        JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
      );
      lastReceived = Date.now();
      setOnline(navigator.onLine);
    });
    stream.addEventListener("trace", (e) => {
      const value: TraceEvent = JSON.parse((e as MessageEvent).data);
      if (value.seq <= cursor.current) return;
      cursor.current = value.seq;
      const events = [...(eventCache.current.get(value.request_id) || []), value].slice(-500);
      eventCache.current.delete(value.request_id);
      eventCache.current.set(value.request_id, events);
      while (eventCache.current.size > 100)
        eventCache.current.delete(eventCache.current.keys().next().value!);
      if (value.request_id === activeRef.current) setActiveIncoming(events);
    });
    stream.onerror = () => setOnline(false);
    return () => {
      clearInterval(watchdog);
      window.removeEventListener("offline", offline);
      stream.close();
      setOnline(false);
    };
  }, [labId]);
  const updated = records.find((r) => r.id === active)?.updated_at;
  useEffect(() => {
    setDetail(null);
    setActiveIncoming(eventCache.current.get(active) || []);
  }, [labId, active]);
  useEffect(() => {
    if (!labId || !active) return;
    let disposed = false;
    api<ClientRequest>(`/labs/${labId}/requests/${active}`)
      .then((value) => {
        if (!disposed) {
          setDetail(value);
          setError("");
        }
      })
      .catch((e) => {
        if (!disposed) setError(String(e));
      });
    return () => {
      disposed = true;
    };
  }, [labId, active, updated]);
  const events = useMemo(
    () =>
      [
        ...new Map(
          [
            ...(detail?.id === active ? detail.events : []),
            ...activeIncoming,
          ].map((e) => [e.seq, e]),
        ).values(),
      ].sort((a, b) => a.seq - b.seq),
    [detail, activeIncoming, active],
  );
  return {
    records: ownerLab === labId ? records : [],
    selected: active,
    select: setSelected,
    detail: detail?.id === active ? detail : null,
    events,
    online,
    error,
  };
}
export type RequestStore = ReturnType<typeof useClientRequests>;

function ResponseBody({ detail }: { detail: ClientRequest }) {
  const result = detail.result;
  const html = useMemo(() => {
    if (!result?.content_type?.includes("text/html")) return "";
    const safe = DOMPurify.sanitize(result.body || "", {
      WHOLE_DOCUMENT: true,
      FORBID_TAGS: [
        "script",
        "iframe",
        "object",
        "embed",
        "link",
        "meta",
        "base",
        "form",
        "svg",
        "math",
        "audio",
        "video",
        "source",
      ],
      FORBID_ATTR: [
        "href",
        "src",
        "srcset",
        "action",
        "formaction",
        "ping",
        "xlink:href",
        "background",
      ],
    });
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">`;
    return safe.replace(/<head[^>]*>/i, (head) => head + csp);
  }, [result?.body, result?.content_type]);
  if (detail.source === "browser")
    return (
      <div className="client-empty">
        响应已直接返回网站标签页。此处仅保留请求指标和链路，不保存网页正文或表单内容。
      </div>
    );
  if (!result)
    return (
      <div className="client-empty">
        等待真实响应；追踪开启时可先查看下方观测事件。
      </div>
    );
  if (html)
    return (
      <iframe
        title="客户端页面预览"
        sandbox=""
        referrerPolicy="no-referrer"
        srcDoc={html}
      />
    );
  let body = result.body || "（未收到响应正文）";
  if (result.content_type?.includes("json")) {
    try {
      body = JSON.stringify(JSON.parse(body), null, 2);
    } catch {
      /* truncated JSON stays raw */
    }
  }
  return <pre className="client-body">{body}</pre>;
}

export default function ClientPanel({
  lab,
  store,
  trace,
  setTrace,
  disabled,
  send,
  replay,
  setReplay,
}: {
  lab: Lab | null;
  store: RequestStore;
  trace: boolean;
  setTrace: (value: boolean) => void;
  disabled: boolean;
  send: (target: string, path: string) => Promise<void>;
  replay: number;
  setReplay: (value: number) => void;
}) {
  const [target, setTarget] = useState("");
  const [path, setPath] = useState("/page");
  const [view, setView] = useState<"preview" | "details">("preview");
  const [playing, setPlaying] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const entries = lab?.applied?.nodes.filter((n) => n.kind === "nginx") || [];
  const effectiveTarget = entries.some((n) => n.id === target)
    ? target
    : entries[0]?.id || "";
  const [entry, setEntry] = useState<{
    id: string;
    node_id: string;
    url: string;
    trace: boolean;
  } | null>(null);
  const [entryError, setEntryError] = useState("");
  const [entryBusy, setEntryBusy] = useState(false);
  useEffect(() => {
    setEntry(null);
    setEntryError("");
    if (!lab || !effectiveTarget) return;
    let disposed = false;
    api<{ id: string; node_id: string; url: string; trace: boolean }[]>(
      `/labs/${lab.id}/entries`,
    )
      .then((values) => {
        if (disposed) return;
        const found = values.find((e) => e.node_id === effectiveTarget) || null;
        setEntry(found);
        if (found) setTrace(!!found.trace);
      })
      .catch((e) => {
        if (!disposed) setEntryError(String(e));
      });
    return () => {
      disposed = true;
    };
  }, [lab?.id, effectiveTarget]);
  async function toggleTrace(value: boolean) {
    setEntryBusy(true);
    setEntryError("");
    try {
      if (entry && lab)
        await api(`/labs/${lab.id}/entries/${entry.id}`, "PATCH", {
          trace: value,
        });
      setTrace(value);
    } catch (error) {
      setEntryError(String(error));
    } finally {
      setEntryBusy(false);
    }
  }
  async function openSite() {
    if (!lab || !effectiveTarget) return;
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    setEntryBusy(true);
    setEntryError("");
    try {
      const value = await api<{
        id: string;
        node_id: string;
        url: string;
        trace: boolean;
      }>(`/labs/${lab.id}/entries`, "POST", {
        node_id: effectiveTarget,
        trace,
      });
      setEntry(value);
      if (tab) tab.location.href = value.url;
      else
        setEntryError(
          "浏览器阻止了新标签页，请点击网站地址打开。首次使用请确认 SSH 同时转发 8089 端口。",
        );
    } catch (error) {
      tab?.close();
      setEntryError(String(error));
    } finally {
      setEntryBusy(false);
    }
  }
  const { detail, events } = store;
  const result = detail?.result;
  const completion = events.find((e) => e.kind === "nginx.completed");
  const steps = completion
    ? [
        "客户端",
        detail?.node_id || "Nginx",
        ...(completion.data.attempts || []).map(
          (a) =>
            `${a.node_id || a.address}${a.mapping === "last_observed" && a.node_id ? "（上次地址）" : ""} [${a.status}]`,
        ),
        "客户端（响应或失败）",
      ]
    : [];
  useEffect(() => {
    setPlaying(false);
    setReplay(-1);
  }, [store.selected, trace]);
  useEffect(() => {
    if (!playing || !trace) return;
    const timer = setTimeout(() => {
      if (replay >= steps.length - 1) setPlaying(false);
      else setReplay(replay + 1);
    }, 900);
    return () => clearTimeout(timer);
  }, [playing, replay, steps.length, trace]);
  return (
    <div className="client-panel">
      {expanded && detail && (
        <div
          className="client-preview-backdrop"
          onClick={() => setExpanded(false)}
        >
          <section
            className="client-preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="放大客户端预览"
            onClick={(e) => e.stopPropagation()}
          >
            <header>
              <strong>客户端预览 · {detail.id}</strong>
              <button autoFocus onClick={() => setExpanded(false)}>
                关闭预览
              </button>
            </header>
            <ResponseBody detail={detail} />
          </section>
        </div>
      )}
      <div className="entry-actions">
        <button
          className="primary"
          disabled={disabled || entryBusy || !effectiveTarget}
          onClick={() => void openSite()}
        >
          打开网站
        </button>
        {entry && (
          <>
            <a href={entry.url} target="_blank" rel="noopener noreferrer">
              {entry.url}
            </a>
            <button
              disabled={entryBusy}
              onClick={() => {
                if (lab) {
                  setEntryBusy(true);
                  void api(`/labs/${lab.id}/entries/${entry.id}`, "DELETE")
                    .then(() => setEntry(null))
                    .catch((e) => setEntryError(String(e)))
                    .finally(() => setEntryBusy(false));
                }
              }}
            >
              撤销入口
            </button>
          </>
        )}
        <span title="首次使用需在 SSH 隧道中转发 8089 端口">网站端口 8089</span>
      </div>
      {entryError && (
        <p role="alert" className="client-notice">
          {entryError}
        </p>
      )}
      <div className="client-form">
        <span className="method">GET</span>
        <select
          aria-label="客户端入口"
          value={effectiveTarget}
          onChange={(e) => setTarget(e.target.value)}
        >
          {!entries.length && <option value="">先部署 Nginx</option>}
          {entries.map((n) => (
            <option key={n.id}>{n.id}</option>
          ))}
        </select>
        <input
          aria-label="请求路径"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="/page?name=demo"
          maxLength={2048}
        />
        <button
          className="primary"
          disabled={disabled || !effectiveTarget}
          onClick={() => void send(effectiveTarget, path)}
        >
          单次请求
        </button>
        <label className="toggle">
          <input
            type="checkbox"
            checked={trace}
            disabled={entryBusy}
            onChange={(e) => void toggleTrace(e.target.checked)}
          />
          请求链路
        </label>
      </div>
      {lab?.observation?.nodes.some(
        (n) => n.kind === "app" && !n.browser_site,
      ) && (
        <p className="client-notice">
          存在旧版应用：主动“应用拓扑”升级后，才能使用新版交互网站。启停不会升级。
        </p>
      )}
      <div className="client-history">
        <select
          aria-label="客户端请求历史"
          value={store.selected}
          onChange={(e) => store.select(e.target.value)}
        >
          <option value="">最近请求（保留 100 次）</option>
          {store.records.map((r) => (
            <option key={r.id} value={r.id}>
              {r.source === "browser" ? "网站" : "单次"} · {r.method || "GET"}{" "}
              {r.path} · {r.error ? "传输失败" : (r.status_code ?? r.status)} ·{" "}
              {r.id.slice(0, 8)}
              {r.trace ? " · 追踪" : ""}
            </option>
          ))}
        </select>
        <span className="tiny">
          {store.online ? "事件流已连接" : "事件流断开 · 观测可能过期"}
        </span>
      </div>
      {store.error && <p className="client-notice">{store.error}</p>}
      {detail ? (
        <>
          <div className="response-meta">
            <strong>
              {result?.status_code
                ? `HTTP ${result.status_code}`
                : detail.status === "running"
                  ? "请求执行中"
                  : detail.status}
            </strong>
            <span>
              {result?.latency_ms != null
                ? `${result.latency_ms} ms`
                : "等待响应"}
            </span>
            <code>{detail.id}</code>
            <span>配置 r{result?.applied_revision ?? "—"}</span>
          </div>
          {result?.error && (
            <p className="client-notice">
              {result.error_kind === "timeout" ? "超时" : "请求异常"}：
              {result.error}
            </p>
          )}
          {result?.truncated && (
            <p className="client-notice">
              响应超过 1 MB，正文已截断。以下只展示已采集部分。
            </p>
          )}
          <div className="response-tabs">
            <button disabled={!result} onClick={() => setExpanded(true)}>
              放大预览
            </button>
            <button
              className={view === "preview" ? "active" : ""}
              onClick={() => setView("preview")}
            >
              页面预览
            </button>
            <button
              className={view === "details" ? "active" : ""}
              onClick={() => setView("details")}
            >
              响应详情
            </button>
            <span className="tiny">
              同一次响应 · 静态隔离预览 · 不执行脚本及页面内交互
            </span>
          </div>
          {view === "preview" ? (
            <ResponseBody detail={detail} />
          ) : (
            <div className="response-details">
              <pre>
                {JSON.stringify(
                  {
                    request_id: detail.id,
                    path: detail.path,
                    status: result?.status_code,
                    headers: result?.headers,
                    bytes_captured: result?.bytes_captured,
                    latency_ms: result?.latency_ms,
                  },
                  null,
                  2,
                )}
              </pre>
              <pre>{result?.body}</pre>
            </div>
          )}
        </>
      ) : (
        <div className="client-empty">
          从 Nginx 入口发送一次请求，查看客户端实际收到的页面。
        </div>
      )}
      {trace && detail?.trace && (
        <div className="trace-inspector">
          <div className="trace-heading">
            <strong>请求观测</strong>
            <span>{result?.trace_note || eventName(events.at(-1))}</span>
            {completion && detail.status === "completed" && (
              <button
                onClick={() => {
                  setReplay(0);
                  setPlaying(true);
                }}
              >
                事后路径回放
              </button>
            )}
          </div>
          {completion && (
            <div className="trace-route">
              {steps.map((step, i) => (
                <span className={replay === i ? "current" : ""} key={i}>
                  {i > 0 ? " → " : ""}
                  {step}
                </span>
              ))}
              <small>依据完成日志还原，动画不代表网络包实时位置</small>
            </div>
          )}
          <ol className="trace-events">
            {events.map((event) => (
              <li key={event.seq}>
                <time>
                  {new Date(event.time * 1000).toLocaleTimeString("zh-CN", {
                    hour12: false,
                  })}
                </time>
                <code>{event.node_id || event.source}</code>
                <span>
                  {eventName(event)}
                  {event.data.response_version != null
                    ? ` · 响应 v${event.data.response_version}`
                    : ""}
                </span>
              </li>
            ))}
          </ol>
          {!events.length && <p className="tiny">等待确认；尚无链路证据。</p>}
        </div>
      )}
    </div>
  );
}
